"""Render checked diagnosis reports for review."""

from method_input_adapters.skillpatch.models import DiagnosisContext
from ..models import DiscoveryReport, EvidenceRef

def render_report(report: DiscoveryReport, context: DiagnosisContext) -> str:
    evidence = {item.context_id: item for item in context.items}
    lines = ["# Stage 1 Diagnosis Report", "", f"Run: `{report.run_id}`", "",
             f"Status: `{report.status}`; semantic status: `{report.semantic_status}`", "",
             "This report passed schema and exact source-reference checks. Findings remain model-proposed candidate diagnoses; independent attribution and patch validation have not yet been performed.", "",
             report.summary, "", f"Input digest: `{report.input_digest}`", ""]

    def references(refs: list[EvidenceRef]) -> None:
        for ref in refs:
            item = evidence[ref.evidence_id]
            location = item.source_path + (f":{item.source_line}" if item.source_line else "")
            lines.extend([f"[{ref.evidence_id}](<{location}>)", ""])
            lines.extend("> " + line for line in ref.quote.splitlines())
            lines.append("")

    for improvement in getattr(report, "improvements", []):
        target = improvement.target
        lines.extend([f"## {improvement.improvement_id} · Skill Improvement", "",
                      f"Target: `{target.skill_id}` · `{target.relative_path}`", "",
                      "### Attachment Anchor", ""])
        references([EvidenceRef(target.skill_ref, target.quote)])
        lines.extend(["### Diagnosis", "", improvement.diagnosis, "",
                      "### Suggested Direction", "", improvement.suggestion, "",
                      "### Evidence", ""])
        references(improvement.evidence)

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



def render_model_report(payload: dict) -> str:
    """Display model output without resolving or validating its references."""
    import json

    lines = ["# Stage 1 Diagnosis Report", ""]
    summary = payload.get("summary")
    if isinstance(summary, str):
        lines.extend([summary, ""])
    improvements = payload.get("improvements")
    if isinstance(improvements, list):
        for index, item in enumerate(improvements, 1):
            lines.extend([f"## Improvement {index}", ""])
            if isinstance(item, dict):
                for key, value in item.items():
                    lines.extend([f"### {key}", ""])
                    if isinstance(value, str):
                        lines.extend([value, ""])
                    else:
                        lines.extend(["```json", json.dumps(value, ensure_ascii=False, indent=2), "```", ""])
            else:
                lines.extend(["```json", json.dumps(item, ensure_ascii=False, indent=2), "```", ""])
    # Preserve every field, including unfamiliar model output, in the readable archive.
    extra = {key: value for key, value in payload.items()
             if key != "summary" or not isinstance(summary, str)}
    if isinstance(improvements, list):
        extra.pop("improvements", None)
    if extra:
        lines.extend(["## Other Output", "", "```json",
                      json.dumps(extra, ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)
