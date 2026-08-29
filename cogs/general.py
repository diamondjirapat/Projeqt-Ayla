import discord
from discord import app_commands
from discord.ext import commands
from database.models import UserModel, GuildModel
from utils.i18n import i18n
import logging
import time
import sys
import os
import subprocess

logger = logging.getLogger(__name__)

class General(commands.Cog):
    """General purpose commands"""
    
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

    @commands.hybrid_command(name='ping')
    async def ping(self, ctx: commands.Context):
        """Checks bot latency (Websocket and API)"""
        websocket_latency = round(self.bot.latency * 1000)

        start_time = time.perf_counter()
        temp_message = await ctx.send(await i18n.t(ctx, 'commands.ping.loading'))
        api_latency = round((time.perf_counter() - start_time) * 1000)

        process_start = time.perf_counter()
        title = await i18n.t(ctx, 'commands.ping.title')
        process_time = round((time.perf_counter() - process_start) * 1000)

        total_time = round((time.perf_counter() - start_time) * 1000)

        embed = discord.Embed(
            title=title,
            color=discord.Color.blue()
        )

        embed.add_field(name=await i18n.t(ctx, 'commands.ping.websocket_latency'), value=f"{websocket_latency}ms")
        embed.add_field(name=await i18n.t(ctx, 'commands.ping.api_latency'), value=f"{api_latency}ms")
        embed.add_field(name=await i18n.t(ctx, 'commands.ping.processing_time'), value=f"{process_time}ms")
        embed.add_field(name=await i18n.t(ctx, 'commands.ping.total_time'), value=f"{total_time}ms")

        await temp_message.edit(content=None, embed=embed)
    
    @commands.hybrid_command(name='botinfo')
    async def info_command(self, ctx: commands.Context):
        """Display bot information"""
        title = await i18n.t(ctx, 'commands.info.title')
        servers_label = await i18n.t(ctx, 'commands.info.servers')
        users_label = await i18n.t(ctx, 'commands.info.users')
        version_label = await i18n.t(ctx, 'commands.info.version')
        commit_label = await i18n.t(ctx, 'commands.info.commit')
        unknown_text = await i18n.t(ctx, 'general.unknown')
        
        try:
            result = subprocess.run(
                ['git', 'log', '-1', '--format=%h (%s)'],
                capture_output=True, text=True, timeout=5
            )
            commit_info = result.stdout.strip() if result.returncode == 0 else unknown_text
        except Exception:
            commit_info = unknown_text
        
        embed = discord.Embed(
            title=title,
            color=discord.Color.blue()
        )
        embed.add_field(name=servers_label, value=len(self.bot.guilds), inline=True)
        embed.add_field(name=users_label, value=len(self.bot.users), inline=True)
        embed.add_field(name=version_label, value=discord.__version__, inline=True)
        embed.add_field(name=commit_label, value=f"`{commit_info}`", inline=False)
        
        await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='userinfo')
    @app_commands.describe(member="The member to view profile for")
    async def user_profile(self, ctx: commands.Context, member: discord.Member = None):
        """Display a user profile from a database"""
        if member is None:
            member = ctx.author
        
        user_data = await self.user_model.get_user(member.id)
        
        title = await i18n.t(ctx, 'commands.profile.title', username=member.display_name)
        registered_label = await i18n.t(ctx, 'commands.profile.registered')
        updated_label = await i18n.t(ctx, 'commands.profile.last_updated')
        unknown_text = await i18n.t(ctx, 'general.unknown')
        
        embed = discord.Embed(
            title=title,
            color=member.color
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        if user_data:
            embed.add_field(name=registered_label, value=user_data.get('created_at', unknown_text), inline=True)
            embed.add_field(name=updated_label, value=user_data.get('updated_at', unknown_text), inline=True)
        else:
            not_found_text = await i18n.t(ctx, 'commands.profile.not_found')
            embed.description = not_found_text
        
        await ctx.send(embed=embed)
    
    @commands.command(name='reload', hidden=True)
    @commands.is_owner()
    async def reload_command(self, ctx: commands.Context, *, cog: str = None):
        """Reload a cog or all cogs (Owner only)"""
        if cog is None:
            reloaded = []
            failed = []
            
            for extension in list(self.bot.extensions.keys()):
                try:
                    await self.bot.reload_extension(extension)
                    reloaded.append(extension)
                    logger.info(f"Reloaded: {extension}")
                except Exception as e:
                    failed.append(f"{extension}: {str(e)}")
                    logger.error(f"Failed to reload {extension}: {e}")
            
            embed = discord.Embed(
                title=await i18n.t(ctx, 'commands.reload.title'),
                color=discord.Color.green() if not failed else discord.Color.orange()
            )
            
            if reloaded:
                embed.add_field(
                    name=await i18n.t(ctx, 'commands.reload.reloaded_count', count=len(reloaded)),
                    value="\n".join([f"`{ext}`" for ext in reloaded]) or await i18n.t(ctx, 'general.unknown'),
                    inline=False
                )
            
            if failed:
                embed.add_field(
                    name=await i18n.t(ctx, 'commands.reload.failed_count', count=len(failed)),
                    value="\n".join([f"`{fail}`" for fail in failed[:5]]) or await i18n.t(ctx, 'general.unknown'),
                    inline=False
                )
            
            await ctx.send(embed=embed)
        else:
            extension_name = f"cogs.{cog}" if not cog.startswith("cogs.") else cog
            
            try:
                await self.bot.reload_extension(extension_name)
                await ctx.send(await i18n.t(ctx, 'commands.reload.success', extension=extension_name))
                logger.info(f"Reloaded: {extension_name}")
            except commands.ExtensionNotLoaded:
                await ctx.send(await i18n.t(ctx, 'commands.reload.not_loaded', extension=extension_name))
            except commands.ExtensionNotFound:
                await ctx.send(await i18n.t(ctx, 'commands.reload.not_found', extension=extension_name))
            except Exception as e:
                await ctx.send(await i18n.t(ctx, 'commands.reload.failed', extension=extension_name, error=str(e)))
                logger.error(f"Failed to reload {extension_name}: {e}")

    @commands.command(name='restart', hidden=True)
    @commands.is_owner()
    async def restart_command(self, ctx: commands.Context):
        """Restart the entire bot (Owner only)"""
        embed = discord.Embed(
            title=await i18n.t(ctx, 'commands.restart.title'),
            description=await i18n.t(ctx, 'commands.restart.description'),
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        logger.info(f"Bot restart requested by {ctx.author} ({ctx.author.id})")

        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @commands.command(name='update', hidden=True)
    @commands.is_owner()
    async def update_command(self, ctx: commands.Context, branch: str = None):
        """Pull latest changes from GitHub (Owner only)"""
        from config import Config
        
        github_url = Config.GITHUB_URL
        if not github_url:
            title = await i18n.t(ctx, 'commands.update.no_url_title')
            description = await i18n.t(ctx, 'commands.update.no_url_description')
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        title = await i18n.t(ctx, 'commands.update.title')
        embed = discord.Embed(
            title=title,
            color=discord.Color.orange()
        )
        status_message = await ctx.send(embed=embed)
        logger.info(f"Bot update requested by {ctx.author} ({ctx.author.id}){f' (branch: {branch})' if branch else ''}")

        try:
            fetch_cmd = ['git', 'fetch', github_url]
            if branch:
                fetch_cmd.append(branch)

            subprocess.run(
                fetch_cmd,
                capture_output=True, text=True, timeout=60
            )

            result = subprocess.run(
                ['git', 'reset', '--hard', 'FETCH_HEAD'],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                output = result.stdout.strip() or "Already up to date."
                title = await i18n.t(ctx, 'commands.update.success_title')
                description = await i18n.t(ctx, 'commands.update.success_description', output=output)
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.green()
                )
            else:
                error_output = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                title = await i18n.t(ctx, 'commands.update.fail_title')
                description = await i18n.t(ctx, 'commands.update.fail_description', error=error_output)
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.red()
                )

        except subprocess.TimeoutExpired:
            title = await i18n.t(ctx, 'commands.update.fail_title')
            description = await i18n.t(ctx, 'commands.update.fail_description', error="Process timed out after 60 seconds")
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.red()
            )
        except Exception as e:
            title = await i18n.t(ctx, 'commands.update.fail_title')
            description = await i18n.t(ctx, 'commands.update.fail_description', error=str(e))
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.red()
            )

        await status_message.edit(embed=embed)

async def setup(bot):
    await bot.add_cog(General(bot))
