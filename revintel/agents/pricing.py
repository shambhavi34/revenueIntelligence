from .base import Agent


class PricingOptimizationAgent(Agent):
    """Recommends BAR adjustments from pickup pace, occupancy, comp-set rates and remaining capacity (section 4 guardrails)."""
    name = "pricing_optimization"
