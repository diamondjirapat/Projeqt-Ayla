import asyncio
import io
import logging
from typing import Any, Dict

import discord
from discord.ext import commands

from database.models import GuildModel, GuildModuleModel
from utils.greetings import is_http_url, normalize_greeting_config, render_greeting_template
from utils.level_alert_images import fetch_level_alert_images, render_level_alert_image


logger = logging.getLogger(__name__)


def build_placeholder_values(
    member: discord.Member,
    channel: discord.abc.GuildChannel | None = None,
) -> Dict[str, str]:
    guild = member.guild
    avatar_url = str(member.display_avatar.url)
    server_icon_url = str(guild.icon.url) if guild.icon else ''
    guild_banner = getattr(guild, 'banner', None)
    server_banner_url = str(guild_banner.url) if guild_banner else ''
    member_count = guild.member_count if guild.member_count is not None else len(guild.members)
    channel_name = str(getattr(channel, 'name', '') or '')
    channel_id = str(getattr(channel, 'id', '') or '')
    return {
        'mention': member.mention,
        'member': member.mention,
        'user': member.display_name,
        'author': member.display_name,
        'username': member.name,
        'display_name': member.display_name,
        'user_id': str(member.id),
        'author_id': str(member.id),
        'author_mention': member.mention,
        'user_mention': member.mention,
        'server': guild.name,
        'guild': guild.name,
        'server_id': str(getattr(guild, 'id', '') or ''),
        'guild_id': str(getattr(guild, 'id', '') or ''),
        'channel': channel_name,
        'channel_mention': getattr(channel, 'mention', f'#{channel_name}' if channel_name else ''),
        'channel_id': channel_id,
        'member_count': str(member_count),
        'avatar': avatar_url,
        'server_icon': server_icon_url,
        'server_banner': server_banner_url,
    }


def build_greeting_embed(
    member: discord.Member,
    kind: str,
    raw_config: Dict[str, Any],
    channel: discord.abc.GuildChannel | None = None,
) -> tuple[discord.Embed, Dict[str, str]]:
    config = normalize_greeting_config(kind, raw_config)
    placeholders = build_placeholder_values(member, channel)
    color = discord.Color(int(config['color'].lstrip('#'), 16))
    title = render_greeting_template(config['title'], placeholders)
    description = render_greeting_template(config['description'], placeholders)
    embed = discord.Embed(title=title or None, description=description or None, color=color)

    image_url = render_greeting_template(config['image_url'], placeholders)
    if is_http_url(image_url):
        embed.set_image(url=image_url)

    thumbnail_url = render_greeting_template(config['thumbnail_url'], placeholders)
    if is_http_url(thumbnail_url):
        embed.set_thumbnail(url=thumbnail_url)

    footer_text = render_greeting_template(config['footer_text'], placeholders)
    footer_icon_url = render_greeting_template(config['footer_icon_url'], placeholders)
    if footer_text:
        embed.set_footer(text=footer_text, icon_url=footer_icon_url if is_http_url(footer_icon_url) else None)

    author = config.get('author', {})
    author_name = render_greeting_template(author.get('name', ''), placeholders)
    if author_name:
        author_url = render_greeting_template(author.get('url', ''), placeholders)
        author_icon = render_greeting_template(author.get('icon_url', ''), placeholders)
        embed.set_author(
            name=author_name,
            url=author_url if is_http_url(author_url) else None,
            icon_url=author_icon if is_http_url(author_icon) else None,
        )
    for field in config.get('fields', []):
        embed.add_field(
            name=render_greeting_template(field['name'], placeholders),
            value=render_greeting_template(field['value'], placeholders),
            inline=field.get('inline', False),
        )

    return embed, placeholders


class Greetings(commands.Cog):
    """Send configurable welcome and goodbye embeds for guild members."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_model = GuildModel()
        self.guild_module_model = GuildModuleModel()

    def _is_primary_bot_for_guild(self, guild: discord.Guild) -> bool:
        guild_bots = [bot for bot in getattr(self.bot, 'all_bots', [self.bot]) if bot.get_guild(guild.id) is not None]
        return not guild_bots or guild_bots[0] is self.bot

    async def _send_greeting(self, member: discord.Member, kind: str) -> None:
        if member.bot or not self._is_primary_bot_for_guild(member.guild):
            return
        if not await self.guild_module_model.is_enabled(member.guild.id, 'greetings', default=True):
            return

        greetings = await self.guild_model.get_greetings_config(member.guild.id)
        config = greetings[kind]
        if not config['enabled'] or not config['channel_id']:
            return

        channel = member.guild.get_channel(config['channel_id'])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(config['channel_id'])
            except discord.HTTPException:
                channel = None
        if channel is None or not hasattr(channel, 'send'):
            logger.warning(
                'Greeting channel %s is unavailable in guild %s.',
                config['channel_id'],
                member.guild.id,
            )
            return

        embed, placeholders = build_greeting_embed(member, kind, config, channel)
        content = render_greeting_template(config.get('content', ''), placeholders)
        if (
            config.get('mode') != 'image'
            and not content
            and not embed.title
            and not embed.description
            and not embed.image.url
            and not embed.fields
        ):
            logger.warning('Skipped empty %s greeting for guild %s.', kind, member.guild.id)
            return

        try:
            allowed_mentions = discord.AllowedMentions(users=[member], roles=False, everyone=False)
            if config.get('mode') == 'image':
                image_payloads = await fetch_level_alert_images(
                    config,
                    avatar_url=str(member.display_avatar.url),
                    server_icon_url=str(member.guild.icon.url) if member.guild.icon else '',
                    server_banner_url=(str(member.guild.banner.url) if getattr(member.guild, 'banner', None) else ''),
                )
                image_bytes = await asyncio.to_thread(
                    render_level_alert_image,
                    config,
                    placeholders,
                    image_payloads,
                    False,
                )
                await channel.send(
                    content=content or None,
                    file=discord.File(io.BytesIO(image_bytes), filename=f'{kind}.png'),
                    allowed_mentions=allowed_mentions,
                )
            else:
                await channel.send(
                    content=content or None,
                    embed=embed,
                    allowed_mentions=allowed_mentions,
                )
        except discord.Forbidden:
            logger.warning('Missing permission to send %s greeting in guild %s.', kind, member.guild.id)
        except discord.HTTPException as exc:
            logger.error('Failed to send %s greeting in guild %s: %s', kind, member.guild.id, exc)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._send_greeting(member, 'welcome')

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._send_greeting(member, 'goodbye')


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Greetings(bot))
