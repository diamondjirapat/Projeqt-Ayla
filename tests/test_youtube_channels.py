import json
import unittest
from unittest.mock import AsyncMock, patch

from services import youtube_channels as lookup


CHANNEL = 'UCabcdefghijklmnopqrstuv'
VIDEO = 'dQw4w9WgXcQ'


class ChannelInputTests(unittest.TestCase):
    def test_supported_inputs_are_classified_before_any_network_request(self):
        cases = [
            (CHANNEL, ('id', CHANNEL)),
            ('  ' + CHANNEL + '  ', ('id', CHANNEL)),
            ('https://youtube.com/channel/' + CHANNEL, ('id', CHANNEL)),
            ('https://www.youtube.com/@Creator/videos', ('handle', '@Creator')),
            ('youtube.com/@Creator', ('handle', '@Creator')),
            ('@Creator', ('handle', '@Creator')),
            ('https://youtube.com/@เพลงไทย', ('handle', '@เพลงไทย')),
            ('https://youtube.com/user/Creator', ('username', 'Creator')),
            ('https://youtube.com/c/Creator', ('custom', 'Creator')),
            ('https://youtu.be/' + VIDEO + '?si=share', ('video', VIDEO)),
            ('https://m.youtube.com/watch?v=' + VIDEO + '&list=playlist', ('video', VIDEO)),
            ('https://music.youtube.com/watch?v=' + VIDEO, ('video', VIDEO)),
            ('https://youtube.com/shorts/' + VIDEO, ('video', VIDEO)),
            ('https://youtube.com/live/' + VIDEO, ('video', VIDEO)),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(lookup.parse_channel_input(value), expected)

    def test_invalid_inputs_and_untrusted_hosts_are_rejected(self):
        for value in (
            'UCbad',
            'Uploader Name',
            'https://youtube.com.evil.test/@Creator',
            'https://youtube.com@evil.test/@Creator',
            'https://localhost/watch?v=' + VIDEO,
            'https://youtube.com:9000/@Creator',
            'file:///video',
            'https://youtube.com/playlist?list=abc',
            'https://youtube.com/watch?v=short',
            'https://youtube.com/@Creator/../../other',
        ):
            with self.subTest(value=value), self.assertRaises(lookup.ChannelLookupError) as caught:
                lookup.parse_channel_input(value)
            self.assertEqual(caught.exception.code, 'invalid_input')


class ChannelLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lookup._cache.clear()
        self.addCleanup(patch.stopall)
        patch.object(lookup.Config, 'YOUTUBE_API_KEY', '').start()

    def channel_page(self):
        data = {
            'metadata': {
                'channelMetadataRenderer': {
                    'externalId': CHANNEL,
                    'title': 'Actual Channel',
                    'avatar': {'thumbnails': [{'url': 'https://yt3.googleusercontent.com/avatar'}]},
                }
            },
            'unrelated': {'channelId': 'UCABCDEFGHIJKLMNOPQRSTUV'},
        }
        return ('var ytInitialData = ' + json.dumps(data) + ';').encode()

    async def test_channel_page_is_verified_and_cached(self):
        with patch.object(lookup, '_fetch', AsyncMock(return_value=self.channel_page())) as fetch:
            result = await lookup.resolve_channel(CHANNEL)
            self.assertEqual(result['id'], CHANNEL)
            self.assertEqual(result['name'], 'Actual Channel')
            self.assertEqual(result['avatar'], 'https://yt3.googleusercontent.com/avatar')
            await lookup.resolve_channel('https://youtube.com/channel/' + CHANNEL)
            fetch.assert_awaited_once()

    async def test_video_resolves_the_actual_uploader_then_loads_the_channel(self):
        player = {'videoDetails': {'videoId': VIDEO, 'channelId': CHANNEL}}
        video_page = ('var ytInitialPlayerResponse = ' + json.dumps(player) + ';').encode()
        with patch.object(lookup, '_fetch', AsyncMock(side_effect=[video_page, self.channel_page()])) as fetch:
            result = await lookup.resolve_channel('https://youtu.be/' + VIDEO)
            self.assertEqual(result['id'], CHANNEL)
            self.assertIn('watch?v=' + VIDEO, fetch.await_args_list[0].args[0])
            self.assertIn('/channel/' + CHANNEL, fetch.await_args_list[1].args[0])

    async def test_handle_and_legacy_links_resolve_channel_metadata(self):
        with patch.object(lookup, '_fetch', AsyncMock(return_value=self.channel_page())):
            for value in (
                'https://youtube.com/@Creator',
                'https://youtube.com/c/Creator',
                'https://youtube.com/user/Creator',
            ):
                self.assertEqual((await lookup.resolve_channel(value))['id'], CHANNEL)

    async def test_unrelated_channel_or_video_metadata_is_not_accepted(self):
        wrong = self.channel_page().replace(CHANNEL.encode(), b'UCABCDEFGHIJKLMNOPQRSTUV')
        with patch.object(lookup, '_fetch', AsyncMock(return_value=wrong)):
            with self.assertRaises(lookup.ChannelLookupError) as caught:
                await lookup.resolve_channel(CHANNEL)
            self.assertEqual(caught.exception.code, 'not_found')

    async def test_provider_failure_is_retryable_and_not_cached(self):
        with patch.object(lookup, '_fetch', AsyncMock(side_effect=[TimeoutError(), self.channel_page()])):
            with self.assertRaises(lookup.ChannelLookupError) as caught:
                await lookup.resolve_channel(CHANNEL)
            self.assertEqual(caught.exception.code, 'lookup_unavailable')
            self.assertEqual((await lookup.resolve_channel(CHANNEL))['id'], CHANNEL)

    async def test_optional_official_api_resolves_video_and_channel(self):
        with (
            patch.object(lookup.Config, 'YOUTUBE_API_KEY', 'test-key'),
            patch.object(
                lookup,
                '_api',
                AsyncMock(
                    side_effect=[
                        [{'snippet': {'channelId': CHANNEL}}],
                        [{'id': CHANNEL, 'snippet': {'title': 'API Channel'}}],
                    ]
                ),
            ) as api,
        ):
            result = await lookup.resolve_channel('https://youtu.be/' + VIDEO)
            self.assertEqual(result['name'], 'API Channel')
            api.assert_any_await('channels', id=CHANNEL)

    async def test_page_does_not_treat_any_embedded_id_as_the_channel(self):
        with patch.object(lookup, '_fetch', AsyncMock(return_value=b'{"channelId":"UCabcdefghijklmnopqrstuv"}')):
            with self.assertRaises(lookup.ChannelLookupError) as caught:
                await lookup.resolve_channel('@Creator')
            self.assertEqual(caught.exception.code, 'lookup_unavailable')
