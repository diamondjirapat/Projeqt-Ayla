"""The single bot registry used by commands, API handlers, and workers."""

all_bots = []


def is_primary_for_guild(bot, guild_id: int) -> bool:
    """Whether ``bot`` owns guild-wide side effects (listeners, workers) for a guild.

    Every bot loads every cog, so each guild event reaches each bot sharing that
    guild. Exactly one of them, the first registered bot in the guild, acts on it.
    A bot that is not in the guild never does.
    """
    if bot.get_guild(guild_id) is None:
        return False
    for candidate in all_bots:
        if candidate.get_guild(guild_id) is not None:
            return candidate is bot
    return True


def eligible_bot(guild_id: int, channel_id: int | None = None):
    """Choose a ready bot that can send messages in a guild."""
    for bot in all_bots:
        guild = bot.get_guild(guild_id)
        if not bot.is_ready() or guild is None:
            continue
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if channel is None or guild.me is None:
                continue
            permissions = channel.permissions_for(guild.me)
            if not permissions.send_messages or not permissions.embed_links:
                continue
        return bot
    return None
