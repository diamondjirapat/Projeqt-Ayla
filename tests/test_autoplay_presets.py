import unittest
from collections import deque
from unittest.mock import AsyncMock, MagicMock

from database.models import GuildModel
from utils.autoplay import (
    AUTOPLAY_PRESET_CUSTOM,
    AUTOPLAY_PRESET_DEFAULT,
    AUTOPLAY_PRESET_VERSION,
    DEFAULT_AUTOPLAY_CONFIG,
    default_autoplay_config,
    effective_autoplay_config,
    normalize_autoplay_config,
)


class MockTrack:
    def __init__(self, title: str, author: str, uri: str, identifier: str = ''):
        self.title = title
        self.author = author
        self.uri = uri
        self.identifier = identifier or uri
        self.requester = None


class _UpdateResult:
    modified_count = 1
    upserted_id = 'new-id'


class _GuildCollection:
    def __init__(self, doc=None):
        self.doc = doc
        self.updates = []

    async def find_one(self, query):
        return self.doc

    async def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return _UpdateResult()


def _guild_model(doc=None):
    model = GuildModel()
    model._collection = _GuildCollection(doc)
    return model


class NormalizeAutoplayConfigTests(unittest.TestCase):
    def test_channel_blacklist_normalizes_names_and_rejects_invalid_values(self):
        normalized = normalize_autoplay_config({'channel_blacklist': ['  Uploader  ', 'UPLOADER', '', None, 123, 'Other']})
        self.assertEqual(normalized['channel_blacklist'], ['Uploader', 'Other'])
        self.assertEqual(normalize_autoplay_config({'channel_blacklist': 'Uploader'})['channel_blacklist'], [])

    def test_channel_blacklist_limits_size_and_does_not_share_default_list(self):
        normalized = normalize_autoplay_config({'channel_blacklist': [str(i) for i in range(150)] + ['x' * 300]})
        self.assertEqual(len(normalized['channel_blacklist']), 100)
        self.assertEqual(len(normalize_autoplay_config({'channel_blacklist': ['x' * 300]})['channel_blacklist'][0]), 200)
        config = default_autoplay_config()
        config['channel_blacklist'].append('Blocked')
        self.assertEqual(default_autoplay_config()['channel_blacklist'], [])

    def test_fills_missing_keys_with_current_defaults(self):
        self.assertEqual(normalize_autoplay_config({}), default_autoplay_config())
        self.assertEqual(normalize_autoplay_config(None), default_autoplay_config())

    def test_drops_unknown_keys(self):
        normalized = normalize_autoplay_config({'nope': 1, 'history_size': 10})
        self.assertNotIn('nope', normalized)
        self.assertEqual(normalized['history_size'], 10)

    def test_clamps_out_of_range_values(self):
        normalized = normalize_autoplay_config(
            {
                'same_artist_streak_limit': 99,
                'history_size': -5,
                'candidate_pool_size': 0,
                'prefetch_seconds': 60,
            }
        )
        self.assertEqual(normalized['same_artist_streak_limit'], 5)
        self.assertEqual(normalized['history_size'], 0)
        self.assertEqual(normalized['candidate_pool_size'], 1)
        # Prefetch is hardcoded, not tunable: legacy values are dropped.
        self.assertNotIn('prefetch_seconds', normalized)

    def test_rejects_non_integer_values(self):
        before = dict(DEFAULT_AUTOPLAY_CONFIG)
        normalized = normalize_autoplay_config({'history_size': 'abc', 'candidate_pool_size': True})
        self.assertEqual(normalized['history_size'], before['history_size'])
        self.assertEqual(normalized['candidate_pool_size'], before['candidate_pool_size'])

    def test_default_preset_is_never_mutated(self):
        before = dict(DEFAULT_AUTOPLAY_CONFIG)
        first = default_autoplay_config()
        first['history_size'] = 1
        normalized = normalize_autoplay_config({'history_size': 2})
        normalized['history_size'] = 3
        self.assertEqual(DEFAULT_AUTOPLAY_CONFIG, before)
        self.assertEqual(default_autoplay_config(), before)

    def test_effective_config_merges_custom_over_default(self):
        effective = effective_autoplay_config({'history_size': 5})
        self.assertEqual(effective['history_size'], 5)
        self.assertEqual(effective['candidate_pool_size'], DEFAULT_AUTOPLAY_CONFIG['candidate_pool_size'])


class GuildAutoplayPresetTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_blacklist_persists_and_loads_with_custom_config(self):
        model = _guild_model(None)
        await model.set_autoplay_custom_config(123, {'channel_blacklist': [' Uploader ', 'UPLOADER']})
        stored = model._collection.updates[0][0][1]['$set']
        self.assertEqual(stored['autoplay_config']['channel_blacklist'], ['Uploader'])
        model._collection.doc = {'guild_id': 123, **stored}
        settings = await model.get_autoplay_settings(123)
        self.assertEqual(settings['config']['channel_blacklist'], ['Uploader'])

    async def test_new_guild_defaults_to_default_preset(self):
        model = _guild_model(None)
        settings = await model.get_autoplay_settings(123)
        self.assertEqual(settings['preset'], AUTOPLAY_PRESET_DEFAULT)
        self.assertEqual(settings['config'], DEFAULT_AUTOPLAY_CONFIG)
        self.assertIsNone(settings['custom'])
        self.assertEqual(settings['default'], DEFAULT_AUTOPLAY_CONFIG)
        self.assertEqual(settings['default_version'], AUTOPLAY_PRESET_VERSION)
        self.assertIsNone(settings['custom_source_version'])

    async def test_rejects_unknown_preset(self):
        model = _guild_model(None)
        with self.assertRaises(ValueError):
            await model.set_autoplay_preset(123, 'bogus')

    async def test_custom_config_forces_custom_preset_and_records_source_version(self):
        model = _guild_model(None)
        normalized = await model.set_autoplay_custom_config(123, {'history_size': 7, 'nope': 1})
        self.assertEqual(normalized['history_size'], 7)
        self.assertNotIn('nope', normalized)
        stored = model._collection.updates[0][0][1]['$set']
        self.assertEqual(stored['autoplay_preset'], AUTOPLAY_PRESET_CUSTOM)
        self.assertEqual(stored['autoplay_config_source_version'], AUTOPLAY_PRESET_VERSION)
        self.assertEqual(stored['autoplay_config']['history_size'], 7)

    async def test_switching_presets_preserves_custom_copy(self):
        doc = {
            'guild_id': 123,
            'autoplay_preset': 'custom',
            'autoplay_config': {'history_size': 7},
            'autoplay_config_source_version': 1,
        }
        model = _guild_model(doc)
        await model.set_autoplay_preset(123, 'default')
        stored = model._collection.updates[0][0][1]['$set']
        self.assertEqual(stored['autoplay_preset'], AUTOPLAY_PRESET_DEFAULT)
        self.assertNotIn('autoplay_config', stored)

    async def test_custom_effective_config_tracks_current_default(self):
        doc = {
            'guild_id': 123,
            'autoplay_preset': 'custom',
            'autoplay_config': {'history_size': 7},
            'autoplay_config_source_version': 1,
        }
        model = _guild_model(doc)
        settings = await model.get_autoplay_settings(123)
        self.assertEqual(settings['preset'], AUTOPLAY_PRESET_CUSTOM)
        self.assertEqual(settings['config']['history_size'], 7)
        self.assertEqual(settings['config']['candidate_pool_size'], DEFAULT_AUTOPLAY_CONFIG['candidate_pool_size'])
        self.assertEqual(settings['custom_source_version'], 1)


class AutoplaySettingsSyncTests(unittest.IsolatedAsyncioTestCase):
    def _create_cog(self):
        from cogs.music import Music

        cog = Music(MagicMock())
        cog.guild_model = MagicMock()
        cog.user_model = MagicMock()
        return cog

    async def test_apply_autoplay_settings_caches_effective_config_on_player(self):
        cog = self._create_cog()
        cog.guild_model.get_autoplay_settings = AsyncMock(
            return_value={
                'preset': 'custom',
                'config': {**default_autoplay_config(), 'history_size': 7},
                'custom': {'history_size': 7},
                'default': default_autoplay_config(),
                'default_version': AUTOPLAY_PRESET_VERSION,
                'custom_source_version': AUTOPLAY_PRESET_VERSION,
            }
        )
        player = MagicMock()
        player.guild.id = 123
        config = await cog.apply_autoplay_settings(player, 123)
        self.assertEqual(config['history_size'], 7)
        self.assertEqual(player.autoplay_config['history_size'], 7)

    async def test_load_autoplay_config_falls_back_to_default_on_db_error(self):
        cog = self._create_cog()
        cog.guild_model.get_autoplay_settings = AsyncMock(side_effect=RuntimeError('db down'))
        player = MagicMock()
        player.guild.id = 123
        config = await cog.apply_autoplay_settings(player)
        self.assertEqual(config, DEFAULT_AUTOPLAY_CONFIG)


class ConfigDrivenAutoplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_blacklist_applies_to_every_stage_even_when_variety_relaxes(self):
        for stage in ('recommendations', 'artist', 'radio'):
            with self.subTest(stage=stage):
                cog = self._create_cog()
                player = self._player()
                blocked = MockTrack('Blocked Song', '  UPLOADER  ', 'uri-blocked')
                allowed = MockTrack('Allowed Song', 'Last Artist', 'uri-allowed')
                # Force selection to relax artist variety, while still respecting the blacklist.
                cog.autoplay_recent_artists[123] = deque(['last artist', 'last artist'], maxlen=50)
                player.get_recommendations = AsyncMock(return_value=[blocked, allowed])
                player.get_tracks = AsyncMock(return_value=[blocked, allowed])
                config = {
                    'channel_blacklist': ['Uploader'],
                    'weighted_random': False,
                    'enable_recommendations': stage == 'recommendations',
                    'enable_artist_fallback': stage == 'artist',
                    'enable_radio_fallback': stage == 'radio',
                }
                chosen = await cog._fetch_autoplay_track(player, MockTrack('Last Song', 'Last Artist', 'uri-last'), config)
                self.assertIs(chosen, allowed)

    async def test_all_channels_blocked_returns_none(self):
        cog = self._create_cog()
        player = self._player()
        blocked = MockTrack('Blocked Song', 'Uploader', 'uri-blocked')
        player.get_recommendations = AsyncMock(return_value=[blocked])
        player.get_tracks = AsyncMock(return_value=[blocked])
        chosen = await cog._fetch_autoplay_track(player, MockTrack('Last Song', 'Last Artist', 'uri-last'), {'channel_blacklist': ['Uploader']})
        self.assertIsNone(chosen)
        self.assertNotIn('uri-blocked', cog.autoplay_played_uris[123])

    async def test_channel_blacklist_uses_exact_name_not_artist_cleanup_or_substring(self):
        cog = self._create_cog()
        player = self._player()
        allowed = MockTrack('New Song', 'Uploader - Topic', 'uri-allowed')
        player.get_recommendations = AsyncMock(return_value=[allowed])
        chosen = await cog._fetch_autoplay_track(player, MockTrack('Last Song', 'Last Artist', 'uri-last'), {'channel_blacklist': ['Uploader']})
        self.assertIs(chosen, allowed)

    def _create_cog(self):
        from cogs.music import Music

        cog = Music(MagicMock())
        cog.guild_model = MagicMock()
        cog.user_model = MagicMock()
        return cog

    def _player(self, guild_id=123):
        player = MagicMock()
        player.guild.id = guild_id
        player.history = []
        return player

    async def test_all_stages_disabled_returns_none_without_provider_calls(self):
        cog = self._create_cog()
        player = self._player()
        player.get_recommendations = AsyncMock(return_value=[MockTrack('A', 'B', 'uri-1')])
        player.get_tracks = AsyncMock(return_value=[MockTrack('A', 'B', 'uri-1')])
        last = MockTrack('Last Song', 'Last Artist', 'uri-last')
        config = {
            'enable_recommendations': False,
            'enable_artist_fallback': False,
            'enable_radio_fallback': False,
        }
        chosen = await cog._fetch_autoplay_track(player, last, config)
        self.assertIsNone(chosen)
        player.get_recommendations.assert_not_called()
        player.get_tracks.assert_not_called()

    async def test_deterministic_top_pick_when_weighted_random_off(self):
        cog = self._create_cog()
        player = self._player()
        cand1 = MockTrack('Fresh Song One', 'Fresh Artist', 'uri-1')
        cand2 = MockTrack('Fresh Song Two', 'Other Artist', 'uri-2')
        player.get_recommendations = AsyncMock(return_value=[cand1, cand2])
        last = MockTrack('Last Song', 'Last Artist', 'uri-last')
        config = {'weighted_random': False, 'candidate_pool_size': 2}
        for _ in range(5):
            cog.autoplay_played_uris.pop(player.guild.id, None)
            cog.autoplay_played_titles.pop(player.guild.id, None)
            cog.autoplay_recent_artists.pop(player.guild.id, None)
            chosen = await cog._fetch_autoplay_track(player, last, config)
            self.assertEqual(chosen.title, 'Fresh Song One')

    async def test_variety_off_allows_same_artist_streak(self):
        cog = self._create_cog()
        player = self._player()
        cog.autoplay_recent_artists[player.guild.id] = deque(['lonown', 'lonown'], maxlen=10)
        cand = MockTrack('Another Song', 'LONOWN', 'uri-2')
        player.get_recommendations = AsyncMock(return_value=[cand])
        last = MockTrack('Song 1', 'LONOWN', 'uri-1')
        chosen = await cog._fetch_autoplay_track(player, last, {'artist_variety': False})
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.author, 'LONOWN')

    async def test_history_size_zero_disables_repeat_filter(self):
        cog = self._create_cog()
        player = self._player()
        last = MockTrack('Last Song', 'Last Artist', 'uri-last')
        cand = MockTrack('Different Title', 'Other Artist', 'uri-last')
        player.get_recommendations = AsyncMock(return_value=[cand])
        chosen = await cog._fetch_autoplay_track(player, last, {'history_size': 0, 'history_seed_size': 0})
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.uri, 'uri-last')

    async def test_history_size_default_blocks_repeat_uri(self):
        cog = self._create_cog()
        player = self._player()
        last = MockTrack('Last Song', 'Last Artist', 'uri-last')
        cand = MockTrack('Different Title', 'Other Artist', 'uri-last')
        player.get_recommendations = AsyncMock(return_value=[cand])
        chosen = await cog._fetch_autoplay_track(player, last, None)
        self.assertIsNone(chosen)


if __name__ == '__main__':
    unittest.main()
