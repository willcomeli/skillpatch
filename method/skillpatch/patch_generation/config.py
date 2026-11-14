"""Stage2 patch-generation settings, independent of benchmark execution."""
from dataclasses import dataclass, field
from pathlib import Path
from method.skillpatch.config import DiscoveryConfig, StageAgentConfig, load_stage_agent_config


@dataclass(frozen=True)
class Stage2Config:
    generation_config: Path
    resolved: DiscoveryConfig | None = field(default=None, compare=False, repr=False)

    def agent_config(self, environment=None) -> DiscoveryConfig:
        return self.resolved or load_stage_agent_config(
            self.generation_config, 'stage2', environment=environment,
        )[1]

    def induction_prompts(self, environment=None) -> dict[str, Path]:
        config = self.agent_config(environment)
        return (config if isinstance(config, StageAgentConfig) else
                StageAgentConfig(config.agent, config.prompt_path)).induction_prompts()


def load_stage2_config(path: Path, *, environment=None) -> Stage2Config:
    generation_config, resolved = load_stage_agent_config(
        path, 'stage2', config_key='generation_config', environment=environment,
    )
    return Stage2Config(generation_config, resolved)
