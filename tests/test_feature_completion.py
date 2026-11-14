from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from fastapi.testclient import TestClient

from cogs import internal_api
from database.scheduler_models import ScheduledItemModel
from database.tickets import TicketPanelModel
from services.moderation_actions import undo_case_action


class ReversalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.expiry = datetime.now(UTC) + timedelta(hours=1)
        self.target = SimpleNamespace(id=7, top_role=1, timed_out_until=self.expiry, timeout=AsyncMock())
        self.actor = SimpleNamespace(
            id=9,
            top_role=3,
            guild_permissions=SimpleNamespace(administrator=False, ban_members=True, moderate_members=True),
        )
        self.guild = SimpleNamespace(
            owner_id=99,
            me=SimpleNamespace(top_role=5),
            get_member=lambda _: self.target,
            unban=AsyncMock(),
            fetch_member=AsyncMock(),
        )

    async def test_ban_reversal_and_already_unbanned_retry(self):
        case = {'action': 'ban', 'target_user_id': 7}
        await undo_case_action(self.guild, self.actor, case, 'Accepted')
        self.assertEqual(self.guild.unban.await_args.args[0].id, 7)
        self.guild.unban.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='missing'), 'Unknown ban')
        await undo_case_action(self.guild, self.actor, case, 'Retry')

    async def test_timeout_is_removed_but_a_newer_timeout_is_preserved(self):
        case = {'action': 'timeout', 'target_user_id': 7, 'expires_at': self.expiry}
        await undo_case_action(self.guild, self.actor, case, 'Accepted')
        self.target.timeout.assert_awaited_once_with(None, reason='Accepted')
        self.target.timeout.reset_mock()
        self.target.timed_out_until += timedelta(hours=2)
        with self.assertRaisesRegex(ValueError, 'newer timeout'):
            await undo_case_action(self.guild, self.actor, case, 'Old case')
        self.target.timeout.assert_not_awaited()

    async def test_native_permissions_and_hierarchy_are_required(self):
        self.actor.guild_permissions.ban_members = False
        with self.assertRaises(PermissionError):
            await undo_case_action(self.guild, self.actor, {'action': 'ban', 'target_user_id': 7}, 'Review')
        self.guild.unban.assert_not_awaited()
        self.target.top_role = self.actor.top_role
        with self.assertRaises(PermissionError):
            await undo_case_action(
                self.guild,
                self.actor,
                {
                    'action': 'timeout',
                    'target_user_id': 7,
                    'expires_at': self.expiry,
                },
                'Review',
            )
        self.target.timeout.assert_not_awaited()

    async def test_deleted_panels_keep_settings_for_existing_ticket_controls(self):
        model = TicketPanelModel()
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=SimpleNamespace(matched_count=1))
        with patch.object(type(model), 'collection', new_callable=unittest.mock.PropertyMock, return_value=collection):
            self.assertTrue(await model.delete_panel(123, 'support'))
        values = collection.update_one.await_args.args[1]['$set']
        self.assertTrue(values['deleted'])
        self.assertFalse(values['enabled'])
        collection.delete_one.assert_not_called()

    async def test_calendar_query_includes_series_starting_before_visible_month(self):
        model = ScheduledItemModel()
        collection = MagicMock()
        cursor = collection.find.return_value.sort.return_value.limit.return_value
        cursor.to_list = AsyncMock(return_value=[])
        start = datetime(2026, 12, 1, tzinfo=UTC)
        end = datetime(2027, 1, 1, tzinfo=UTC)
        with patch.object(type(model), 'collection', new_callable=unittest.mock.PropertyMock, return_value=collection):
            await model.list_guild_items(123, start=start, end=end)
        query = collection.find.call_args.args[0]
        self.assertEqual(query['guild_id'], 123)
        series = query['$or'][1]
        self.assertEqual(series['next_run_at'], {'$lte': end})
        self.assertNotIn('done', series['status']['$in'])


