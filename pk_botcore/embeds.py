"""Discord embed utilities."""

import discord


def make_embed(title: str, msg) -> discord.Embed:
    """
    Create a Discord embed from various input types.

    Args:
        title: The embed title
        msg: Content - can be str, list (joined with newlines), or dict (fields)

    Returns:
        Configured Discord Embed
    """
    embed = discord.Embed(title=title)

    if isinstance(msg, list):
        embed.description = "\n".join(str(x) for x in msg)
    elif isinstance(msg, dict):
        for k, v in msg.items():
            embed.add_field(name=k, value=v, inline=False)
    else:
        embed.description = str(msg)

    return embed
