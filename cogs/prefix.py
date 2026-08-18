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

    @commands.hybrid_group(name='prefix', invoke_without_command=True)
    async def prefix(self, ctx):
        """Show current prefix information"""
        prefix_info = await prefix_manager.get_prefix_info(
            ctx.author.id, 
            ctx.guild.id if ctx.guild else None
        )
        
        title = await i18n.t(ctx, "prefix.info_title")
        current_prefix_name = await i18n.t(ctx, "prefix.current_prefix")
        personal_prefix_name = await i18n.t(ctx, "prefix.personal_prefix")
        server_prefix_name = await i18n.t(ctx, "prefix.server_prefix")
        default_prefix_name = await i18n.t(ctx, "prefix.default_prefix")
        not_set = await i18n.t(ctx, "prefix.not_set")
        how_it_works = await i18n.t(ctx, "prefix.how_it_works")
        priority_text = await i18n.t(ctx, "prefix.priority_explanation", mention=self.bot.user.mention)
        commands_label = await i18n.t(ctx, "prefix.commands")
        commands_help = await i18n.t(ctx, "prefix.commands_help")
        source = await i18n.t(ctx, f"prefix.source_{prefix_info['priority']}")
        effective_prefix = await i18n.t(
            ctx, "prefix.effective_source",
            prefix=f"`{prefix_info['effective_prefix']}`", source=source
        )

        embed = discord.Embed(
            title=title,
            color=discord.Color.blue()
        )

        embed.add_field(
            name=current_prefix_name,
            value=effective_prefix,
            inline=False
        )

        if prefix_info['user_prefix']:
            embed.add_field(
                name=personal_prefix_name,
                value=f"`{prefix_info['user_prefix']}` ✅",
                inline=True
            )
        else:
            embed.add_field(
                name=personal_prefix_name,
                value=not_set,
                inline=True
            )

        if ctx.guild:
            if prefix_info['guild_prefix']:
                embed.add_field(
                    name=server_prefix_name,
                    value=f"`{prefix_info['guild_prefix']}` ✅",
                    inline=True
                )
            else:
                embed.add_field(
                    name=server_prefix_name,
                    value=not_set,
                    inline=True
                )

        embed.add_field(
            name=default_prefix_name,
            value=f"`{prefix_info['default_prefix']}`",
            inline=True
        )

        embed.add_field(
            name=how_it_works,
            value=priority_text,
            inline=False
        )
        
        embed.add_field(
            name=commands_label,
            value=commands_help,
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @prefix.command(name='set')
    async def set_user_prefix(self, ctx, *, prefix: str):
        """Set your personal prefix"""
        success, reason = await prefix_manager.set_user_prefix(ctx.author.id, prefix)
        
        if success:
            title = await i18n.t(ctx, "prefix.set_success_title")
            description = await i18n.t(ctx, "prefix.set_success", prefix=prefix)
            example_usage = await i18n.t(ctx, "prefix.example_usage")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
            embed.add_field(
                name=example_usage,
                value=f"`{prefix}help` | `{prefix}play music`",
                inline=False
            )
        else:
            title = await i18n.t(ctx, "prefix.set_failed_title")
            description = await i18n.t(
                ctx, f"prefix.validation.{reason}",
                max=prefix_manager.max_prefix_length, prefix=prefix
            ) if reason else await i18n.t(ctx, "prefix.set_failed")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.red()
            )
        
        await ctx.send(embed=embed)
        
    
    @prefix.command(name='remove', aliases=['reset', 'delete'])
    async def remove_user_prefix(self, ctx):
        """Remove your personal prefix"""
        success = await prefix_manager.remove_user_prefix(ctx.author.id)
        
        if success:
            prefix_info = await prefix_manager.get_prefix_info(
                ctx.author.id, 
                ctx.guild.id if ctx.guild else None
            )
            
            title = await i18n.t(ctx, "prefix.remove_success_title")
            description = await i18n.t(ctx, "prefix.remove_success", prefix=prefix_info['effective_prefix'])
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
        else:
            title = await i18n.t(ctx, "prefix.remove_failed_title")
            description = await i18n.t(ctx, "prefix.remove_failed")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.orange()
            )
        
        await ctx.send(embed=embed)
    

    @commands.hybrid_group(name='serverprefix', invoke_without_command=True)
    async def serverprefix(self, ctx):
        """Server prefix commands"""
        pass
    
    @serverprefix.command(name='set')
    @commands.has_permissions(manage_guild=True)
    async def set_serverprefix(self, ctx, *, prefix: str):
        """Set server default prefix"""
        success, reason = await prefix_manager.set_guild_prefix(ctx.guild.id, prefix)
        
        if success:
            title = await i18n.t(ctx, "prefix.server_set_success_title")
            description = await i18n.t(ctx, "prefix.server_set_success", prefix=prefix)
            note_name = await i18n.t(ctx, "prefix.note")
            note_value = await i18n.t(ctx, "prefix.personal_prefix_note")
            example_usage = await i18n.t(ctx, "prefix.example_usage")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
            embed.add_field(
                name=note_name,
                value=note_value,
                inline=False
            )
            embed.add_field(
                name=example_usage,
                value=f"`{prefix}help` or `{prefix}play music`",
                inline=False
            )
        else:
            title = await i18n.t(ctx, "prefix.server_set_failed_title")
            description = await i18n.t(
                ctx, f"prefix.validation.{reason}",
                max=prefix_manager.max_prefix_length, prefix=prefix
            ) if reason else await i18n.t(ctx, "prefix.server_set_failed")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.red()
            )
        
        await ctx.send(embed=embed)
    
    @serverprefix.command(name='remove', aliases=['reset', 'delete'])
    @commands.has_permissions(manage_guild=True)
    async def reset_serverprefix(self, ctx):
        """Reset server prefix to default"""
        success = await prefix_manager.remove_guild_prefix(ctx.guild.id)
        
        if success:
            title = await i18n.t(ctx, "prefix.server_reset_success_title")
            description = await i18n.t(ctx, "prefix.server_reset_success")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
        else:
            title = await i18n.t(ctx, "prefix.server_reset_failed_title")
            description = await i18n.t(ctx, "prefix.server_reset_failed")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.orange()
            )
        
        await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='myprefix')
    async def my_prefix(self, ctx):
        """Quick command to show your current prefix"""
        effective_prefix = await prefix_manager.get_effective_prefix(
            ctx.author.id, 
            ctx.guild.id if ctx.guild else None
        )
        
        title = await i18n.t(ctx, "prefix.your_current_prefix_title", prefix=effective_prefix)
        embed = discord.Embed(
            title=title,
            color=discord.Color.blue()
        )
        example_name = await i18n.t(ctx, "prefix.example_usage")
        embed.add_field(
            name=example_name,
            value=f"`{effective_prefix}help` | `{effective_prefix}play music`",
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
            
            hello_title = await i18n.t(message, "prefix.hello", username=message.author.display_name)
            my_prefix_desc = await i18n.t(message, "prefix.my_prefix_here", prefix=effective_prefix)
            quick_start_name = await i18n.t(message, "prefix.quick_start")
            help_command_value = await i18n.t(message, "prefix.help_command", prefix=effective_prefix)
            mention_alt_value = await i18n.t(message, "prefix.mention_alternative", mention=self.bot.user.mention)
            mention_alt_name = await i18n.t(message, "prefix.mention_alternative_title")

            embed = discord.Embed(
                title=hello_title,
                description=my_prefix_desc,
                color=discord.Color.blue()
            )
            embed.add_field(
                name=quick_start_name,
                value=help_command_value,
                inline=False
            )
            embed.add_field(
                name=mention_alt_name,
                value=mention_alt_value,
                inline=False
            )
            
            await message.channel.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Prefix(bot))