class FeatureCompletionRoutes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(internal_api, '_INTERNAL_KEY', 'test-key'))
        self.headers = {'X-Internal-Key': 'test-key', 'X-User-Id': '9'}
        self.client = TestClient(internal_api.internal_app)
        self.actor = SimpleNamespace(
            id=9,
            name='Moderator',
            display_name='Moderator',
            top_role=3,
            guild_permissions=SimpleNamespace(administrator=False, ban_members=True, moderate_members=True),
        )
        self.guild = MagicMock(id=123, name='Server', icon=None)
        self.guild.id = 123
        self.guild.name = 'Server'
        self.guild.get_member.return_value = self.actor
        self.guild.unban = AsyncMock()
        self.find = self.stack.enter_context(patch.object(internal_api, '_find_guild', return_value=self.guild))
        self.permission = self.stack.enter_context(
            patch.object(
                internal_api,
                'check_user_guild_capability',
                AsyncMock(return_value=True),
            )
        )
        self.stack.enter_context(
            patch.object(internal_api.guild_module_model, 'get_module', AsyncMock(return_value=None))
        )
        self.stack.enter_context(
            patch.object(internal_api.guild_module_model, 'is_enabled', AsyncMock(return_value=True))
        )
        self.case = {'case_id': 1, 'guild_id': 123, 'action': 'ban', 'target_user_id': 7, 'status': 'active'}
        self.stack.enter_context(
            patch.object(internal_api.moderation_case_model, 'get_case', AsyncMock(return_value=self.case))
        )
        self.save = self.stack.enter_context(
            patch.object(
                internal_api.moderation_case_model,
                'update_case',
                AsyncMock(return_value=True),
            )
        )
        self.stack.enter_context(
            patch.object(
                internal_api.moderation_appeal_model,
                'get_pending_appeal',
                AsyncMock(return_value={'case_id': 1}),
            )
        )
        self.review = self.stack.enter_context(
            patch.object(
                internal_api.moderation_appeal_model,
                'review_appeal',
                AsyncMock(return_value=True),
            )
        )

    def accept(self):
        return self.client.patch(
            '/internal/guild/moderation/appeals/1',
            headers=self.headers,
            json={'guild_id': 123, 'status': 'accepted', 'reason': 'Overturned'},
        )

    def test_acceptance_reverses_discord_before_persisting_review(self):
        order = []
        self.guild.unban.side_effect = lambda *a, **kw: order.append('discord')
        self.save.side_effect = lambda *a, **kw: order.append('case') or True
        self.review.side_effect = lambda *a, **kw: order.append('review') or True
        self.assertEqual(self.accept().status_code, 200)
        self.assertEqual(order, ['discord', 'case', 'review'])

    def test_failed_discord_reversal_leaves_appeal_retryable(self):
        self.guild.unban.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='denied'), 'Denied')
        self.assertEqual(self.accept().status_code, 403)
        self.save.assert_not_awaited()
        self.review.assert_not_awaited()

    def test_acceptance_cannot_bypass_native_ban_permission(self):
        self.actor.guild_permissions.ban_members = False
        self.assertEqual(self.accept().status_code, 403)
        self.guild.unban.assert_not_awaited()
        self.review.assert_not_awaited()

    def test_failed_case_save_does_not_finalize_appeal(self):
        self.save.return_value = False
        self.assertEqual(self.accept().status_code, 409)
        self.review.assert_not_awaited()

    def test_revocation_reverses_the_sanction(self):
        response = self.client.patch(
            '/internal/guild/moderation/cases/1', headers=self.headers, json={'guild_id': 123, 'status': 'revoked'}
        )
        self.assertEqual(response.status_code, 200)
        self.guild.unban.assert_awaited_once()

    def test_unban_accepts_a_banned_user_outside_the_member_list(self):
        user = SimpleNamespace(id=7, name='Former member', display_name='Former member')
        self.guild.fetch_ban = AsyncMock(return_value=SimpleNamespace(user=user))
        create = self.stack.enter_context(
            patch.object(
                internal_api.moderation_case_model,
                'create_case',
                AsyncMock(return_value={**self.case, 'action': 'unban'}),
            )
        )
        response = self.client.post(
            '/internal/guild/moderation/cases',
            headers=self.headers,
            json={'guild_id': 123, 'target_user_id': 7, 'action': 'unban', 'reason': 'Reviewed'},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.guild.unban.assert_awaited_once_with(user, reason='Reviewed')
        self.assertEqual(create.await_args.args[1], 'unban')

    def test_delegated_capabilities_include_server_in_picker_and_dashboard_access(self):
        self.stack.enter_context(patch.object(internal_api, 'get_all_guilds', return_value=[self.guild]))

        async def capability(_user, _guild, cap):
            return cap == 'manage_tickets'

        self.permission.side_effect = capability
        response = self.client.get('/internal/guilds/manageable', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]['capabilities'], ['manage_tickets'])
        response = self.client.get('/internal/auth/check-admin?user_id=9', headers=self.headers)
        self.assertTrue(response.json()['is_admin'])

    def test_no_capabilities_do_not_expose_server(self):
        self.stack.enter_context(patch.object(internal_api, 'get_all_guilds', return_value=[self.guild]))
        self.permission.return_value = False
        self.assertEqual(self.client.get('/internal/guilds/manageable', headers=self.headers).json(), [])

    def test_cleanup_is_bounded_to_target_member_and_records_count(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 555
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            manage_messages=True,
        )

        async def purge(*, limit, check):
            self.assertEqual(limit, 100)
            messages = [SimpleNamespace(author=SimpleNamespace(id=uid)) for uid in [7, 8, 7, 7]]
            return [message for message in messages if check(message)]

        channel.purge = AsyncMock(side_effect=purge)
        self.guild.get_channel.return_value = channel
        revision = self.stack.enter_context(
            patch.object(internal_api.moderation_case_model, 'record_revision', AsyncMock())
        )
        response = self.client.post(
            '/internal/guild/moderation/cases/1/cleanup',
            headers=self.headers,
            json={'guild_id': 123, 'channel_id': 555, 'amount': 2},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['deleted_count'], 2)
        self.assertEqual(revision.await_args.args[-1], {'channel_id': 555, 'deleted_count': 2})
        channel.permissions_for.return_value.manage_messages = False
        channel.purge.reset_mock()
        response = self.client.post(
            '/internal/guild/moderation/cases/1/cleanup',
            headers=self.headers,
            json={'guild_id': 123, 'channel_id': 555, 'amount': 2},
        )
        self.assertEqual(response.status_code, 403)
        channel.purge.assert_not_awaited()
