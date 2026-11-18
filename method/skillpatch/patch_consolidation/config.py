"""Settings for the consolidation proposal agent."""
from dataclasses import dataclass, field
from pathlib import Path
from method.skillpatch.config import DiscoveryConfig, StageAgentConfig, load_stage_agent_config


@dataclass(frozen=True)
class ConsolidationConfig:
    consolidation_config: Path
    resolved: DiscoveryConfig | None = field(default=None, compare=False, repr=False)

    def agent_config(self, environment=None) -> DiscoveryConfig:
        return self.resolved or load_stage_agent_config(
            self.consolidation_config, 'stage3', environment=environment,
        )[1]

    def proposal_config(self, environment=None, *, patch_format=None) -> DiscoveryConfig:
        from method.skillpatch.patch_generation.models import NOTE_EXAMPLE_FORMAT

        config = self.agent_config(environment)
        return (config if isinstance(config, StageAgentConfig) else
                StageAgentConfig(config.agent, config.prompt_path)).for_consolidation(
                    boundary_aware=patch_format == NOTE_EXAMPLE_FORMAT,
                )


def load_consolidation_config(path: Path, *, environment=None) -> ConsolidationConfig:
    consolidation_config, resolved = load_stage_agent_config(
        path, 'stage3', config_key='consolidation_config', environment=environment,
    )
    return ConsolidationConfig(consolidation_config, resolved)
