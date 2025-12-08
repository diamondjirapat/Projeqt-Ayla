import discord
from discord import app_commands
from discord.ext import commands
from database.models import UserModel, GuildModel
from utils.i18n import i18n
import logging
import time

logger = logging.getLogger(__name__)

class General(commands.Cog):
    """General purpose commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.user_model = UserModel()
        self.guild_model = GuildModel()
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'{self.__class__.__name__} cog loaded')

    @commands.hybrid_command(name='ping')
    async def ping(self, ctx: commands.Context):
        """Checks bot latency (Websocket and API)"""
        websocket_latency = round(self.bot.latency * 1000)
        start_time = time.time()
        temp_message = await ctx.send("Pinging...")
        end_time = time.time()
        api_latency = round((end_time - start_time) * 1000)

        embed = discord.Embed(
            title="🏓 Pong!",
            color=discord.Color.blue()
        )

        embed.add_field(name="Websocket Latency 📡", value=f"{websocket_latency}ms", inline=True)
        embed.add_field(name="API Latency 📝", value=f"{api_latency}ms", inline=True)
        await temp_message.edit(content=None, embed=embed)
    
    @commands.hybrid_command(name='info')
    async def info_command(self, ctx: commands.Context):
        """Display bot information"""
        title = await i18n.t(ctx, 'commands.info.title')
        servers_label = await i18n.t(ctx, 'commands.info.servers')
        users_label = await i18n.t(ctx, 'commands.info.users')
        version_label = await i18n.t(ctx, 'commands.info.version')
        
        embed = discord.Embed(
            title=title,
            color=discord.Color.blue()
        )
        embed.add_field(name=servers_label, value=len(self.bot.guilds), inline=True)
        embed.add_field(name=users_label, value=len(self.bot.users), inline=True)
        embed.add_field(name=version_label, value=discord.__version__, inline=True)
        
        await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='profile')
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
    
    @commands.hybrid_command(name='test')
    async def test_command(self, ctx: commands.Context):
        """Simple test command"""
        await ctx.send(await i18n.t(ctx, "general.test_command"))
        logger.info(f"Test command executed by {ctx.author}")
    
    @commands.command(name='reload')
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
                title="🔄 Reload Complete",
                color=discord.Color.green() if not failed else discord.Color.orange()
            )
            
            if reloaded:
                embed.add_field(
                    name=f"✅ Reloaded ({len(reloaded)})",
                    value="\n".join([f"`{ext}`" for ext in reloaded]) or "None",
                    inline=False
                )
            
            if failed:
                embed.add_field(
                    name=f"❌ Failed ({len(failed)})",
                    value="\n".join([f"`{fail}`" for fail in failed[:5]]) or "None",
                    inline=False
                )
            
            await ctx.send(embed=embed)
        else:
            extension_name = f"cogs.{cog}" if not cog.startswith("cogs.") else cog
            
            try:
                await self.bot.reload_extension(extension_name)
                await ctx.send(f"✅ Reloaded: `{extension_name}`")
                logger.info(f"Reloaded: {extension_name}")
            except commands.ExtensionNotLoaded:
                await ctx.send(f"❌ Extension `{extension_name}` is not loaded.")
            except commands.ExtensionNotFound:
                await ctx.send(f"❌ Extension `{extension_name}` not found.")
            except Exception as e:
                await ctx.send(f"❌ Failed to reload `{extension_name}`: {str(e)}")
                logger.error(f"Failed to reload {extension_name}: {e}")
    
    @commands.command(name='help')
    async def help_command(self, ctx, *, command: str = None):
        """Show help information"""
        if command:
            cmd = self.bot.get_command(command)
            if cmd:
                title = await i18n.t(ctx, "help.help_for", command=cmd.name)
                no_desc = await i18n.t(ctx, "help.no_description")
                embed = discord.Embed(
                    title=title,
                    description=cmd.help or no_desc,
                    color=discord.Color.blue()
                )
                if cmd.usage:
                    usage_label = await i18n.t(ctx, "help.usage")
                    embed.add_field(name=usage_label, value=f"`{ctx.prefix}{cmd.name} {cmd.usage}`", inline=False)
                if cmd.aliases:
                    aliases_label = await i18n.t(ctx, "help.aliases")
                    embed.add_field(name=aliases_label, value=", ".join(cmd.aliases), inline=False)
                await ctx.send(embed=embed)
            else:
                title = await i18n.t(ctx, "help.command_not_found_title")
                description = await i18n.t(ctx, "help.command_not_found", command=command)
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
        else:
            await self._send_interactive_help(ctx)
    
    async def _send_interactive_help(self, ctx):
        """Send interactive help with buttons for each cog"""
        cogs = {}
        for cmd in self.bot.commands:
            if cmd.cog_name:
                if cmd.cog_name not in cogs:
                    cogs[cmd.cog_name] = []
                cogs[cmd.cog_name].append(cmd)
            else:
                if "General" not in cogs:
                    cogs["General"] = []
                cogs["General"].append(cmd)

        title = await i18n.t(ctx, "help.title")
        description = await i18n.t(ctx, "help.description")
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue()
        )

        for cog_name, commands in cogs.items():
            if commands:
                category_name = await i18n.t(ctx, "help.category_format", name=cog_name)
                commands_text = await i18n.t(ctx, "help.commands_available", count=len(commands))
                embed.add_field(
                    name=category_name,
                    value=commands_text,
                    inline=True
                )
        
        tip_label = await i18n.t(ctx, "help.tip")
        tip_text = await i18n.t(ctx, "help.tip_text", prefix=ctx.prefix)
        embed.add_field(
            name=tip_label,
            value=tip_text,
            inline=False
        )

        view = HelpView(ctx, cogs, timeout=300)
        message = await ctx.send(embed=embed, view=view)
        view.message = message


class HelpView(discord.ui.View):
    def __init__(self, ctx, cogs, timeout=300):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.cogs = cogs
        self.message = None

        cog_names = list(cogs.keys())[:25]

        for i, cog_name in enumerate(cog_names):
            button = CogButton(cog_name, self.cogs[cog_name], row=i // 5)
            self.add_item(button)

        self.add_item(BackButton(row=4))
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow the command author to use the buttons"""
        return interaction.user == self.ctx.author
    
    async def on_timeout(self):
        """Disable all buttons when a timeout occurs"""
        for item in self.children:
            item.disabled = True
        
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


