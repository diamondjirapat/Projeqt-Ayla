import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, Literal
import logging
import random
import time

from database.models import LevelingModel
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
            embed = discord.Embed(
                title=i18n.get_text("commands.level_up.title", locale),
                description=i18n.get_text(
                    "commands.level_up.description", locale,
                    member=message.author.mention, level=new_lvl
                ),
                color=discord.Color.gold()
            )
            embed.set_thumbnail(url=avatar_url)
            embed.add_field(name=i18n.get_text("commands.level_up.total_xp", locale), value=f"{result['xp']:,} XP", inline=True)
            embed.set_footer(
                text=i18n.get_text("commands.level_up.footer", locale, server=message.guild.name),
                icon_url=message.guild.icon.url if message.guild.icon else None
            )

            try:
                await message.channel.send(embed=embed)
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


async def setup(bot):
    await bot.add_cog(Leveling(bot))
