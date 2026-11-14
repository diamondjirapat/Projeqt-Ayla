import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.scheduler_models import (
    CLAIM_LEASE,
    MAX_CLAIM_PER_TICK,
    SCHEDULER_KINDS,
    SCHEDULER_RECURRENCES,
    STALE_AFTER,
    ScheduledItemModel,
    ensure_utc,
    normalize_scheduler_embed,
    serialize_scheduler_item,
    validate_scheduler_payload,
)
from utils.i18n import i18n
from runtime.bots import eligible_bot
from services.scheduler_delivery import DeliveryUncertain, delivery_marker, reconcile_delivery

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULER_EMBED_COLOR = '#5865f2'


def render_scheduler_template(template: str, values: Dict[str, str]) -> str:
    """Replace ``{server}``, ``{channel}``, and ``{date}`` placeholders."""
    rendered = str(template or '')
    for name, value in values.items():
        rendered = rendered.replace(f'{{{name}}}', value)
    return rendered


def build_scheduler_embed(item: Dict[str, Any], guild_name: str = '', channel_name: str = '') -> discord.Embed:
    """Construct the announcement embed for a scheduled item."""
    embed_config = normalize_scheduler_embed(item.get('embed'))
    values = {'server': guild_name, 'channel': f'#{channel_name}' if channel_name else '', 'date': ''}
    title_source = item.get('title') or embed_config.get('title') or '📢 Announcement'
    title = render_scheduler_template(title_source, values)
    description_source = embed_config.get('description') or item.get('content', '')
    description = render_scheduler_template(description_source, values)
    raw_color = embed_config.get('color', DEFAULT_SCHEDULER_EMBED_COLOR)
    try:
        color = discord.Color(int(raw_color.removeprefix('#'), 16))
    except ValueError:
        color = discord.Color.blurple()
    embed = discord.Embed(title=title[:256], description=description[:4000], color=color)
    thumbnail_url = render_scheduler_template(embed_config.get('thumbnail_url', ''), values)
    image_url = render_scheduler_template(embed_config.get('image_url', ''), values)
    if thumbnail_url.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=thumbnail_url)
    if image_url.startswith(('http://', 'https://')):
        embed.set_image(url=image_url)
    footer = render_scheduler_template(embed_config.get('footer', ''), values)
    if footer:
        footer_icon = render_scheduler_template(embed_config.get('footer_icon_url', ''), values)
        if footer_icon.startswith(('http://', 'https://')):
            embed.set_footer(text=footer[:2048], icon_url=footer_icon)
        else:
            embed.set_footer(text=footer[:2048])
    return embed


def describe_scheduler_payload_error(exc: ValueError) -> str:
    """Map a payload validation error to a scheduler locale message key."""
    message = str(exc)
    if 'must be in the future' in message:
        return 'scheduler.past_time'
    if 'ISO-8601' in message:
        return 'scheduler.invalid_datetime'
    return 'scheduler.invalid_field'


