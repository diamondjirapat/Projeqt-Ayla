import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from database.guild_cache import GuildDocCache
from database.models import GuildModel, GuildModuleModel, ModerationCaseModel
from services.trap_channel import missing_permissions, penalty_text, publish_warning
from utils.i18n import i18n
from utils.moderation import normalize_casebook_config
from utils.trap_channel import DEFAULT_TIMEOUT_MINUTES, MAX_TIMEOUT_MINUTES, normalize_trap_channel_config

logger = logging.getLogger(__name__)

# One trap hit per author is enough; a spam burst must not stack penalties or flood the casebook.
PUNISH_COOLDOWN_SECONDS = 30
# The bot's own new warning reaches on_message before its id is saved, so own messages are
# re-checked after this delay instead of being deleted on sight.
OWN_MESSAGE_GRACE_SECONDS = 5

TrapAction = Literal['timeout', 'kick', 'ban', 'delete']


class TrapChannel(commands.Cog):
    """Delete every message in a trap channel and penalise its author."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_model = GuildModel()
        self.guild_module_model = GuildModuleModel()
        self.case_model = ModerationCaseModel()
        # Guild writes invalidate this cache, so dashboard changes apply immediately.
        self._config_cache = GuildDocCache(ttl=60, max_size=5000, name='guild.trap_channel')
        self._recent_hits: Dict[tuple[int, int], float] = {}

    # ------------------------------------------------------------------ helpers

    async def _get_config(self, guild_id: int, *, fresh: bool = False) -> Dict[str, Any]:
        if fresh:
            self._config_cache.invalidate(guild_id)
        doc = await self._config_cache.get_or_load(
            guild_id,
            lambda: self.guild_model.collection.find_one({'guild_id': int(guild_id)}, {'trap_channel': 1}),
        )
        return normalize_trap_channel_config((doc or {}).get('trap_channel'))

    def _bots(self) -> list[commands.Bot]:
        return list(getattr(self.bot, 'all_bots', [self.bot]))

    def _own_bot_ids(self) -> set[int]:
        return {bot.user.id for bot in self._bots() if getattr(bot, 'user', None)}

    def _is_primary_bot_for_guild(self, guild: discord.Guild) -> bool:
        guild_bots = [bot for bot in self._bots() if bot.get_guild(guild.id) is not None]
        return not guild_bots or guild_bots[0] is self.bot

    def _claim_hit(self, guild_id: int, user_id: int) -> bool:
        now = time.monotonic()
        if len(self._recent_hits) > 1000:
            self._recent_hits = {key: until for key, until in self._recent_hits.items() if until > now}
        key = (guild_id, user_id)
        if self._recent_hits.get(key, 0) > now:
            return False
        self._recent_hits[key] = now + PUNISH_COOLDOWN_SECONDS
        return True

    @staticmethod
    async def _delete(message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning('Missing Manage Messages to clear trap channel %s in guild %s', message.channel.id, message.guild.id)
        except discord.HTTPException as exc:
            logger.warning('Could not delete trap channel message %s: %s', message.id, exc)

    @staticmethod
    def _snapshot(user: discord.abc.User) -> dict[str, Any]:
        return {
            'user_id': str(user.id),
            'username': user.name,
            'display_name': getattr(user, 'display_name', user.name),
        }

    async def _log_case(self, message: discord.Message, member: discord.Member, config: Dict[str, Any], reason: str):
        guild = message.guild
        moderator = guild.me or self.bot.user
        module = await self.guild_module_model.get_module(guild.id, 'moderation_casebook')
        casebook = normalize_casebook_config(module.get('config', {}) if module else {})
        duration = config['timeout_minutes'] * 60 if config['action'] == 'timeout' else None
        try:
            await self.case_model.create_case(
                guild.id,
                config['action'],
                member.id,
                moderator.id,
                reason=reason,
                internal_note=f'Trap channel message: {message.content[:900]}' if message.content else '',
                duration_seconds=duration,
                expires_at=datetime.now(UTC) + timedelta(seconds=duration) if duration else None,
                target_snapshot=self._snapshot(member),
                moderator_snapshot=self._snapshot(moderator),
                source='trap_channel',
                retention_days=casebook['case_retention_days'],
            )
        except Exception:
            logger.exception('Could not record trap channel case in guild %s', guild.id)

    async def _punish(self, message: discord.Message, config: Dict[str, Any]) -> None:
        guild = message.guild
        action = config['action']
        if action == 'delete' or message.webhook_id or message.is_system():
            return
        member = message.author if isinstance(message.author, discord.Member) else guild.get_member(message.author.id)
        if member is None or member.id == guild.owner_id:
            return
        if member.bot and not config['punish_bots']:
            return
        exempt = set(config['exempt_role_ids'])
        if exempt and any(role.id in exempt for role in member.roles):
            return
        if action == 'timeout' and member.is_timed_out():
            return
        if not self._claim_hit(guild.id, member.id):
            return

        reason = f'{config["reason"]} (#{message.channel.name})'[:512]
        try:
            if action == 'timeout':
                await member.timeout(timedelta(minutes=config['timeout_minutes']), reason=reason)
            elif action == 'kick':
                await member.kick(reason=reason)
            elif action == 'ban':
                # Spam usually lands in several channels at once; clear the last hour of it too.
                await member.ban(reason=reason, delete_message_seconds=3600)
        except discord.Forbidden:
            logger.info('Trap channel could not %s %s in guild %s (hierarchy or permissions)', action, member.id, guild.id)
            return
        except discord.HTTPException as exc:
            logger.warning('Trap channel %s failed for %s in guild %s: %s', action, member.id, guild.id, exc)
            return
        logger.info('Trap channel applied %s to %s in guild %s', action, member.id, guild.id)
        if config['log_case']:
            await self._log_case(message, member, config, reason)

    async def _save_and_publish(self, guild: discord.Guild, config: Dict[str, Any], previous: Dict[str, Any]):
        """Persist the config, then post or edit the warning and store its message id."""
        config = await self.guild_model.set_trap_channel_config(guild.id, config)
        if config['enabled'] and config['channel_id']:
            message_id = await publish_warning(
                guild,
                config,
                previous_channel_id=previous.get('channel_id'),
                previous_message_id=previous.get('message_id'),
            )
            if message_id != config['message_id']:
                config['message_id'] = message_id
                config = await self.guild_model.set_trap_channel_config(guild.id, config)
        return config

    async def _summary_embed(self, ctx: commands.Context, config: Dict[str, Any], title_key: str) -> discord.Embed:
        locale = await i18n.get_guild_locale(ctx.guild.id) or i18n.default_locale
        channel = ctx.guild.get_channel(config['channel_id']) if config['channel_id'] else None
        embed = discord.Embed(title=await i18n.t(ctx, title_key), color=discord.Color.red())
        embed.add_field(
            name=await i18n.t(ctx, 'trap_channel.info.channel'),
            value=channel.mention if channel else await i18n.t(ctx, 'trap_channel.info.not_set'),
            inline=True,
        )
        status_key = 'trap_channel.info.enabled' if config['enabled'] else 'trap_channel.info.disabled'
        embed.add_field(name=await i18n.t(ctx, 'trap_channel.info.status'), value=await i18n.t(ctx, status_key))
        embed.add_field(
            name=await i18n.t(ctx, 'trap_channel.info.penalty'),
            value=penalty_text(config, locale),
            inline=False,
        )
        missing = missing_permissions(ctx.guild, channel, config['action']) if channel else []
        if missing:
            embed.add_field(
                name=await i18n.t(ctx, 'trap_channel.info.missing_permissions'),
                value=', '.join(f'`{name}`' for name in missing),
                inline=False,
            )
        embed.set_footer(text=await i18n.t(ctx, 'trap_channel.info.dashboard_hint'))
        return embed

    async def _validate_minutes(self, ctx: commands.Context, minutes: Optional[int]) -> bool:
        if minutes is not None and not 1 <= minutes <= MAX_TIMEOUT_MINUTES:
            await ctx.send(await i18n.t(ctx, 'trap_channel.errors.invalid_minutes', max=MAX_TIMEOUT_MINUTES))
            return False
        return True

    # ------------------------------------------------------------------ events

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or not self._is_primary_bot_for_guild(guild):
            return
        try:
            config = await self._get_config(guild.id)
        except Exception:
            logger.exception('Could not load trap channel settings for guild %s', guild.id)
            return
        if not config['enabled'] or message.channel.id != config['channel_id'] or message.id == config['message_id']:
            return
        if not await self.guild_module_model.is_enabled(guild.id, 'trap_channel', default=True):
            return

        if message.author.id in self._own_bot_ids():
            await asyncio.sleep(OWN_MESSAGE_GRACE_SECONDS)
            config = await self._get_config(guild.id, fresh=True)
            if message.id == config['message_id'] or message.channel.id != config['channel_id']:
                return
            await self._delete(message)
            return

        await self._delete(message)
        await self._punish(message, config)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not self._is_primary_bot_for_guild(channel.guild):
            return
        config = await self._get_config(channel.guild.id, fresh=True)
        if config['channel_id'] == channel.id:
            config.update({'enabled': False, 'channel_id': None, 'message_id': None})
            await self.guild_model.set_trap_channel_config(channel.guild.id, config)
            logger.info('Trap channel deleted in guild %s; trap disabled', channel.guild.id)

    # ------------------------------------------------------------------ commands

    @commands.hybrid_group(name='trapchannel', fallback='info')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def trapchannel(self, ctx: commands.Context):
        """View the trap channel settings"""
        config = await self._get_config(ctx.guild.id, fresh=True)
        embed = await self._summary_embed(ctx, config, 'trap_channel.info.title')
        embed.description = await i18n.t(ctx, 'trap_channel.info.usage', prefix=ctx.prefix)
        await ctx.send(embed=embed)

    @trapchannel.command(name='create')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_channels=True)
    @app_commands.describe(
        name='Name of the new trap channel',
        timeout_minutes='Timeout length in minutes (default 30)',
    )
    async def trapchannel_create(
        self,
        ctx: commands.Context,
        name: str = 'do-not-type-here',
        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    ):
        """Create a trap channel that times out anyone who posts in it"""
        if not await self._validate_minutes(ctx, timeout_minutes):
            return
        if ctx.interaction:
            await ctx.defer()
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(
                add_reactions=False,
                create_public_threads=False,
                create_private_threads=False,
            ),
        }
        if ctx.guild.me:
            overwrites[ctx.guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                manage_messages=True,
                read_message_history=True,
            )
        try:
            channel = await ctx.guild.create_text_channel(
                name=name[:100],
                topic=await i18n.t(ctx, 'trap_channel.channel_topic'),
                overwrites=overwrites,
                reason=f'Trap channel created by {ctx.author} ({ctx.author.id})',
            )
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'trap_channel.errors.create_failed'))
            return
        previous = await self._get_config(ctx.guild.id, fresh=True)
        config = {
            **previous,
            'enabled': True,
            'channel_id': channel.id,
            'message_id': None,
            'action': 'timeout',
            'timeout_minutes': timeout_minutes,
        }
        try:
            config = await self._save_and_publish(ctx.guild, config, previous)
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'trap_channel.errors.publish_failed'))
            return
        await ctx.send(embed=await self._summary_embed(ctx, config, 'trap_channel.create.success'))

    @trapchannel.command(name='set')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(
        channel='Existing channel to turn into a trap',
        timeout_minutes='Timeout length in minutes (default 30)',
    )
    async def trapchannel_set(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        timeout_minutes: Optional[int] = None,
    ):
        """Turn an existing channel into the trap channel"""
        if not await self._validate_minutes(ctx, timeout_minutes):
            return
        previous = await self._get_config(ctx.guild.id, fresh=True)
        config = {**previous, 'enabled': True, 'channel_id': channel.id}
        if previous['channel_id'] != channel.id:
            config['message_id'] = None
        if timeout_minutes is not None:
            config['timeout_minutes'] = timeout_minutes
        try:
            config = await self._save_and_publish(ctx.guild, config, previous)
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'trap_channel.errors.publish_failed'))
            return
        await ctx.send(embed=await self._summary_embed(ctx, config, 'trap_channel.set.success'))

    @trapchannel.command(name='penalty')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(
        action='What happens to someone who posts in the trap channel',
        timeout_minutes='Timeout length in minutes (default 30)',
    )
    async def trapchannel_penalty(
        self,
        ctx: commands.Context,
        action: TrapAction,
        timeout_minutes: Optional[int] = None,
    ):
        """Change the trap channel penalty"""
        if not await self._validate_minutes(ctx, timeout_minutes):
            return
        previous = await self._get_config(ctx.guild.id, fresh=True)
        config = {**previous, 'action': action}
        if timeout_minutes is not None:
            config['timeout_minutes'] = timeout_minutes
        try:
            config = await self._save_and_publish(ctx.guild, config, previous)
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'trap_channel.errors.publish_failed'))
            return
        await ctx.send(embed=await self._summary_embed(ctx, config, 'trap_channel.penalty.success'))

    @trapchannel.command(name='refresh')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def trapchannel_refresh(self, ctx: commands.Context):
        """Repost or update the trap channel warning"""
        config = await self._get_config(ctx.guild.id, fresh=True)
        if not config['enabled'] or not config['channel_id']:
            await ctx.send(await i18n.t(ctx, 'trap_channel.errors.not_configured', prefix=ctx.prefix))
            return
        try:
            config = await self._save_and_publish(ctx.guild, config, config)
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'trap_channel.errors.publish_failed'))
            return
        await ctx.send(embed=await self._summary_embed(ctx, config, 'trap_channel.refresh.success'))

    @trapchannel.command(name='disable')
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def trapchannel_disable(self, ctx: commands.Context):
        """Stop the trap channel (the channel itself is kept)"""
        config = await self._get_config(ctx.guild.id, fresh=True)
        config['enabled'] = False
        config = await self.guild_model.set_trap_channel_config(ctx.guild.id, config)
        await ctx.send(embed=await self._summary_embed(ctx, config, 'trap_channel.disable.success'))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TrapChannel(bot))
