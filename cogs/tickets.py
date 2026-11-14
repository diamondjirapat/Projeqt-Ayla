"""Persistent Discord ticket panels and staff workflow."""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.connection import db_manager
from database.models import GuildModuleModel, GuildPermissionModel, TicketModel, TicketPanelModel
from runtime.bots import is_primary_for_guild
from utils.permissions import has_capability, member_has_capability
from utils.tickets import safe_channel_name


logger = logging.getLogger(__name__)


def _staff_member(member: discord.Member, panel: dict[str, Any]) -> bool:
    if has_capability(member, 'manage_tickets'):
        return True
    configured = {int(role_id) for role_id in panel.get('staff_role_ids', [])}
    return bool(configured.intersection(role.id for role in member.roles))


async def _can_publish_ticket_panel(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member) or interaction.guild is None:
        return False
    if not await member_has_capability(member, 'manage_tickets'):
        raise app_commands.MissingPermissions(['manage_channels'])
    return True


class TicketFormModal(discord.ui.Modal):
    def __init__(self, panel: dict[str, Any]):
        super().__init__(title=str(panel.get('title') or 'Open ticket')[:45], timeout=900)
        self.panel = panel
        self.inputs: list[tuple[dict[str, Any], discord.ui.TextInput]] = []
        self.upload: Optional[discord.ui.FileUpload] = None
        for question in panel.get('questions', [])[:4]:
            field = discord.ui.TextInput(
                custom_id=f'ticket_question:{question["id"]}',
                style=discord.TextStyle.paragraph if question.get('paragraph') else discord.TextStyle.short,
                placeholder=question.get('placeholder') or None,
                required=bool(question.get('required', True)),
                max_length=2000,
            )
            self.add_item(discord.ui.Label(text=question['label'], component=field))
            self.inputs.append((question, field))
        if panel.get('evidence_enabled') and len(self.children) < 5:
            self.upload = discord.ui.FileUpload(
                custom_id='ticket_evidence',
                required=False,
                min_values=0,
                max_values=3,
            )
            self.add_item(discord.ui.Label(text='Optional evidence', component=self.upload))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog('Tickets')
        if not isinstance(cog, Tickets):
            await interaction.response.send_message('The ticket service is unavailable.', ephemeral=True)
            return
        answers = [
            {'question_id': question['id'], 'question': question['label'], 'answer': field.value.strip()}
            for question, field in self.inputs
        ]
        evidence = [
            {
                'filename': attachment.filename[:260],
                'url': attachment.url,
                'content_type': attachment.content_type,
                'size': attachment.size,
            }
            for attachment in (self.upload.values if self.upload else [])
        ]
        await cog.create_from_interaction(interaction, self.panel, answers, evidence)


