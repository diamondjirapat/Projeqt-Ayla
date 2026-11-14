import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import lava_lyra
from discord.ext import commands

from cogs.music import Music
from utils.autoplay import default_autoplay_config, is_channel_blocked, normalize_channel_blacklist, track_channel_id


CHANNEL_ID = 'UCabcdefghijklmnopqrstuv'
OTHER_CHANNEL_ID = 'UCABCDEFGHIJKLMNOPQRSTUV'


def track(channel_id=CHANNEL_ID, author='Uploader'):
    return lava_lyra.Track(
        track_id='encoded',
        info={
            'title': 'Song',
            'author': author,
            'uri': 'https://youtube.com/watch?v=video123',
            'identifier': 'video123',
            'sourceName': 'youtube',
            'channelId': channel_id,
        },
        track_type=lava_lyra.TrackType.YOUTUBE,
    )


class ChannelIdentityTests(unittest.TestCase):
    def test_ids_are_case_sensitive_and_survive_channel_renames(self):
        config = {'channel_blacklist': [CHANNEL_ID]}
        self.assertTrue(is_channel_blocked(track(author='New Name'), config))
        self.assertFalse(is_channel_blocked(track(OTHER_CHANNEL_ID), config))
        self.assertFalse(is_channel_blocked(track(None, author=CHANNEL_ID), config))
        self.assertEqual(
            normalize_channel_blacklist([CHANNEL_ID, OTHER_CHANNEL_ID, CHANNEL_ID]), [CHANNEL_ID, OTHER_CHANNEL_ID]
        )

    def test_resolved_track_identity_is_used(self):
        resolved = SimpleNamespace(original=track(), info={'author': 'Spotify artist'})
        self.assertEqual(track_channel_id(resolved), CHANNEL_ID)
        self.assertTrue(is_channel_blocked(resolved, {'channel_blacklist': [CHANNEL_ID]}))

    def test_legacy_names_remain_supported(self):
        self.assertTrue(is_channel_blocked(track(None), {'channel_blacklist': ['uploader']}))


class AutoplayBlockCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.cog = Music(self.bot)
        self.bot.get_cog.return_value = self.cog
        self.current = track()
        self.player = SimpleNamespace(
            guild=SimpleNamespace(id=123), current=self.current, get_tracks=AsyncMock(return_value=[])
        )
        self.bot.voice_clients = [self.player]
        self.ctx = SimpleNamespace(
            guild=self.player.guild, voice_client=self.player, permissions=discord.Permissions(manage_guild=True)
        )
        self.config = {**default_autoplay_config(), 'history_size': 7}
        self.cog.guild_model.get_autoplay_settings = AsyncMock(return_value={'config': self.config})
        self.cog.guild_model.set_autoplay_custom_config = AsyncMock()
        self.cog.handle_command_cleanup = AsyncMock()
        self.cog.get_response_channel = AsyncMock(return_value=None)
        self.cog.acknowledge_static_redirect = AsyncMock(return_value=False)
        self.cog.check_voice_channel = AsyncMock(return_value=True)
        self.cog.send_response = AsyncMock()
        self.cog._schedule_autoplay_prefetch = MagicMock()
        self.translate = AsyncMock(return_value='response')
        self.addCleanup(patch.stopall)
        patch('cogs.music.i18n.t', self.translate).start()
        patch('runtime.bots.all_bots', []).start()

    async def invoke(self):
        await Music.autoplay_block.callback(self.cog, self.ctx)

    async def test_adds_current_channel_preserves_recipe_and_refreshes_all_players(self):
        other_bot = MagicMock()
        other_cog = Music(other_bot)
        other_bot.get_cog.return_value = other_cog
        other_cog._schedule_autoplay_prefetch = MagicMock()
        same_guild = SimpleNamespace(guild=self.player.guild, current=track())
        other_guild = SimpleNamespace(guild=SimpleNamespace(id=999), current=track())
        other_bot.voice_clients = [same_guild, other_guild]
        other_cog._autoplay_prefetch_cache[123] = {'track': self.current}
        self.cog._autoplay_prefetch_cache[123] = {'track': self.current}
        with patch('runtime.bots.all_bots', [self.bot, other_bot]):
            await self.invoke()
        saved = self.cog.guild_model.set_autoplay_custom_config.await_args.args[1]
        self.assertEqual(saved['channel_blacklist'], [CHANNEL_ID])
        self.assertEqual(saved['history_size'], 7)
        self.assertEqual(self.player.autoplay_config, saved)
        self.assertEqual(same_guild.autoplay_config, saved)
        self.assertFalse(hasattr(other_guild, 'autoplay_config'))
        self.assertNotIn(123, self.cog._autoplay_prefetch_cache)
        self.assertNotIn(123, other_cog._autoplay_prefetch_cache)
        self.assertIs(self.player.current, self.current)
        self.translate.assert_awaited_with(self.ctx, 'music.commands.autoplay_block.added', channel_id=CHANNEL_ID)

    async def test_reloads_metadata_for_an_already_queued_track(self):
        self.player.current = track(None)
        self.player.get_tracks.return_value = [track()]
        await self.invoke()
        self.player.get_tracks.assert_awaited_once_with(self.player.current.uri)
        self.assertEqual(track_channel_id(self.player.current), CHANNEL_ID)
        self.cog.guild_model.set_autoplay_custom_config.assert_awaited_once()

    async def test_unavailable_channel_does_not_write(self):
        self.player.current = track(None)
        await self.invoke()
        self.cog.guild_model.set_autoplay_custom_config.assert_not_awaited()
        self.translate.assert_awaited_with(self.ctx, 'music.commands.autoplay_block.unavailable')

    async def test_reloads_the_resolved_youtube_upload_for_a_cross_provider_track(self):
        original = track(None)
        self.player.current = SimpleNamespace(original=original, info={}, uri='https://open.spotify.com/track/example')
        self.player.get_tracks.return_value = [track()]
        await self.invoke()
        self.player.get_tracks.assert_awaited_once_with(original.uri)
        self.assertEqual(track_channel_id(self.player.current), CHANNEL_ID)

    async def test_no_current_track_does_not_write(self):
        self.player.current = None
        await self.invoke()
        self.cog.guild_model.set_autoplay_custom_config.assert_not_awaited()
        self.translate.assert_awaited_with(self.ctx, 'music.commands.autoplay_block.nothing_playing')

    async def test_duplicate_and_full_list_do_not_write(self):
        for entries, key in (([CHANNEL_ID], 'already_blocked'), ([f'UC{i:022d}' for i in range(100)], 'full')):
            with self.subTest(key=key):
                self.config['channel_blacklist'] = entries
                await self.invoke()
                self.assertIn(key, self.translate.await_args.args[1])
        self.cog.guild_model.set_autoplay_custom_config.assert_not_awaited()

    async def test_same_voice_channel_is_required(self):
        self.cog.check_voice_channel.return_value = False
        await self.invoke()
        self.cog.guild_model.set_autoplay_custom_config.assert_not_awaited()

    async def test_manage_server_permission_is_required(self):
        self.ctx.permissions = discord.Permissions.none()
        with self.assertRaises(commands.MissingPermissions):
            for check in Music.autoplay_block.checks:
                await check(self.ctx)


