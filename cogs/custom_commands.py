import logging
import re
from datetime import datetime, UTC
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

from database.models import CustomCommandModel
from utils.i18n import i18n
from utils.prefix_manager import prefix_manager

logger = logging.getLogger(__name__)


def parse_placeholders(
    template: str,
    author: discord.User | discord.Member,
    guild: Optional[discord.Guild],
    channel: Optional[discord.abc.Messageable],
    args_text: str = ""
) -> str:
    """Parse placeholder variables inside a custom command response template."""
    if not template:
        return ""

    args_list = args_text.split() if args_text else []

    placeholders = {
        "{user}": getattr(author, 'display_name', author.name),
        "{author}": getattr(author, 'display_name', author.name),
        "{username}": author.name,
        "{author_mention}": author.mention,
        "{user_mention}": author.mention,
        "{mention}": author.mention,
        "{author_id}": str(author.id),
        "{user_id}": str(author.id),
        "{server}": guild.name if guild else "Direct Message",
        "{guild}": guild.name if guild else "Direct Message",
        "{server_id}": str(guild.id) if guild else "0",
        "{guild_id}": str(guild.id) if guild else "0",
        "{channel}": getattr(channel, 'name', 'chat') if channel else "chat",
        "{channel_mention}": getattr(channel, 'mention', '#chat') if channel else "#chat",
        "{channel_id}": str(getattr(channel, 'id', 0)),
        "{member_count}": str(guild.member_count) if guild else "1",
        "{date}": datetime.now(UTC).strftime('%Y-%m-%d'),
        "{time}": datetime.now(UTC).strftime('%H:%M:%S UTC'),
        "{args}": args_text,
    }

    for i in range(1, 10):
        placeholders[f"{{arg{i}}}"] = args_list[i - 1] if i - 1 < len(args_list) else ""

    result = template
    for key, val in placeholders.items():
        result = result.replace(key, val)

    return result


