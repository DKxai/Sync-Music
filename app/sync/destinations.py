"""Destination operations for additive Spotify and YouTube Music transfers."""
import asyncio

import httpx

from ..connectors import spotify
from .undo import fingerprint


def batches(ids, size):
    for offset in range(0, len(ids), size):
        yield ids[offset:offset + size]


def artist_names(values):
    return ', '.join(value['name'] for value in values or [] if value.get('name'))


class SpotifyDestination:
    def __init__(self, http):
        self.http = http

    async def request(self, method, path, **kwargs):
        result = await self.http.request(method, f'{spotify.API}{path}', **kwargs)
        result.raise_for_status()
        return result

    async def search(self, track):
        query = f'isrc:{track.isrc}' if track.isrc else f'{track.title} {track.artist or ""}'
        result = await self.request('GET', '/search', params={'q': query, 'type': 'track', 'limit': 8})
        return [
            (item['id'], item.get('name'), artist_names(item.get('artists')),
             item.get('duration_ms'), (item.get('external_ids') or {}).get('isrc'))
            for item in result.json()['tracks']['items'] if item.get('id') and item.get('is_playable') is not False
        ]

    async def account(self):
        profile = (await self.request('GET', '/me')).json()
        return profile.get('account_id') or profile.get('id')

    async def _saved(self, ids):
        uris = ','.join(f'spotify:track:{value}' for value in ids)
        response = await self.request('GET', '/me/library/contains', params={'uris': uris})
        return response.json()

    async def apply_likes(self, ids, journal):
        for batch in batches(ids, 40):
            saved = await self._saved(batch)
            if not isinstance(saved, list) or len(saved) != len(batch) or any(type(value) is not bool for value in saved):
                raise ValueError('Spotify did not confirm the existing likes.')
            batch = [value for value, present in zip(batch, saved) if not present]
            if not batch:
                continue
            entry = journal.record('likes', batch)
            uris = ','.join(f'spotify:track:{value}' for value in batch)
            with journal.attempt(entry):
                await self.request('PUT', '/me/library', params={'uris': uris})
            if await self._saved(batch) != [True] * len(batch):
                raise ValueError('Spotify could not verify saved tracks.')
            journal.confirm(entry)

    async def undo_likes(self, ids):
        for batch in batches(ids, 40):
            uris = ','.join(f'spotify:track:{value}' for value in batch)
            if await self._saved(batch) == [False] * len(batch):
                continue
            await self.request('DELETE', '/me/library', params={'uris': uris})
            if await self._saved(batch) != [False] * len(batch):
                raise ValueError('Spotify could not verify removed likes.')

    async def playlist_value(self, playlist_id):
        path = f'/playlists/{playlist_id}'
        params = {'fields': 'snapshot_id,public,collaborative'}
        before = (await self.request('GET', path, params=params)).json()
        ids = await self.playlist_tracks(playlist_id)
        after = (await self.request('GET', path, params=params)).json()
        if not before.get('snapshot_id') or before != after or after.get('public') is not False or after.get('collaborative'):
            raise ValueError('Spotify playlist changed or is no longer private.')
        return ids, after['snapshot_id']

    async def undo_playlist(self, playlist_id, ids, guard):
        current, snapshot = await self.playlist_value(playlist_id)
        if fingerprint(current) == guard['before']:
            return True
        if snapshot != guard['snapshot'] or fingerprint(current) != guard['after'] or not guard['unique']:
            return False
        items = [{'uri': f'spotify:track:{value}'} for value in dict.fromkeys(ids)]
        await self.request('DELETE', f'/playlists/{playlist_id}/items',
                           json={'snapshot_id': snapshot, 'items': items})
        observed, restored_snapshot = await self.playlist_value(playlist_id)
        return restored_snapshot if fingerprint(observed) == guard['before'] else False

    async def create_playlist(self, title):
        result = await self.request('POST', '/me/playlists', json={'name': title, 'public': False})
        return result.json()['id']

    async def playlist_tracks(self, playlist_id):
        tracks = []
        url = f'/playlists/{playlist_id}/items'
        for offset in range(0, 10050, 50):
            response = await self.request('GET', url, params={'offset': offset, 'limit': 50})
            data = response.json()
            for row in data['items']:
                track = row.get('item') or row.get('track') or {}
                tracks.append(track.get('id'))
            if not data.get('next'):
                return tracks
        raise ValueError('Spotify playlist exceeds the verification limit.')

    async def append(self, playlist_id, ids):
        response = None
        for batch in batches(ids, 100):
            response = await self.request('POST', f'/playlists/{playlist_id}/items',
                                          json={'uris': [f'spotify:track:{value}' for value in batch]})
        return response.json().get('snapshot_id') if response else None