class Scheduler(commands.Cog):
    """Scheduled announcements and Discord events with calendar management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduler_model = ScheduledItemModel()
        self.worker_id = secrets.token_hex(16)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            self.check_schedules.start()

    async def cog_unload(self):
        task = self.check_schedules.get_task()
        self.check_schedules.cancel()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def cog_before_invoke(self, ctx: commands.Context):
        """Automatically defer slash commands to prevent timeout."""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_schedules.is_running():
            self.check_schedules.start()
        logger.info(f'{self.__class__.__name__} cog loaded successfully')

    @tasks.loop(seconds=30)
    async def check_schedules(self):
        """Periodic background task to dispatch due scheduled items."""
        try:
            guild_ids, channel_ids = [], []
            for guild in self.bot.guilds:
                if guild.me is None:
                    continue
                for channel in guild.text_channels:
                    permissions = channel.permissions_for(guild.me)
                    if not permissions.send_messages or not permissions.embed_links:
                        continue
                    owner = eligible_bot(guild.id, channel.id)
                    if owner is None or owner is self.bot:
                        guild_ids.append(guild.id)
                        channel_ids.append(channel.id)
            # Claim immediately before dispatch so queued work cannot outlive its lease.
            for _ in range(MAX_CLAIM_PER_TICK):
                now = datetime.now(UTC)
                due = await self.scheduler_model.claim_due_items(now, guild_ids=guild_ids, channel_ids=channel_ids, owner=self.worker_id)
                if not due:
                    break
                item = due[0]
                try:
                    await asyncio.wait_for(self.dispatch_item(item, now), CLAIM_LEASE.total_seconds() / 2)
                except asyncio.CancelledError:
                    await self.scheduler_model.release_claim(item['_id'], claim_token=item['claim_token'])
                    raise
                except DeliveryUncertain:
                    logger.warning(f'Scheduled item {item.get("_id")} awaits delivery reconciliation')
                except Exception as e:
                    logger.error(f'Error dispatching scheduled item {item.get("_id")}: {e}')
                    await self.scheduler_model.mark_failed(item['_id'], str(e), claim_token=item['claim_token'])
        except Exception as e:
            logger.error(f'Error in check_schedules task loop: {e}')

    @check_schedules.before_loop
    async def before_check_schedules(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def mentions_for(guild: discord.Guild, channel: Any, item: Dict[str, Any]) -> discord.AllowedMentions:
        """Allow @everyone/role pings only when whoever last wrote the content may ping in that channel."""
        author_id = int(item.get('content_author_id') or item.get('creator_id') or 0)
        author = guild.get_member(author_id) if author_id else None
        can_ping = False
        if author is not None:
            try:
                can_ping = bool(channel.permissions_for(author).mention_everyone)
            except Exception:
                can_ping = False
        return discord.AllowedMentions(everyone=can_ping, roles=can_ping, users=True, replied_user=False)

    def template_values(self, guild: Optional[discord.Guild], channel: Any, item: Dict[str, Any]) -> Dict[str, str]:
        """Build placeholder values for announcement templates."""
        starts_at = ensure_utc(item.get('next_run_at') or item.get('starts_at') or datetime.now(UTC))
        return {
            'server': str(getattr(guild, 'name', '') or ''),
            'channel': str(getattr(channel, 'mention', '') or ''),
            'date': f'<t:{int(starts_at.timestamp())}:F>',
        }

    async def create_native_event(self, guild: discord.Guild, channel: Any, payload: Dict[str, Any]) -> Optional[int]:
        """Create the Discord scheduled event for an event-kind payload."""
        starts_at = ensure_utc(payload['starts_at'])
        event_end_at = ensure_utc(payload.get('event_end_at') or (starts_at + timedelta(hours=1)))
        if event_end_at <= starts_at:
            event_end_at = starts_at + timedelta(hours=1)
        location = (payload.get('location') or '').strip() or f'#{getattr(channel, "name", "events")}'
        event = await guild.create_scheduled_event(
            name=(payload.get('title') or 'Community Event')[:100],
            description=payload.get('content', '')[:1000],
            start_time=starts_at,
            end_time=event_end_at,
            location=location[:100],
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
        )
        return int(event.id)

    async def delete_native_event(self, guild: discord.Guild, discord_event_id: Any) -> None:
        """Delete a previously created Discord scheduled event, if it still exists."""
        if not discord_event_id:
            return
        try:
            event = guild.get_scheduled_event(int(discord_event_id))
            if event is None:
                event = await guild.fetch_scheduled_event(int(discord_event_id))
            await event.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError) as e:
            logger.debug(f'Could not delete native scheduled event {discord_event_id}: {e}')

    async def sync_native_event(self, guild: discord.Guild, item: Dict[str, Any]) -> None:
        """Align the native Discord event with a rescheduled item."""
        discord_event_id = item.get('discord_event_id')
        if item.get('kind') != 'event':
            if discord_event_id:
                await self.delete_native_event(guild, discord_event_id)
                await self.scheduler_model.collection.update_one({'_id': item['_id'], 'discord_event_id': discord_event_id}, {'$set': {'discord_event_id': None}})
            return
        try:
            if not discord_event_id:
                channel = guild.get_channel(int(item['channel_id']))
                discord_event_id = await self.create_native_event(guild, channel, item)
                try:
                    query = {'_id': item['_id'], 'kind': 'event', 'status': 'scheduled', 'discord_event_id': None}
                    if 'updated_at' in item:
                        query['updated_at'] = item['updated_at']
                    result = await self.scheduler_model.collection.update_one(query, {'$set': {'discord_event_id': discord_event_id}})
                    if not result.matched_count:
                        await self.delete_native_event(guild, discord_event_id)
                except BaseException:
                    await self.delete_native_event(guild, discord_event_id)
                    raise
                return
            event = guild.get_scheduled_event(int(discord_event_id))
            if event is None:
                event = await guild.fetch_scheduled_event(int(discord_event_id))
            starts_at = ensure_utc(item.get('next_run_at') or item.get('starts_at'))
            event_end_at = item.get('event_end_at')
            event_end_at = ensure_utc(event_end_at) if event_end_at else starts_at + timedelta(hours=1)
            if event_end_at <= starts_at:
                event_end_at = starts_at + timedelta(hours=1)
            channel = guild.get_channel(int(item.get('channel_id') or 0))
            await event.edit(
                name=(item.get('title') or 'Community Event')[:100],
                description=item.get('content', '')[:1000],
                start_time=starts_at,
                end_time=event_end_at,
                location=(item.get('location') or f'#{getattr(channel, "name", "events")}')[:100],
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError) as e:
            logger.debug(f'Could not sync native scheduled event {discord_event_id}: {e}')

    async def schedule_item(
        self,
        guild: discord.Guild,
        channel: Any,
        creator_id: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist an item, creating its native Discord event first for event kinds."""
        discord_event_id = None
        if payload.get('kind') == 'event':
            discord_event_id = await self.create_native_event(guild, channel, payload)
        try:
            return await self.scheduler_model.create_item(guild.id, channel.id, creator_id, payload, discord_event_id=discord_event_id)
        except BaseException:
            if discord_event_id:
                await self.delete_native_event(guild, discord_event_id)
            raise

    async def cancel_item(self, guild: discord.Guild, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Cancel an item and remove its native Discord event."""
        cancelled = await self.scheduler_model.cancel_item(item.get('_id'), item.get('guild_id'))
        if cancelled:
            await self.delete_native_event(guild, cancelled.get('discord_event_id'))
        return cancelled

    async def dispatch_item(self, item: Dict[str, Any], now: datetime) -> None:
        """Send one claimed item, then mark it done or advance its recurrence."""
        next_run_at = ensure_utc(item.get('next_run_at') or now)
        if now - next_run_at > STALE_AFTER and not item.get('delivery_attempt'):
            logger.info(f'Skipping stale scheduled item {item.get("_id")} from {next_run_at.isoformat()}')
            await self.scheduler_model.resolve_item(item['_id'], now, claim_token=item['claim_token'])
            return
        guild = self.bot.get_guild(int(item.get('guild_id') or 0))
        if guild is None:
            raise RuntimeError('Server not found.')
        channel = guild.get_channel(int(item.get('channel_id') or 0))
        if channel is None or not isinstance(channel, discord.TextChannel):
            raise RuntimeError('Announcement channel not found.')
        if item.get('delivery_attempt'):
            await reconcile_delivery(self.scheduler_model, item, channel, now)
            return
        values = self.template_values(guild, channel, item)
        content = render_scheduler_template(item.get('content', ''), values)
        embed = build_scheduler_embed(item, getattr(guild, 'name', ''), getattr(channel, 'name', ''))
        marker = delivery_marker(item)
        footer = str(embed.footer.text or '')
        embed.set_footer(text=f'{footer} · {marker}' if footer else marker, icon_url=embed.footer.icon_url)
        if not await self.scheduler_model.begin_delivery(item['_id'], datetime.now(UTC), claim_token=item['claim_token'], marker=marker, bot_id=self.bot.user.id):
            return
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=self.mentions_for(guild, channel, item))
        except Exception as exc:
            raise DeliveryUncertain('Discord send outcome requires reconciliation') from exc
        if item.get('kind') == 'event' and not item.get('discord_event_id'):
            try:
                discord_event_id = await self.create_native_event(
                    guild,
                    channel,
                    {
                        **item,
                        'starts_at': next_run_at,
                    },
                )
                await self.scheduler_model.collection.update_one({'_id': item.get('_id')}, {'$set': {'discord_event_id': discord_event_id}})
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f'Scheduled event announcement posted but native event failed: {e}')
        try:
            await self.scheduler_model.resolve_item(item['_id'], now, claim_token=item['claim_token'])
        except Exception as exc:
            raise DeliveryUncertain('Discord accepted the message; completion requires reconciliation') from exc

    def _require_guild(self, ctx: commands.Context) -> Optional[discord.Guild]:
        return getattr(ctx, 'guild', None)

    @commands.hybrid_group(name='schedule', invoke_without_command=True)
    async def schedule_group(self, ctx: commands.Context):
        """Manage scheduled announcements and events."""
        if ctx.invoked_subcommand is None:
            await self.list_schedules(ctx)

    @schedule_group.command(name='create')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel='Announcement channel',
        start='Start time in ISO-8601 format',
        message='Announcement message',
        kind='Announcement or event',
        title='Optional title',
        recurrence='Repeat schedule',
        location='Optional event location',
    )
    @app_commands.choices(
        kind=[app_commands.Choice(name=name, value=name) for name in SCHEDULER_KINDS],
        recurrence=[app_commands.Choice(name=name, value=name) for name in SCHEDULER_RECURRENCES],
    )
    async def schedule_create(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
        start: str,
        message: str,
        kind: str = 'announcement',
        title: str = '',
        recurrence: str = 'once',
        location: str = '',
    ):
        """Schedule an announcement or event."""
        guild = self._require_guild(ctx)
        if guild is None:
            await ctx.send(await i18n.t(ctx, 'scheduler.no_guild'))
            return
        try:
            payload = validate_scheduler_payload(
                {
                    'kind': kind,
                    'title': title,
                    'content': message,
                    'starts_at': start,
                    'recurrence': recurrence,
                    'location': location,
                }
            )
        except ValueError as exc:
            await ctx.send(await i18n.t(ctx, describe_scheduler_payload_error(exc), error=str(exc)))
            return
        try:
            await self.schedule_item(guild, channel, ctx.author.id, payload)
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(await i18n.t(ctx, 'scheduler.error', error=str(exc)))
            return
        starts_at = ensure_utc(payload['starts_at'])
        await ctx.send(
            await i18n.t(
                ctx,
                'scheduler.created',
                kind=payload['kind'],
                timestamp=int(starts_at.timestamp()),
            )
        )

    @schedule_group.command(name='list')
    @commands.has_permissions(manage_guild=True)
    async def schedule_list(self, ctx: commands.Context):
        """List upcoming scheduled announcements and events."""
        await self.list_schedules(ctx)

    async def list_schedules(self, ctx: commands.Context):
        """List upcoming scheduled announcements and events."""
        guild = self._require_guild(ctx)
        if guild is None:
            await ctx.send(await i18n.t(ctx, 'scheduler.no_guild'))
            return
        items = await self.scheduler_model.list_guild_items(guild.id, include_cancelled=False)
        upcoming = [item for item in items if item.get('status') == 'scheduled'][:10]
        if not upcoming:
            await ctx.send(await i18n.t(ctx, 'scheduler.list_empty'))
            return
        lines = []
        for item in upcoming:
            serialized = serialize_scheduler_item(item)
            channel = guild.get_channel(int(item.get('channel_id') or 0))
            starts_at = ensure_utc(item.get('next_run_at') or item.get('starts_at') or datetime.now(UTC))
            lines.append(
                await i18n.t(
                    ctx,
                    'scheduler.list_item',
                    id=serialized.get('item_id', ''),
                    title=item.get('title') or item.get('kind', ''),
                    timestamp=int(starts_at.timestamp()),
                    channel=getattr(channel, 'mention', '#unknown'),
                )
            )
        header = await i18n.t(ctx, 'scheduler.list_title', server=guild.name)
        await ctx.send(f'{header}\n' + '\n'.join(lines))

    @schedule_group.command(name='cancel')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(item_id='Scheduled item ID')
    async def schedule_cancel(self, ctx: commands.Context, item_id: str):
        """Cancel a scheduled announcement or event."""
        guild = self._require_guild(ctx)
        if guild is None:
            await ctx.send(await i18n.t(ctx, 'scheduler.no_guild'))
            return
        item = await self.scheduler_model.get_item(item_id)
        if not item or int(item.get('guild_id') or 0) != int(guild.id):
            await ctx.send(await i18n.t(ctx, 'scheduler.not_found'))
            return
        cancelled = await self.cancel_item(guild, item)
        if not cancelled:
            await ctx.send(await i18n.t(ctx, 'scheduler.not_found'))
            return
        await ctx.send(await i18n.t(ctx, 'scheduler.cancelled', id=str(item.get('_id'))))


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