class CustomCommands(commands.Cog):
    """Cog for Guild Custom Commands management and trigger execution."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.model = CustomCommandModel()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"{self.__class__.__name__} cog loaded successfully")

    def build_embed(
        self,
        embed_config: Dict[str, Any],
        author: discord.User | discord.Member,
        guild: Optional[discord.Guild],
        channel: Optional[discord.abc.Messageable],
        args_text: str = ""
    ) -> discord.Embed:
        """Construct a discord.Embed object from custom command embed_config with placeholder parsing."""
        title = parse_placeholders(embed_config.get('title', ''), author, guild, channel, args_text)
        description = parse_placeholders(embed_config.get('description', ''), author, guild, channel, args_text)
        
        color_val = embed_config.get('color', '#5865F2')
        try:
            if isinstance(color_val, str):
                color_int = int(color_val.lstrip('#'), 16)
            else:
                color_int = int(color_val)
            color = discord.Color(color_int)
        except Exception:
            color = discord.Color.blurple()

        embed = discord.Embed(
            title=title if title else None,
            description=description if description else None,
            color=color
        )

        image_url = parse_placeholders(embed_config.get('image_url', ''), author, guild, channel, args_text)
        if image_url and (image_url.startswith('http://') or image_url.startswith('https://')):
            embed.set_image(url=image_url)

        thumbnail_url = parse_placeholders(embed_config.get('thumbnail_url', ''), author, guild, channel, args_text)
        if thumbnail_url and (thumbnail_url.startswith('http://') or thumbnail_url.startswith('https://')):
            embed.set_thumbnail(url=thumbnail_url)

        footer_text = parse_placeholders(embed_config.get('footer_text', ''), author, guild, channel, args_text)
        footer_icon = parse_placeholders(embed_config.get('footer_icon', ''), author, guild, channel, args_text)
        if footer_text:
            embed.set_footer(
                text=footer_text,
                icon_url=footer_icon if footer_icon and (footer_icon.startswith('http://') or footer_icon.startswith('https://')) else None
            )

        return embed

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listener to match message prefix triggers with registered guild custom commands."""
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()
        if not content:
            return

        prefixes = await prefix_manager.get_prefix(self.bot, message)
        used_prefix = None
        for p in prefixes:
            if content.startswith(p):
                used_prefix = p
                break

        if not used_prefix:
            return

        after_prefix = content[len(used_prefix):].strip()
        if not after_prefix:
            return

        parts = after_prefix.split(maxsplit=1)
        command_name = parts[0].lower()
        args_text = parts[1] if len(parts) > 1 else ""

        # Avoid shadowing built-in bot commands
        if self.bot.get_command(command_name):
            return

        cmd = await self.model.get_command(message.guild.id, command_name)
        if not cmd or not cmd.get('enabled', True):
            return

        # Increment command trigger count asynchronously
        await self.model.increment_use_count(message.guild.id, command_name)

        response_text = parse_placeholders(cmd.get('response', ''), message.author, message.guild, message.channel, args_text)
        is_embed = cmd.get('is_embed', False)
        embed_config = cmd.get('embed_config', {})

        try:
            if is_embed or (embed_config and any(embed_config.values())):
                embed = self.build_embed(embed_config, message.author, message.guild, message.channel, args_text)
                await message.channel.send(
                    content=response_text if response_text else None,
                    embed=embed
                )
            else:
                if response_text:
                    await message.channel.send(response_text)
        except Exception as e:
            logger.error(f"[CustomCommands] Failed to execute trigger '{command_name}' in guild {message.guild.id}: {e}")

    @commands.hybrid_group(name='customcommand', aliases=['cc', 'customcmd'], invoke_without_command=True)
    async def custom_command_group(self, ctx: commands.Context):
        """Custom command management menu."""
        embed = discord.Embed(
            title=await i18n.t(ctx, "custom_commands.menu_title"),
            description=await i18n.t(ctx, "custom_commands.menu_description"),
            color=discord.Color.blurple()
        )
        embed.add_field(
            name=await i18n.t(ctx, "custom_commands.available_commands"),
            value=await i18n.t(ctx, "custom_commands.commands_help"),
            inline=False
        )
        embed.add_field(
            name=await i18n.t(ctx, "custom_commands.response_variables"),
            value="`{user}`, `{author_mention}`, `{server}`, `{channel}`, `{member_count}`, `{date}`, `{time}`, `{args}`",
            inline=False
        )
        embed.set_footer(text=await i18n.t(ctx, "custom_commands.dashboard_tip"))
        await ctx.send(embed=embed)

    @custom_command_group.command(name='add', aliases=['create'])
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(name="Command trigger name", response="Response content template")
    async def cc_add(self, ctx: commands.Context, name: str, *, response: str):
        """Add a new custom command for this server."""
        name_clean = name.strip().lower()
        if not re.match(r'^[a-z0-9_-]+$', name_clean):
            await ctx.send(await i18n.t(ctx, "custom_commands.invalid_name"))
            return

        if self.bot.get_command(name_clean):
            await ctx.send(await i18n.t(ctx, "custom_commands.built_in_conflict", name=name_clean))
            return

        existing = await self.model.get_command(ctx.guild.id, name_clean)
        if existing:
            await ctx.send(await i18n.t(ctx, "custom_commands.already_exists", name=name_clean))
            return

        created = await self.model.create_command(
            guild_id=ctx.guild.id,
            name=name_clean,
            response=response,
            created_by=ctx.author.id,
            created_by_name=str(ctx.author)
        )

        if created:
            embed = discord.Embed(
                title=await i18n.t(ctx, "custom_commands.created_title"),
                description=await i18n.t(ctx, "custom_commands.created_description", name=name_clean, response=response),
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(await i18n.t(ctx, "custom_commands.create_failed"))

    @custom_command_group.command(name='edit', aliases=['update'])
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(name="Command trigger name", response="New response content template")
    async def cc_edit(self, ctx: commands.Context, name: str, *, response: str):
        """Edit an existing custom command."""
        name_clean = name.strip().lower()
        existing = await self.model.get_command(ctx.guild.id, name_clean)
        if not existing:
            await ctx.send(await i18n.t(ctx, "custom_commands.not_found", name=name_clean))
            return

        success = await self.model.update_command(ctx.guild.id, name_clean, {'response': response})
        if success:
            embed = discord.Embed(
                title=await i18n.t(ctx, "custom_commands.updated_title"),
                description=await i18n.t(ctx, "custom_commands.updated_description", name=name_clean, response=response),
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(await i18n.t(ctx, "custom_commands.update_failed"))

    @custom_command_group.command(name='delete', aliases=['remove', 'del'])
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(name="Command trigger name to delete")
    async def cc_delete(self, ctx: commands.Context, name: str):
        """Delete a custom command from this server."""
        name_clean = name.strip().lower()
        existing = await self.model.get_command(ctx.guild.id, name_clean)
        if not existing:
            await ctx.send(await i18n.t(ctx, "custom_commands.not_found", name=name_clean))
            return

        success = await self.model.delete_command(ctx.guild.id, name_clean)
        if success:
            await ctx.send(await i18n.t(ctx, "custom_commands.deleted", name=name_clean))
        else:
            await ctx.send(await i18n.t(ctx, "custom_commands.delete_failed"))

    @custom_command_group.command(name='list')
    async def cc_list(self, ctx: commands.Context):
        """List all custom commands in this server."""
        commands_list = await self.model.get_guild_commands(ctx.guild.id)
        if not commands_list:
            await ctx.send(await i18n.t(ctx, "custom_commands.list_empty"))
            return

        embed = discord.Embed(
            title=await i18n.t(ctx, "custom_commands.list_title", server=ctx.guild.name),
            description=await i18n.t(ctx, "custom_commands.list_total", count=len(commands_list)),
            color=discord.Color.gold()
        )

        formatted_cmds = []
        for cmd in commands_list:
            status = "🟢" if cmd.get('enabled', True) else "🔴"
            embed_badge = await i18n.t(ctx, "custom_commands.embed_badge") if cmd.get('is_embed') else ""
            uses = cmd.get('use_count', 0)
            formatted_cmds.append(await i18n.t(
                ctx, "custom_commands.list_item", status=status,
                name=cmd['name'], type=embed_badge, uses=uses
            ))

        embed.add_field(
            name=await i18n.t(ctx, "custom_commands.commands"),
            value="\n".join(formatted_cmds[:25]) if len(formatted_cmds) > 25 else "\n".join(formatted_cmds),
            inline=False
        )
        if len(formatted_cmds) > 25:
            embed.set_footer(text=await i18n.t(ctx, "custom_commands.list_more", count=len(formatted_cmds) - 25))

        await ctx.send(embed=embed)

    @custom_command_group.command(name='info', aliases=['show'])
    @app_commands.describe(name="Command trigger name")
    async def cc_info(self, ctx: commands.Context, name: str):
        """View detailed information about a custom command."""
        name_clean = name.strip().lower()
        cmd = await self.model.get_command(ctx.guild.id, name_clean)
        if not cmd:
            await ctx.send(await i18n.t(ctx, "custom_commands.not_found", name=name_clean))
            return

        embed = discord.Embed(
            title=await i18n.t(ctx, "custom_commands.info_title", name=cmd['name']),
            color=discord.Color.blurple()
        )
        embed.add_field(
            name=await i18n.t(ctx, "custom_commands.status"),
            value=await i18n.t(ctx, "custom_commands.enabled" if cmd.get('enabled', True) else "custom_commands.disabled"),
            inline=True
        )
        embed.add_field(
            name=await i18n.t(ctx, "custom_commands.type"),
            value=await i18n.t(ctx, "custom_commands.rich_embed" if cmd.get('is_embed') else "custom_commands.plain_text"),
            inline=True
        )
        embed.add_field(name=await i18n.t(ctx, "custom_commands.times_used"), value=str(cmd.get('use_count', 0)), inline=True)
        embed.add_field(name=await i18n.t(ctx, "custom_commands.created_by"), value=cmd.get('created_by_name') or await i18n.t(ctx, "general.unknown"), inline=True)
        if cmd.get('description'):
            embed.add_field(name=await i18n.t(ctx, "custom_commands.description"), value=cmd['description'], inline=False)
        embed.add_field(name=await i18n.t(ctx, "custom_commands.response_template"), value=f"```{cmd.get('response', '')}```", inline=False)

        await ctx.send(embed=embed)

    @custom_command_group.command(name='toggle')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(name="Command trigger name")
    async def cc_toggle(self, ctx: commands.Context, name: str):
        """Toggle a custom command between enabled and disabled."""
        name_clean = name.strip().lower()
        cmd = await self.model.get_command(ctx.guild.id, name_clean)
        if not cmd:
            await ctx.send(await i18n.t(ctx, "custom_commands.not_found", name=name_clean))
            return

        new_status = not cmd.get('enabled', True)
        success = await self.model.update_command(ctx.guild.id, name_clean, {'enabled': new_status})
        if success:
            status_text = await i18n.t(ctx, "custom_commands.enabled" if new_status else "custom_commands.disabled")
            await ctx.send(await i18n.t(ctx, "custom_commands.toggled", name=name_clean, status=status_text))
        else:
            await ctx.send(await i18n.t(ctx, "custom_commands.toggle_failed"))


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomCommands(bot))
