"""Stage 1 orchestration: evidence -> one diagnosis call -> checked candidate report."""

from pathlib import Path
from typing import Callable, Mapping

from skillpatch.context import build_discovery_input, render_evidence
from skillpatch.discovery_validation import validate_report
from skillpatch.environment import redact
from skillpatch.model_client import ChatCompletionClient, DiagnosisConfig, diagnosis_secrets, parse_json_object
from skillpatch.models import DiscoveryInput, DiscoveryReport, EvidenceRef
from skillpatch.storage import jsonable, now, sha256_file, write_json


def render_report(report: DiscoveryReport, context: DiscoveryInput) -> str:
    evidence = {item.evidence_id: item for item in context.evidence}
    lines = ["# Stage 1 Diagnosis Report", "", f"Run: `{report.run_id}`", "",
             f"Status: `{report.status}`; semantic status: `{report.semantic_status}`", "",
             "This report passed schema and exact source-reference checks. Conflicts remain model-proposed candidate diagnoses; independent attribution and patch validation have not yet been performed.", "",
             report.summary, "", f"Input digest: `{report.input_digest}`", ""]

    def references(refs: list[EvidenceRef]) -> None:
        for ref in refs:
            item = evidence[ref.evidence_id]
            location = item.source_path + (f":{item.source_line}" if item.source_line else "")
            lines.extend([f"[{ref.evidence_id}](<{location}>)", ""])
            lines.extend("> " + line for line in ref.quote.splitlines())
            lines.append("")

    for conflict in report.conflicts:
        target = conflict.target
        lines.extend([f"## {conflict.conflict_id} · {conflict.type}", "",
                      f"Target: `{target.scope}` · `{target.skill_id}` · `{target.relative_path}`", "",
                      f"Skill file version: `{target.version}`", "",
                      f"Cited trajectory span: `{conflict.trajectory_range['start'] or 'not cited'}` → `{conflict.trajectory_range['end'] or 'not cited'}` (does not establish complete inspection coverage)", "",
                      "### Target Clause", ""])
        references([EvidenceRef(target.skill_ref, target.quote)])
        lines.extend(["### Diagnosis", "", conflict.diagnosis, ""])
        for label, refs in (("Decision Conditions", conflict.decision_evidence),
                            ("Behavior Evidence", conflict.behavior_evidence),
                            ("Verifier Feedback", conflict.feedback_evidence)):
            lines.extend([f"### {label}", ""])
            references(refs)
            if not refs:
                lines.extend(["No direct reference; see the diagnosis.", ""])
        if conflict.uncertainty:
            lines.extend(["### Uncertainties", ""])
            lines.extend(f"- {item}" for item in conflict.uncertainty)
            lines.append("")
    if report.other_findings:
        lines.extend(["## Other Findings", ""])
        for finding in report.other_findings:
            lines.extend([f"**{finding['category']}:** {finding['description']}", ""])
            references([EvidenceRef(**item) for item in finding["evidence"]])
    lines.extend(["## Coverage and Limitations", ""])
    lines.extend(f"- {note}" for note in report.coverage_notes)
    if context.limitations:
        lines.extend(["", "### Limitations Recorded During Input Collection", ""])
        lines.extend(f"- {note}" for note in context.limitations)
    return "\n".join(lines) + "\n"


def run_discovery(run_dir: Path, config: DiagnosisConfig, output: Path,
                  environment: Mapping[str, str], *, prepare_only: bool = False,
                  client: ChatCompletionClient | None = None,
                  progress: Callable[[str], None] | None = None,
                  input_builder: Callable[[Path], DiscoveryInput] | None = None) -> dict:
    """Consume an Executor record; never execute or resume the benchmark here."""
    run_dir, output = run_dir.resolve(), output.resolve()
    if output.is_relative_to(run_dir) and not output.is_relative_to(run_dir / "discovery"):
        raise ValueError("Diagnosis artifacts inside a run must be under its discovery/ directory")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    secrets = diagnosis_secrets(config, environment)
    state: dict = {"schema_version": 1, "status": "running", "phase": "input",
                   "run_dir": str(run_dir), "output_dir": str(output), "started_at": now(),
                   "prepare_only": prepare_only, "semantic_status": "not_assessed"}
    emit = progress or (lambda _: None)
    try:
        emit("Checking the run snapshot and collecting Stage 1 evidence")
        # Alternate benchmark/backend collectors supply the same evidence contract.
        context = (input_builder or build_discovery_input)(run_dir)
        prompt = config.prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("Diagnosis prompt is empty")
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": render_evidence(context)}]
        input_data = jsonable(context)
        if redact(input_data, secrets) != input_data or redact(messages, secrets) != messages:
            raise ValueError("Known credential value appears in the diagnosis input; input was not sent")
        write_json(output / "input.json", input_data)
        (output / "prompt.md").write_text(prompt, encoding="utf-8")
        write_json(output / "config.json", config)
        write_json(output / "messages.json", messages)
        state.update(run_id=context.run_id, input_digest=context.input_digest,
                     evidence_count=len(context.evidence),
                     input_chars=sum(len(message["content"]) for message in messages),
                     prompt_sha256=sha256_file(output / "prompt.md"))
        if state["input_chars"] > config.max_input_chars:
            raise ValueError("Diagnosis input exceeds max_input_chars; no evidence was silently truncated")
        if prepare_only:
            state["status"] = "prepared"
        else:
            state["phase"] = "model_call"
            emit(f"Diagnosing {context.run_id} with {config.model}; one model call")
            content = (client or ChatCompletionClient()).complete(config, messages, environment, output)
            state["phase"] = "validation"
            payload = parse_json_object(content)
            if redact(payload, secrets) != payload:
                raise ValueError("Known credential value appears in decoded diagnosis content")
            report = validate_report(payload, context, require_english=True)
            write_json(output / "validation.json", {
                "status": "passed", "scope": "basic_schema_target_and_source_references",
                "semantic_correctness": "not_verified",
            })
            write_json(output / "report.json", report)
            (output / "report.md").write_text(render_report(report, context), encoding="utf-8")
            state.update(status="completed", diagnosis_status=report.status,
                         semantic_status=report.semantic_status, conflict_count=len(report.conflicts),
                         report_path=str(output / "report.json"))
            emit(f"Stage 1 report saved: {output / 'report.md'}")
    except KeyboardInterrupt:
        state.update(status="cancelled", error="Diagnosis interrupted")
    except (OSError, ValueError) as exc:
        state.update(status="failed", error=redact(str(exc), secrets))
        if state["phase"] == "validation":
            write_json(output / "validation.json", {
                "status": "failed", "error": state["error"], "semantic_correctness": "not_verified",
            })
    finally:
        state["finished_at"] = now()
        write_json(output / "result.json", state)
    return state
