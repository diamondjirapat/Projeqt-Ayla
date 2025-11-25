import discord
from discord import app_commands
from discord.ext import commands
from database.models import UserModel
from utils.i18n import i18n
import logging

logger = logging.getLogger(__name__)

class Language(commands.Cog):
    """Language management commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.user_model = UserModel()
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'{self.__class__.__name__} cog loaded')
    
    @commands.hybrid_command(name='langinfo')
    async def language_info(self, ctx: commands.Context):
        """Show language priority and current settings"""
        embed = discord.Embed(
            title="🌐 Language Settings",
            description="Language priority system explained",
            color=discord.Color.blue()
        )

        user_locale = await i18n.get_user_locale(ctx.author.id)
        guild_locale = await i18n.get_guild_locale(ctx.guild.id) if ctx.guild else None
        effective_locale = await i18n.get_locale(ctx)

        priority_text = "**Priority Order:**\n"
        priority_text += "1️⃣ Your personal language (highest)\n"
        priority_text += "2️⃣ Server language\n"
        priority_text += "3️⃣ Default language (English)\n\n"
        priority_text += "**Static embeds** (music player, etc.) always use server language."
        
        embed.add_field(
            name="📊 How It Works",
            value=priority_text,
            inline=False
        )

        settings_text = ""
        if user_locale:
            lang_name = await i18n.t(ctx, f'languages.{user_locale}')
            settings_text += f"**Your Language:** {lang_name} ({user_locale})\n"
        else:
            settings_text += "**Your Language:** Not set\n"
        
        if guild_locale and ctx.guild:
            lang_name = await i18n.t(ctx, f'languages.{guild_locale}')
            settings_text += f"**Server Language:** {lang_name} ({guild_locale})\n"
        else:
            settings_text += "**Server Language:** Not set (using English)\n"
        
        effective_lang_name = await i18n.t(ctx, f'languages.{effective_locale}')
        settings_text += f"\n**You're currently using:** {effective_lang_name} ({effective_locale})"
        
        embed.add_field(
            name="⚙️ Current Settings",
            value=settings_text,
            inline=False
        )

        commands_text = f"`{ctx.prefix}mylang <code>` - Set your personal language\n"
        commands_text += f"`{ctx.prefix}setlang <code>` - Set server language (Admin only)\n"
        commands_text += f"\n**Available:** English (en), ไทย (th)"
        
        embed.add_field(
            name="🔧 Commands",
            value=commands_text,
            inline=False
        )
        
        embed.set_footer(text="Tip: Personal language overrides server language for you!")
        
        await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='mylang')
    @app_commands.describe(language="Language code (en, th)")
    @app_commands.choices(language=[
        app_commands.Choice(name="English", value="en"),
        app_commands.Choice(name="ไทย (Thai)", value="th")
    ])
    async def set_user_language(self, ctx: commands.Context, language: str = None):
        """Set your personal language preference"""
        if language is None:
            user_locale = await i18n.get_user_locale(ctx.author.id)
            if user_locale:
                lang_name = await i18n.t(ctx, f'languages.{user_locale}')
                title = await i18n.t(ctx, "language.your_language_title")
                description = await i18n.t(ctx, "language.current_language", language=lang_name, code=user_locale)
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.blue()
                )
            else:
                title = await i18n.t(ctx, "language.your_language_title")
                description = await i18n.t(ctx, "language.no_personal_language")
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.blue()
                )

            available_langs = []
            for lang_code in i18n.supported_locales:
                lang_name = await i18n.t(ctx, f'languages.{lang_code}')
                available_langs.append(f"{lang_name} ({lang_code})")
            
            embed.add_field(
                name="Available Languages",
                value="\n".join(available_langs),
                inline=False
            )
            embed.add_field(
                name="Usage",
                value=f"`{ctx.prefix}mylang <language_code>`",
                inline=False
            )
            
            await ctx.send(embed=embed)
            return
        
        language = language.lower()
        
        if language not in i18n.supported_locales:
            available_langs = ', '.join([f"{await i18n.t(ctx, f'languages.{lang}')} ({lang})" for lang in i18n.supported_locales])
            error_msg = f"❌ Invalid language. Available: {available_langs}"
            await ctx.send(error_msg)
            return

        user_data = await self.user_model.get_user(ctx.author.id)
        if not user_data:
            await self.user_model.create_user(ctx.author.id, ctx.author.display_name, locale=language)
        
        success = await i18n.set_user_locale(ctx.author.id, language)
        
        if success:
            i18n.clear_user_cache(ctx.author.id)
            
            lang_name = await i18n.t(ctx, f'languages.{language}')
            title = await i18n.t(ctx, "language.updated_title")
            description = await i18n.t(ctx, "language.updated", language=lang_name)
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(await i18n.t(ctx, "general.language_update_error"))
    
    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Auto-register new members with the server locale"""
        if member.bot:
            return
        
        try:
            # Get guild locale or default to English
            guild_locale = await i18n.get_guild_locale(member.guild.id)
            if not guild_locale:
                guild_locale = 'en'

            existing_user = await self.user_model.get_user(member.id)
            if not existing_user:
                await self.user_model.create_user(
                    member.id, 
                    member.display_name, 
                    locale=guild_locale
                )
                logger.info(f"Auto-registered user {member} with locale {guild_locale}")
        
        except Exception as e:
            logger.error(f"Error auto-registering user {member}: {e}")


    @commands.hybrid_command(name='setlang')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(language="Language code (en, th)")
    @app_commands.choices(language=[
        app_commands.Choice(name="English", value="en"),
        app_commands.Choice(name="ไทย (Thai)", value="th")
    ])
    async def set_language(self, ctx: commands.Context, language: str):
        """Set server language"""
        language = language.lower()
        
        if language not in i18n.supported_locales:
            available_langs = ', '.join([await i18n.t(ctx, f'languages.{lang}') for lang in i18n.supported_locales])
            error_msg = await i18n.t(ctx, 'commands.setlang.invalid_language', languages=available_langs)
            await ctx.send(error_msg)
            return
        
        success = await i18n.set_guild_locale(ctx.guild.id, language)
        
        if success:
            i18n.clear_guild_cache(ctx.guild.id)
            
            lang_name = await i18n.t(ctx, f'languages.{language}')
            title = await i18n.t(ctx, 'commands.setlang.title')
            description = await i18n.t(ctx, 'commands.setlang.description', language=lang_name)
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        else:
            error_msg = await i18n.t(ctx, 'errors.unexpected_error')
            await ctx.send(error_msg)
    
    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        """Auto-register new guilds with the default locale"""
        try:
            existing_guild = await i18n.guild_model.get_guild(guild.id)
            if not existing_guild:
                await i18n.guild_model.create_guild(
                    guild.id,
                    guild.name,
                    locale='en'
                )
                logger.info(f"Auto-registered guild {guild.name} with default locale")
        
        except Exception as e:
            logger.error(f"Error auto-registering guild {guild}: {e}")

async def setup(bot):
    await bot.add_cog(Language(bot))