class TicketPanelView(discord.ui.View):
    def __init__(self, panel: Optional[dict[str, Any]] = None):
        super().__init__(timeout=None)
        if panel:
            button = next(item for item in self.children if getattr(item, 'custom_id', None) == 'ticket:create')
            button.label = panel.get('button_label') or 'Open ticket'
            button.emoji = panel.get('button_emoji') or '🎫'

    @discord.ui.button(label='Open ticket', emoji='🎫', style=discord.ButtonStyle.primary, custom_id='ticket:create')
    async def create_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or not interaction.message:
            await interaction.response.send_message('Tickets can only be opened in a server.', ephemeral=True)
            return
        if not await GuildModuleModel().is_enabled(interaction.guild.id, 'tickets', default=True):
            await interaction.response.send_message('The ticket module is disabled.', ephemeral=True)
            return
        panel = await TicketPanelModel().get_by_message(interaction.guild.id, interaction.message.id)
        if not panel:
            await interaction.response.send_message('This ticket panel is no longer active.', ephemeral=True)
            return
        allowed, reason = await TicketModel().can_open(interaction.guild.id, interaction.user.id, panel)
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        if panel.get('questions') or panel.get('evidence_enabled'):
            await interaction.response.send_modal(TicketFormModal(panel))
            return
        cog = interaction.client.get_cog('Tickets')
        if isinstance(cog, Tickets):
            await cog.create_from_interaction(interaction, panel, [], [])
        else:
            await interaction.response.send_message('The ticket service is unavailable.', ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _cog(self, interaction: discord.Interaction) -> Optional['Tickets']:
        cog = interaction.client.get_cog('Tickets')
        if not isinstance(cog, Tickets):
            await interaction.response.send_message('The ticket service is unavailable.', ephemeral=True)
            return None
        return cog

    @discord.ui.button(label='Claim', emoji='📌', style=discord.ButtonStyle.primary, custom_id='ticket:claim')
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.control_action(interaction, 'claim')

    @discord.ui.button(label='Close', emoji='🔒', style=discord.ButtonStyle.danger, custom_id='ticket:close')
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.control_action(interaction, 'close')

    @discord.ui.button(label='Reopen', emoji='🔓', style=discord.ButtonStyle.secondary, custom_id='ticket:reopen')
    async def reopen(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.control_action(interaction, 'reopen')


class TicketRatingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for rating in range(1, 6):
            button = discord.ui.Button(
                label=str(rating),
                emoji='⭐',
                style=discord.ButtonStyle.secondary,
                custom_id=f'ticket:rate:{rating}',
            )
            button.callback = self._rating_callback
            self.add_item(button)

    async def _rating_callback(self, interaction: discord.Interaction) -> None:
        rating = int(str(interaction.data.get('custom_id', '')).rsplit(':', 1)[-1])
        if not interaction.message:
            await interaction.response.send_message('This rating request is invalid.', ephemeral=True)
            return
        saved = await TicketModel().rate_ticket(interaction.message.id, interaction.user.id, rating)
        if not saved:
            await interaction.response.send_message(
                'This ticket was already rated or does not belong to you.',
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(content=f'Thank you for rating this ticket {rating}/5.', view=None)


class Tickets(commands.Cog):
    ticket = app_commands.Group(name='ticket', description='Manage support tickets')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ticket_model = TicketModel()
        self.panel_model = TicketPanelModel()
        self.module_model = GuildModuleModel()
        self.permission_model = GuildPermissionModel()
        self.inactivity_worker.start()

    def cog_unload(self) -> None:
        self.inactivity_worker.cancel()

    async def _panel_for_ticket(self, ticket: dict[str, Any]) -> Optional[dict[str, Any]]:
        return await self.panel_model.get_panel(ticket['guild_id'], ticket['panel_id'])

    async def _staff_allowed(self, interaction: discord.Interaction, ticket: dict[str, Any]) -> bool:
        panel = await self._panel_for_ticket(ticket)
        return (
            isinstance(interaction.user, discord.Member)
            and bool(panel)
            and await self._is_staff_member(interaction.user, panel)
        )

    async def _is_staff_member(self, member: discord.Member, panel: dict[str, Any]) -> bool:
        if _staff_member(member, panel):
            return True
        grants = await self.permission_model.get_role_grants(member.guild.id)
        return has_capability(member, 'manage_tickets', grants)

    async def create_from_interaction(
        self,
        interaction: discord.Interaction,
        panel: dict[str, Any],
        answers: list[dict[str, str]],
        evidence: list[dict[str, Any]],
    ) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('Tickets can only be opened in a server.', ephemeral=True)
            return
        allowed, reason = await self.ticket_model.can_open(interaction.guild.id, interaction.user.id, panel)
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        bot_member = guild.me
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True,
            ),
        }
        if not panel.get('anonymous'):
            overwrites[interaction.user] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                attach_files=True,
                read_message_history=True,
            )
        for role_id in panel.get('staff_role_ids', []):
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                    read_message_history=True,
                )
        grants = await self.permission_model.get_role_grants(guild.id)
        for role_id, capabilities in grants.items():
            role = guild.get_role(int(role_id))
            if role and 'manage_tickets' in capabilities:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                    read_message_history=True,
                )
        category = guild.get_channel(panel.get('discord_category_id')) if panel.get('discord_category_id') else None
        if not isinstance(category, discord.CategoryChannel):
            category = None
        placeholder_id = await self.ticket_model.counter_collection.find_one({'guild_id': guild.id}) or {}
        next_id = int(placeholder_id.get('next_ticket_id', 0)) + 1
        channel = await guild.create_text_channel(
            safe_channel_name(
                next_id,
                panel.get('category_name', 'ticket'),
                '' if panel.get('anonymous') else interaction.user.name,
            ),
            category=category,
            overwrites=overwrites,
            topic='Ayla managed support ticket',
            reason=f'Ticket opened by {interaction.user} ({interaction.user.id})',
        )
        try:
            ticket = await self.ticket_model.create_ticket(
                guild.id,
                panel,
                interaction.user.id,
                channel.id,
                answers=answers,
                evidence=evidence,
            )
        except Exception:
            await channel.delete(reason='Ticket persistence failed')
            raise
        await channel.edit(
            name=safe_channel_name(
                ticket['ticket_id'],
                panel.get('category_name', 'ticket'),
                '' if panel.get('anonymous') else interaction.user.name,
            ),
        )
        embed = discord.Embed(
            title=f'Ticket #{ticket["ticket_id"]} · {panel.get("category_name", "Support")}',
            description=(
                'A staff member will respond here.' if not panel.get('anonymous') else 'Anonymous report received.'
            ),
            color=discord.Color.blurple(),
        )
        if not panel.get('anonymous'):
            embed.add_field(name='Member', value=interaction.user.mention, inline=False)
        for answer in answers:
            embed.add_field(name=answer['question'][:256], value=answer['answer'][:1024] or '—', inline=False)
        if evidence:
            embed.add_field(name='Evidence', value='\n'.join(item['url'] for item in evidence)[:1024], inline=False)
        await channel.send(embed=embed, view=TicketControlView())
        await self.ticket_model.record_event(guild.id, ticket['ticket_id'], 'created', interaction.user.id)
        if panel.get('anonymous'):
            await interaction.followup.send(
                f'Your anonymous report was submitted as ticket #{ticket["ticket_id"]}. Your identity is restricted.',
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f'Your ticket is ready: {channel.mention}', ephemeral=True)

    async def control_action(self, interaction: discord.Interaction, action: str) -> None:
        if not interaction.guild or not interaction.channel:
            return
        ticket = await self.ticket_model.get_by_channel(interaction.guild.id, interaction.channel.id)
        if not ticket:
            await interaction.response.send_message('This channel is not an Ayla ticket.', ephemeral=True)
            return
        if not await self._staff_allowed(interaction, ticket):
            await interaction.response.send_message('You do not have permission to manage tickets.', ephemeral=True)
            return
        if action == 'claim':
            await self.ticket_model.update_ticket(
                interaction.guild.id,
                ticket['ticket_id'],
                {'assigned_staff_id': interaction.user.id},
            )
            await self.ticket_model.record_event(
                interaction.guild.id,
                ticket['ticket_id'],
                'claimed',
                interaction.user.id,
            )
            await interaction.response.send_message(f'{interaction.user.mention} claimed this ticket.')
            return
        if action == 'close':
            await interaction.response.defer()
            closed = await self.close_ticket(interaction.guild, ticket, interaction.user.id)
            if not closed:
                await interaction.followup.send('This ticket is already closed.', ephemeral=True)
            return
        if action == 'reopen':
            reopened = await self.ticket_model.reopen_ticket(
                interaction.guild.id,
                ticket['ticket_id'],
                interaction.user.id,
            )
            if not reopened:
                await interaction.response.send_message('Only closed tickets can be reopened.', ephemeral=True)
                return
            if not ticket.get('anonymous') and interaction.guild.get_member(ticket['member_user_id']):
                await interaction.channel.set_permissions(
                    interaction.guild.get_member(ticket['member_user_id']),
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    reason=f'Ticket #{ticket["ticket_id"]} reopened',
                )
            await self.ticket_model.record_event(
                interaction.guild.id,
                ticket['ticket_id'],
                'reopened',
                interaction.user.id,
            )
            await interaction.response.send_message(f'Ticket #{ticket["ticket_id"]} reopened.')

    async def close_ticket(
        self,
        guild: discord.Guild,
        ticket: dict[str, Any],
        actor_user_id: int,
    ) -> Optional[dict[str, Any]]:
        if ticket.get('status') != 'open':
            return None
        # Claim the close atomically before building the transcript, so concurrent
        # closers (several bots' workers, a command, the API) cannot interleave it.
        closed = await self.ticket_model.close_ticket(guild.id, ticket['ticket_id'], actor_user_id)
        if not closed:
            return None
        expires_at = closed.get('expires_at')
        channel = guild.get_channel(ticket['channel_id'])
        transcript_messages = db_manager.get_collection('ticket_transcript_messages')
        await transcript_messages.delete_many({'guild_id': guild.id, 'ticket_id': ticket['ticket_id']})
        message_count = 0
        batch: list[dict[str, Any]] = []
        if isinstance(channel, discord.TextChannel):
            async for message in channel.history(limit=None, oldest_first=True):
                batch.append(
                    {
                        'guild_id': guild.id,
                        'ticket_id': ticket['ticket_id'],
                        'sequence': message_count,
                        'message_id': str(message.id),
                        'author_id': str(message.author.id),
                        'author_name': str(message.author),
                        'content': message.clean_content,
                        'attachments': [attachment.url for attachment in message.attachments],
                        'created_at': message.created_at.isoformat(),
                        'expires_at': expires_at,
                    }
                )
                message_count += 1
                if len(batch) >= 500:
                    await transcript_messages.insert_many(batch)
                    batch = []
        if batch:
            await transcript_messages.insert_many(batch)
        transcript_id = f'{guild.id}-{ticket["ticket_id"]}'
        transcript = {
            'transcript_id': transcript_id,
            'guild_id': guild.id,
            'ticket_id': ticket['ticket_id'],
            'message_count': message_count,
            'created_at': datetime.now(UTC),
            'expires_at': expires_at,
        }
        await db_manager.get_collection('ticket_transcripts').replace_one(
            {'guild_id': guild.id, 'ticket_id': ticket['ticket_id']},
            transcript,
            upsert=True,
        )
        transcript_ref = {'transcript_id': transcript_id, 'message_count': message_count}
        await self.ticket_model.update_ticket(guild.id, ticket['ticket_id'], {'transcript': transcript_ref})
        closed['transcript'] = transcript_ref
        await self.ticket_model.record_event(guild.id, ticket['ticket_id'], 'closed', actor_user_id)
        if isinstance(channel, discord.TextChannel):
            member = guild.get_member(ticket['member_user_id'])
            if member and not ticket.get('anonymous'):
                await channel.set_permissions(member, view_channel=True, send_messages=False, read_message_history=True)
            await channel.send(
                f'Ticket #{ticket["ticket_id"]} closed. The transcript is available to authorized staff.',
            )
        user = self.bot.get_user(ticket['member_user_id'])
        if user is None:
            try:
                user = await self.bot.fetch_user(ticket['member_user_id'])
            except discord.HTTPException:
                user = None
        if user:
            try:
                rating_message = await user.send(
                    f'How was your support experience for ticket #{ticket["ticket_id"]}?',
                    view=TicketRatingView(),
                )
                await self.ticket_model.update_ticket(
                    guild.id,
                    ticket['ticket_id'],
                    {'rating_message_id': rating_message.id},
                )
            except discord.HTTPException:
                pass
        return closed

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        ticket = await self.ticket_model.get_message_ticket(message.guild.id, message.channel.id)
        if not ticket or ticket.get('status') != 'open':
            return
        panel = await self._panel_for_ticket(ticket)
        is_staff = (
            isinstance(message.author, discord.Member)
            and bool(panel)
            and await self._is_staff_member(message.author, panel)
        )
        evidence = [
            {
                'filename': item.filename[:260],
                'url': item.url,
                'content_type': item.content_type,
                'size': item.size,
            }
            for item in message.attachments
        ]
        await self.ticket_model.record_activity(
            message.guild.id,
            message.channel.id,
            message.author.id,
            is_staff=is_staff,
            evidence=evidence,
        )

    @ticket.command(name='panel', description='Publish a basic persistent ticket panel')
    @app_commands.checks.has_permissions(manage_channels=True)
    # interaction.permissions is channel-scoped (a channel overwrite can grant Manage Channel), so also
    # require guild-wide ticket authority before publishing a panel.
    @app_commands.check(_can_publish_ticket_panel)
    async def panel_command(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = 'Support tickets',
        category: str = 'Support',
        anonymous: bool = False,
    ) -> None:
        if not interaction.guild:
            return
        panel = await self.panel_model.create_panel(
            interaction.guild.id,
            interaction.user.id,
            {'name': title, 'title': title, 'category_name': category, 'anonymous': anonymous},
        )
        embed = discord.Embed(title=panel['title'], description=panel['description'], color=discord.Color.blurple())
        message = await channel.send(embed=embed, view=TicketPanelView(panel))
        await self.panel_model.bind_message(interaction.guild.id, panel['panel_id'], channel.id, message.id)
        await interaction.response.send_message(f'Ticket panel published in {channel.mention}.', ephemeral=True)

    @ticket.command(name='priority', description='Set this ticket priority')
    @app_commands.choices(
        priority=[
            app_commands.Choice(name='Low', value='low'),
            app_commands.Choice(name='Normal', value='normal'),
            app_commands.Choice(name='High', value='high'),
            app_commands.Choice(name='Urgent', value='urgent'),
        ]
    )
    async def priority_command(self, interaction: discord.Interaction, priority: app_commands.Choice[str]) -> None:
        await self._simple_staff_update(interaction, {'priority': priority.value}, 'priority_changed')

    @ticket.command(name='transfer', description='Transfer this ticket to another staff member')
    async def transfer_command(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not interaction.guild or not interaction.channel:
            return
        ticket = await self.ticket_model.get_by_channel(interaction.guild.id, interaction.channel.id)
        panel = await self._panel_for_ticket(ticket) if ticket else None
        if not panel or not await self._is_staff_member(member, panel):
            await interaction.response.send_message('The assignee must be a ticket staff member.', ephemeral=True)
            return
        await self._simple_staff_update(interaction, {'assigned_staff_id': member.id}, 'transferred')

    @ticket.command(name='rename', description='Rename this ticket channel')
    async def rename_command(self, interaction: discord.Interaction, name: str) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        ticket = await self.ticket_model.get_by_channel(interaction.guild.id, interaction.channel.id)
        if not ticket or not await self._staff_allowed(interaction, ticket):
            await interaction.response.send_message('You do not have permission to manage this ticket.', ephemeral=True)
            return
        await interaction.channel.edit(name=safe_channel_name(ticket['ticket_id'], name))
        await self.ticket_model.record_event(interaction.guild.id, ticket['ticket_id'], 'renamed', interaction.user.id)
        await interaction.response.send_message('Ticket channel renamed.', ephemeral=True)

    @ticket.command(name='add-member', description='Add a member to this ticket')
    async def add_member_command(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._change_member(interaction, member, add=True)

    @ticket.command(name='remove-member', description='Remove a member from this ticket')
    async def remove_member_command(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._change_member(interaction, member, add=False)

    @ticket.command(name='note', description='Add a private staff note')
    async def note_command(self, interaction: discord.Interaction, note: str) -> None:
        if not interaction.guild or not interaction.channel:
            return
        ticket = await self.ticket_model.get_by_channel(interaction.guild.id, interaction.channel.id)
        if not ticket or not await self._staff_allowed(interaction, ticket):
            await interaction.response.send_message('You do not have permission to manage this ticket.', ephemeral=True)
            return
        await self.ticket_model.add_note(interaction.guild.id, ticket['ticket_id'], interaction.user.id, note)
        await interaction.response.send_message(
            'Private note saved. It is only visible in the staff dashboard.',
            ephemeral=True,
        )

    @ticket.command(name='delete', description='Delete a closed ticket channel and retain its transcript')
    async def delete_command(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        ticket = await self.ticket_model.get_by_channel(interaction.guild.id, interaction.channel.id)
        if not ticket or ticket.get('status') != 'closed' or not await self._staff_allowed(interaction, ticket):
            await interaction.response.send_message('Only authorized staff can delete a closed ticket.', ephemeral=True)
            return
        await interaction.response.send_message(
            'Deleting this channel; the protected transcript remains available.',
            ephemeral=True,
        )
        await self.ticket_model.update_ticket(interaction.guild.id, ticket['ticket_id'], {'status': 'deleted'})
        await self.ticket_model.record_event(interaction.guild.id, ticket['ticket_id'], 'deleted', interaction.user.id)
        await interaction.channel.delete(reason=f'Ticket #{ticket["ticket_id"]} deleted by {interaction.user}')

    async def _simple_staff_update(
        self,
        interaction: discord.Interaction,
        values: dict[str, Any],
        event_type: str,
    ) -> None:
        if not interaction.guild or not interaction.channel:
            return
        ticket = await self.ticket_model.get_by_channel(interaction.guild.id, interaction.channel.id)
        if not ticket or not await self._staff_allowed(interaction, ticket):
            await interaction.response.send_message('You do not have permission to manage this ticket.', ephemeral=True)
            return
        await self.ticket_model.update_ticket(interaction.guild.id, ticket['ticket_id'], values)
        await self.ticket_model.record_event(
            interaction.guild.id,
            ticket['ticket_id'],
            event_type,
            interaction.user.id,
            values,
        )
        await interaction.response.send_message('Ticket updated.', ephemeral=True)

    async def _change_member(self, interaction: discord.Interaction, member: discord.Member, *, add: bool) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        ticket = await self.ticket_model.get_by_channel(interaction.guild.id, interaction.channel.id)
        if not ticket or not await self._staff_allowed(interaction, ticket):
            await interaction.response.send_message('You do not have permission to manage this ticket.', ephemeral=True)
            return
        participants = {int(value) for value in ticket.get('participant_user_ids', [])}
        if add:
            participants.add(member.id)
            await interaction.channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
        else:
            participants.discard(member.id)
            await interaction.channel.set_permissions(member, overwrite=None)
        await self.ticket_model.update_ticket(
            interaction.guild.id,
            ticket['ticket_id'],
            {'participant_user_ids': sorted(participants)},
        )
        await self.ticket_model.record_event(
            interaction.guild.id,
            ticket['ticket_id'],
            'member_added' if add else 'member_removed',
            interaction.user.id,
            {'member_user_id': member.id},
        )
        await interaction.response.send_message('Ticket member access updated.', ephemeral=True)

    @tasks.loop(minutes=5)
    async def inactivity_worker(self) -> None:
        now = datetime.now(UTC)
        # Each bot runs this worker; handle only guilds this bot is primary for,
        # so reminders are never claimed by a bot that cannot post them.
        guild_ids = [guild.id for guild in self.bot.guilds if is_primary_for_guild(self.bot, guild.id)]
        if not guild_ids:
            return
        cursor = self.ticket_model.collection.find({'status': 'open', 'guild_id': {'$in': guild_ids}})
        panel_cache: dict = {}
        async for ticket in cursor:
            panel_id = ticket.get('panel_id')
            panel = panel_cache.get(panel_id)
            if panel is None:
                panel = await self._panel_for_ticket(ticket)
                panel_cache[panel_id] = panel
            if not panel:
                continue
            last_activity = ticket.get('last_activity_at') or ticket.get('created_at')
            close_hours = int(panel.get('inactivity_close_hours', 0))
            reminder_hours = int(panel.get('inactivity_reminder_hours', 0))
            guild = self.bot.get_guild(ticket['guild_id'])
            channel = guild.get_channel(ticket['channel_id']) if guild else None
            if close_hours and last_activity <= now - timedelta(hours=close_hours):
                if guild:
                    await self.close_ticket(guild, ticket, self.bot.user.id)
            elif (
                reminder_hours
                and not ticket.get('reminder_sent_at')
                and last_activity <= now - timedelta(hours=reminder_hours)
            ):
                claimed = await self.ticket_model.collection.update_one(
                    {'_id': ticket['_id'], 'status': 'open', 'reminder_sent_at': None},
                    {'$set': {'reminder_sent_at': now}},
                )
                if claimed.modified_count and isinstance(channel, discord.TextChannel):
                    await channel.send('This ticket is inactive and will close automatically if there is no response.')

    @inactivity_worker.before_loop
    async def before_inactivity_worker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
