from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.models import GuildModuleModel, ModerationAppealModel, ModerationCaseModel
from config import Config
from services.moderation_actions import undo_case_action
from utils.i18n import i18n
from utils.moderation import normalize_casebook_config, select_crossed_escalation_rule


logger = logging.getLogger(__name__)


class ModuleDisabled(commands.CheckFailure):
    """Raised when a guild has disabled the moderation casebook."""


class Moderation(commands.Cog):
    """Permission-checked moderation commands backed by the casebook."""

    def __init__(self, bot):
        self.bot = bot
        self.case_model = ModerationCaseModel()
        self.appeal_model = ModerationAppealModel()
        self.guild_module_model = GuildModuleModel()
        # Case expiry is database-only; one worker per process is enough.
        if getattr(bot, 'is_main', True):
            self.expire_cases.start()

    def cog_unload(self) -> None:
        self.expire_cases.cancel()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        if not await self.guild_module_model.is_enabled(ctx.guild.id, 'moderation_casebook', default=True):
            raise ModuleDisabled()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, ModuleDisabled):
            await ctx.send(await i18n.t(ctx, 'errors.module_disabled'))
            return
        await self.bot.on_command_error(ctx, error)

    async def cog_before_invoke(self, ctx: commands.Context):
        """Automatically defer slash commands to prevent timeout."""
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info('%s cog loaded', self.__class__.__name__)

    @staticmethod
    def _snapshot(user: discord.abc.User) -> dict[str, str]:
        return {
            'user_id': str(user.id),
            'username': user.name,
            'display_name': getattr(user, 'display_name', user.name),
        }

    def _target_above_invoker(self, ctx: commands.Context, member: discord.Member) -> bool:
        """True when Discord's hierarchy rules should stop this moderator."""
        if ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator:
            return False
        if member.id == ctx.guild.owner_id:
            return True
        return member.top_role >= ctx.author.top_role

    async def _notify_member(
        self,
        member: discord.Member,
        action: str,
        reason: str,
        enabled: bool,
        *,
        case_id: Optional[int] = None,
    ) -> None:
        if not enabled:
            return
        try:
            locale = await i18n.get_guild_locale(member.guild.id) or 'en'
            message = i18n.get_text(
                'commands.casebook.dm_notification',
                locale,
                action=action,
                server=member.guild.name,
                reason=reason[:1000],
                case_id=case_id or '?',
            )
            view = None
            if case_id is not None:
                view = discord.ui.View()
                view.add_item(
                    discord.ui.Button(
                        label=i18n.get_text('commands.casebook.appeal_button', locale),
                        url=f'{Config.WEB_URL.rstrip("/")}/appeal?guild_id={member.guild.id}&case_id={case_id}',
                    )
                )
            await member.send(message, view=view)
        except (discord.Forbidden, discord.HTTPException):
            logger.info('Could not privately notify user %s about case action', member.id)

    async def _create_case(
        self,
        ctx: commands.Context,
        action: str,
        target: discord.abc.User,
        reason: str,
        *,
        notify: bool = True,
        duration_seconds: Optional[int] = None,
        expires_at: Any = None,
        internal_note: str = '',
        points: int = 0,
    ) -> Optional[dict[str, Any]]:
        reason = str(reason or '')[:1024]
        module = await self.guild_module_model.get_module(ctx.guild.id, 'moderation_casebook')
        config = normalize_casebook_config(module.get('config', {}) if module else {})
        try:
            case = await self.case_model.create_case(
                ctx.guild.id,
                action,
                target.id,
                ctx.author.id,
                reason=reason,
                duration_seconds=duration_seconds,
                expires_at=expires_at,
                target_snapshot=self._snapshot(target),
                moderator_snapshot=self._snapshot(ctx.author),
                internal_note=internal_note,
                points=points,
                retention_days=config['case_retention_days'],
            )
        except Exception:
            logger.exception('Could not create %s moderation case in guild %s', action, ctx.guild.id)
            return None

        await self._notify_member(target, action, reason, notify, case_id=case['case_id'])
        return case

    async def _apply_warning_escalation(
        self,
        ctx: commands.Context,
        member: discord.Member,
        warning_case: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        module = await self.guild_module_model.get_module(ctx.guild.id, 'moderation_casebook')
        config = normalize_casebook_config(module.get('config', {}) if module else {})
        decay_days = config['warning_decay_days']
        summary = await self.case_model.warning_summary(ctx.guild.id, member.id, decay_days=decay_days)
        current_points = summary['points']
        previous_points = max(0, current_points - warning_case.get('points', 0))
        rule = select_crossed_escalation_rule(config, previous_points, current_points)
        if not rule:
            return None
        reason = f'Automatic escalation after reaching {current_points} active warning points'
        try:
            if rule['action'] == 'timeout':
                duration = rule['duration_seconds']
                await member.timeout(timedelta(seconds=duration), reason=reason)
                return await self._create_case(
                    ctx,
                    'timeout',
                    member,
                    reason,
                    duration_seconds=duration,
                    expires_at=datetime.now(UTC) + timedelta(seconds=duration),
                    internal_note=f'Escalated from warning case #{warning_case["case_id"]}',
                )
            if rule['action'] == 'kick':
                await member.kick(reason=reason)
            else:
                await member.ban(reason=reason)
            return await self._create_case(
                ctx,
                rule['action'],
                member,
                reason,
                internal_note=f'Escalated from warning case #{warning_case["case_id"]}',
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception('Could not apply warning escalation in guild %s', ctx.guild.id)
            await ctx.send(await i18n.t(ctx, 'commands.casebook.escalation_failed'))
            return None

    @staticmethod
    def _case_embed(case: dict[str, Any]) -> discord.Embed:
        status_colors = {
            'active': discord.Color.orange(),
            'revoked': discord.Color.green(),
            'expired': discord.Color.dark_grey(),
        }
        embed = discord.Embed(
            title=f'Moderation Case #{case["case_id"]}',
            color=status_colors.get(case.get('status'), discord.Color.blurple()),
        )
        target_id = case.get('target_user_id', 'unknown')
        moderator_id = case.get('moderator_user_id', 'unknown')
        embed.add_field(name='Action', value=str(case.get('action', 'unknown')).title(), inline=True)
        embed.add_field(name='Status', value=str(case.get('status', 'unknown')).title(), inline=True)
        embed.add_field(name='Member', value=f'<@{target_id}> (`{target_id}`)', inline=False)
        embed.add_field(name='Moderator', value=f'<@{moderator_id}>', inline=True)
        embed.add_field(name='Reason', value=case.get('reason') or 'No reason provided', inline=False)
        if case.get('duration_seconds'):
            embed.add_field(name='Duration', value=f'{case["duration_seconds"]} seconds', inline=True)
        if case.get('points'):
            embed.add_field(name='Warning points', value=str(case['points']), inline=True)
        return embed

    @tasks.loop(minutes=1)
    async def expire_cases(self) -> None:
        await self.case_model.expire_due_cases()

    @expire_cases.before_loop
    async def before_expire_cases(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name='warn', description='Warn a member and create a moderation case')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(
        member='The member to warn',
        points='Warning points from 1 to 100',
        reason='The reason for this warning',
        notify='Send the member a private notification',
    )
    async def warn_member(
        self,
        ctx: commands.Context,
        member: discord.Member,
        points: int = 1,
        *,
        reason: str = 'No reason provided',
        notify: bool = True,
    ):
        if self._target_above_invoker(ctx, member):
            await ctx.send(await i18n.t(ctx, 'commands.casebook.hierarchy', member=member.display_name))
            return
        if points < 1 or points > 100:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.invalid_points'))
            return
        case = await self._create_case(ctx, 'warn', member, reason, notify=notify, points=points)
        if case:
            await ctx.send(embed=self._case_embed(case))
            escalated_case = await self._apply_warning_escalation(ctx, member, case)
            if escalated_case:
                await ctx.send(embed=self._case_embed(escalated_case))
        else:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))

    @commands.hybrid_command(name='note', description='Add a private moderation note about a member')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(member='The member to add a note about', reason='The internal note')
    async def note_member(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if self._target_above_invoker(ctx, member):
            await ctx.send(await i18n.t(ctx, 'commands.casebook.hierarchy', member=member.display_name))
            return
        case = await self._create_case(ctx, 'note', member, '', notify=False, internal_note=reason)
        if case:
            await ctx.send(embed=self._case_embed(case))
        else:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))

    @commands.hybrid_command(name='timeout', description='Timeout a member and create a moderation case')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(
        member='The member to timeout',
        duration='Timeout duration in seconds (60 to 2419200)',
        reason='The reason for this timeout',
        notify='Send the member a private notification',
    )
    async def timeout_member(
        self,
        ctx: commands.Context,
        member: discord.Member,
        duration: int,
        *,
        reason: str = 'No reason provided',
        notify: bool = True,
    ):
        if duration < 60 or duration > 28 * 24 * 60 * 60:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.invalid_timeout'))
            return
        if self._target_above_invoker(ctx, member):
            await ctx.send(await i18n.t(ctx, 'commands.casebook.hierarchy', member=member.display_name))
            return
        try:
            await member.timeout(timedelta(seconds=duration), reason=reason)
        except discord.Forbidden:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.bot_permission_error'))
            return
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.action_error'))
            return
        case = await self._create_case(
            ctx,
            'timeout',
            member,
            reason,
            notify=notify,
            duration_seconds=duration,
            expires_at=datetime.now(UTC) + timedelta(seconds=duration),
        )
        if case:
            await ctx.send(embed=self._case_embed(case))
        else:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))

    @commands.hybrid_command(name='kick', description='Kick a member and create a moderation case')
    @commands.has_permissions(kick_members=True)
    @app_commands.describe(
        member='The member to kick',
        reason='Reason for kicking',
        notify='Send the member a private notification',
    )
    async def kick_member(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = 'No reason provided',
        notify: bool = True,
    ):
        if self._target_above_invoker(ctx, member):
            await ctx.send(await i18n.t(ctx, 'commands.kick.hierarchy', member=member.display_name))
            return
        await self._notify_member(member, 'kick', reason, notify)
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await ctx.send(await i18n.t(ctx, 'commands.kick.no_permission'))
            return
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'commands.kick.error', error='Discord rejected the action'))
            return
        case = await self._create_case(ctx, 'kick', member, reason, notify=False)
        if case:
            await ctx.send(embed=self._case_embed(case))
        else:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))
        logger.info('%s kicked by %s for: %s', member, ctx.author, reason)

    @commands.hybrid_command(name='ban', description='Ban a member and create a moderation case')
    @commands.has_permissions(ban_members=True)
    @app_commands.describe(
        member='The member to ban',
        reason='Reason for banning',
        notify='Send the member a private notification',
    )
    async def ban_member(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = 'No reason provided',
        notify: bool = True,
    ):
        if self._target_above_invoker(ctx, member):
            await ctx.send(await i18n.t(ctx, 'commands.ban.hierarchy', member=member.display_name))
            return
        await self._notify_member(member, 'ban', reason, notify)
        try:
            await member.ban(reason=reason)
        except discord.Forbidden:
            await ctx.send(await i18n.t(ctx, 'commands.ban.no_permission'))
            return
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'commands.ban.error', error='Discord rejected the action'))
            return
        case = await self._create_case(ctx, 'ban', member, reason, notify=False)
        if case:
            await ctx.send(embed=self._case_embed(case))
        else:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))
        logger.info('%s banned by %s for: %s', member, ctx.author, reason)

    @commands.hybrid_command(name='unban', description='Unban a user and create a moderation case')
    @commands.has_permissions(ban_members=True)
    @app_commands.describe(user='The user to unban', reason='Reason for unbanning')
    async def unban_user(self, ctx: commands.Context, user: discord.User, *, reason: str = 'No reason provided'):
        try:
            await ctx.guild.unban(user, reason=reason)
        except discord.NotFound:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.user_not_banned'))
            return
        except discord.Forbidden:
            await ctx.send(await i18n.t(ctx, 'commands.ban.no_permission'))
            return
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.action_error'))
            return
        case = await self._create_case(ctx, 'unban', user, reason, notify=False)
        if case:
            await ctx.send(embed=self._case_embed(case))
        else:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))

    @commands.hybrid_command(name='case', description='View a moderation case by its server case number')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(case_id='The sequential moderation case number')
    async def get_case(self, ctx: commands.Context, case_id: int):
        case = await self.case_model.get_case(ctx.guild.id, case_id)
        if not case:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.not_found'))
            return
        await ctx.send(embed=self._case_embed(case))

    @commands.hybrid_command(name='revoke-case', description='Revoke an active moderation case')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(case_id='The sequential moderation case number', reason='Why this case is being revoked')
    async def revoke_case(self, ctx: commands.Context, case_id: int, *, reason: str = 'Case revoked by moderator'):
        case = await self.case_model.get_case(ctx.guild.id, case_id)
        if not case:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.not_found'))
            return
        if case.get('status') != 'active':
            await ctx.send(await i18n.t(ctx, 'commands.casebook.already_closed'))
            return
        try:
            await undo_case_action(ctx.guild, ctx.author, case, reason)
        except (PermissionError, ValueError, discord.HTTPException):
            await ctx.send(await i18n.t(ctx, 'commands.casebook.reversal_failed'))
            return
        updated = await self.case_model.update_case(
            ctx.guild.id,
            case_id,
            ctx.author.id,
            {'status': 'revoked', 'reason': reason},
            revision_action='revoke',
        )
        if updated:
            updated_case = await self.case_model.get_case(ctx.guild.id, case_id)
            await ctx.send(embed=self._case_embed(updated_case or case))
        else:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))

    @commands.hybrid_command(name='warnings', description='Show a member warning summary')
    @commands.has_permissions(moderate_members=True)
    @app_commands.describe(
        member='The member whose active warning points should be shown', decay_days='Decay window in days'
    )
    async def warning_summary(self, ctx: commands.Context, member: discord.Member, decay_days: int = 90):
        if decay_days < 1 or decay_days > 3650:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.invalid_decay'))
            return
        summary = await self.case_model.warning_summary(ctx.guild.id, member.id, decay_days=decay_days)
        embed = discord.Embed(
            title=await i18n.t(ctx, 'commands.casebook.warning_summary_title', member=member.display_name),
            description=await i18n.t(
                ctx,
                'commands.casebook.warning_summary_description',
                points=summary['points'],
                count=summary['warning_count'],
                days=summary['decay_days'],
            ),
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name='appeal', description='Appeal one of your active moderation cases')
    @commands.guild_only()
    @app_commands.describe(case_id='Your moderation case number', statement='Why staff should review this case')
    async def appeal_case(self, ctx: commands.Context, case_id: int, *, statement: str):
        if not await self.guild_module_model.is_enabled(ctx.guild.id, 'appeals', default=True):
            await ctx.send(await i18n.t(ctx, 'commands.casebook.appeals_disabled'))
            return
        module = await self.guild_module_model.get_module(ctx.guild.id, 'moderation_casebook')
        config = normalize_casebook_config(module.get('config', {}) if module else {})
        if not config['appeals_enabled']:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.appeals_disabled'))
            return
        case = await self.case_model.get_case(ctx.guild.id, case_id)
        if not case or case['target_user_id'] != ctx.author.id or case.get('status') != 'active':
            await ctx.send(await i18n.t(ctx, 'commands.casebook.appeal_not_found'))
            return
        appeal = await self.appeal_model.create_appeal(
            ctx.guild.id,
            case_id,
            ctx.author.id,
            statement,
            retention_days=config['case_retention_days'],
        )
        await ctx.send(await i18n.t(ctx, 'commands.casebook.appeal_submitted', case_id=appeal['case_id']))

    @commands.hybrid_command(name='review-appeal', description='Accept or decline a pending moderation appeal')
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(case_id='Moderation case number', decision='Accept or decline', reason='Staff review reason')
    async def review_appeal(self, ctx: commands.Context, case_id: int, decision: str, *, reason: str = ''):
        if not await self.guild_module_model.is_enabled(ctx.guild.id, 'appeals', default=True):
            await ctx.send(await i18n.t(ctx, 'commands.casebook.appeals_disabled'))
            return
        status = decision.lower().strip()
        if status not in {'accepted', 'declined'}:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.invalid_appeal_decision'))
            return
        if status == 'accepted':
            if not await self.appeal_model.get_pending_appeal(ctx.guild.id, case_id):
                await ctx.send(await i18n.t(ctx, 'commands.casebook.appeal_not_found'))
                return
            case = await self.case_model.get_case(ctx.guild.id, case_id)
            if case and case.get('status') == 'active':
                try:
                    await undo_case_action(ctx.guild, ctx.author, case, reason or 'Appeal accepted')
                except (PermissionError, ValueError, discord.HTTPException):
                    await ctx.send(await i18n.t(ctx, 'commands.casebook.reversal_failed'))
                    return
                saved = await self.case_model.update_case(
                    ctx.guild.id,
                    case_id,
                    ctx.author.id,
                    {'status': 'revoked'},
                    revision_action='appeal_accept',
                )
                if not saved:
                    await ctx.send(await i18n.t(ctx, 'commands.casebook.persistence_error'))
                    return
        updated = await self.appeal_model.review_appeal(ctx.guild.id, case_id, ctx.author.id, status, reason)
        if not updated:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.appeal_not_found'))
            return
        await ctx.send(await i18n.t(ctx, 'commands.casebook.appeal_reviewed', case_id=case_id, status=status))

    @commands.hybrid_command(
        name='case-cleanup', description='Delete recent messages from the member attached to a case'
    )
    @commands.has_permissions(manage_messages=True, moderate_members=True)
    @app_commands.describe(case_id='Moderation case number', amount='Maximum matching messages to delete (1-100)')
    async def cleanup_case_messages(self, ctx: commands.Context, case_id: int, amount: int = 25):
        if amount < 1 or amount > 100:
            await ctx.send(await i18n.t(ctx, 'commands.purge.invalid_amount'))
            return
        case = await self.case_model.get_case(ctx.guild.id, case_id)
        if not case:
            await ctx.send(await i18n.t(ctx, 'commands.casebook.not_found'))
            return
        matched = 0

        def belongs_to_target(message: discord.Message) -> bool:
            nonlocal matched
            if message.author.id == case['target_user_id'] and matched < amount:
                matched += 1
                return True
            return False

        try:
            deleted = await ctx.channel.purge(limit=min(2000, max(100, amount * 10)), check=belongs_to_target)
        except discord.Forbidden:
            await ctx.send(await i18n.t(ctx, 'commands.purge.no_permission'))
            return
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'commands.purge.error', error='Discord rejected the action'))
            return
        await self.case_model.record_revision(
            ctx.guild.id,
            case_id,
            ctx.author.id,
            'messages.cleanup',
            {'channel_id': ctx.channel.id, 'deleted_count': len(deleted)},
        )
        await ctx.send(await i18n.t(ctx, 'commands.casebook.cleanup_complete', count=len(deleted), case_id=case_id))

    @commands.hybrid_command(name='purge', description='Delete multiple messages')
    @commands.has_permissions(manage_messages=True)
    @app_commands.describe(amount='Number of messages to delete (1-100)')
    async def purge_messages(self, ctx: commands.Context, amount: int):
        """Delete multiple messages."""
        if amount < 1 or amount > 100:
            await ctx.send(await i18n.t(ctx, 'commands.purge.invalid_amount'))
            return
        try:
            deleted = await ctx.channel.purge(limit=amount + 1)
            embed = discord.Embed(
                title=await i18n.t(ctx, 'commands.purge.title', static_embed=True),
                description=await i18n.t(ctx, 'commands.purge.description', count=len(deleted) - 1, static_embed=True),
                color=discord.Color.green(),
            )
            embed.add_field(
                name=await i18n.t(ctx, 'commands.purge.moderator', static_embed=True),
                value=ctx.author.mention,
                inline=True,
            )
            await ctx.send(embed=embed, delete_after=5)
        except discord.Forbidden:
            await ctx.send(await i18n.t(ctx, 'commands.purge.no_permission'))
        except discord.HTTPException:
            await ctx.send(await i18n.t(ctx, 'commands.purge.error', error='Discord rejected the action'))


async def setup(bot):
    await bot.add_cog(Moderation(bot))
