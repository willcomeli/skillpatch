"""Validate an existing SkillsBench patch; generation remains independent."""
from pathlib import Path
from dataclasses import replace
from method_input_adapters.skillpatch import build_diagnosis_context
from excutor.executor_adapter.registry import get_execution_adapter
from excutor.executor_adapter.storage import tree_hashes, hash_manifest, write_json, jsonable, now, sha256_file
from excutor.excutor_runners.benchflow import BenchFlowBackend
from excutor.excutors import Executor
from set_up.config import load_config
from set_up.environment import redact, redact_artifacts, secret_values
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_generation.source import read_json
from prompts.prompt_augmenters.skill_invocation import check_patch_reads
from .source import load_validation_source
from .records import usable_execution
from .rendering import render_patch, target_file
from .integrity import _protected_inputs_equal
from .task_messages import task_messages_equal
from .comparison import compare_feedback
from .evaluation import evaluate_target, decide_validation
from .models import TargetEvaluation


def run_validation(patch_file: Path, config, output: Path, environment, *, prepare_only=False,
                   client=None, backend_factory=None, progress=None):
    output = output.expanduser().resolve()
    patch,source,episode,source_baseline,request = load_validation_source(patch_file)
    protected = {episode,source.diagnosis_dir,source.run_dir}
    # Reject output placement before writing anything into historical evidence.
    if any(output.is_relative_to(root.resolve()) for root in protected):
        raise ValueError('Validation output must be outside generation, diagnosis and execution archives')
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    emit = progress or (lambda _:None)
    state = {'status':'running','phase':'preflight','started_at':now(),
        'patch_id':patch.patch_id,'ready_for_stage3':False,'cases':[],
        'baseline_strategy':'reuse_source_execution','active_version_changed':False}
    secrets = set(secret_values(environment))
    try:
        if type(config.repetitions) is not int or config.repetitions < 1:
            raise ValueError('repetitions must be a positive integer')
        evaluation = load_discovery_config(config.evaluation_config)
        if environment.get(evaluation.agent.api_key_env):
            secrets.add(environment[evaluation.agent.api_key_env])
        pinned = {root:tree_hashes(root) for root in protected}
        provider_settings = {name:environment[name] for name in (
            'DEEPSEEK_THINKING','DEEPSEEK_BASE_URL','LLM_BASE_URL','OPENAI_BASE_URL') if name in environment}
        if read_json(source.run_dir/'runtime_environment.json').get('provider_settings') != provider_settings:
            raise ValueError('Provider settings differ from the reused source baseline')
        adapter = get_execution_adapter('skillsbench')
        cases = [{'id':'source','role':'source','request':request,
                  'pack':source_baseline.prepared.skills_dir}]
        for index,path in enumerate(config.regression_configs):
            regression,_ = load_config(path)
            if regression.benchmark != 'skillsbench' or regression.backend != 'benchflow':
                raise ValueError('Regression validation currently supports only SkillsBench with BenchFlow')
            if regression.skill_mode != 'with-skill' or not regression.explicit_skill_read:
                raise ValueError('Regression cases require explicit with-skill execution')
            task = adapter.prepare(regression,output/'regression-inputs'/f'{index:03d}')
            cases.append({'id':f'regression-{index:03d}','role':'regression',
                'request':replace(regression,task_dir=task.task_dir,skills_dir=task.skills_dir),
                'pack':task.skills_dir})
        for case in cases:
            if case['pack'] is None:
                raise ValueError('Validation case has no Skill pack')
            target_file(case['pack'],patch)
            rendered = render_patch(case['pack'],patch,output/'packs'/case['id'])
            case['candidate_pack'] = output/'packs'/case['id']
            diff = rendered.pop('diff')
            write_json(output/'presentation'/(case['id']+'.json'),rendered)
            (output/'presentation'/(case['id']+'.diff')).write_text(diff,encoding='utf-8')
        write_json(output/'patch.json',patch)
        write_json(output/'config.json',config)
        write_json(output/'plan.json',{'source_patch':patch_file,'diagnosis_dir':source.diagnosis_dir,
            'source_baseline':source.run_dir,'baseline_strategy':'reuse_source_execution',
            'repetitions':config.repetitions,'max_backend_runs':config.repetitions*(1+2*len(config.regression_configs)),
            'max_evaluation_sessions':config.repetitions*len(cases),
            'cases':[{key:value for key,value in case.items()} for case in cases],
            'presentation_mode':'sidecar_v1','generation_calls':0,'active_version_changed':False})
        if prepare_only:
            state.update(status='prepared',phase='finished')
            return state
        factory = backend_factory or BenchFlowBackend
        evaluations,comparisons,regression_evaluations,regression_comparisons = [],[],[],[]
        executions_ok = True
        state['phase'] = 'validation'
        write_json(output/'result.json',state)
        for case in cases:
            for repetition in range(config.repetitions):
                directory = output/'runs'/case['id']/f'{repetition:03d}'
                item = {'case_id':case['id'],'role':case['role'],'repetition':repetition}
                state['cases'].append(item)
                try:
                    if case['role'] == 'source':
                        baseline,baseline_context,baseline_dir = source_baseline,source.context,source.run_dir
                        item['baseline_origin'] = 'source_execution'
                    else:
                        emit(f"Running regression baseline {case['id']} ({repetition+1})")
                        baseline_dir = directory/'baseline'
                        baseline = Executor(adapter,factory()).run(case['request'],baseline_dir,environment,progress=emit)
                        if baseline.status == 'cancelled': raise KeyboardInterrupt
                        if not usable_execution(baseline) or baseline.feedback.passed is not True:
                            raise ValueError('Regression baseline is unavailable or did not pass')
                        baseline_context = build_diagnosis_context(baseline_dir)
                        item['baseline_origin'] = 'fresh_execution'
                    item.update(baseline_dir=str(baseline_dir),baseline_result_sha256=sha256_file(baseline_dir/'result.json'))
                    candidate_dir = directory/'candidate'
                    emit(f"Running patched candidate {case['id']} ({repetition+1})")
                    candidate = Executor(adapter,factory()).run(case['request'],candidate_dir,environment,
                        patch_skills_dir=case['candidate_pack'],progress=emit)
                    if candidate.status == 'cancelled': raise KeyboardInterrupt
                    item['candidate_dir'] = str(candidate_dir)
                    if not usable_execution(candidate):
                        raise ValueError('Candidate needs a complete, scored execution without errors')
                    if not _protected_inputs_equal(baseline,candidate,patch):
                        raise ValueError('Protected inputs or exact sidecar deployment differ')
                    if (candidate.prepared.skill_digest != hash_manifest(tree_hashes(case['candidate_pack']))
                            or baseline.prepared.skill_digest != hash_manifest(tree_hashes(case['pack']))):
                        raise ValueError('Prepared Skill pack digest differs from pinned deployment')
                    if (baseline.execution.backend_version != candidate.execution.backend_version
                            or read_json(baseline_dir/'runtime_environment.json') != read_json(candidate_dir/'runtime_environment.json')):
                        raise ValueError('Execution backend version or provider settings differ')
                    candidate_context = build_diagnosis_context(candidate_dir)
                    if not task_messages_equal(baseline_context,candidate_context,baseline_dir,candidate_dir):
                        raise ValueError('Actual task messages differ beyond the audited patch-reading prefix')
                    audit = check_patch_reads(case['request'],candidate.prepared,candidate.execution.normalized_trajectory_path)
                    item['patch_read_audit'] = audit
                    write_json(candidate_dir/'patch_read_audit.json',audit)
                    if audit['status'] == 'unconfirmed':
                        item['read_warning'] = 'Patch reading unconfirmed; objective evaluation continues'
                    comparison = compare_feedback(baseline.feedback,candidate.feedback,
                        target=source.conflict if case['role']=='source' else None)
                    try:
                        assessment = evaluate_target(source,baseline_context,candidate_context,evaluation,
                            output/'evaluation'/case['id']/f'{repetition:03d}',environment,client=client,progress=emit)
                    except (OSError,ValueError,TimeoutError) as error:
                        assessment = TargetEvaluation('uncertain','uncertain',
                            'Evaluation unavailable; retain objective verifier comparison.',[],[],[],'none')
                        item['evaluation_warning'] = redact(str(error),secrets)
                        write_json(output/'evaluation'/case['id']/f'{repetition:03d}'/'evaluation.json',{
                            'status':'unavailable','evaluation':assessment,'error':item['evaluation_warning']})
                    if case['role']=='source':
                        evaluations.append(assessment); comparisons.append(comparison)
                    else:
                        regression_evaluations.append(assessment); regression_comparisons.append(comparison)
                    item.update(status='completed',comparison=jsonable(comparison),evaluation=jsonable(assessment),
                        baseline_input_digest=baseline_context.input_digest,candidate_input_digest=candidate_context.input_digest,
                        deployed_pack_digest=candidate.prepared.skill_digest)
                except (OSError,ValueError) as error:
                    executions_ok = False
                    item.update(status='failed',error=redact(str(error),secrets))
                write_json(output/'result.json',state)
        if any(tree_hashes(root) != hashes for root,hashes in pinned.items()):
            raise ValueError('Historical source archives changed during validation')
        decision = decide_validation(evaluations,comparisons,regression_comparisons,
            executions_ok=executions_ok,regression_evaluations=regression_evaluations)
        coverage = {'configured_tasks':len(config.regression_configs),'checked_pairs':len(regression_comparisons),
            'status':'not_configured' if not config.regression_configs else
                'complete' if len(regression_comparisons)==len(config.regression_configs)*config.repetitions else 'incomplete'}
        state.update(status='completed',phase='finished',regression_coverage=coverage,**decision)
        write_json(output/'validation.json',state)
        if decision['ready_for_stage3']:
            write_json(output/'stage3_queue.json',{'patch':patch,'validation_path':str(output/'validation.json'),
                'validation_sha256':sha256_file(output/'validation.json'),'candidate_pack':str(cases[0]['candidate_pack']),
                'candidate_pack_digest':hash_manifest(tree_hashes(cases[0]['candidate_pack'])),
                'source_patch':str(patch_file.resolve()),'presentation_mode':'sidecar_v1',
                'regression_coverage':coverage,'active_version_changed':False})
    except KeyboardInterrupt:
        state.update(status='cancelled',ready_for_stage3=False)
    except (OSError,ValueError) as error:
        state.update(status='failed',error=redact(str(error),secrets),ready_for_stage3=False)
    finally:
        state['finished_at'] = now()
        try:
            redact_artifacts(output,secrets)
        except OSError as error:
            state.update(status='failed',error=redact(str(error),secrets),ready_for_stage3=False)
        write_json(output/'result.json',state)
    return state
