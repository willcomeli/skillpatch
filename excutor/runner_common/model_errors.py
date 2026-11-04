"""Model budget limits are agent outcomes, distinct from unavailable infrastructure."""


class ModelActionBudgetExceeded(ValueError):
    """The fixed completion budget was exhausted without a complete next action."""
