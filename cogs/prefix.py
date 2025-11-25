import discord
from discord.ext import commands
from utils.prefix_manager import prefix_manager
from utils.i18n import i18n
import logging

logger = logging.getLogger(__name__)

class Prefix(commands.Cog):
    """Prefix management commands"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'{self.__class__.__name__} cog loaded')
    
    @commands.group(name='prefix', invoke_without_command=True)
    async def prefix_group(self, ctx):
        """Show current prefix information"""
        prefix_info = await prefix_manager.get_prefix_info(
            ctx.author.id, 
            ctx.guild.id if ctx.guild else None
        )
        
        title = await i18n.t(ctx, "prefix.info_title")
        embed = discord.Embed(
            title=title,
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Current Prefix",
            value=f"`{prefix_info['effective_prefix']}` ({prefix_info['priority']} priority)",
            inline=False
        )

        if prefix_info['user_prefix']:
            embed.add_field(
                name="Your Personal Prefix",
                value=f"`{prefix_info['user_prefix']}` ✅",
                inline=True
            )
        else:
            embed.add_field(
                name="Your Personal Prefix",
                value="Not set",
                inline=True
            )

        if ctx.guild:
            if prefix_info['guild_prefix']:
                embed.add_field(
                    name="Server Prefix",
                    value=f"`{prefix_info['guild_prefix']}` ✅",
                    inline=True
                )
            else:
                embed.add_field(
                    name="Server Prefix",
                    value="Using default",
                    inline=True
                )

        embed.add_field(
            name="Default Prefix",
            value=f"`{prefix_info['default_prefix']}`",
            inline=True
        )

        priority_text = (
            "**Priority Order:**\n"
            "1. Personal prefix (highest)\n"
            "2. Server prefix\n"
            "3. Default prefix\n"
            f"4. Mention: {self.bot.user.mention}"
        )
        embed.add_field(
            name="How It Works",
            value=priority_text,
            inline=False
        )
        
        # Commands help
        commands_help = (
            "`prefix set <prefix>` - Set your personal prefix\n"
            "`prefix remove` - Remove your personal prefix\n"
            "`prefix server <prefix>` - Set server prefix (Admin)\n"
            "`prefix server reset` - Reset server prefix (Admin)"
        )
        embed.add_field(
            name="Commands",
            value=commands_help,
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @prefix_group.command(name='set')
    async def set_user_prefix(self, ctx, *, prefix: str):
        """Set your personal prefix"""
        success, message = await prefix_manager.set_user_prefix(ctx.author.id, prefix)
        
        if success:
            title = await i18n.t(ctx, "prefix.set_success_title")
            example_usage = await i18n.t(ctx, "prefix.example_usage")
            embed = discord.Embed(
                title=title,
                description=message,
                color=discord.Color.green()
            )
            embed.add_field(
                name=example_usage,
                value=f"`{prefix}help` or `{prefix}play music`",
                inline=False
            )
        else:
            title = await i18n.t(ctx, "prefix.set_failed_title")
            embed = discord.Embed(
                title=title,
                description=message,
                color=discord.Color.red()
            )
        
        await ctx.send(embed=embed)
    
    @prefix_group.command(name='remove', aliases=['reset', 'delete'])
    async def remove_user_prefix(self, ctx):
        """Remove your personal prefix"""
        success, message = await prefix_manager.remove_user_prefix(ctx.author.id)
        
        if success:
            prefix_info = await prefix_manager.get_prefix_info(
                ctx.author.id, 
                ctx.guild.id if ctx.guild else None
            )
            
            title = await i18n.t(ctx, "prefix.remove_success_title")
            embed = discord.Embed(
                title=title,
                description=f"{message}\nNow using: `{prefix_info['effective_prefix']}`",
                color=discord.Color.green()
            )
        else:
            title = await i18n.t(ctx, "prefix.remove_failed_title")
            embed = discord.Embed(
                title=title,
                description=message,
                color=discord.Color.orange()
            )
        
        await ctx.send(embed=embed)
    
    @prefix_group.group(name='server', invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def server_prefix_group(self, ctx, *, prefix: str):
        """Set server default prefix"""
        success, message = await prefix_manager.set_guild_prefix(ctx.guild.id, prefix)
        
        if success:
            title = await i18n.t(ctx, "prefix.server_set_success_title")
            embed = discord.Embed(
                title=title,
                description=message,
                color=discord.Color.green()
            )
            embed.add_field(
                name="Note",
                value="Users with personal prefixes will still use their own prefix.",
                inline=False
            )
            embed.add_field(
                name="Example Usage",
                value=f"`{prefix}help` or `{prefix}play music`",
                inline=False
            )
        else:
            title = await i18n.t(ctx, "prefix.server_set_failed_title")
            embed = discord.Embed(
                title=title,
                description=message,
                color=discord.Color.red()
            )
        
        await ctx.send(embed=embed)
    
    @server_prefix_group.command(name='reset', aliases=['remove', 'delete'])
    @commands.has_permissions(manage_guild=True)
    async def reset_server_prefix(self, ctx):
        """Reset server prefix to default"""
        success, message = await prefix_manager.remove_guild_prefix(ctx.guild.id)
        
        if success:
            title = await i18n.t(ctx, "prefix.server_reset_success_title")
            embed = discord.Embed(
                title=title,
                description=message,
                color=discord.Color.green()
            )
        else:
            title = await i18n.t(ctx, "prefix.server_reset_failed_title")
            embed = discord.Embed(
                title=title,
                description=message,
                color=discord.Color.orange()
            )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='myprefix')
    async def my_prefix(self, ctx):
        """Quick command to show your current prefix"""
        effective_prefix = await prefix_manager.get_effective_prefix(
            ctx.author.id, 
            ctx.guild.id if ctx.guild else None
        )
        
        title = await i18n.t(ctx, "prefix.your_current_prefix_title")
        embed = discord.Embed(
            title=title,
            description=f"`{effective_prefix}`",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Example",
            value=f"`{effective_prefix}help`",
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Handle bot mentions help"""
        if message.author.bot:
            return

        if message.content.strip() in [f'<@{self.bot.user.id}>', f'<@!{self.bot.user.id}>']:
            effective_prefix = await prefix_manager.get_effective_prefix(
                message.author.id,
                message.guild.id if message.guild else None
            )
            
            embed = discord.Embed(
                title=f"👋 Hello {message.author.display_name}!",
                description=f"My prefix here is `{effective_prefix}`",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Quick Start",
                value=f"`{effective_prefix}help` - Show all commands\n`{effective_prefix}prefix` - Manage prefixes",
                inline=False
            )
            embed.add_field(
                name="Mention Alternative",
                value=f"You can also use {self.bot.user.mention} as a prefix!",
                inline=False
            )
            
            await message.channel.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Prefix(bot))
