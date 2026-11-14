"""Guild playback settings must reach every bot and reconcile voice timers."""

import asyncio
from dataclasses import replace
import discord
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI

from api.dependencies.guild_settings import GuildSettingsDependencies
from api.routes.guild_settings import register_routes
from cogs.music import Music
from database.models import GuildModel
from utils.autoplay import default_autoplay_config
from utils.i18n import I18nManager
from utils.queue import CustomPlayer, CustomQueue


class GuildMusicSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = MagicMock(spec=GuildModel)
        self.config = {**default_autoplay_config(), 'history_size': 7}
        self.model.get_autoplay_settings = AsyncMock(return_value={'config': self.config})
        self.players = []
        self.cogs = []
        for bot_id, guild_id in ((42, 123), (43, 123), (44, 456)):
            bot = MagicMock()
            bot.user.id = bot_id
            bot.loop = asyncio.get_running_loop()
            cog = Music(bot)
            cog.guild_model = self.model
            bot.get_cog.return_value = cog
            player = MagicMock(spec=CustomPlayer)
            player.client = bot
            player.guild = SimpleNamespace(id=guild_id, voice_client=player)
            player.queue = CustomQueue()
            player.current = None
            player.channel = SimpleNamespace(members=[SimpleNamespace(id=bot_id), SimpleNamespace(id=99)])
            player.autoplay_enabled = True
            player.twenty_four_seven = False
            player.is_playing = False
            player.is_connected = True
            player.destroy = AsyncMock()
            self.players.append(player)
            self.cogs.append(cog)
        self.addAsyncCleanup(self.clean_timers)
        dependencies = GuildSettingsDependencies(
            _find_guild=lambda gid: SimpleNamespace(get_member=lambda uid: object()),
            _guild_preview_context=MagicMock(),
            _validate_assignable_role=MagicMock(),
            bot_instance=None,
            check_user_guild_permission=AsyncMock(return_value=True),
            check_user_guild_capability=AsyncMock(return_value=True),
            get_all_voice_clients=lambda: self.players,
            guild_model=self.model,
            i18n=MagicMock(spec=I18nManager),
        )
        self.dependencies = dependencies
        self.save_route = register_routes(FastAPI(), lambda: self.dependencies)['internal_save_guild_settings']

    async def clean_timers(self):
        tasks = []
        for cog in self.cogs:
            for guild_id, task in list(cog.timeout_tasks.items()):
                tasks.append(task)
                cog.cancel_timeout(guild_id)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def save(self, **settings):
        result = await self.save_route(user_id=99, data={'guild_id': 123, **settings})
        self.assertEqual(result, {'success': True})

    async def test_channel_save_refreshes_directory_before_returning_without_active_players(self):
        channel = MagicMock(spec=discord.TextChannel)
        guild = SimpleNamespace(get_member=lambda uid: object(), get_channel=lambda cid: channel)
        cog = SimpleNamespace(_update_static_embed=AsyncMock())
        bot = MagicMock()
        bot.all_bots = [bot]
        bot.get_guild.return_value = guild
        bot.get_cog.return_value = cog
        self.dependencies = replace(
            self.dependencies, bot_instance=bot, _find_guild=lambda gid: guild, get_all_voice_clients=lambda: []
        )

        async def verify_saved(guild_id):
            self.model.set_music_channel.assert_awaited_once_with(123, 999)
            self.model.set_music_message.assert_awaited_once_with(123, 1001)

        cog._update_static_embed.side_effect = verify_saved
        await self.save(music_channel_id='999', music_message_id='1001')
        cog._update_static_embed.assert_awaited_once_with(123)

    async def test_channel_lookup_requires_manage_server_permission_and_does_not_save(self):
        route = register_routes(FastAPI(), lambda: self.dependencies)['internal_resolve_autoplay_channel']
        from fastapi import HTTPException

        self.model.set_autoplay_custom_config = AsyncMock()
        with patch('services.youtube_channels.resolve_channel', AsyncMock(return_value={'id': 'channel'})) as resolve:
            self.dependencies.check_user_guild_permission.return_value = False
            with self.assertRaises(HTTPException) as caught:
                await route(user_id=99, guild_id=123, input='@Creator')
            self.assertEqual(caught.exception.status_code, 403)
            resolve.assert_not_awaited()
            self.dependencies.check_user_guild_permission.return_value = True
            self.assertEqual(await route(user_id=99, guild_id=123, input='@Creator'), {'id': 'channel'})
            resolve.assert_awaited_once_with('@Creator')
            self.model.set_autoplay_custom_config.assert_not_awaited()

    async def test_removing_channel_refreshes_directory_once_across_bots(self):
        cogs = [SimpleNamespace(_update_static_embed=AsyncMock()) for _ in range(2)]
        bots = [MagicMock() for _ in cogs]
        for bot, cog in zip(bots, cogs):
            bot.get_cog.return_value = cog
        bots[0].all_bots = bots
        self.dependencies = replace(self.dependencies, bot_instance=bots[0])
        await self.save(music_channel_id=None)
        self.model.remove_music_channel.assert_awaited_once_with(123)
        cogs[0]._update_static_embed.assert_awaited_once_with(123)
        cogs[1]._update_static_embed.assert_not_awaited()

    async def test_unrelated_settings_do_not_refresh_directory(self):
        bot = MagicMock()
        bot.all_bots = [bot]
        cog = SimpleNamespace(_update_static_embed=AsyncMock())
        bot.get_cog.return_value = cog
        self.dependencies = replace(self.dependencies, bot_instance=bot)
        await self.save(default_volume=75)
        cog._update_static_embed.assert_not_awaited()

    async def test_disabling_autoplay_updates_all_guild_players_and_cancels_their_prefetch(self):
        workers = []
        for cog in self.cogs:
            worker = asyncio.get_running_loop().create_future()
            cog._autoplay_prefetch_tasks[123] = worker
            cog._autoplay_prefetch_cache[123] = {'track': 'stale'}
            workers.append(worker)
        await self.save(autoplay=False)
        self.model.set_autoplay_enabled.assert_awaited_once_with(123, False)
        for player, cog, worker in zip(self.players[:2], self.cogs[:2], workers[:2]):
            self.assertFalse(player.autoplay_enabled)
            self.assertTrue(worker.cancelled())
            self.assertNotIn(123, cog._autoplay_prefetch_cache)
        self.assertTrue(self.players[2].autoplay_enabled)
        self.assertFalse(workers[2].cancelled())
        workers[2].cancel()

    async def test_enabling_autoplay_schedules_prefetch_on_each_players_own_cog(self):
        for player, cog in zip(self.players, self.cogs):
            player.autoplay_enabled = False
            player.current = SimpleNamespace(title='Current')
            cog._schedule_autoplay_prefetch = MagicMock()
        await self.save(autoplay=True)
        for player, cog in zip(self.players[:2], self.cogs[:2]):
            self.assertTrue(player.autoplay_enabled)
            cog._schedule_autoplay_prefetch.assert_called_once_with(player, player.current)
        self.assertFalse(self.players[2].autoplay_enabled)
        self.cogs[2]._schedule_autoplay_prefetch.assert_not_called()

    async def test_preset_refreshes_config_and_cached_recommendations_on_all_guild_players(self):
        for cog in self.cogs:
            cog._autoplay_prefetch_cache[123] = {'track': 'old recipe'}
        await self.save(autoplay_preset='default')
        self.model.set_autoplay_preset.assert_awaited_once_with(123, 'default')
        self.assertEqual(self.model.get_autoplay_settings.await_count, 2)
        for player, cog in zip(self.players[:2], self.cogs[:2]):
            self.assertEqual(player.autoplay_config, self.config)
            self.assertNotIn(123, cog._autoplay_prefetch_cache)
        self.assertIn(123, self.cogs[2]._autoplay_prefetch_cache)

    async def test_blacklist_save_refreshes_all_guild_players_and_discards_prefetched_tracks(self):
        self.config['channel_blacklist'] = ['Blocked Uploader']
        for player, cog in zip(self.players[:2], self.cogs[:2]):
            player.current = SimpleNamespace(title='Current Song')
            cog._autoplay_prefetch_cache[123] = {'track': 'blocked recommendation'}
            cog._schedule_autoplay_prefetch = MagicMock()
        await self.save(autoplay_preset='custom', autoplay_config=self.config)
        self.model.set_autoplay_custom_config.assert_awaited_once_with(123, self.config)
        for player, cog in zip(self.players[:2], self.cogs[:2]):
            self.assertEqual(player.autoplay_config['channel_blacklist'], ['Blocked Uploader'])
            self.assertNotIn(123, cog._autoplay_prefetch_cache)
            cog._schedule_autoplay_prefetch.assert_called_once_with(player, player.current)
        self.assertNotEqual(getattr(self.players[2], 'autoplay_config', None), self.config)

    async def test_enabling_247_cancels_pending_disconnects_for_every_guild_player(self):
        tasks = []
        for player, cog in zip(self.players[:2], self.cogs[:2]):
            cog.start_timeout(123, player)
            tasks.append(cog.timeout_tasks[123])
        await self.save(twenty_four_seven=True)
        await asyncio.gather(*tasks, return_exceptions=True)
        for player, cog, task in zip(self.players[:2], self.cogs[:2], tasks):
            self.assertTrue(player.twenty_four_seven)
            self.assertTrue(task.cancelled())
            self.assertNotIn(123, cog.timeout_tasks)
            player.destroy.assert_not_awaited()
        self.assertFalse(self.players[2].twenty_four_seven)

    async def test_disabling_247_starts_disconnect_timers_for_idle_players(self):
        for player in self.players[:2]:
            player.twenty_four_seven = True
        await self.save(twenty_four_seven=False)
        self.model.set_247_enabled.assert_awaited_once_with(123, False)
        for player, cog in zip(self.players[:2], self.cogs[:2]):
            self.assertFalse(player.twenty_four_seven)
            self.assertIn(123, cog.timeout_tasks)
        self.assertNotIn(123, self.cogs[2].timeout_tasks)

    async def test_disabling_247_keeps_active_listened_playback_and_times_out_alone_player(self):
        for player in self.players[:2]:
            player.is_playing = True
            player.current = SimpleNamespace(title='Current')
        self.players[1].channel.members = [SimpleNamespace(id=43)]
        await self.save(**{'247': False})
        self.assertNotIn(123, self.cogs[0].timeout_tasks)
        self.assertIn(123, self.cogs[1].timeout_tasks)

    async def test_expiring_timer_rechecks_247_before_destroying_player(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait_for_release(seconds):
            entered.set()
            await release.wait()

        cog, player = self.cogs[0], self.players[0]
        with patch('cogs.music.asyncio.sleep', new_callable=AsyncMock, side_effect=wait_for_release):
            cog.start_timeout(123, player)
            task = cog.timeout_tasks[123]
            await asyncio.wait_for(entered.wait(), timeout=1)
            player.twenty_four_seven = True
            release.set()
            await task
        player.destroy.assert_not_awaited()