class YouTubeDestination:
    def __init__(self, api):
        self.api = api

    async def search(self, track):
        results = await asyncio.to_thread(self.api.search, f'{track.title} {track.artist or ""}',
                                          filter='songs', limit=8)
        return [
            (item['videoId'], item.get('title'), artist_names(item.get('artists')),
             (item.get('duration_seconds') or 0) * 1000 or None, None)
            for item in results if item.get('videoId')
        ]

    async def account(self):
        info = await asyncio.to_thread(self.api.get_account_info)
        return info.get('channelHandle')

    async def apply_likes(self, ids, journal):
        from ytmusicapi import LikeStatus

        existing = await self.liked_ids()
        entries = []
        for video_id in ids:
            if video_id in existing:
                continue
            entry = journal.record('likes', [video_id])
            await asyncio.to_thread(self.api.rate_song, video_id, LikeStatus.LIKE)
            entries.append((entry, video_id))
        observed = await self.liked_ids()
        for entry, video_id in entries:
            if video_id in observed:
                journal.confirm(entry)
        if any(video_id not in observed for _, video_id in entries):
            raise ValueError('YouTube Music could not verify saved tracks.')
        return journal.stats['liked_added']

    async def undo_likes(self, ids):
        from ytmusicapi import LikeStatus

        existing = await self.liked_ids()
        for video_id in ids:
            if video_id in existing:
                await asyncio.to_thread(self.api.rate_song, video_id, LikeStatus.INDIFFERENT)
        if set(ids).intersection(await self.liked_ids()):
            raise ValueError('YouTube Music could not verify removed likes.')

    async def liked_ids(self):
        limit = 100000
        data = await asyncio.to_thread(self.api.get_liked_songs, limit=limit)
        if not isinstance(data.get('tracks'), list) or len(data['tracks']) >= limit:
            raise ValueError('YouTube Music liked tracks could not be verified.')
        return {item.get('videoId') for item in data['tracks']}

    async def undo_playlist(self, playlist_id, ids, guard):
        playlist = await asyncio.to_thread(self.api.get_playlist, playlist_id, limit=None)
        tracks = playlist['tracks']
        entries = {track.get('setVideoId'): track for track in tracks if track.get('setVideoId')}
        remove = []
        for video_id, item_id in zip(ids, guard.get('item_ids', []), strict=True):
            item = entries.get(item_id)
            if item is None:
                continue
            if item.get('videoId') != video_id:
                return False
            remove.append(item)
        if remove:
            await asyncio.to_thread(self.api.remove_playlist_items, playlist_id, remove)
            observed = await asyncio.to_thread(self.api.get_playlist, playlist_id, limit=None)
            return not {item.get('setVideoId') for item in observed['tracks']}.intersection(guard['item_ids'])
        return True

    async def create_playlist(self, title):
        result = await asyncio.to_thread(self.api.create_playlist, title, 'Synced by Sync-Music',
                                         privacy_status='PRIVATE')
        if not isinstance(result, str) or not result:
            raise ValueError('YouTube Music did not confirm playlist creation.')
        return result

    async def playlist_tracks(self, playlist_id):
        playlist = await asyncio.to_thread(self.api.get_playlist, playlist_id, limit=None)
        return [row.get('videoId') for row in playlist['tracks']]

    async def append(self, playlist_id, ids):
        playlist = await asyncio.to_thread(self.api.get_playlist, playlist_id, limit=None)
        before = {item.get('setVideoId') for item in playlist['tracks']}
        for batch in batches(ids, 100):
            result = await asyncio.to_thread(self.api.add_playlist_items, playlist_id, batch, duplicates=True)
            if not isinstance(result, dict) or result.get('status') != 'STATUS_SUCCEEDED':
                raise ValueError('YouTube Music did not confirm the track batch.')
        playlist = await asyncio.to_thread(self.api.get_playlist, playlist_id, limit=None)
        added = [item for item in playlist['tracks'] if item.get('setVideoId') not in before]
        return [item['setVideoId'] for item in added] if (
            [item.get('videoId') for item in added] == ids and all(item.get('setVideoId') for item in added)
        ) else None

