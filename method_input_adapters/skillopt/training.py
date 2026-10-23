"""Run the unmodified SkillOpt trainer with shared execution."""

from pathlib import Path
from ..execution import SharedExecutorBridge, load_tasks
from .adapter import SharedExecutionAdapter


def train(manifest, initial_skill, output, environment, *, config=None, model='deepseek-flash',
          api_key_env='DEEPSEEK_API_KEY', base_url_env='DEEPSEEK_BASE_URL'):
    from skillopt.config import load_config, flatten_config
    from skillopt.engine.trainer import ReflACTTrainer
    from skillopt.model import configure_openai_compatible
    root = Path(__file__).resolve().parents[2]
    tasks = load_tasks(manifest)
    initial_skill = Path(initial_skill).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Training output must be new; implicit resume is disabled')
    cfg = flatten_config(load_config(str(root / 'method/skillopt/configs/_base_/default.yaml')))
    if config is not None:
        cfg.update(flatten_config(load_config(str(Path(config).resolve()))))
    # Small default run; optional epoch-level optimizers remain configurable.
    if config is None:
        cfg.update(num_epochs=1, batch_size=1, minibatch_size=1, analyst_workers=1,
                   use_slow_update=False, use_meta_skill=False, eval_test=False,
                   edit_budget=2, min_edit_budget=1, sel_env_num=1)
    if not any(row['split'] == 'val' for row in tasks):
        raise ValueError('SkillOpt requires a disjoint validation split')
    if cfg.get('eval_test') and not any(row['split'] == 'test' for row in tasks):
        raise ValueError('eval_test requires a held-out test split')
    cfg.update(out_root=str(output), skill_init=str(initial_skill),
               train_size=sum(row['split'] == 'train' for row in tasks),
               model_backend='openai_compatible', optimizer_backend='openai_compatible',
               target_backend='openai_compatible', optimizer_model=model,
               # This label is not used to execute targets: the bridge uses each
               # task's original execution config, unchanged across methods.
               target_model='shared-executor', reasoning_effort=None)
    cfg.setdefault('optimizer_openai_compatible_timeout_seconds', 1800)
    key, url = environment.get(api_key_env), environment.get(base_url_env)
    if not key or not url:
        raise ValueError('Configured optimizer key and URL are required')
    configure_openai_compatible(base_url=url, api_key=key, model=model,
                               timeout_seconds=cfg['optimizer_openai_compatible_timeout_seconds'],
                               max_tokens=8192, temperature=0)
    adapter = SharedExecutionAdapter(tasks, SharedExecutorBridge(environment))
    return ReflACTTrainer(cfg, adapter).train()
