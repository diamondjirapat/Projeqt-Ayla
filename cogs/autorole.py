import discord
from discord.ext import commands
from discord import app_commands
from database.models import GuildModel
from utils.i18n import i18n
import logging


logger = logging.getLogger(__name__)

class AutoRole(commands.Cog):
    """Auto-assign roles to new members"""
    
    def __init__(self, bot):
        self.bot = bot
        self.guild_model = GuildModel()
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'{self.__class__.__name__} cog loaded')
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Automatically assign a role when a new member joins"""
        if member.bot:
            return
        
        guild_data = await self.guild_model.get_guild(member.guild.id)
        if not guild_data or 'auto_role_id' not in guild_data:
            return
        
        role_id = guild_data['auto_role_id']
        role = member.guild.get_role(role_id)
        
        if not role:
            logger.warning(f"Auto-role {role_id} not found in guild {member.guild.id}")
            return
        
        try:
            await member.add_roles(role, reason="Auto-role on join")
            logger.info(f"Assigned auto-role {role.name} to {member} in {member.guild.name}")
        except discord.Forbidden:
            logger.error(f"Missing permissions to assign auto-role in {member.guild.name}")
        except Exception as e:
            logger.error(f"Error assigning auto-role: {e}")
    
    @commands.hybrid_group(name='autorole', fallback='info')
    @commands.has_permissions(manage_roles=True)
    async def autorole(self, ctx: commands.Context):
        """View current auto-role settings"""
        guild_data = await self.guild_model.get_guild(ctx.guild.id)
        
        title = await i18n.t(ctx, 'autorole.info.title')
        
        embed = discord.Embed(
            title=title,
            color=discord.Color.blue()
        )
        
        if guild_data and 'auto_role_id' in guild_data:
            role = ctx.guild.get_role(guild_data['auto_role_id'])
            if role:
                current_role_label = await i18n.t(ctx, 'autorole.info.current_role')
                embed.add_field(name=current_role_label, value=role.mention, inline=False)
            else:
                not_found_text = await i18n.t(ctx, 'autorole.info.role_not_found')
                embed.description = not_found_text
        else:
            not_set_text = await i18n.t(ctx, 'autorole.info.not_set')
            embed.description = not_set_text
        
        usage_label = await i18n.t(ctx, 'autorole.info.usage_label')
        usage_text = await i18n.t(ctx, 'autorole.info.usage_text', prefix=ctx.prefix)
        embed.add_field(name=usage_label, value=usage_text, inline=False)
        
        await ctx.send(embed=embed)
    
    @autorole.command(name='set')
    @commands.has_permissions(manage_roles=True)
    @app_commands.describe(role="The role to auto-assign to new members")
    async def autorole_set(self, ctx: commands.Context, role: discord.Role):
        """Set the auto-role for new members"""
        if role >= ctx.guild.me.top_role:
            error_msg = await i18n.t(ctx, 'autorole.set.role_too_high')
            await ctx.send(error_msg)
            return
        
        if role.managed:
            error_msg = await i18n.t(ctx, 'autorole.set.role_managed')
            await ctx.send(error_msg)
            return
        
        guild_data = await self.guild_model.get_guild(ctx.guild.id)
        
        if not guild_data:
            await self.guild_model.create_guild(ctx.guild.id, ctx.guild.name, auto_role_id=role.id)
        else:
            await self.guild_model.update_guild(ctx.guild.id, {'auto_role_id': role.id})
        
        title = await i18n.t(ctx, 'autorole.set.success_title')
        description = await i18n.t(ctx, 'autorole.set.success_description', role=role.mention)
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
        logger.info(f"Auto-role set to {role.name} in {ctx.guild.name}")
    
    @autorole.command(name='remove')
    @commands.has_permissions(manage_roles=True)
    async def autorole_remove(self, ctx: commands.Context):
        """Remove the auto-role"""
        guild_data = await self.guild_model.get_guild(ctx.guild.id)
        
        if not guild_data or 'auto_role_id' not in guild_data:
            error_msg = await i18n.t(ctx, 'autorole.remove.not_set')
            await ctx.send(error_msg)
            return
        
        await self.guild_model.update_guild(ctx.guild.id, {'auto_role_id': None})
        
        title = await i18n.t(ctx, 'autorole.remove.success_title')
        description = await i18n.t(ctx, 'autorole.remove.success_description')
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
        logger.info(f"Auto-role removed in {ctx.guild.name}")
    
    @autorole.error
    async def autorole_error(self, ctx: commands.Context, error):
        """Error handler for autorole commands"""
        if isinstance(error, commands.MissingPermissions):
            error_msg = await i18n.t(ctx, 'autorole.errors.no_permission')
            await ctx.send(error_msg)
        else:
            error_msg = await i18n.t(ctx, 'autorole.errors.command_error', error=str(error))
            await ctx.send(error_msg)
            logger.error(f"Auto-role command error: {error}")

async def setup(bot):
    await bot.add_cog(AutoRole(bot))
