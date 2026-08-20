import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, Literal
import logging
import random
import time

from database.models import LevelingModel, GuildModel
from utils.i18n import i18n

logger = logging.getLogger(__name__)


def make_progress_bar(pct: float, length: int = 12) -> str:
    """Create a clean ASCII progress bar."""
    filled_length = int(round(length * pct / 100.0))
    filled_length = max(0, min(length, filled_length))
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"`[{bar}]` {pct:.1f}%"


class LeaderboardPaginatorView(discord.ui.View):
    def __init__(self, cog, guild_id: int, guild_name: str, author_id: int, scope: str = 'server', initial_page: int = 1, locale: str = 'en'):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.author_id = author_id
        self.scope = scope
        self.page = initial_page
        self.total_pages = 1
        self.locale = locale

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                i18n.get_text("commands.leaderboard.author_only", self.locale),
                ephemeral=True
            )
            return False
        return True

    def update_buttons(self):
        self.prev_button.disabled = (self.page <= 1)
        self.next_button.disabled = (self.page >= self.total_pages)
        self.page_button.label = i18n.get_text(
            "commands.leaderboard.page", self.locale,
            page=self.page, pages=self.total_pages
        )
        self.scope_button.label = i18n.get_text(
            "commands.leaderboard.global_button" if self.scope == 'server' else "commands.leaderboard.server_button",
            self.locale
        )

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.primary, custom_id="lb_prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 1:
            self.page -= 1
            await self.refresh_embed(interaction)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, disabled=True, custom_id="lb_page")
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.primary, custom_id="lb_next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages:
            self.page += 1
            await self.refresh_embed(interaction)

    @discord.ui.button(label="🌐 Global", style=discord.ButtonStyle.success, custom_id="lb_scope")
    async def scope_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.scope = 'global' if self.scope == 'server' else 'server'
        self.page = 1
        await self.refresh_embed(interaction)

    @discord.ui.button(label="🔄", style=discord.ButtonStyle.secondary, custom_id="lb_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.refresh_embed(interaction)

    async def refresh_embed(self, interaction: discord.Interaction):
        embed, total_pages = await self.cog.build_leaderboard_embed(self.guild_id, self.guild_name, self.scope, self.page)
        self.total_pages = total_pages
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)


