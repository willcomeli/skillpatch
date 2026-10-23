"""Read generated candidates and produce one consolidation proposal."""
from pathlib import Path
from excutor.executor_adapter.storage import tree_hashes, write_json, jsonable, now
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from set_up.environment import secret_values, redact, redact_artifacts
from .source import load_patch, bind_target
from .patch_consolidation import propose_consolidation


def run_consolidation(existing, incoming: Path, skill_key: str, config, output: Path,
                      environment, *, client=None, progress=None):
    if not existing:
        raise ValueError('At least one existing patch is required')
    output = output.expanduser().resolve()
    loaded = [load_patch(Path(path)) for path in [*existing, incoming]]
    protected = {root for _, source, episode in loaded for root in (episode, source.diagnosis_dir)}
    if any(output.is_relative_to(root.resolve()) for root in protected):
        raise ValueError('Consolidation output must be outside all source archives')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    state = {'status': 'running', 'phase': 'input', 'started_at': now(),
             'ready_for_activation': False, 'semantic_status': 'not_assessed', 'skill_key': skill_key}
    secrets = set(secret_values(environment))
    emit = progress or (lambda _: None)
    try:
        agent_config = load_discovery_config(config.consolidation_config)
        if environment.get(agent_config.agent.api_key_env):
            secrets.add(environment[agent_config.agent.api_key_env])
        pinned = {root: tree_hashes(root) for root in protected}
        bindings = [bind_target(patch, source, skill_key) for patch, source, _ in loaded]
        if any(binding != bindings[0] for binding in bindings[1:]):
            raise ValueError('Only patches for the exact same archived Skill target and anchor can be consolidated')
        ids = [patch.patch_id for patch, _, _ in loaded]
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate patch ID; each patch must be supplied once')
        contexts, entries = {}, []
        for index, (patch, source, episode) in enumerate(loaded):
            collection = f'source_{index}'
            contexts[collection] = source.context
            entries.append({'collection': collection, 'patch': jsonable(patch),
                'issue': jsonable(source.conflict), 'generation_dir': str(episode),
                'diagnosis_dir': str(source.diagnosis_dir)})
        write_json(output / 'config.json', config)
        write_json(output / 'sources.json', entries)
        write_json(output / 'target_binding.json', bindings[0])
        state['phase'] = 'proposal'
        emit('Proposing consolidation of candidate patches; source archives remain separate')
        proposal = propose_consolidation(entries, contexts, bindings[0], agent_config,
            output / 'proposal', environment, client=client, progress=emit)
        if any(tree_hashes(root) != hashes for root, hashes in pinned.items()):
            raise ValueError('A source archive changed during consolidation')
        state.update(status='proposed', phase='finished', action=proposal['action'],
            reason=proposal['reason'], semantic_status='candidate_consolidation',
            proposal_path=str(output / 'proposal/proposal.json'))
    except KeyboardInterrupt:
        state['status'] = 'cancelled'
    except (OSError, ValueError) as error:
        state.update(status='failed', error=redact(str(error), secrets))
    finally:
        state['finished_at'] = now()
        try:
            redact_artifacts(output, secrets)
        except OSError as error:
            state.update(status='failed', error=redact(str(error), secrets))
        write_json(output / 'result.json', state)
    return state
