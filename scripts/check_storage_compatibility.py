"""Cross-runtime storage and two-process guild-read checks in a disposable database.

Run with AYLA_STORAGE_TEST_URI explicitly set. Never falls back to MONGODB_URI.
The supplied URI selects the server only; a random ayla_compat_* database is used.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from pymongo import AsyncMongoClient

from database.guilds import GuildModel
from database.users import UserModel
from utils.artwork import normalize_saved_track

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = Path(os.getenv('AYLA_FRONTEND_DIR', str(ROOT.parent / 'frontend'))).resolve()
FIXTURES = json.loads((ROOT / 'database/users.fixtures.json').read_text(encoding='utf-8'))


def isolated_uri(uri, name):
    parsed = urlsplit(uri)
    if parsed.scheme not in ('mongodb', 'mongodb+srv') or not name.startswith('ayla_compat_'):
        raise ValueError('Expected a MongoDB server URI and disposable database name')
    query = dict(parse_qsl(parsed.query))
    if parsed.username and 'authSource' not in query:
        query['authSource'] = parsed.path.lstrip('/') or 'admin'
    return urlunsplit(parsed._replace(path='/' + name, query=urlencode(query)))


async def guild_reader(uri):
    client = AsyncMongoClient(uri)
    model = GuildModel()
    model._collection = client.get_default_database().guilds
    try:
        while await asyncio.to_thread(sys.stdin.readline):
            print(await model.get_default_volume(1), flush=True)
    finally:
        await client.close()


async def check(uri):
    name = 'ayla_compat_' + uuid.uuid4().hex[:20]
    test_uri = isolated_uri(uri, name)
    client = AsyncMongoClient(test_uri, serverSelectionTimeoutMS=5000)
    db = client[name]
    model = UserModel()
    model._collection = db.users
    reader = None
    try:
        manifest = json.loads((ROOT / 'database/users.schema.json').read_text(encoding='utf-8'))
        assert manifest['schema_version'] == FIXTURES['schema_version'] == 1
        assert normalize_saved_track(FIXTURES['track']) == FIXTURES['normalized_track']
        await db.users.create_index('user_id', unique=True)
        for entry in FIXTURES['cases']:
            uid = int(entry['id'])
            await model.create_user(uid, 'python-writer', locale='th', future_extension={'preserve': True})
            await model.update_lastfm(uid, 'python-lastfm', 'isolated-fixture-key')
            assert await model.create_playlist_with_tracks(uid, entry['name'], [FIXTURES['track']])
            assert await model.import_playlist(uid, 'Imported', 'https://example.org/playlist', 10)
        result = await asyncio.to_thread(
            subprocess.run,
            ['bun', 'server/integration/storage-compatibility.ts'],
            cwd=FRONTEND_ROOT,
            env={**os.environ, 'MONGODB_URI': test_uri},
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            # Driver errors can contain credentials. Never print subprocess stderr.
            raise RuntimeError('Bun storage compatibility assertions failed (subprocess output withheld)')
        for entry in FIXTURES['cases']:
            uid = int(entry['id'])
            doc = await model.get_user(uid)
            assert doc['locale'] == 'en' and doc['future_extension'] == {'preserve': True}
            assert doc['created_at'].year > 2020
            assert doc['lastfm']['scrobbling'] is False
            playlist = await model.get_playlist(uid, entry['name'])
            assert playlist['tracks'] == [] and playlist['cover'] == 'https://example.org/custom.png'
            imported = await model.get_playlist(uid, 'Imported')
            assert imported['modifications']['additions'][0]['canonical_url'] == FIXTURES['track']['uri']
            assert (await model.remove_track_from_playlist(uid, 'Imported', 0))[0]
            bun_playlist = await model.get_playlist(uid, 'Bun.Mix$')
            assert bun_playlist['tracks'][0]['canonical_url'] == FIXTURES['track']['uri']
            assert await model.set_playlist_cover(uid, 'Bun.Mix$', 'https://example.org/python.png')
            # A Python writer cannot replace a playlist created concurrently by Bun.
            assert not await model.create_playlist_with_tracks(uid, 'Concurrent Bun', [])
            assert len((await model.get_playlist(uid, 'Concurrent Bun'))['tracks']) == 1
            creations = await asyncio.gather(*(model.create_playlist_with_tracks(uid, 'Concurrent Python', [FIXTURES['track']]) for _ in range(20)))
            assert sum(creations) == 1
        uid = 368581475660201987
        doc = await model.get_user(uid)
        assert doc['username'] == 'bun-writer' and doc['settings'] == {} and doc['avatar'] == ''
        assert doc['lastfm']['username'] == 'bun-lastfm'
        await model.update_user(uid, {'locale': 'th'})
        assert await model.add_track_to_playlist(uid, 'Bun Playlist', FIXTURES['track'])
        assert len((await model.get_playlist(uid, 'Bun Playlist'))['tracks']) == 2
        await model.remove_lastfm(uid)
        assert 'lastfm' not in await model.get_user(uid)
        await db.guilds.insert_one({'guild_id': 1, 'default_volume': 25})
        reader = subprocess.Popen(
            [sys.executable, '-m', 'scripts.check_storage_compatibility', '--guild-reader'],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, 'GUILD_CACHE_MODE': 'fresh', 'AYLA_COMPAT_READER_URI': test_uri},
        )
        reader.stdin.write('read\n')
        reader.stdin.flush()
        assert await asyncio.wait_for(asyncio.to_thread(reader.stdout.readline), 10) == '25\n'
        # The parent is a separate Python process. Writes do not use invalidation in the reader.
        await db.guilds.update_one({'guild_id': 1}, {'$set': {'default_volume': 80}})
        reader.stdin.write('read\n')
        reader.stdin.flush()
        assert await asyncio.wait_for(asyncio.to_thread(reader.stdout.readline), 10) == '80\n'
        print('Python/Bun BSON storage compatibility and two-process fresh guild reads passed')
    finally:
        if reader is not None:
            reader.stdin.close()
            try:
                await asyncio.to_thread(reader.wait, timeout=5)
            except subprocess.TimeoutExpired:
                reader.kill()
                await asyncio.to_thread(reader.wait)
        try:
            await client.drop_database(name)
        finally:
            await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guild-reader', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.guild_reader:
        asyncio.run(guild_reader(os.environ['AYLA_COMPAT_READER_URI']))
        return
    uri = os.getenv('AYLA_STORAGE_TEST_URI')
    if not uri:
        raise SystemExit('Set AYLA_STORAGE_TEST_URI to a test MongoDB server URI')
    try:
        asyncio.run(check(uri))
    except Exception as error:
        raise SystemExit(f'Storage compatibility failed: {type(error).__name__} (details withheld)') from None


if __name__ == '__main__':
    main()
