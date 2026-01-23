import discord
from discord import app_commands
from discord.ext import commands
from database.models import UserModel, GuildModel
from utils.i18n import i18n
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class Moderation(commands.Cog):
    """Moderation commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.user_model = UserModel()
        self.guild_model = GuildModel()
    
    async def cog_before_invoke(self, ctx: commands.Context):
        """Automatically defer slash commands to prevent timeout"""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'{self.__class__.__name__} cog loaded')
    
    @commands.hybrid_command(name='kick')
    @commands.has_permissions(kick_members=True)
    @app_commands.describe(
        member="The member to kick",
        reason="Reason for kicking"
    )
    async def kick_member(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Kick a member from the server"""
        if reason is None:
            reason = await i18n.t(ctx, 'general.no_reason')
        
        try:
            await member.kick(reason=reason)
            
            title = await i18n.t(ctx, 'commands.kick.title', static_embed=True)
            description = await i18n.t(ctx, 'commands.kick.description', member=member.mention, static_embed=True)
            reason_label = await i18n.t(ctx, 'commands.kick.reason', static_embed=True)
            moderator_label = await i18n.t(ctx, 'commands.kick.moderator', static_embed=True)
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.orange()
            )
            embed.add_field(name=reason_label, value=reason, inline=False)
            embed.add_field(name=moderator_label, value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=embed)
            logger.info(f"{member} kicked by {ctx.author} for: {reason}")
            
        except discord.Forbidden:
            error_msg = await i18n.t(ctx, 'commands.kick.no_permission')
            await ctx.send(error_msg)
        except Exception as e:
            error_msg = await i18n.t(ctx, 'commands.kick.error', error=str(e))
            await ctx.send(error_msg)
    
    @commands.hybrid_command(name='ban')
    @commands.has_permissions(ban_members=True)
    @app_commands.describe(
        member="The member to ban",
        reason="Reason for banning"
    )
    async def ban_member(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Ban a member from the server"""
        if reason is None:
            reason = await i18n.t(ctx, 'general.no_reason')
        
        try:
            await member.ban(reason=reason)
            
            title = await i18n.t(ctx, 'commands.ban.title', static_embed=True)
            description = await i18n.t(ctx, 'commands.ban.description', member=member.mention, static_embed=True)
            reason_label = await i18n.t(ctx, 'commands.ban.reason', static_embed=True)
            moderator_label = await i18n.t(ctx, 'commands.ban.moderator', static_embed=True)
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.red()
            )
            embed.add_field(name=reason_label, value=reason, inline=False)
            embed.add_field(name=moderator_label, value=ctx.author.mention, inline=True)
            
            await ctx.send(embed=embed)
            logger.info(f"{member} banned by {ctx.author} for: {reason}")
            
        except discord.Forbidden:
            error_msg = await i18n.t(ctx, 'commands.ban.no_permission')
            await ctx.send(error_msg)
        except Exception as e:
            error_msg = await i18n.t(ctx, 'commands.ban.error', error=str(e))
            await ctx.send(error_msg)
    
    @commands.hybrid_command(name='purge')
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(amount="Number of messages to delete (1-100)")
    async def purge_messages(self, ctx: commands.Context, amount: int):
        """Delete multiple messages"""
        if amount < 1 or amount > 100:
            error_msg = await i18n.t(ctx, 'commands.purge.invalid_amount')
            await ctx.send(error_msg)
            return
        
        try:
            deleted = await ctx.channel.purge(limit=amount + 1)
            
            title = await i18n.t(ctx, 'commands.purge.title', static_embed=True)
            description = await i18n.t(ctx, 'commands.purge.description', count=len(deleted) - 1, static_embed=True)
            moderator_label = await i18n.t(ctx, 'commands.purge.moderator', static_embed=True)
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
            embed.add_field(name=moderator_label, value=ctx.author.mention, inline=True)

            await ctx.send(embed=embed, delete_after=5)
            
        except discord.Forbidden:
            error_msg = await i18n.t(ctx, 'commands.purge.no_permission')
            await ctx.send(error_msg)
        except Exception as e:
            error_msg = await i18n.t(ctx, 'commands.purge.error', error=str(e))
            await ctx.send(error_msg)


async def setup(bot):
    await bot.add_cog(Moderation(bot))