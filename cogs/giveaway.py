from datetime import datetime, timedelta, UTC
import logging
import random
import re
from typing import Optional, List, Dict, Any
import urllib.parse

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.models import GiveawayModel, GuildModel
from database.giveaways import is_giveaway_closed
from runtime.bots import is_primary_for_guild
from utils.i18n import i18n

logger = logging.getLogger(__name__)

DURATION_PATTERN = re.compile(
    r'^((?P<days>\d+?)\s*(?:d|days?))?\s*'
    r'((?P<hours>\d+?)\s*(?:h|hours?|hrs?))?\s*'
    r'((?P<minutes>\d+?)\s*(?:m|mins?|minutes?))?\s*'
    r'((?P<seconds>\d+?)\s*(?:s|secs?|seconds?))?$',
    re.IGNORECASE,
)

DEFAULT_GIVEAWAY_EMBED_COLOR = '#f1c40f'
GIVEAWAY_EMBED_TEXT_LIMITS = {
    'title': 256,
    'description': 3500,
    'footer': 512,
}
GIVEAWAY_EMBED_URL_FIELDS = ('image_url', 'thumbnail_url', 'footer_icon_url')
GIVEAWAY_MEDIA_VARIABLES = {'{avatar}', '{server_icon}', '{server_banner}'}

GIVEAWAY_PRIZE_LIMIT = 1000
GIVEAWAY_LIST_DESCRIPTION_LIMIT = 4096


def _coerce_end_time(end_time: Any) -> datetime:
    """Return a timezone-aware end time, tolerating bad stored values.

    Older or hand-edited records may carry a missing, naive, or non-datetime
    value. Naive values are assumed to be UTC; anything unusable falls back
    to "now" so embed rendering never crashes.
    """
    if isinstance(end_time, datetime):
        if end_time.tzinfo is None:
            return end_time.replace(tzinfo=UTC)
        return end_time
    return datetime.now(UTC)


def _chunk_winner_mentions(winners: List[int], limit: int = 1000) -> List[str]:
    """Split winner mentions into chunks that fit an embed field value.

    Discord caps field values at 1024 characters; a giveaway with many
    winners would otherwise fail the message edit with a 400.
    """
    chunks: List[str] = []
    current = ''
    for winner in winners or []:
        mention = f'<@{winner}>'
        piece = mention if not current else f'{current}, {mention}'
        if len(piece) > limit and current:
            chunks.append(current)
            current = mention
        else:
            current = piece
    if current:
        chunks.append(current)
    return chunks


def normalize_giveaway_embed_config(raw_config: Any, *, strict: bool = False) -> Dict[str, str]:
    """Return a Discord-safe giveaway embed configuration.

    Strict mode is used at the HTTP boundary so malformed user input returns a
    useful error. Non-strict mode keeps rendering resilient if an older record
    contains an invalid or unexpectedly shaped value.
    """
    if raw_config is None:
        return {}
    if not isinstance(raw_config, dict):
        if strict:
            raise ValueError('embed must be an object.')
        return {}

    normalized: Dict[str, str] = {}
    for field, maximum in GIVEAWAY_EMBED_TEXT_LIMITS.items():
        value = str(raw_config.get(field, '') or '').strip()
        if len(value) > maximum:
            if strict:
                raise ValueError(f'embed.{field} must be {maximum} characters or fewer.')
            value = value[:maximum]
        if value:
            normalized[field] = value

    raw_color = str(raw_config.get('color', '') or '').strip()
    if raw_color:
        color = raw_color if raw_color.startswith('#') else f'#{raw_color}'
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            if strict:
                raise ValueError('embed.color must be a six-digit hex color.')
        else:
            normalized['color'] = color.lower()

    for field in GIVEAWAY_EMBED_URL_FIELDS:
        value = str(raw_config.get(field, '') or '').strip()
        if not value:
            continue
        if len(value) > 2048:
            if strict:
                raise ValueError(f'embed.{field} must be 2048 characters or fewer.')
            continue
        if value in GIVEAWAY_MEDIA_VARIABLES:
            normalized[field] = value
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
            valid_url = parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
        except ValueError:
            valid_url = False
            parsed = None
        if not valid_url or (parsed and (parsed.username or parsed.password)):
            if strict:
                raise ValueError(f'embed.{field} must be a valid HTTP or HTTPS URL.')
            continue
        normalized[field] = value

    return normalized