class PrefetchedChannelTests(unittest.TestCase):
    def test_prefetched_track_is_rejected_after_channel_is_blocked(self):
        cog = Music(MagicMock())
        ended = track(OTHER_CHANNEL_ID)
        player = SimpleNamespace(guild=SimpleNamespace(id=123), autoplay_config={'channel_blacklist': [CHANNEL_ID]})
        cog._autoplay_prefetch_cache[123] = {'for_track': ended, 'track': track()}
        self.assertIsNone(cog._take_autoplay_prefetch(player, ended))


class ChannelSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_id_is_filtered_in_every_stage_despite_a_rename(self):
        for stage in ('recommendations', 'artist', 'radio'):
            with self.subTest(stage=stage):
                cog = Music(MagicMock())
                blocked = track(author='Renamed Uploader')
                allowed = track(OTHER_CHANNEL_ID)
                allowed.title = 'Allowed Song'
                allowed.uri = 'https://youtube.com/watch?v=allowed'
                allowed.identifier = 'allowed'
                player = SimpleNamespace(
                    guild=SimpleNamespace(id=123),
                    history=[],
                    get_recommendations=AsyncMock(return_value=[blocked, allowed]),
                    get_tracks=AsyncMock(return_value=[blocked, allowed]),
                )
                config = {
                    'channel_blacklist': [CHANNEL_ID],
                    'weighted_random': False,
                    'enable_recommendations': stage == 'recommendations',
                    'enable_artist_fallback': stage == 'artist',
                    'enable_radio_fallback': stage == 'radio',
                }
                last = track(OTHER_CHANNEL_ID)
                last.title = 'Last Song'
                last.uri = 'https://youtube.com/watch?v=last'
                last.identifier = 'last'
                self.assertIs(await cog._fetch_autoplay_track(player, last, config), allowed)

    async def test_spotify_youtube_equivalent_is_checked_before_autoplay(self):
        cog = Music(MagicMock())
        spotify = lava_lyra.Track(
            track_id='spotify',
            info={
                'title': 'Spotify Song',
                'author': 'Artist',
                'identifier': 'spotify123',
                'uri': 'https://open.spotify.com/track/spotify123',
            },
            track_type=lava_lyra.TrackType.SPOTIFY,
        )
        player = SimpleNamespace(
            guild=SimpleNamespace(id=123),
            history=[],
            get_recommendations=AsyncMock(return_value=[spotify]),
            get_tracks=AsyncMock(return_value=[track()]),
        )
        last = track(OTHER_CHANNEL_ID)
        last.title = 'Last Song'
        last.uri = 'last-uri'
        result = await cog._fetch_autoplay_track(
            player,
            last,
            {'channel_blacklist': [CHANNEL_ID], 'enable_artist_fallback': False, 'enable_radio_fallback': False},
        )
        self.assertIsNone(result)
        self.assertEqual(track_channel_id(spotify), CHANNEL_ID)
        player.get_tracks.assert_awaited_once()

    async def test_youtube_candidate_with_unavailable_identity_is_not_allowed_to_bypass_id_blocks(self):
        cog = Music(MagicMock())
        unknown = track(None)
        player = SimpleNamespace(
            guild=SimpleNamespace(id=123),
            history=[],
            get_recommendations=AsyncMock(return_value=[unknown]),
            get_tracks=AsyncMock(return_value=[]),
        )
        last = track(OTHER_CHANNEL_ID)
        last.title = 'Last Song'
        last.uri = 'last-uri'
        result = await cog._fetch_autoplay_track(
            player,
            last,
            {'channel_blacklist': [CHANNEL_ID], 'enable_artist_fallback': False, 'enable_radio_fallback': False},
        )
        self.assertIsNone(result)