class Leveling(commands.Cog):
    """Leveling system with XP, level progression, and leaderboards."""

    def __init__(self, bot):
        self.bot = bot
        self.level_model = LevelingModel()
        self.guild_model = GuildModel()
        # Cooldown map: (guild_id, user_id) -> timestamp of last awarded XP
        self.cooldowns = {}
        self.cooldown_seconds = 60

    async def cog_before_invoke(self, ctx: commands.Context):
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"{self.__class__.__name__} cog loaded")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not await self.guild_model.is_leveling_enabled(message.guild.id):
            return

        # Check prefix if any, but standard XP triggers on normal messages
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            # Don't award XP for command invocations if desired, or allow them
            pass

        key = (message.guild.id, message.author.id)
        now = time.time()
        last_xp_time = self.cooldowns.get(key, 0)

        if now - last_xp_time < self.cooldown_seconds:
            return

        self.cooldowns[key] = now
        xp_gain = random.randint(15, 25)

        avatar_url = str(message.author.display_avatar.url)
        username = message.author.display_name

        result = await self.level_model.add_xp(
            guild_id=message.guild.id,
            user_id=message.author.id,
            username=username,
            avatar_url=avatar_url,
            xp_amount=xp_gain
        )

        if result.get('leveled_up'):
            new_lvl = result['new_level']
            locale = await i18n.get_guild_locale(message.guild.id) or i18n.default_locale
            config = await self.guild_model.get_level_alert_config(message.guild.id)

            if not config.get('enabled', False):
                return

            server_icon_url = str(message.guild.icon.url) if message.guild.icon else ""
            total_xp_str = f"{result['xp']:,}"

            def replace_placeholders(text: str) -> str:
                if not text:
                    return ""
                return (
                    text.replace('{user}', message.author.display_name)
                    .replace('{username}', message.author.name)
                    .replace('{mention}', message.author.mention)
                    .replace('{member}', message.author.mention)
                    .replace('{level}', str(new_lvl))
                    .replace('{xp}', total_xp_str)
                    .replace('{server}', message.guild.name)
                    .replace('{guild}', message.guild.name)
                    .replace('{channel}', message.channel.name)
                    .replace('{avatar}', avatar_url)
                    .replace('{server_icon}', server_icon_url)
                )

            custom_title = config.get('title')
            title = replace_placeholders(custom_title) if custom_title else i18n.get_text("commands.level_up.title", locale)

            custom_desc = config.get('description')
            if custom_desc:
                description = replace_placeholders(custom_desc)
            else:
                description = i18n.get_text(
                    "commands.level_up.description", locale,
                    member=message.author.mention, level=new_lvl
                )

            raw_color = config.get('color') or '#f1c40f'
            color_int = 0xF1C40F
            if isinstance(raw_color, str):
                cleaned_color = raw_color.lstrip('#')
                try:
                    color_int = int(cleaned_color, 16)
                except ValueError:
                    color_int = 0xF1C40F
            elif isinstance(raw_color, int):
                color_int = raw_color
            embed_color = discord.Color(color_int)

            embed = discord.Embed(
                title=title,
                description=description,
                color=embed_color
            )

            thumb = config.get('thumbnail_url')
            if thumb:
                thumb_url = replace_placeholders(thumb)
                if thumb_url:
                    embed.set_thumbnail(url=thumb_url)
            elif thumb is None:
                embed.set_thumbnail(url=avatar_url)

            img = config.get('image_url')
            if img:
                img_url = replace_placeholders(img)
                if img_url:
                    embed.set_image(url=img_url)

            if config.get('show_xp_field', True):
                embed.add_field(
                    name=i18n.get_text("commands.level_up.total_xp", locale),
                    value=f"{total_xp_str} XP",
                    inline=True
                )

            footer_text = config.get('footer_text')
            footer_url = config.get('footer_url')
            f_text = replace_placeholders(footer_text) if footer_text else i18n.get_text("commands.level_up.footer", locale, server=message.guild.name)
            f_icon = replace_placeholders(footer_url) if footer_url else (server_icon_url or None)
            if f_text:
                embed.set_footer(text=f_text, icon_url=f_icon or None)

            target_channel = message.channel
            bound_channel_id = config.get('channel_id') or await self.guild_model.get_level_channel(message.guild.id)
            if bound_channel_id:
                bound_channel = message.guild.get_channel(bound_channel_id)
                if not bound_channel:
                    try:
                        bound_channel = await self.bot.fetch_channel(bound_channel_id)
                    except Exception:
                        bound_channel = None
                if bound_channel and hasattr(bound_channel, 'send'):
                    target_channel = bound_channel

            try:
                await target_channel.send(embed=embed)
            except discord.Forbidden:
                pass
            except Exception as e:
                logger.error(f"Failed to send level up message: {e}")

    async def build_leaderboard_embed(self, guild_id: int, guild_name: str, scope: str, page: int = 1):
        """Build leaderboard embed and return (embed, total_pages)."""
        limit = 10
        locale = await i18n.get_guild_locale(guild_id) or i18n.default_locale
        if scope == 'global':
            data = await self.level_model.get_global_leaderboard(page=page, limit=limit)
            title = i18n.get_text('commands.leaderboard.title_global', locale)
        else:
            data = await self.level_model.get_guild_leaderboard(guild_id, page=page, limit=limit)
            title = i18n.get_text('commands.leaderboard.title_server', locale, server_name=guild_name)

        entries = data.get('entries', [])
        total_pages = data.get('total_pages', 1)
        total_count = data.get('total', 0)

        embed = discord.Embed(
            title=title,
            color=discord.Color.blurple() if scope == 'server' else discord.Color.teal()
        )

        if not entries:
            embed.description = i18n.get_text('commands.leaderboard.empty_hint', locale)
            return embed, total_pages

        medal_icons = {1: "🥇", 2: "🥈", 3: "🥉"}

        lines = []
        for item in entries:
            rank = item['rank']
            rank_str = medal_icons.get(rank, f"`#{rank}`")
            name = item['username']
            lvl = item['level']
            xp = item['xp']
            msg_cnt = item.get('messages_count', 0)
            p_bar = make_progress_bar(item['progress']['progress_pct'], length=8)

            lines.append(i18n.get_text(
                'commands.leaderboard.entry', locale,
                rank=rank_str, name=name, level=lvl, xp=f"{xp:,}",
                progress=p_bar, messages=msg_cnt
            ))

        embed.description = "\n\n".join(lines)
        embed.set_footer(text=i18n.get_text(
            'commands.leaderboard.footer', locale,
            total=total_count, page=page, pages=total_pages
        ))
        return embed, total_pages

    @commands.hybrid_command(name='rank', aliases=['level', 'lvl'])
    @app_commands.describe(member="The member whose rank you want to inspect")
    async def rank_command(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """View leveling rank card for yourself or another member."""
        target = member or ctx.author
        guild_id = ctx.guild.id if ctx.guild else 0
        guild_name = ctx.guild.name if ctx.guild else "Direct Messages"

        stats = await self.level_model.get_user_stats(guild_id, target.id)

        title = await i18n.t(ctx, 'commands.rank.title', username=target.display_name) or f"📊 {target.display_name}'s Rank Card"
        embed = discord.Embed(
            title=title,
            color=discord.Color.gold() if target == ctx.author else discord.Color.blue()
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        g_rank = stats['guild_rank']
        g_lvl = stats['guild_level']
        g_xp = stats['guild_xp']
        g_msgs = stats['guild_messages']
        g_prog = stats['guild_progress']

        glb_rank = stats['global_rank']
        glb_lvl = stats['global_level']
        glb_xp = stats['global_xp']

        guild_rank_label = await i18n.t(ctx, 'commands.rank.guild_rank')
        guild_level_label = await i18n.t(ctx, 'commands.rank.guild_level')
        guild_xp_label = await i18n.t(ctx, 'commands.rank.guild_xp')
        global_rank_label = await i18n.t(ctx, 'commands.rank.global_rank')
        global_level_label = await i18n.t(ctx, 'commands.rank.global_level')
        global_xp_label = await i18n.t(ctx, 'commands.rank.global_xp')
        messages_label = await i18n.t(ctx, 'commands.rank.messages')
        progress_label = await i18n.t(ctx, 'commands.rank.progress')

        embed.add_field(
            name=f"🏰 {guild_rank_label} ({guild_name})",
            value=f"**{guild_rank_label}:** #{g_rank}\n**{guild_level_label}:** {g_lvl}\n**{guild_xp_label}:** {g_xp:,}\n**{messages_label}:** {g_msgs:,}",
            inline=True
        )

        embed.add_field(
            name=f"🌐 {global_rank_label}",
            value=f"**{global_rank_label}:** #{glb_rank}\n**{global_level_label}:** {glb_lvl}\n**{global_xp_label}:** {glb_xp:,}",
            inline=True
        )

        p_bar = make_progress_bar(g_prog['progress_pct'], length=12)
        next_xp_in_level = g_prog['xp_needed_for_next']
        curr_xp_in_level = g_prog['xp_in_level']

        embed.add_field(
            name=f"⭐ {progress_label}",
            value=f"{p_bar}\n`{curr_xp_in_level:,} / {next_xp_in_level:,} XP` → {guild_level_label} {g_lvl + 1}",
            inline=False
        )

        await ctx.send(embed=embed)

    @commands.hybrid_command(name='leaderboard', aliases=['lb', 'top'])
    @app_commands.describe(scope="Scope of leaderboard (server or global)", page="Page number")
    async def leaderboard_command(self, ctx: commands.Context, scope: Literal['server', 'global'] = 'server', page: int = 1):
        """View server or global leveling leaderboard."""
        guild_id = ctx.guild.id if ctx.guild else 0
        guild_name = ctx.guild.name if ctx.guild else "Global"

        embed, total_pages = await self.build_leaderboard_embed(guild_id, guild_name, scope, page)
        locale = await i18n.get_locale(ctx)

        view = LeaderboardPaginatorView(
            cog=self,
            guild_id=guild_id,
            guild_name=guild_name,
            author_id=ctx.author.id,
            scope=scope,
            initial_page=page,
            locale=locale
        )
        view.total_pages = total_pages
        view.update_buttons()

        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name='serverleaderboard', aliases=['slb'])
    async def server_leaderboard_command(self, ctx: commands.Context, page: int = 1):
        """Quick shortcut to Server Leaderboard."""
        await self.leaderboard_command(ctx, scope='server', page=page)

    @commands.hybrid_command(name='globalleaderboard', aliases=['glb'])
    async def global_leaderboard_command(self, ctx: commands.Context, page: int = 1):
        """Quick shortcut to Global Leaderboard."""
        await self.leaderboard_command(ctx, scope='global', page=page)

    @commands.hybrid_command(name='setxp')
    @commands.has_permissions(administrator=True)
    @app_commands.describe(member="Member to set XP for", xp="Total XP to set")
    async def set_xp_command(self, ctx: commands.Context, member: discord.Member, xp: int):
        """Set XP for a member in this server (Admin only)."""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        avatar_url = str(member.display_avatar.url)
        username = member.display_name

        result = await self.level_model.set_xp(
            guild_id=ctx.guild.id,
            user_id=member.id,
            username=username,
            avatar_url=avatar_url,
            total_xp=xp
        )

        msg = await i18n.t(
            ctx, 'commands.setxp.success',
            member=member.mention,
            xp=f"{result['xp']:,}",
            level=result['level']
        ) or f"✅ Set XP for {member.mention} to **{result['xp']:,}** XP (Level {result['level']})."

        await ctx.send(msg)

    @commands.hybrid_command(name='addxp')
    @commands.has_permissions(administrator=True)
    @app_commands.describe(member="Member to add XP to", amount="XP amount to add")
    async def add_xp_command(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Add XP to a member in this server (Admin only)."""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        avatar_url = str(member.display_avatar.url)
        username = member.display_name

        result = await self.level_model.add_xp(
            guild_id=ctx.guild.id,
            user_id=member.id,
            username=username,
            avatar_url=avatar_url,
            xp_amount=amount
        )

        msg = await i18n.t(
            ctx, 'commands.addxp.success',
            member=member.mention,
            amount=f"{amount:,}",
            xp=f"{result['xp']:,}",
            level=result['new_level']
        ) or f"✅ Added **{amount:,}** XP to {member.mention}. Total: **{result['xp']:,}** XP (Level {result['new_level']})."

        await ctx.send(msg)

    @commands.hybrid_group(name='leveling', aliases=['levelsystem', 'levels'], fallback='info')
    @commands.has_permissions(manage_guild=True)
    async def leveling(self, ctx: commands.Context):
        """View current leveling system settings"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        is_enabled = await self.guild_model.is_leveling_enabled(ctx.guild.id)
        alert_cfg = await self.guild_model.get_level_alert_config(ctx.guild.id)

        title = await i18n.t(ctx, 'commands.leveling.info_title')
        embed = discord.Embed(title=title, color=discord.Color.blue())

        status_label = await i18n.t(ctx, 'commands.leveling.status_label')
        status_val = await i18n.t(ctx, 'commands.leveling.status_enabled') if is_enabled else await i18n.t(ctx, 'commands.leveling.status_disabled')
        embed.add_field(name=status_label, value=status_val, inline=False)

        alerts_label = await i18n.t(ctx, 'commands.leveling.alerts_label')
        alerts_val = await i18n.t(ctx, 'commands.levelchannel.status_enabled') if alert_cfg.get('enabled') else await i18n.t(ctx, 'commands.levelchannel.status_disabled')
        embed.add_field(name=alerts_label, value=alerts_val, inline=False)

        usage_label = await i18n.t(ctx, 'commands.leveling.usage_label')
        usage_text = await i18n.t(ctx, 'commands.leveling.usage_text', prefix=ctx.prefix)
        embed.add_field(name=usage_label, value=usage_text, inline=False)

        await ctx.send(embed=embed)

    @leveling.command(name='enable')
    @commands.has_permissions(manage_guild=True)
    async def leveling_enable(self, ctx: commands.Context):
        """Enable leveling and XP tracking for this server"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        await self.guild_model.set_leveling_enabled(ctx.guild.id, True)
        title = await i18n.t(ctx, 'commands.leveling.enable_success_title')
        desc = await i18n.t(ctx, 'commands.leveling.enable_success_desc')
        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())
        await ctx.send(embed=embed)

    @leveling.command(name='disable')
    @commands.has_permissions(manage_guild=True)
    async def leveling_disable(self, ctx: commands.Context):
        """Disable leveling and XP tracking for this server"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        await self.guild_model.set_leveling_enabled(ctx.guild.id, False)
        title = await i18n.t(ctx, 'commands.leveling.disable_success_title')
        desc = await i18n.t(ctx, 'commands.leveling.disable_success_desc')
        embed = discord.Embed(title=title, description=desc, color=discord.Color.orange())
        await ctx.send(embed=embed)

    @leveling.command(name='toggle')
    @commands.has_permissions(manage_guild=True)
    async def leveling_toggle(self, ctx: commands.Context):
        """Toggle leveling system on/off for this server"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        current = await self.guild_model.is_leveling_enabled(ctx.guild.id)
        new_state = not current
        await self.guild_model.set_leveling_enabled(ctx.guild.id, new_state)
        if new_state:
            title = await i18n.t(ctx, 'commands.leveling.enable_success_title')
            desc = await i18n.t(ctx, 'commands.leveling.enable_success_desc')
            color = discord.Color.green()
        else:
            title = await i18n.t(ctx, 'commands.leveling.disable_success_title')
            desc = await i18n.t(ctx, 'commands.leveling.disable_success_desc')
            color = discord.Color.orange()
        embed = discord.Embed(title=title, description=desc, color=color)
        await ctx.send(embed=embed)

    @commands.hybrid_group(name='levelchannel', aliases=['levelalert', 'levelingchannel'], fallback='info')
    @commands.has_permissions(manage_guild=True)
    async def levelchannel(self, ctx: commands.Context):
        """View current leveling alert settings"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        config = await self.guild_model.get_level_alert_config(ctx.guild.id)
        is_enabled = config.get('enabled', False)
        channel_id = config.get('channel_id')

        title = await i18n.t(ctx, 'commands.levelchannel.info_title')
        embed = discord.Embed(title=title, color=discord.Color.blue())

        status_label = await i18n.t(ctx, 'commands.levelchannel.status_label')
        status_val = await i18n.t(ctx, 'commands.levelchannel.status_enabled') if is_enabled else await i18n.t(ctx, 'commands.levelchannel.status_disabled')
        embed.add_field(name=status_label, value=status_val, inline=False)

        current_label = await i18n.t(ctx, 'commands.levelchannel.current_channel')
        if channel_id:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                embed.add_field(name=current_label, value=channel.mention, inline=False)
            else:
                not_found_text = await i18n.t(ctx, 'commands.levelchannel.channel_not_found')
                embed.add_field(name=current_label, value=not_found_text, inline=False)
        else:
            not_set_text = await i18n.t(ctx, 'commands.levelchannel.not_set')
            embed.add_field(name=current_label, value=not_set_text, inline=False)

        usage_label = await i18n.t(ctx, 'commands.levelchannel.usage_label')
        usage_text = await i18n.t(ctx, 'commands.levelchannel.usage_text', prefix=ctx.prefix)
        embed.add_field(name=usage_label, value=usage_text, inline=False)

        await ctx.send(embed=embed)

    @levelchannel.command(name='enable')
    @commands.has_permissions(manage_guild=True)
    async def levelchannel_enable(self, ctx: commands.Context):
        """Enable level-up alert announcements"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        await self.guild_model.set_level_alert_config(ctx.guild.id, {'enabled': True})
        title = await i18n.t(ctx, 'commands.levelchannel.enable_success_title')
        desc = await i18n.t(ctx, 'commands.levelchannel.enable_success_desc')
        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())
        await ctx.send(embed=embed)

    @levelchannel.command(name='disable')
    @commands.has_permissions(manage_guild=True)
    async def levelchannel_disable(self, ctx: commands.Context):
        """Disable level-up alert announcements"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        await self.guild_model.set_level_alert_config(ctx.guild.id, {'enabled': False})
        title = await i18n.t(ctx, 'commands.levelchannel.disable_success_title')
        desc = await i18n.t(ctx, 'commands.levelchannel.disable_success_desc')
        embed = discord.Embed(title=title, description=desc, color=discord.Color.orange())
        await ctx.send(embed=embed)

    @levelchannel.command(name='toggle')
    @commands.has_permissions(manage_guild=True)
    async def levelchannel_toggle(self, ctx: commands.Context):
        """Toggle level-up alert announcements on/off"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        config = await self.guild_model.get_level_alert_config(ctx.guild.id)
        new_state = not config.get('enabled', False)
        await self.guild_model.set_level_alert_config(ctx.guild.id, {'enabled': new_state})
        if new_state:
            title = await i18n.t(ctx, 'commands.levelchannel.enable_success_title')
            desc = await i18n.t(ctx, 'commands.levelchannel.enable_success_desc')
            color = discord.Color.green()
        else:
            title = await i18n.t(ctx, 'commands.levelchannel.disable_success_title')
            desc = await i18n.t(ctx, 'commands.levelchannel.disable_success_desc')
            color = discord.Color.orange()
        embed = discord.Embed(title=title, description=desc, color=color)
        await ctx.send(embed=embed)

    @levelchannel.command(name='set')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(channel="The text channel to send leveling alerts in")
    async def levelchannel_set(self, ctx: commands.Context, channel: discord.TextChannel):
        """Bind leveling alerts to a specific text channel and enable them"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        await self.guild_model.set_level_alert_config(ctx.guild.id, {'channel_id': channel.id, 'enabled': True})

        title = await i18n.t(ctx, 'commands.levelchannel.set_success_title')
        desc = await i18n.t(ctx, 'commands.levelchannel.set_success_desc', channel=channel.mention)
        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())

        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages or not perms.embed_links:
            warning = await i18n.t(ctx, 'commands.levelchannel.bot_no_permission', channel=channel.mention)
            embed.set_footer(text=warning)

        await ctx.send(embed=embed)

    @levelchannel.command(name='remove', aliases=['reset', 'delete', 'unset'])
    @commands.has_permissions(manage_guild=True)
    async def levelchannel_remove(self, ctx: commands.Context):
        """Remove the bound leveling alert channel"""
        if not ctx.guild:
            await ctx.send(await i18n.t(ctx, 'general.server_only'))
            return

        current = await self.guild_model.get_level_channel(ctx.guild.id)
        if not current:
            not_set_msg = await i18n.t(ctx, 'commands.levelchannel.remove_not_set')
            await ctx.send(not_set_msg)
            return

        await self.guild_model.remove_level_channel(ctx.guild.id)

        title = await i18n.t(ctx, 'commands.levelchannel.remove_success_title')
        desc = await i18n.t(ctx, 'commands.levelchannel.remove_success_desc')
        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())
        await ctx.send(embed=embed)

    @levelchannel.error
    async def levelchannel_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            msg = await i18n.t(ctx, 'commands.levelchannel.no_permission')
            await ctx.send(msg)
        else:
            logger.error(f"Levelchannel command error: {error}")
            await ctx.send(f"❌ An error occurred: {error}")


async def setup(bot):
    await bot.add_cog(Leveling(bot))
