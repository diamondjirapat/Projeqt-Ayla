import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import logging
import os
import traceback
from pathlib import Path

from config import Config
from database.connection import db_manager
from utils.helpers import setup_logging
from utils.prefix_manager import prefix_manager

setup_logging()
logger = logging.getLogger(__name__)

class DiscordBot(commands.Bot):
    all_bots = []

    def __init__(self, is_main=True):
        self.is_main = is_main

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
            owner_ids=Config.OWNER_IDS,
            help_command=None
        )
        DiscordBot.all_bots.append(self)

    async def setup_hook(self):
        logger.info("Setting up bot...")

        if db_manager.client is None:
            if not await db_manager.connect():
                logger.error("Failed to connect to database")
                return

        await self.load_cogs()

        from cogs.music import IdlePlaylistView
        from database.models import UserModel, GuildModel
        self.add_view(IdlePlaylistView(0, UserModel(), GuildModel(), self))

        self.tree.on_error = self.on_app_command_error

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

        music_cog = self.get_cog("Music")
        if music_cog:
            for guild in self.guilds:
                try:
                    await music_cog.update_static_embed(guild.id)
                except Exception as e:
                    logger.warning(f"Failed to refresh static embed for guild {guild.id}: {e}")

    async def on_guild_join(self, guild):
        logger.info(f"Joined new guild: {guild.name} ({guild.id})")
        await self.update_status()

    async def on_guild_remove(self, guild):
        logger.info(f"Left guild: {guild.name} ({guild.id})")
        await self.update_status()


    async def should_handle_message(self, message):
        """
        Coordinating logic for multiple bots
        """
        ctx = await self.get_context(message)
        if not ctx.valid or not ctx.command:
            return False

        is_music = ctx.command.cog_name == "Music"

        if message.guild:
            guild_bots = [b for b in DiscordBot.all_bots if b.get_guild(message.guild.id) is not None]
        else:
            guild_bots = DiscordBot.all_bots

        if not guild_bots:
            return self.is_main

        if not is_music:
            main_bot = guild_bots[0] if guild_bots else self
            return self == main_bot

        if not message.guild:
            return self == guild_bots[0]

        active_bot = None
        for b in guild_bots:
            for vc in b.voice_clients:
                if vc.guild.id == message.guild.id:
                    active_bot = b
                    break
            if active_bot:
                break

        if active_bot:
            play_cmds = {"play", "playnow", "playnext", "join", "connect", "pn", "pnx", "pl", "playlist"}
            is_play_cmd = ctx.command.name in play_cmds or (ctx.invoked_with in play_cmds)

            if is_play_cmd:
                user_vc = message.author.voice.channel if (message.author and message.author.voice) else None
                active_vc = next((vc for vc in active_bot.voice_clients if vc.guild.id == message.guild.id), None)
                active_channel = active_vc.channel if active_vc else None

                if user_vc and active_channel and user_vc == active_channel:
                    return self == active_bot
                else:
                    free_bot = None
                    for b in guild_bots:
                        is_busy = any(vc.guild.id == message.guild.id for vc in b.voice_clients)
                        if not is_busy:
                            free_bot = b
                            break

                    if free_bot:
                        return self == free_bot
                    else:
                        return self == active_bot
            else:
                return self == active_bot
        else:
            return self == guild_bots[0]

    async def on_message(self, message):
        if message.author.bot:
            return

        if not await self.should_handle_message(message):
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

        elif isinstance(error, commands.BotMissingPermissions):
            perms = ', '.join(error.missing_permissions)
            error_msg = await i18n.t(ctx, 'errors.bot_missing_permissions', perms=perms)
            await ctx.send(error_msg)

        elif isinstance(error, commands.CheckFailure):
             error_msg = await i18n.t(ctx, 'errors.check_failure')
             await ctx.send(error_msg)

        elif isinstance(error, commands.CommandInvokeError):
             if isinstance(error.original, discord.Forbidden):
                error_msg = await i18n.t(ctx, 'errors.forbidden')
                await ctx.send(error_msg)
             else:
                  logger.error(f"Unhandled error in {ctx.command}: {error}")
                  error_msg = await i18n.t(ctx, 'errors.unexpected_error')
                  tb = ''.join(traceback.format_exception(type(error.original), error.original, error.original.__traceback__))
                  await ctx.send(f"{error_msg}\n```\n{tb[:1900]}\n```")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Global error handler for slash commands"""
        from utils.i18n import i18n

        logger.error(f"App command error: {error}")
        async def send_error(message: str):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except discord.HTTPException:
                pass

        if isinstance(error, app_commands.CommandNotFound):
            await send_error("⚠️ This command is outdated. Please try again — it should update shortly!")
            return

        if isinstance(error, app_commands.CommandOnCooldown):
            await send_error(f"⏳ Command on cooldown. Try again in {error.retry_after:.1f}s")

        elif isinstance(error, app_commands.MissingPermissions):
            error_msg = await i18n.t(interaction, 'errors.missing_permissions')
            await send_error(error_msg)

        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ', '.join(error.missing_permissions)
            error_msg = await i18n.t(interaction, 'errors.bot_missing_permissions', perms=perms)
            await send_error(error_msg)

        elif isinstance(error, app_commands.CheckFailure):
            error_msg = await i18n.t(interaction, 'errors.check_failure')
            await send_error(error_msg)

        elif isinstance(error, app_commands.CommandInvokeError):
            if isinstance(error.original, discord.Forbidden):
                error_msg = await i18n.t(interaction, 'errors.forbidden')
                await send_error(error_msg)
            else:
                logger.error(f"Unhandled app command error: {error.original}", exc_info=error.original)
                error_msg = await i18n.t(interaction, 'errors.unexpected_error')
                tb = ''.join(traceback.format_exception(type(error.original), error.original, error.original.__traceback__))
                await send_error(f"{error_msg}\n```\n{tb[:1900]}\n```")

        else:
            logger.error(f"Unknown app command error type: {type(error)}", exc_info=error)
            error_msg = await i18n.t(interaction, 'errors.unexpected_error')
            tb = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            await send_error(f"{error_msg}\n```\n{tb[:1900]}\n```")

    async def close(self):
        logger.info("Shutting down bot...")
        if self in DiscordBot.all_bots:
            DiscordBot.all_bots.remove(self)
        if not DiscordBot.all_bots:
            await db_manager.disconnect()
        await super().close()

async def main():
    try:
        Config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return

    bots = []
    tasks = []

    for i, token in enumerate(Config.DISCORD_TOKENS):
        is_main = (i == 0)
        bot = DiscordBot(is_main=is_main)
        bots.append(bot)
        tasks.append(bot.start(token))

    try:
        await asyncio.gather(*tasks)
    except discord.LoginFailure:
        logger.error("Invalid Discord token found in list")
    except Exception as e:
        logger.error(f"Error running bots: {e}")
    finally:
        for bot in bots:
            if not bot.is_closed():
                await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
