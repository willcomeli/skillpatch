"""Upstream text analysis -> parsed records -> combined parallel evolution."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil

from ..archive import load_archive, write_json
from ..execution import SharedExecutorBridge, load_tasks
from .results import export_results
from .upstream import ROOT, activate, domain_prompt


def _hashes(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Skill folders must not contain symlinks')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def evolve(sources, initial_skill_dir, output, environment, *, validation_manifest=None,
           model='deepseek-flash', api_key_env='DEEPSEEK_API_KEY', base_url_env='DEEPSEEK_BASE_URL',
           max_output_tokens=32768):
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError('max_output_tokens must be a positive integer')
    activate()
    from openai import OpenAI
    from analysis.run_error_analysis_llm import analyze_instance as analyze_error
    from analysis.run_success_analysis_llm import analyze_instance as analyze_success
    from analysis.parse_error_analysis_outputs import parse_report as parse_error
    from analysis.parse_success_analysis_outputs import parse_report as parse_success
    from src.react_agent.models import OpenAIClient
    from skill_evolver.skill_evolving_agent import QUICK_VALIDATE_SCRIPT
    from skill_evolver.parallel_success_evolving_agent import CombinedParallelSkillEvolver, normalize_mixed_records

    source = Path(initial_skill_dir).resolve(strict=True)
    if not (source / 'SKILL.md').is_file():
        raise ValueError('initial_skill_dir requires SKILL.md')
    output = Path(output).resolve()
    if output.is_relative_to(source):
        raise ValueError('Evolution output must be outside the initial Skill folder')
    initial_hashes = _hashes(source)
    key, url = environment.get(api_key_env), environment.get(base_url_env)
    if not key or not url:
        raise ValueError('Configured optimizer key and URL are required')
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'method_config.json', {'model': model, 'api_key_env': api_key_env,
               'base_url_env': base_url_env, 'request_timeout_sec': 1800,
               'analysis_max_tokens': 8192, 'evolution_max_tokens': max_output_tokens,
               'initial_skill_dir': str(source), 'initial_file_hashes': initial_hashes})
    # Validate execution archives before any paid model request.
    rows = export_results(sources, output / 'input')
    validation_tasks = load_tasks(validation_manifest) if validation_manifest else []
    input_tasks = {(row['benchmark'], row['task_id']) for row in rows}
    if validation_tasks:
        for entry in validation_tasks:
            if entry['split'] != 'val':
                raise ValueError('Trace2Skill validation manifest must contain only val tasks')
            from set_up.native_config import read_benchmark
            from set_up.config import load_config
            benchmark = read_benchmark(Path(entry['config']))
            if benchmark == 'skillsbench':
                identity = load_config(Path(entry['config']))[0].task_dir.name
            else:
                ref = entry['task_ref']
                identity = str(ref.get('task_id')) if isinstance(ref, dict) else str(ref)
            if (benchmark, identity) in input_tasks:
                raise ValueError('Validation task overlaps an optimization source task')
    skill_dir = output / 'skill'
    shutil.copytree(source, skill_dir)
    errors, successes = [], []
    client = OpenAI(base_url=url, api_key=key, timeout=1800, max_retries=0)
    try:
        for row in rows:
            success = row['success']
            kind = 'success' if success else 'error'
            system = domain_prompt((ROOT / f'analysis/{kind}_analysis_system_llm.txt').read_text())
            system += '\n\nDataset: ' + row['benchmark'] + '. Treat tool execution feedback and final verifier feedback as distinct. Do not infer missing observations or expose task-specific answers as general rules.\n'
            template = (ROOT / f'analysis/{kind}_analysis_user_llm.txt').read_text()
            messages, report = (analyze_success if success else analyze_error)(
                client, model, system, template, row['log_path'], {'temperature': 0, 'max_tokens': 8192})
            folder = output / 'analysis' / row['instance_id']
            write_json(folder / 'prompt.json', messages)
            path = folder / ('success_analysis.md' if success else 'analysis_report.md')
            path.write_text(report, encoding='utf-8')
            parsed = (parse_success if success else parse_error)(path, row['instance_id'])
            if not parsed['items']:
                raise ValueError('Trace2Skill analysis produced no parseable items: ' + str(path))
            (successes if success else errors).append(parsed)
        write_json(output / 'parsed_error_records.json', errors)
        write_json(output / 'parsed_success_records.json', successes)
        records = normalize_mixed_records(errors, successes)
        source_client = OpenAIClient(model=model, api_key=key, base_url=url, use_cache=False,
                                     generation_config={'temperature': 0, 'max_tokens': max_output_tokens},
                                     retry_times=(), timeout=1800)

        class DatasetEvolver(CombinedParallelSkillEvolver):
            def _build_map_system_prompt(self):
                return domain_prompt(super()._build_map_system_prompt())

            def _build_map_system_prompt_from_patterns(self):
                return domain_prompt(super()._build_map_system_prompt_from_patterns())

        evolver = DatasetEvolver(source_client, skill_dir, max_workers=1, output_dir=output / 'evolution')
        original_apply = evolver._evolver.apply_edits

        def apply_skill_edits(edits):
            for edit in edits:
                relative = PurePosixPath(edit.relative_path)
                if (relative.is_absolute() or '..' in relative.parts or
                        not (relative.as_posix() == 'SKILL.md' or
                             len(relative.parts) > 1 and relative.parts[0] == 'references') or
                        not (skill_dir / relative).resolve().is_relative_to(skill_dir.resolve())):
                    raise ValueError('Evolution edit is outside editable Skill documents: ' + edit.relative_path)
            return original_apply(edits)

        evolver._evolver.apply_edits = apply_skill_edits
        summary = evolver.run(records)
        final_hashes = _hashes(skill_dir)
        if 'SKILL.md' not in final_hashes or not (skill_dir / 'SKILL.md').read_text().strip():
            raise ValueError('Evolution produced an empty or missing SKILL.md')
        for relative, digest in initial_hashes.items():
            if relative != 'SKILL.md' and not relative.startswith('references/'):
                if final_hashes.get(relative) != digest:
                    raise ValueError('Protected Skill resource changed: ' + relative)
        write_json(output / 'evolution_summary.json', summary)
        validation = []
        bridge = SharedExecutorBridge(environment)
        for entry in validation_tasks:
            before = bridge.run(entry, '', output / 'validation' / entry['id'] / 'baseline', skill_dir=source)
            after = bridge.run(entry, '', output / 'validation' / entry['id'] / 'candidate', skill_dir=skill_dir)
            baseline, candidate = load_archive(before), load_archive(after)
            validation.append({'id': entry['id'], 'baseline_archive': str(before), 'candidate_archive': str(after),
                               'baseline_success': baseline.success, 'candidate_success': candidate.success,
                               'baseline_score': baseline.score, 'candidate_score': candidate.score})
        write_json(output / 'validation.json', validation)
        result = {'status': 'completed', 'skill_dir': str(skill_dir),
                  'skill_changed': initial_hashes != final_hashes,
                  'analysis_records': len(records), 'validation_tasks': len(validation),
                  'validation_status': 'executed' if validation else 'not_requested',
                  'upstream_quick_validate_available': QUICK_VALIDATE_SCRIPT.is_file(),
                  'baseline_selection': 'candidate only; validation results do not automatically activate the Skill'}
        write_json(output / 'result.json', result)
        return result
    finally:
        client.close()
        if _hashes(source) != initial_hashes:
            raise ValueError('The original Skill source changed during evolution')
