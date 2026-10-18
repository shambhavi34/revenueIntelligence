from .base import Agent


class ChannelInventoryRestrictionAgent(Agent):
    """Recommends OTA restrictions (MinLOS, closing discount plans) on high-demand dates from channel mix, events and group blocks."""
    name = "channel_inventory_restriction"
