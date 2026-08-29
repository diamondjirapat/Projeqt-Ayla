from datetime import datetime, timedelta, UTC
import logging
import random
import re
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.models import GiveawayModel, GuildModel
from utils.i18n import i18n

logger = logging.getLogger(__name__)

DURATION_PATTERN = re.compile(
    r"^((?P<days>\d+?)\s*(?:d|days?))?\s*"
    r"((?P<hours>\d+?)\s*(?:h|hours?|hrs?))?\s*"
    r"((?P<minutes>\d+?)\s*(?:m|mins?|minutes?))?\s*"
    r"((?P<seconds>\d+?)\s*(?:s|secs?|seconds?))?$",
    re.IGNORECASE,
)


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

    td = timedelta(**time_params)
    return td if td.total_seconds() > 0 else None


class GiveawayView(discord.ui.View):
    """Persistent interactive view for entering and leaving a giveaway."""

    def __init__(self, message_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(
        label="🎉 Enter Giveaway",
        style=discord.ButtonStyle.primary,
        custom_id="giveaway_entry_button",
    )
    async def enter_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_msg_id = self.message_id or interaction.message.id
        giveaway_model = GiveawayModel()
        giveaway = await giveaway_model.get_giveaway(target_msg_id)

        if not giveaway:
            msg = await i18n.t(interaction, "giveaway.not_found")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if giveaway.get("ended", False):
            msg = await i18n.t(interaction, "giveaway.already_ended")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        user_id = interaction.user.id
        entries: list = giveaway.get("entries", [])

        if user_id in entries:
            # User is already entered -> toggle/leave
            await giveaway_model.remove_entry(target_msg_id, user_id)
            msg = await i18n.t(interaction, "giveaway.left_success", prize=giveaway.get("prize", "Prize"))
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await giveaway_model.add_entry(target_msg_id, user_id)
            msg = await i18n.t(interaction, "giveaway.entered_success", prize=giveaway.get("prize", "Prize"))
            await interaction.response.send_message(msg, ephemeral=True)

        # Refresh embed entry count in background
        cog = interaction.client.get_cog("Giveaway")
        if cog and interaction.message:
            try:
                updated_giveaway = await giveaway_model.get_giveaway(target_msg_id)
                if updated_giveaway and not updated_giveaway.get("ended", False):
                    embed = await cog.build_giveaway_embed(updated_giveaway, interaction)
                    await interaction.message.edit(embed=embed)
            except Exception as e:
                logger.debug(f"Failed to update giveaway message embed: {e}")


class Giveaway(commands.Cog):
    """Giveaway system with interactive buttons, automated timer, and reroll support."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.giveaway_model = GiveawayModel()
        self.guild_model = GuildModel()
        self.check_giveaways.start()

    def cog_unload(self):
        self.check_giveaways.cancel()

    async def cog_before_invoke(self, ctx: commands.Context):
        """Automatically defer slash commands to prevent timeout."""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"{self.__class__.__name__} cog loaded successfully")

    async def build_giveaway_embed(
        self,
        giveaway: Dict[str, Any],
        ctx_or_interaction: Any,
        ended: bool = False,
        winners: Optional[List[int]] = None,
    ) -> discord.Embed:
        """Construct the giveaway embed for active and ended states."""
        prize = giveaway.get("prize", "Prize")
        host_id = giveaway.get("host_id", 0)
        winners_count = giveaway.get("winners_count", 1)
        end_time: datetime = giveaway.get("end_time")
        entries = giveaway.get("entries", [])
        entry_count = len(entries)

        if end_time.tzinfo is None:
            end_timestamp = int(end_time.replace(tzinfo=UTC).timestamp())
        else:
            end_timestamp = int(end_time.timestamp())

        if ended:
            title = await i18n.t(ctx_or_interaction, "giveaway.ended_title")
            color = discord.Color.dark_grey()
        else:
            title = giveaway.get("extra_data", {}).get("title") or await i18n.t(
                ctx_or_interaction, "giveaway.title"
            )
            color = discord.Color.gold()

        embed = discord.Embed(
            title=title,
            description=f"**{prize}**",
            color=color,
        )

        ends_label = await i18n.t(ctx_or_interaction, "giveaway.ends_at")
        hosted_label = await i18n.t(ctx_or_interaction, "giveaway.hosted_by")
        winners_label = await i18n.t(ctx_or_interaction, "giveaway.winners_label")
        entries_label = await i18n.t(ctx_or_interaction, "giveaway.entries_label")

        if ended:
            if winners:
                winner_mentions = ", ".join(f"<@{w}>" for w in winners)
                embed.add_field(name=winners_label, value=winner_mentions, inline=False)
            else:
                no_entries_text = await i18n.t(ctx_or_interaction, "giveaway.no_entries")
                embed.add_field(name=winners_label, value=no_entries_text, inline=False)
            embed.add_field(name=hosted_label, value=f"<@{host_id}>", inline=True)
            embed.add_field(name=entries_label, value=f"{entry_count}", inline=True)
            embed.set_footer(text=f"Ended • {entry_count} entries")
        else:
            embed.add_field(name=ends_label, value=f"<t:{end_timestamp}:R> (<t:{end_timestamp}:f>)", inline=False)
            embed.add_field(name=hosted_label, value=f"<@{host_id}>", inline=True)
            embed.add_field(name=winners_label, value=f"**{winners_count}**", inline=True)
            embed.add_field(name=entries_label, value=f"**{entry_count}**", inline=True)
            embed.set_footer(text=f"Ends at • {entry_count} entries")

        return embed

    async def pick_winners(self, entries: List[int], count: int) -> List[int]:
        """Randomly select unique winners from entries."""
        if not entries:
            return []
        unique_entries = list(set(entries))
        winner_count = min(count, len(unique_entries))
        return random.sample(unique_entries, winner_count)

    async def finalize_giveaway(self, giveaway: Dict[str, Any]):
        """Complete a giveaway, update its message, pick winners, and announce."""
        message_id = giveaway["message_id"]
        channel_id = giveaway["channel_id"]
        winners_count = giveaway.get("winners_count", 1)
        prize = giveaway.get("prize", "Prize")
        entries = giveaway.get("entries", [])

        channel = self.bot.get_channel(channel_id)
        if channel is None:
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

        winners = await self.pick_winners(entries, winners_count)
        await self.giveaway_model.end_giveaway(message_id, winners)

        if message and channel:
            # Edit original message with ended embed and disabled view
            ended_embed = await self.build_giveaway_embed(giveaway, channel, ended=True, winners=winners)
            empty_view = discord.ui.View()
            try:
                await message.edit(embed=ended_embed, view=empty_view)
            except Exception as e:
                logger.error(f"Failed to edit giveaway message on finish: {e}")

            # Send winner announcement message in the channel
            if winners:
                winner_mentions = ", ".join(f"<@{w}>" for w in winners)
                announcement = await i18n.t(
                    channel,
                    "giveaway.ended_winners",
                    winners=winner_mentions,
                    prize=prize,
                )
            else:
                announcement = await i18n.t(channel, "giveaway.no_entries")

            try:
                await channel.send(
                    announcement,
                    reference=message.to_reference(fail_if_not_exists=False),
                )
            except Exception as e:
                logger.error(f"Failed to send giveaway winner announcement: {e}")

    @tasks.loop(seconds=15)
    async def check_giveaways(self):
        """Periodic background task to check and end expired giveaways."""
        try:
            expired = await self.giveaway_model.get_expired_active_giveaways()
            for giveaway in expired:
                try:
                    await self.finalize_giveaway(giveaway)
                except Exception as e:
                    logger.error(f"Error finalizing giveaway {giveaway.get('message_id')}: {e}")
        except Exception as e:
            logger.error(f"Error in check_giveaways task loop: {e}")

    @check_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_group(name="giveaway", invoke_without_command=True)
    async def giveaway_group(self, ctx: commands.Context):
        """List all active giveaways in this server."""
        if ctx.invoked_subcommand is None:
            await self.list_giveaways(ctx)

    @giveaway_group.command(name="start")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        duration="Duration of the giveaway (e.g., 10m, 1h, 1d, 30s)",
        prize="The prize or item being given away",
        winners="Number of winners to pick (default: 1)",
        title="Optional custom title for the giveaway",
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
        td = parse_duration(duration)
        if not td:
            msg = await i18n.t(ctx, "giveaway.invalid_duration")
            await ctx.send(msg)
            return

        winners_count = max(1, winners)
        end_time = datetime.now(UTC) + td
        extra_data = {}
        if title:
            extra_data["title"] = title

        dummy_giveaway = {
            "prize": prize,
            "host_id": ctx.author.id,
            "winners_count": winners_count,
            "end_time": end_time,
            "entries": [],
            "extra_data": extra_data,
        }

        embed = await self.build_giveaway_embed(dummy_giveaway, ctx)
        view = GiveawayView()

        msg = await ctx.send(embed=embed, view=view)

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

    @giveaway_group.command(name="end")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message_id="Message ID of the giveaway to manage",
    )
    async def end_giveaway_cmd(self, ctx: commands.Context, message_id: str):
        """End a giveaway immediately and pick winners."""
        try:
            mid = int(message_id)
        except ValueError:
            msg = await i18n.t(ctx, "giveaway.not_found")
            await ctx.send(msg)
            return

        giveaway = await self.giveaway_model.get_giveaway(mid)
        if not giveaway or giveaway.get("guild_id") != ctx.guild.id:
            msg = await i18n.t(ctx, "giveaway.not_found")
            await ctx.send(msg)
            return

        if giveaway.get("ended", False):
            msg = await i18n.t(ctx, "giveaway.already_ended")
            await ctx.send(msg)
            return

        await self.finalize_giveaway(giveaway)
        msg = await i18n.t(ctx, "giveaway.end_success", message_id=str(mid))
        await ctx.send(msg)

    @giveaway_group.command(name="reroll")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message_id="Message ID of the giveaway to manage",
        winners="Number of winners to pick (default: 1)",
    )
    async def reroll_giveaway(self, ctx: commands.Context, message_id: str, winners: int = 1):
        """Reroll winners for an ended giveaway."""
        try:
            mid = int(message_id)
        except ValueError:
            msg = await i18n.t(ctx, "giveaway.not_found")
            await ctx.send(msg)
            return

        giveaway = await self.giveaway_model.get_giveaway(mid)
        if not giveaway or giveaway.get("guild_id") != ctx.guild.id:
            msg = await i18n.t(ctx, "giveaway.not_found")
            await ctx.send(msg)
            return

        entries = giveaway.get("entries", [])
        if not entries:
            msg = await i18n.t(ctx, "giveaway.reroll_no_entries")
            await ctx.send(msg)
            return

        winners_count = max(1, winners)
        new_winners = await self.pick_winners(entries, winners_count)
        if not new_winners:
            msg = await i18n.t(ctx, "giveaway.reroll_no_entries")
            await ctx.send(msg)
            return

        await self.giveaway_model.set_winners(mid, new_winners)

        # Update original message embed
        channel_id = giveaway.get("channel_id", ctx.channel.id)
        channel = self.bot.get_channel(channel_id)
        if channel:
            try:
                orig_msg = await channel.fetch_message(mid)
                if orig_msg:
                    ended_embed = await self.build_giveaway_embed(
                        giveaway, channel, ended=True, winners=new_winners
                    )
                    await orig_msg.edit(embed=ended_embed)
            except Exception as e:
                logger.debug(f"Could not update original embed on reroll: {e}")

        winner_mentions = ", ".join(f"<@{w}>" for w in new_winners)
        prize = giveaway.get("prize", "Prize")
        announcement = await i18n.t(
            ctx,
            "giveaway.reroll_success",
            winners=winner_mentions,
            prize=prize,
        )
        await ctx.send(announcement)

    @giveaway_group.command(name="list")
    async def list_giveaways(self, ctx: commands.Context):
        """List all active giveaways in this server."""
        active = await self.giveaway_model.get_guild_giveaways(ctx.guild.id, include_ended=False)

        title = await i18n.t(ctx, "giveaway.list_title", server=ctx.guild.name)
        embed = discord.Embed(title=title, color=discord.Color.blue())

        if not active:
            embed.description = await i18n.t(ctx, "giveaway.list_empty")
        else:
            lines = []
            for g in active:
                end_time = g.get("end_time")
                if end_time.tzinfo is None:
                    end_timestamp = int(end_time.replace(tzinfo=UTC).timestamp())
                else:
                    end_timestamp = int(end_time.timestamp())

                jump_url = f"https://discord.com/channels/{g.get('guild_id')}/{g.get('channel_id')}/{g.get('message_id')}"
                line = await i18n.t(
                    ctx,
                    "giveaway.list_item",
                    prize=g.get("prize", "Prize"),
                    winners=g.get("winners_count", 1),
                    end_timestamp=str(end_timestamp),
                    jump_url=jump_url,
                )
                lines.append(line)

            embed.description = "\n".join(lines)

        await ctx.send(embed=embed)

    @giveaway_group.command(name="delete")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        message_id="Message ID of the giveaway to manage",
    )
    async def delete_giveaway(self, ctx: commands.Context, message_id: str):
        """Delete a giveaway and remove its message."""
        try:
            mid = int(message_id)
        except ValueError:
            msg = await i18n.t(ctx, "giveaway.not_found")
            await ctx.send(msg)
            return

        giveaway = await self.giveaway_model.get_giveaway(mid)
        if not giveaway or giveaway.get("guild_id") != ctx.guild.id:
            msg = await i18n.t(ctx, "giveaway.not_found")
            await ctx.send(msg)
            return

        channel_id = giveaway.get("channel_id")
        channel = self.bot.get_channel(channel_id)
        if channel:
            try:
                orig_msg = await channel.fetch_message(mid)
                if orig_msg:
                    await orig_msg.delete()
            except Exception as e:
                logger.debug(f"Could not delete message during giveaway deletion: {e}")

        await self.giveaway_model.delete_giveaway(mid)
        msg = await i18n.t(ctx, "giveaway.delete_success", message_id=str(mid))
        await ctx.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
