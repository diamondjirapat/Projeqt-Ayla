import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional, Dict, List
import json

from utils.i18n import i18n
from database.models import GuildModel

logger = logging.getLogger(__name__)


class ReactionRolesCog(commands.Cog):
    """Reaction roles system - assign roles by reacting to messages"""

    def __init__(self, bot):
        self.bot = bot
        self.guild_model = GuildModel()
        self.reaction_roles: Dict[int, Dict[int, Dict[str, int]]] = {}

    async def cog_load(self):
        """Load reaction roles from a database when cog loads"""
        await self.load_reaction_roles()

    async def load_reaction_roles(self):
        """Load all reaction roles from a database"""
        try:
            await self.bot.wait_until_ready()

            loaded_count = 0
            for guild in self.bot.guilds:
                guild_data = await self.guild_model.get_guild(guild.id)
                if guild_data and 'reaction_roles' in guild_data:
                    reaction_roles_data = guild_data['reaction_roles']
                    self.reaction_roles[guild.id] = {}

                    for message_id_str, emoji_roles in reaction_roles_data.items():
                        message_id = int(message_id_str)
                        self.reaction_roles[guild.id][message_id] = {}

                        for emoji, role_id in emoji_roles.items():
                            self.reaction_roles[guild.id][message_id][emoji] = role_id
                            loaded_count += 1

            logger.info(f"Loaded {loaded_count} reaction roles for {len(self.reaction_roles)} guilds")
        except Exception as e:
            logger.error(f"Failed to load reaction roles: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def get_reaction_roles(self, guild_id: int) -> Dict[int, Dict[str, int]]:
        """Get reaction roles for a guild"""
        return self.reaction_roles.get(guild_id, {})

    async def _update_message_embed(self, message: discord.Message, guild_id: int, message_id: int, ctx: commands.Context = None):
        """Update a message's embed to show configured reaction roles"""
        try:
            # Get the configured roles
            if guild_id not in self.reaction_roles or message_id not in self.reaction_roles[guild_id]:
                return

            emoji_roles = self.reaction_roles[guild_id][message_id]

            title = "Reaction Roles"
            custom_description = "React below to get roles!"

            if message.embeds:
                old_embed = message.embeds[0]
                if old_embed.title:
                    title = old_embed.title
                if old_embed.description and not any(x in str(old_embed.description) for x in ['|', '•', ' - ']):
                    custom_description = old_embed.description

            embed = discord.Embed(
                title=title,
                description=custom_description,
                color=discord.Color.blue()
            )

            for emoji, role_id in emoji_roles.items():
                role = message.guild.get_role(role_id)
                if role:
                    embed.add_field(
                        name=emoji,
                        value=role.mention,
                        inline=True
                    )
                else:
                    embed.add_field(
                        name=emoji,
                        value="(Deleted)",
                        inline=True
                    )

            embed.set_footer(text=ctx.guild.name,icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

            await message.edit(embed=embed)
            logger.debug(f"Updated embed for message {message_id}")
        except Exception as e:
            logger.error(f"Failed to update message embed: {e}")

    async def save_reaction_role(self, guild_id: int, message_id: int, emoji: str, role_id: int):
        """Save a reaction role mapping"""
        if guild_id not in self.reaction_roles:
            self.reaction_roles[guild_id] = {}
        if message_id not in self.reaction_roles[guild_id]:
            self.reaction_roles[guild_id][message_id] = {}

        self.reaction_roles[guild_id][message_id][emoji] = role_id

        guild_data = await self.guild_model.get_guild(guild_id)
        reaction_roles_data = guild_data.get('reaction_roles', {}) if guild_data else {}

        if str(message_id) not in reaction_roles_data:
            reaction_roles_data[str(message_id)] = {}

        reaction_roles_data[str(message_id)][emoji] = role_id

        await self.guild_model.update_guild(guild_id, {
            'reaction_roles': reaction_roles_data
        })

    async def remove_reaction_role(self, guild_id: int, message_id: int, emoji: str = None):
        """Remove a reaction role mapping"""
        if guild_id not in self.reaction_roles:
            return False

        if message_id not in self.reaction_roles[guild_id]:
            return False

        if emoji:
            if emoji in self.reaction_roles[guild_id][message_id]:
                del self.reaction_roles[guild_id][message_id][emoji]
        else:
            del self.reaction_roles[guild_id][message_id]

        guild_data = await self.guild_model.get_guild(guild_id)
        reaction_roles_data = guild_data.get('reaction_roles', {}) if guild_data else {}

        if str(message_id) in reaction_roles_data:
            if emoji:
                if emoji in reaction_roles_data[str(message_id)]:
                    del reaction_roles_data[str(message_id)][emoji]
                if not reaction_roles_data[str(message_id)]:
                    del reaction_roles_data[str(message_id)]
            else:
                del reaction_roles_data[str(message_id)]

        await self.guild_model.update_guild(guild_id, {
            'reaction_roles': reaction_roles_data
        })

        return True


    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reaction adds"""
        if payload.user_id == self.bot.user.id:
            return

        guild_id = payload.guild_id
        if not guild_id:
            logger.debug("No guild_id in reaction payload")
            return

        message_id = payload.message_id

        if payload.emoji.id:
            # Custom emoji
            emoji_str = f"<{'a' if payload.emoji.animated else ''}:{payload.emoji.name}:{payload.emoji.id}>"
        else:
            # Unicode emoji
            emoji_str = str(payload.emoji)

        logger.debug(f"Reaction add: guild={guild_id}, message={message_id}, emoji={emoji_str}, user={payload.user_id}")

        if guild_id not in self.reaction_roles:
            logger.debug(f"Guild {guild_id} not in reaction_roles cache")
            return
        if message_id not in self.reaction_roles[guild_id]:
            logger.debug(f"Message {message_id} not in reaction_roles for guild {guild_id}")
            logger.debug(f"Available messages: {list(self.reaction_roles[guild_id].keys())}")
            return
        if emoji_str not in self.reaction_roles[guild_id][message_id]:
            logger.debug(f"Emoji {emoji_str} not configured for message {message_id}")
            logger.debug(f"Available emojis: {list(self.reaction_roles[guild_id][message_id].keys())}")
            return

        role_id = self.reaction_roles[guild_id][message_id][emoji_str]
        guild = self.bot.get_guild(guild_id)
        if not guild:
            logger.error(f"Guild {guild_id} not found")
            return

        role = guild.get_role(role_id)
        if not role:
            logger.warning(f"Role {role_id} not found in guild {guild_id}")
            return

        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
                logger.debug(f"Fetched member {member.name} from API")
            except discord.NotFound:
                logger.warning(f"Member {payload.user_id} not found in guild {guild_id}")
                return
            except Exception as e:
                logger.error(f"Failed to fetch member {payload.user_id}: {e}")
                return

        if role in member.roles:
            logger.debug(f"Member {member.name} already has role {role.name}")
            return

        try:
            await member.add_roles(role, reason="Reaction role")
            logger.info(f"✅ Added role {role.name} to {member.name} in {guild.name}")
        except discord.Forbidden:
            logger.error(f"❌ Missing permissions to add role {role.name} in {guild.name}")
        except Exception as e:
            logger.error(f"❌ Failed to add role: {e}")
            import traceback
            logger.error(traceback.format_exc())

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Handle reaction remove"""
        if payload.user_id == self.bot.user.id:
            return

        guild_id = payload.guild_id
        if not guild_id:
            return

        message_id = payload.message_id

        if payload.emoji.id:
            # Custom emoji
            emoji_str = f"<{'a' if payload.emoji.animated else ''}:{payload.emoji.name}:{payload.emoji.id}>"
        else:
            # Unicode emoji
            emoji_str = str(payload.emoji)

        logger.debug(f"Reaction remove: guild={guild_id}, message={message_id}, emoji={emoji_str}, user={payload.user_id}")

        if guild_id not in self.reaction_roles:
            return
        if message_id not in self.reaction_roles[guild_id]:
            return
        if emoji_str not in self.reaction_roles[guild_id][message_id]:
            return

        role_id = self.reaction_roles[guild_id][message_id][emoji_str]
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        role = guild.get_role(role_id)
        if not role:
            return

        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
                logger.debug(f"Fetched member {member.name} from API")
            except discord.NotFound:
                logger.warning(f"Member {payload.user_id} not found in guild {guild_id}")
                return
            except Exception as e:
                logger.error(f"Failed to fetch member {payload.user_id}: {e}")
                return

        if role not in member.roles:
            logger.debug(f"Member {member.name} doesn't have role {role.name}")
            return

        try:
            await member.remove_roles(role, reason="Reaction role removed")
            logger.info(f"✅ Removed role {role.name} from {member.name} in {guild.name}")
        except discord.Forbidden:
            logger.error(f"❌ Missing permissions to remove role {role.name} in {guild.name}")
        except Exception as e:
            logger.error(f"❌ Failed to remove role: {e}")
            import traceback
            logger.error(traceback.format_exc())

    @commands.hybrid_group(name='reactionrole', aliases=['rr'])
    @commands.has_permissions(manage_roles=True)
    async def reactionrole(self, ctx: commands.Context):
        """Manage reaction roles"""
        if ctx.invoked_subcommand is None:
            await ctx.send(await i18n.t(ctx, "reactionrole.help"))

    @reactionrole.command(name='add')
    @app_commands.describe(
        message_id="ID of the message to add reaction role to",
        emoji="Emoji to react with",
        role="Role to assign"
    )
    async def rr_add(self, ctx: commands.Context, message_id: str, emoji: str, role: discord.Role):
        """Add a reaction role to a message"""
        try:
            msg_id = int(message_id)
        except ValueError:
            await ctx.send(await i18n.t(ctx, "reactionrole.add.invalid_message_id"))
            return

        # Check if bot can manage this role
        if role >= ctx.guild.me.top_role:
            await ctx.send(await i18n.t(ctx, "reactionrole.add.role_too_high"))
            return

        message = None
        for channel in ctx.guild.text_channels:
            try:
                message = await channel.fetch_message(msg_id)
                break
            except (discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                continue

        if not message:
            await ctx.send(await i18n.t(ctx, "reactionrole.add.message_not_found"))
            return

        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as e:
            await ctx.send(await i18n.t(ctx, "reactionrole.add.invalid_emoji"))
            logger.error(f"Failed to add reaction {emoji}: {e}")
            return

        emoji_to_save = emoji

        await self.save_reaction_role(ctx.guild.id, msg_id, emoji_to_save, role.id)

        logger.info(f"Saved reaction role: guild={ctx.guild.id}, message={msg_id}, emoji={emoji_to_save}, role={role.name}")
        logger.info(f"Current cache for guild: {self.reaction_roles.get(ctx.guild.id, {})}")

        await self._update_message_embed(message, ctx.guild.id, msg_id)

        await ctx.send(await i18n.t(ctx, "reactionrole.add.success",
                                    emoji=emoji, role=role.mention, message_id=msg_id))

    @reactionrole.command(name='remove')
    @app_commands.describe(
        message_id="ID of the message",
        emoji="Emoji to remove (optional - removes all if not specified)"
    )
    async def rr_remove(self, ctx: commands.Context, message_id: str, emoji: str = None):
        """Remove a reaction role from a message"""
        try:
            msg_id = int(message_id)
        except ValueError:
            await ctx.send(await i18n.t(ctx, "reactionrole.remove.invalid_message_id"))
            return

        success = await self.remove_reaction_role(ctx.guild.id, msg_id, emoji)

        if success:
            message = None
            for channel in ctx.guild.text_channels:
                try:
                    message = await channel.fetch_message(msg_id)
                    break
                except (discord.NotFound, discord.Forbidden):
                    continue
                except Exception:
                    continue

            if message:
                await self._update_message_embed(message, ctx.guild.id, msg_id)

            if emoji:
                await ctx.send(await i18n.t(ctx, "reactionrole.remove.success_emoji",
                                           emoji=emoji, message_id=msg_id))
            else:
                await ctx.send(await i18n.t(ctx, "reactionrole.remove.success_all",
                                           message_id=msg_id))
        else:
            await ctx.send(await i18n.t(ctx, "reactionrole.remove.not_found"))

    @reactionrole.command(name='list')
    async def rr_list(self, ctx: commands.Context):
        """List all reaction roles in this server"""
        reaction_roles = self.get_reaction_roles(ctx.guild.id)

        if not reaction_roles:
            await ctx.send(await i18n.t(ctx, "reactionrole.list.empty"))
            return

        embed = discord.Embed(
            title=await i18n.t(ctx, "reactionrole.list.title"),
            color=discord.Color.blue()
        )

        for message_id, emoji_roles in reaction_roles.items():
            role_list = []
            for emoji, role_id in emoji_roles.items():
                role = ctx.guild.get_role(role_id)
                if role:
                    role_list.append(f"{emoji} → {role.mention}")
                else:
                    role_list.append(f"{emoji} → (Deleted Role)")

            embed.add_field(
                name=f"Message ID: {message_id}",
                value="\n".join(role_list) if role_list else "No roles",
                inline=False
            )

        await ctx.send(embed=embed)

    @reactionrole.command(name='create')
    @app_commands.describe(
        channel="Channel to send the message in",
        title="Title of the reaction role message",
        description="Description text"
    )
    async def rr_create(self, ctx: commands.Context, channel: discord.TextChannel,
                       title: str, *, description: str = None):
        """Create a new reaction role message"""
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color(0x555555)
        )
        embed.set_footer(text=ctx.guild.name,icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

        try:
            message = await channel.send(embed=embed)
            await ctx.send(await i18n.t(ctx, "reactionrole.create.success",
                                       channel=channel.mention, message_id=message.id))
        except discord.Forbidden:
            await ctx.send(await i18n.t(ctx, "reactionrole.create.no_permission"))
        except Exception as e:
            await ctx.send(await i18n.t(ctx, "reactionrole.create.failed", error=str(e)))

    @reactionrole.command(name='update')
    @app_commands.describe(
        message_id="ID of the message to update"
    )
    async def rr_update(self, ctx: commands.Context, message_id: str):
        """Update a reaction role message to show configured roles"""
        try:
            msg_id = int(message_id)
        except ValueError:
            await ctx.send(await i18n.t(ctx, "reactionrole.add.invalid_message_id"))
            return

        if ctx.guild.id not in self.reaction_roles or msg_id not in self.reaction_roles[ctx.guild.id]:
            await ctx.send("❌ No reaction roles configured for this message.")
            return

        message = None
        for channel in ctx.guild.text_channels:
            try:
                message = await channel.fetch_message(msg_id)
                break
            except (discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                continue

        if not message:
            await ctx.send(await i18n.t(ctx, "reactionrole.add.message_not_found"))
            return

        emoji_roles = self.reaction_roles[ctx.guild.id][msg_id]

        title = "Reaction Roles"
        custom_description = "React below to get roles!"

        if message.embeds:
            old_embed = message.embeds[0]
            if old_embed.title:
                title = old_embed.title

            if old_embed.description:
                custom_description = old_embed.description

        embed = discord.Embed(
            title=title,
            description=custom_description,
            color=discord.Color(0x555555)
        )

        for emoji, role_id in emoji_roles.items():
            role = ctx.guild.get_role(role_id)
            if role:
                embed.add_field(
                    name=emoji,
                    value=role.mention,
                    inline=True
                )
            else:
                embed.add_field(
                    name=emoji,
                    value="(Deleted)",
                    inline=True
                )

        embed.set_footer(text=ctx.guild.name,icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

        try:
            await message.edit(embed=embed)
            await ctx.send(f"✅ Updated message {msg_id} with {len(emoji_roles)} reaction roles!")
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to edit that message.")
        except Exception as e:
            await ctx.send(f"❌ Failed to update message: {str(e)}")

    @reactionrole.command(name='reload')
    @commands.has_permissions(administrator=True)
    async def rr_reload(self, ctx: commands.Context):
        """Reload reaction roles from a database (Admin only)"""
        await ctx.send("🔄 Reloading reaction roles from database...")

        try:
            old_count = sum(len(msgs) for msgs in self.reaction_roles.values())
            self.reaction_roles.clear()

            await self.load_reaction_roles()

            new_count = sum(len(msgs) for msgs in self.reaction_roles.values())

            embed = discord.Embed(
                title="✅ Reaction Roles Reloaded",
                description=f"Cleared {old_count} cached entries\nLoaded {new_count} entries from database",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            logger.info(f"Manually reloaded reaction roles for guild {ctx.guild.id}")
        except Exception as e:
            await ctx.send(f"❌ Failed to reload: {str(e)}")
            logger.error(f"Failed to reload reaction roles: {e}")

    @reactionrole.command(name='debug')
    @commands.has_permissions(administrator=True)
    async def rr_debug(self, ctx: commands.Context, message_id: str = None):
        """Debug reaction roles (Admin only)"""
        embed = discord.Embed(
            title="🔍 Reaction Roles Debug",
            color=discord.Color.blue()
        )

        # Show cache status
        guild_cache = self.reaction_roles.get(ctx.guild.id, {})
        embed.add_field(
            name="Cache Status",
            value=f"Messages tracked: {len(guild_cache)}\nGuilds in cache: {len(self.reaction_roles)}",
            inline=False
        )

        if message_id:
            try:
                msg_id = int(message_id)
                if msg_id in guild_cache:
                    emoji_roles = guild_cache[msg_id]
                    role_list = []
                    for emoji, role_id in emoji_roles.items():
                        role = ctx.guild.get_role(role_id)
                        role_list.append(f"`{emoji}` → {role.mention if role else f'(Deleted: {role_id})'}")

                    embed.add_field(
                        name=f"Message {msg_id}",
                        value="\n".join(role_list) if role_list else "No roles configured",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name=f"Message {msg_id}",
                        value="❌ Not found in cache",
                        inline=False
                    )
            except ValueError:
                embed.add_field(name="Error", value="Invalid message ID", inline=False)
        else:
            if guild_cache:
                for msg_id, emoji_roles in list(guild_cache.items())[:5]:  # Limit to 5
                    role_list = []
                    for emoji, role_id in emoji_roles.items():
                        role = ctx.guild.get_role(role_id)
                        role_list.append(f"`{emoji}` → {role.mention if role else f'(Deleted)'}")

                    embed.add_field(
                        name=f"Message {msg_id}",
                        value="\n".join(role_list[:3]) if role_list else "No roles",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="No Messages",
                    value="No reaction roles configured",
                    inline=False
                )

        bot_member = ctx.guild.me
        perms = bot_member.guild_permissions
        embed.add_field(
            name="Bot Permissions",
            value=f"Manage Roles: {'✅' if perms.manage_roles else '❌'}\n"
                  f"Add Reactions: {'✅' if perms.add_reactions else '❌'}\n"
                  f"Read Messages: {'✅' if perms.read_messages else '❌'}",
            inline=False
        )

        await ctx.send(embed=embed)

    async def cog_command_error(self, ctx, error):
        """Handle reaction role command errors"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(await i18n.t(ctx, "reactionrole.errors.no_permission"))
        else:
            logger.error(f"Reaction role command error: {error}")
            await ctx.send(await i18n.t(ctx, "reactionrole.errors.command_error", error=str(error)))


async def setup(bot):
    await bot.add_cog(ReactionRolesCog(bot))