class CogButton(discord.ui.Button):
    def __init__(self, cog_name, commands, row=0):
        emoji_map = {
            'General': '🔧',
            'Music': '🎵',
            'Moderation': '🛡️',
            'Language': '🌐',
            'Prefix': '⚙️'
        }
        
        super().__init__(
            label=cog_name,
            emoji=emoji_map.get(cog_name, '📁'),
            style=discord.ButtonStyle.primary,
            row=row
        )
        self.cog_name = cog_name
        self.commands = commands
    
    async def callback(self, interaction: discord.Interaction):
        """Show detailed commands for this cog"""
        # Use interaction for i18n
        category_title = await i18n.t(interaction, "help.category_format", name=self.cog_name)
        commands_label = await i18n.t(interaction, "help.commands_label")
        title = f"{category_title} {commands_label}"
        description = await i18n.t(interaction, "help.all_commands_in", category=self.cog_name)
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.green()
        )

        cmd_list = []
        no_desc = await i18n.t(interaction, "help.no_description")
        for cmd in self.commands:
            cmd_description = cmd.help or no_desc
            if len(cmd_description) > 50:
                cmd_description = cmd_description[:47] + "..."
            cmd_list.append(f"`{interaction.message.content.split()[0] if interaction.message.content else '!'}{cmd.name}` - {cmd_description}")

        commands_label = await i18n.t(interaction, "help.commands_label")
        if len(cmd_list) <= 10:
            embed.add_field(
                name=commands_label,
                value="\n".join(cmd_list),
                inline=False
            )
        else:
            for i in range(0, len(cmd_list), 10):
                chunk = cmd_list[i:i+10]
                if i == 0:
                    field_name = commands_label
                else:
                    field_name = await i18n.t(interaction, "help.commands_continued", page=i//10 + 1)
                embed.add_field(
                    name=field_name,
                    value="\n".join(chunk),
                    inline=False
                )
        
        footer_text = await i18n.t(interaction, "help.back_to_menu")
        embed.set_footer(text=footer_text)
        
        await interaction.response.edit_message(embed=embed, view=self.view)


class BackButton(discord.ui.Button):
    def __init__(self, row=4):
        super().__init__(
            label="Back",
            emoji="⬅️",
            style=discord.ButtonStyle.secondary,
            row=row
        )
    
    async def callback(self, interaction: discord.Interaction):
        """Go back to the main help menu"""
        title = await i18n.t(interaction, "help.title")
        description = await i18n.t(interaction, "help.description")
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue()
        )

        for cog_name, commands in self.view.cogs.items():
            if commands:
                category_name = await i18n.t(interaction, "help.category_format", name=cog_name)
                commands_text = await i18n.t(interaction, "help.commands_available", count=len(commands))
                embed.add_field(
                    name=category_name,
                    value=commands_text,
                    inline=True
                )
        
        tip_label = await i18n.t(interaction, "help.tip")
        tip_text = await i18n.t(interaction, "help.tip_text", prefix=self.view.ctx.prefix)
        embed.add_field(
            name=tip_label,
            value=tip_text,
            inline=False
        )
        
        await interaction.response.edit_message(embed=embed, view=self.view)
    
async def setup(bot):
    await bot.add_cog(General(bot))