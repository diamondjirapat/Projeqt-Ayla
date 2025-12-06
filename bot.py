import discord
from discord.ext import commands
import asyncio
import logging
import os
from pathlib import Path

from config import Config
from database.connection import db_manager
from utils.helpers import setup_logging
from utils.prefix_manager import prefix_manager

setup_logging()
logger = logging.getLogger(__name__)

class DiscordBot(commands.Bot):
    def __init__(self):

        intents = discord.Intents.default()
        if Config.INTENTS_ALL:
            intents = discord.Intents.all()
        else:
            intents.message_content = True
            intents.guilds = True
            intents.members = True
        
        logger.info(f"Bot intents: message_content={intents.message_content}, guilds={intents.guilds}, members={intents.members}")
        logger.info(f"All intents enabled: {Config.INTENTS_ALL}")
        
        super().__init__(
            command_prefix=prefix_manager.get_prefix,
            intents=intents,
            help_command=None
        )
    
    async def setup_hook(self):
        logger.info("Setting up bot...")

        if not await db_manager.connect():
            logger.error("Failed to connect to database")
            return

        await self.load_cogs()

        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash commands")

        except Exception as e:
            logger.error(f"Failed to sync slash commands: {e}")
        
        logger.info("Bot setup complete")
    
    async def load_cogs(self):
        cogs_dir = Path("cogs")
        
        for cog_file in cogs_dir.glob("*.py"):
            if cog_file.name.startswith("__"):
                continue

            if cog_file.name.startswith("_"):
                continue
            
            cog_name = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(cog_name)
                logger.info(f"Loaded cog: {cog_name}")

            except Exception as e:
                logger.error(f"Failed to load cog {cog_name}: {e}")

    async def update_status(self):
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=f" {len(self.guilds)} servers | @{self.user.name} help"
        )
        await self.change_presence(activity=activity)

    async def on_ready(self):
        logger.info(f"{self.user} has connected to Discord!")
        logger.info(f"Bot is in {len(self.guilds)} guilds")
        await self.update_status()
        logger.info("I18n system initialized")

    async def on_guild_join(self, guild):
        logger.info(f"Joined new guild: {guild.name} ({guild.id})")
        await self.update_status()

    async def on_guild_remove(self, guild):
        logger.info(f"Left guild: {guild.name} ({guild.id})")
        await self.update_status()
    

    async def on_message(self, message):
        if message.author.bot:
            return

        await self.process_commands(message)
    
    async def on_command_error(self, ctx, error):
        """Global error handler"""
        from utils.i18n import i18n
        
        if isinstance(error, commands.CommandNotFound):
            return
        
        elif isinstance(error, commands.MissingPermissions):
            error_msg = await i18n.t(ctx, 'errors.missing_permissions')
            await ctx.send(error_msg)
        
        elif isinstance(error, commands.MissingRequiredArgument):
            error_msg = await i18n.t(ctx, 'errors.missing_argument', param=error.param)
            await ctx.send(error_msg)
        
        elif isinstance(error, commands.BadArgument):
            error_msg = await i18n.t(ctx, 'errors.bad_argument')
            await ctx.send(error_msg)
        
        else:
            logger.error(f"Unhandled error in {ctx.command}: {error}")
            error_msg = await i18n.t(ctx, 'errors.unexpected_error')
            await ctx.send(error_msg)
    
    async def close(self):
        logger.info("Shutting down bot...")
        await db_manager.disconnect()
        await super().close()

async def main():
    try:
        Config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return

    bot = DiscordBot()
    
    try:
        await bot.start(Config.DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid Discord token")
    except Exception as e:
        logger.error(f"Error running bot: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())