def render_giveaway_template(template: str, values: Dict[str, str]) -> str:
    rendered = str(template or '')
    for name, value in values.items():
        rendered = rendered.replace(f'{{{name}}}', value)
    return rendered


def parse_duration(duration_str: str) -> Optional[timedelta]:
    """Parse human-friendly duration strings (e.g. 10m, 1h 30m, 2d, 45s)."""
    if not duration_str:
        return None

    clean_str = duration_str.strip()
    match = DURATION_PATTERN.match(clean_str)
    if not match:
        return None

    parts = match.groupdict()
    time_params = {}
    for name, param in parts.items():
        if param:
            time_params[name] = int(param)

    if not time_params:
        return None

    try:
        td = timedelta(**time_params)
        _ = datetime.now(UTC) + td
    except OverflowError:
        return None
    return td if td.total_seconds() > 0 else None


class GiveawayView(discord.ui.View):
    """Persistent interactive view for entering and leaving a giveaway."""

    def __init__(self, message_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(
        label='🎉 Enter Giveaway',
        style=discord.ButtonStyle.primary,
        custom_id='giveaway_entry_button',
    )
    async def enter_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        source_message = interaction.message
        target_msg_id = self.message_id or (source_message.id if source_message else None)
        if target_msg_id is None:
            msg = await i18n.t(interaction, 'giveaway.not_found')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        giveaway_model = GiveawayModel()
        giveaway = await giveaway_model.get_giveaway(target_msg_id)

        if not giveaway:
            msg = await i18n.t(interaction, 'giveaway.not_found')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if is_giveaway_closed(giveaway):
            msg = await i18n.t(interaction, 'giveaway.already_ended')
            await interaction.response.send_message(msg, ephemeral=True)
            return

        user_id = interaction.user.id

        # add_entry is atomic: success means this click entered the user. On
        # failure the snapshot above may be stale (already entered, or the
        # giveaway ended concurrently), so re-read before responding.
        if await giveaway_model.add_entry(target_msg_id, user_id):
            msg = await i18n.t(interaction, 'giveaway.entered_success', prize=giveaway.get('prize', 'Prize'))
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            fresh_giveaway = await giveaway_model.get_giveaway(target_msg_id)
            if not fresh_giveaway:
                msg = await i18n.t(interaction, 'giveaway.not_found')
                await interaction.response.send_message(msg, ephemeral=True)
                return
            if is_giveaway_closed(fresh_giveaway):
                msg = await i18n.t(interaction, 'giveaway.already_ended')
                await interaction.response.send_message(msg, ephemeral=True)
                return
            if user_id in (fresh_giveaway.get('entries') or []):
                # User was already entered -> toggle/leave
                await giveaway_model.remove_entry(target_msg_id, user_id)
                msg = await i18n.t(interaction, 'giveaway.left_success', prize=fresh_giveaway.get('prize', 'Prize'))
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                msg = await i18n.t(interaction, 'giveaway.error', error='Please try again.')
                await interaction.response.send_message(msg, ephemeral=True)
                return

        # Refresh embed entry count in background
        cog = interaction.client.get_cog('Giveaway')
        if cog and source_message:
            try:
                updated_giveaway = await giveaway_model.get_giveaway(target_msg_id)
                if updated_giveaway and not updated_giveaway.get('ended', False):
                    embed = await cog.build_giveaway_embed(updated_giveaway, interaction)
                    await source_message.edit(embed=embed)
            except Exception as e:
                logger.debug(f'Failed to update giveaway message embed: {e}')


class Giveaway(commands.Cog):
    """Giveaway system with interactive buttons, automated timer, and reroll support."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.giveaway_model = GiveawayModel()
        self.guild_model = GuildModel()

    async def cog_load(self):
        # (Re)start the background task whenever the cog is (re)loaded. The
        # on_ready listener below remains as a backstop for the first connect.
        if not self.check_giveaways.is_running():
            self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    async def cog_before_invoke(self, ctx: commands.Context):
        """Automatically defer slash commands to prevent timeout."""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_giveaways.is_running():
            self.check_giveaways.start()
        logger.info(f'{self.__class__.__name__} cog loaded successfully')

    async def build_giveaway_embed(
        self,
        giveaway: Dict[str, Any],
        ctx_or_interaction: Any,
        ended: bool = False,
        winners: Optional[List[int]] = None,
    ) -> discord.Embed:
        """Construct the giveaway embed for active and ended states."""
        prize = giveaway.get('prize', 'Prize')
        host_id = giveaway.get('host_id', 0)
        winners_count = giveaway.get('winners_count', 1)
        end_time = _coerce_end_time(giveaway.get('end_time'))
        entries = giveaway.get('entries') or []
        entry_count = len(entries)

        end_timestamp = int(end_time.timestamp())

        extra_data = giveaway.get('extra_data', {})
        if not isinstance(extra_data, dict):
            extra_data = {}
        embed_config = normalize_giveaway_embed_config(extra_data.get('embed'))
        guild = getattr(ctx_or_interaction, 'guild', None)
        channel = getattr(ctx_or_interaction, 'channel', None)
        if channel is None and isinstance(ctx_or_interaction, discord.abc.GuildChannel):
            channel = ctx_or_interaction
        guild = guild or getattr(channel, 'guild', None) or self.bot.get_guild(int(giveaway.get('guild_id') or 0))
        host = guild.get_member(int(host_id)) if guild else None
        host_name = str(getattr(host, 'display_name', '') or getattr(host, 'name', '') or host_id)
        avatar_url = str(getattr(getattr(host, 'display_avatar', None), 'url', '') or '')
        server_icon_url = str(guild.icon.url) if guild and guild.icon else ''
        server_banner = getattr(guild, 'banner', None) if guild else None
        server_banner_url = str(server_banner.url) if server_banner else ''
        channel_name = str(getattr(channel, 'name', '') or '')
        placeholder_values = {
            'prize': str(prize),
            'mention': f'<@{host_id}>',
            'member': f'<@{host_id}>',
            'user': host_name,
            'author': host_name,
            'username': str(getattr(host, 'name', '') or host_name),
            'display_name': host_name,
            'user_id': str(host_id),
            'author_id': str(host_id),
            'server': str(getattr(guild, 'name', '') or ''),
            'guild': str(getattr(guild, 'name', '') or ''),
            'server_id': str(getattr(guild, 'id', '') or ''),
            'guild_id': str(getattr(guild, 'id', '') or ''),
            'channel': channel_name,
            'channel_mention': str(getattr(channel, 'mention', '') or (f'#{channel_name}' if channel_name else '')),
            'channel_id': str(getattr(channel, 'id', '') or ''),
            'member_count': str(getattr(guild, 'member_count', 0) or 0),
            'winners': str(winners_count),
            'entries': str(entry_count),
            'avatar': avatar_url,
            'server_icon': server_icon_url,
            'server_banner': server_banner_url,
        }

        if ended:
            title = await i18n.t(ctx_or_interaction, 'giveaway.ended_title')
            color = discord.Color.dark_grey()
        else:
            title_template = embed_config.get('title') or extra_data.get('title') or await i18n.t(ctx_or_interaction, 'giveaway.title')
            title = render_giveaway_template(title_template, placeholder_values)
            raw_color = embed_config.get('color', DEFAULT_GIVEAWAY_EMBED_COLOR)
            color = discord.Color(int(raw_color.removeprefix('#'), 16))

        # Clamp rendered text to Discord's limits so legacy records with
        # overlong values cannot fail the send with a 400 from the API.
        title = title[:256]

        description_template = embed_config.get('description')
        description = render_giveaway_template(description_template, placeholder_values) if description_template else f'**{prize}**'
        description = description[:4096]

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )

        thumbnail_url = render_giveaway_template(embed_config.get('thumbnail_url', ''), placeholder_values)
        image_url = render_giveaway_template(embed_config.get('image_url', ''), placeholder_values)
        footer_icon_url = render_giveaway_template(embed_config.get('footer_icon_url', ''), placeholder_values)
        if thumbnail_url.startswith(('http://', 'https://')):
            embed.set_thumbnail(url=thumbnail_url)
        if image_url.startswith(('http://', 'https://')):
            embed.set_image(url=image_url)

        ends_label = await i18n.t(ctx_or_interaction, 'giveaway.ends_at')
        hosted_label = await i18n.t(ctx_or_interaction, 'giveaway.hosted_by')
        winners_label = await i18n.t(ctx_or_interaction, 'giveaway.winners_label')
        entries_label = await i18n.t(ctx_or_interaction, 'giveaway.entries_label')

        if ended:
            mention_chunks = _chunk_winner_mentions(winners)
            if mention_chunks:
                for index, chunk in enumerate(mention_chunks):
                    # Continuation chunks share the label so winners stay grouped.
                    embed.add_field(name=winners_label if index == 0 else '\u200b', value=chunk, inline=False)
            else:
                no_entries_text = await i18n.t(ctx_or_interaction, 'giveaway.no_entries')
                embed.add_field(name=winners_label, value=no_entries_text, inline=False)
            embed.add_field(name=hosted_label, value=f'<@{host_id}>', inline=True)
            embed.add_field(name=entries_label, value=f'{entry_count}', inline=True)
            embed.set_footer(
                text=f'Ended • {entry_count} entries',
                icon_url=footer_icon_url if footer_icon_url.startswith(('http://', 'https://')) else None,
            )
        else:
            embed.add_field(name=ends_label, value=f'<t:{end_timestamp}:R> (<t:{end_timestamp}:f>)', inline=False)
            embed.add_field(name=hosted_label, value=f'<@{host_id}>', inline=True)
            embed.add_field(name=winners_label, value=f'**{winners_count}**', inline=True)
            embed.add_field(name=entries_label, value=f'**{entry_count}**', inline=True)
            footer_text = render_giveaway_template(
                embed_config.get('footer') or f'Ends at • {entry_count} entries',
                placeholder_values,
            )[:2048]
            embed.set_footer(
                text=footer_text,
                icon_url=footer_icon_url if footer_icon_url.startswith(('http://', 'https://')) else None,
            )

        return embed

    async def pick_winners(self, entries: List[int], count: int) -> List[int]:
        """Randomly select unique winners from entries."""
        if not entries:
            return []
        unique_entries = list(set(entries))
        winner_count = min(count, len(unique_entries))
        return random.sample(unique_entries, winner_count)

    async def finalize_giveaway(
        self,
        giveaway: Dict[str, Any],
        *,
        actor_id: Optional[int] = None,
        source: str = 'background',
    ) -> bool:
        """Complete a giveaway, update its message, pick winners, and announce.

        Returns True when this call ended the giveaway. Concurrent finalizers
        (e.g. a manual `end` racing the background loop) lose the atomic end
        transition and return False without announcing a second time.
        """
        message_id = giveaway.get('message_id')
        if message_id is None:
            return False
        channel_id = giveaway.get('channel_id')
        winners_count = giveaway.get('winners_count', 1)
        prize = giveaway.get('prize', 'Prize')
        entries = giveaway.get('entries') or []

        winners = await self.pick_winners(entries, winners_count)
        if not await self.giveaway_model.end_giveaway(message_id, winners):
            # Another finalizer (manual end vs. background loop) won the race.
            return False

        # Re-read so the ended embed reflects the final entry count.
        giveaway = await self.giveaway_model.get_giveaway(message_id) or giveaway

        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        if channel is None and channel_id is not None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None

        message = None
        if channel:
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                message = None

        if message and channel:
            # Edit original message with ended embed and disabled view
            ended_embed = await self.build_giveaway_embed(giveaway, channel, ended=True, winners=winners)
            empty_view = discord.ui.View()
            try:
                await message.edit(embed=ended_embed, view=empty_view)
            except Exception as e:
                logger.error(f'Failed to edit giveaway message on finish: {e}')

            # Send winner announcement message in the channel
            if winners:
                winner_mentions = ', '.join(f'<@{w}>' for w in winners)
                announcement = await i18n.t(
                    channel,
                    'giveaway.ended_winners',
                    winners=winner_mentions,
                    prize=prize,
                )
            else:
                announcement = await i18n.t(channel, 'giveaway.no_entries')

            try:
                await channel.send(
                    announcement,
                    reference=message.to_reference(fail_if_not_exists=False),
                )
            except Exception as e:
                logger.error(f'Failed to send giveaway winner announcement: {e}')

        return True

    @tasks.loop(seconds=15)
    async def check_giveaways(self):
        """Periodic background task to check and end expired giveaways."""
        try:
            expired = await self.giveaway_model.get_expired_active_giveaways()
            for giveaway in expired:
                # Only a bot in the giveaway's guild can announce it; leave the
                # rest for that guild's primary bot instead of ending it silently.
                guild_id = giveaway.get('guild_id')
                if guild_id is None or not is_primary_for_guild(self.bot, int(guild_id)):
                    continue
                try:
                    await self.finalize_giveaway(giveaway)
                except Exception as e:
                    logger.error(f'Error finalizing giveaway {giveaway.get("message_id")}: {e}')
        except Exception as e:
            logger.error(f'Error in check_giveaways task loop: {e}')

    @check_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_group(name='giveaway', invoke_without_command=True)
    async def giveaway_group(self, ctx: commands.Context):
        """List all active giveaways in this server."""
        if ctx.invoked_subcommand is None:
            await self.list_giveaways(ctx)

    @giveaway_group.command(name='start')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        duration='Duration of the giveaway (e.g., 10m, 1h, 1d, 30s)',
        prize='The prize or item being given away',
        winners='Number of winners to pick (default: 1)',
        title='Optional custom title for the giveaway',
    )
    async def start_giveaway(
        self,
        ctx: commands.Context,
        duration: str,
        prize: str,
        winners: int = 1,
        title: Optional[str] = None,
    ):
        """Start a new giveaway in this channel."""
        if ctx.guild is None or ctx.channel is None:
            msg = await i18n.t(ctx, 'giveaway.guild_only')
            await ctx.send(msg)
            return

        td = parse_duration(duration)
        if not td:
            msg = await i18n.t(ctx, 'giveaway.invalid_duration')
            await ctx.send(msg)
            return

        prize = (prize or '').strip()
        if not prize or len(prize) > GIVEAWAY_PRIZE_LIMIT:
            msg = await i18n.t(ctx, 'giveaway.invalid_prize')
            await ctx.send(msg)
            return

        clean_title = (title or '').strip() or None
        if clean_title and len(clean_title) > GIVEAWAY_EMBED_TEXT_LIMITS['title']:
            msg = await i18n.t(ctx, 'giveaway.invalid_title')
            await ctx.send(msg)
            return

        winners_count = max(1, winners)
        end_time = datetime.now(UTC) + td
        extra_data = {}
        if clean_title:
            extra_data['title'] = clean_title

        dummy_giveaway = {
            'prize': prize,
            'host_id': ctx.author.id,
            'winners_count': winners_count,
            'end_time': end_time,
            'entries': [],
            'extra_data': extra_data,
        }

        embed = await self.build_giveaway_embed(dummy_giveaway, ctx)
        view = GiveawayView()

        msg = await ctx.send(embed=embed, view=view)
        view.message_id = msg.id

        try:
            await self.giveaway_model.create_giveaway(
                message_id=msg.id,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                host_id=ctx.author.id,
                prize=prize,
                winners_count=winners_count,
                end_time=end_time,
                extra_data=extra_data,
            )
        except Exception as e:
            logger.error(f'Failed to store giveaway {msg.id}: {e}')
            try:
                await msg.delete()
            except Exception:
                pass
            err = await i18n.t(ctx, 'giveaway.error', error='Could not save the giveaway. Please try again.')
            await ctx.send(err)

    @giveaway_group.command(name='end')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message_id='Message ID of the giveaway to manage',
    )
    async def end_giveaway_cmd(self, ctx: commands.Context, message_id: str):
        """End a giveaway immediately and pick winners."""
        if ctx.guild is None:
            msg = await i18n.t(ctx, 'giveaway.guild_only')
            await ctx.send(msg)
            return

        try:
            mid = int(message_id)
        except ValueError:
            msg = await i18n.t(ctx, 'giveaway.not_found')
            await ctx.send(msg)
            return

        giveaway = await self.giveaway_model.get_giveaway(mid)
        if not giveaway or giveaway.get('guild_id') != ctx.guild.id:
            msg = await i18n.t(ctx, 'giveaway.not_found')
            await ctx.send(msg)
            return

        if giveaway.get('ended', False):
            msg = await i18n.t(ctx, 'giveaway.already_ended')
            await ctx.send(msg)
            return

        ended = await self.finalize_giveaway(giveaway, actor_id=ctx.author.id, source='discord')
        if not ended:
            msg = await i18n.t(ctx, 'giveaway.already_ended')
            await ctx.send(msg)
            return
        msg = await i18n.t(ctx, 'giveaway.end_success', message_id=str(mid))
        await ctx.send(msg)

    @giveaway_group.command(name='reroll')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message_id='Message ID of the giveaway to manage',
        winners='Number of winners to pick (default: 1)',
    )
    async def reroll_giveaway(self, ctx: commands.Context, message_id: str, winners: int = 1):
        """Reroll winners for an ended giveaway."""
        if ctx.guild is None:
            msg = await i18n.t(ctx, 'giveaway.guild_only')
            await ctx.send(msg)
            return

        try:
            mid = int(message_id)
        except ValueError:
            msg = await i18n.t(ctx, 'giveaway.not_found')
            await ctx.send(msg)
            return

        giveaway = await self.giveaway_model.get_giveaway(mid)
        if not giveaway or giveaway.get('guild_id') != ctx.guild.id:
            msg = await i18n.t(ctx, 'giveaway.not_found')
            await ctx.send(msg)
            return

        if not giveaway.get('ended', False):
            msg = await i18n.t(ctx, 'giveaway.not_ended')
            await ctx.send(msg)
            return

        entries = giveaway.get('entries') or []
        if not entries:
            msg = await i18n.t(ctx, 'giveaway.reroll_no_entries')
            await ctx.send(msg)
            return

        winners_count = max(1, winners)
        new_winners = await self.pick_winners(entries, winners_count)
        if not new_winners:
            msg = await i18n.t(ctx, 'giveaway.reroll_no_entries')
            await ctx.send(msg)
            return

        await self.giveaway_model.set_winners(mid, new_winners)

        # Update original message embed and retire its entry button.
        channel_id = giveaway.get('channel_id', ctx.channel.id)
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        if channel is None and channel_id is not None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        if channel:
            try:
                orig_msg = await channel.fetch_message(mid)
                if orig_msg:
                    ended_embed = await self.build_giveaway_embed(giveaway, channel, ended=True, winners=new_winners)
                    await orig_msg.edit(embed=ended_embed, view=discord.ui.View())
            except Exception as e:
                logger.debug(f'Could not update original embed on reroll: {e}')

        winner_mentions = ', '.join(f'<@{w}>' for w in new_winners)
        prize = giveaway.get('prize', 'Prize')
        announcement = await i18n.t(
            ctx,
            'giveaway.reroll_success',
            winners=winner_mentions,
            prize=prize,
        )
        await ctx.send(announcement)

    @giveaway_group.command(name='list')
    async def list_giveaways(self, ctx: commands.Context):
        """List all active giveaways in this server."""
        if ctx.guild is None:
            msg = await i18n.t(ctx, 'giveaway.guild_only')
            await ctx.send(msg)
            return

        active = await self.giveaway_model.get_guild_giveaways(ctx.guild.id, include_ended=False)

        title = await i18n.t(ctx, 'giveaway.list_title', server=ctx.guild.name)
        embed = discord.Embed(title=title, color=discord.Color.blue())

        if not active:
            embed.description = await i18n.t(ctx, 'giveaway.list_empty')
        else:
            lines = []
            for g in active:
                end_timestamp = int(_coerce_end_time(g.get('end_time')).timestamp())

                jump_url = f'https://discord.com/channels/{g.get("guild_id")}/{g.get("channel_id")}/{g.get("message_id")}'
                line = await i18n.t(
                    ctx,
                    'giveaway.list_item',
                    prize=g.get('prize', 'Prize'),
                    winners=g.get('winners_count', 1),
                    end_timestamp=str(end_timestamp),
                    jump_url=jump_url,
                )
                lines.append(line)

            description = '\n'.join(lines)
            if len(description) > GIVEAWAY_LIST_DESCRIPTION_LIMIT:
                description = description[: GIVEAWAY_LIST_DESCRIPTION_LIMIT - 1] + '…'
            embed.description = description

        await ctx.send(embed=embed)

    @giveaway_group.command(name='delete')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message_id='Message ID of the giveaway to manage',
    )
    async def delete_giveaway(self, ctx: commands.Context, message_id: str):
        """Delete a giveaway and remove its message."""
        if ctx.guild is None:
            msg = await i18n.t(ctx, 'giveaway.guild_only')
            await ctx.send(msg)
            return

        try:
            mid = int(message_id)
        except ValueError:
            msg = await i18n.t(ctx, 'giveaway.not_found')
            await ctx.send(msg)
            return

        giveaway = await self.giveaway_model.get_giveaway(mid)
        if not giveaway or giveaway.get('guild_id') != ctx.guild.id:
            msg = await i18n.t(ctx, 'giveaway.not_found')
            await ctx.send(msg)
            return

        channel_id = giveaway.get('channel_id')
        channel = self.bot.get_channel(channel_id) if channel_id is not None else None
        if channel is None and channel_id is not None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        if channel:
            try:
                orig_msg = await channel.fetch_message(mid)
                if orig_msg:
                    await orig_msg.delete()
            except Exception as e:
                logger.debug(f'Could not delete message during giveaway deletion: {e}')

        await self.giveaway_model.delete_giveaway(mid)
        msg = await i18n.t(ctx, 'giveaway.delete_success', message_id=str(mid))
        await ctx.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
