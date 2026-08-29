import discord
from discord.ext import commands
from utils.i18n import i18n
import logging

logger = logging.getLogger(__name__)


class Help(commands.Cog):
    """Help command"""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'{self.__class__.__name__} cog loaded')

    async def _get_cmd_desc(self, ctx_or_interaction, cmd):
        """Get localised command description, falling back to docstring"""
        key = cmd.qualified_name
        desc = await i18n.t(ctx_or_interaction, f"help_descriptions.{key}")

        if desc and desc != f"help_descriptions.{key}":
            return desc

        return cmd.help or await i18n.t(ctx_or_interaction, "help.no_description")

    @commands.command(name='help')
    async def help_command(self, ctx, *, command: str = None):
        """Show help information"""
        if command:
            cmd = self.bot.get_command(command)
            if cmd:
                title = await i18n.t(ctx, "help.help_for", command=cmd.name)
                cmd_description = await self._get_cmd_desc(ctx, cmd)
                embed = discord.Embed(
                    title=title,
                    description=cmd_description,
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

        for cog_name, cog_commands in cogs.items():
            if cog_commands:
                category_name = await i18n.t(ctx, "help.category_format", name=cog_name)
                commands_text = await i18n.t(ctx, "help.commands_available", count=len(cog_commands))
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

        view = HelpView(ctx, cogs, timeout=30)
        message = await ctx.send(embed=embed, view=view)
        view.message = message


class HelpView(discord.ui.View):
    def __init__(self, ctx, cogs, timeout=30):
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
            'Prefix': '⚙️',
            'Giveaway': '🎉',
            'Leveling': '📊',
            'CustomCommands': '✨',
            'AutoRole': '🎭',
            'ReactionRolesCog': '📋',
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
        for cmd in self.commands:
            # Look up localised description
            key = cmd.qualified_name
            cmd_description = await i18n.t(interaction, f"help_descriptions.{key}")
            # Fall back to docstring if key not found
            if not cmd_description or cmd_description == f"help_descriptions.{key}":
                no_desc = await i18n.t(interaction, "help.no_description")
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

        for cog_name, cog_commands in self.view.cogs.items():
            if cog_commands:
                category_name = await i18n.t(interaction, "help.category_format", name=cog_name)
                commands_text = await i18n.t(interaction, "help.commands_available", count=len(cog_commands))
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
    await bot.add_cog(Help(bot))
