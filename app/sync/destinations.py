"""Destination operations for additive Spotify and YouTube Music transfers."""
import asyncio
from collections import Counter

import httpx

from ..connectors import spotify, ytmusic


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

    async def like(self, ids):
        for batch in batches(ids, 40):
            uris = ','.join(f'spotify:track:{value}' for value in batch)
            await self.request('PUT', '/me/library', params={'uris': uris})
            result = await self.request('GET', '/me/library/contains', params={'uris': uris})
            if result.json() != [True] * len(batch):
                raise ValueError('Spotify could not verify saved tracks.')

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
        for batch in batches(ids, 100):
            await self.request('POST', f'/playlists/{playlist_id}/items',
                               json={'uris': [f'spotify:track:{value}' for value in batch]})


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

    async def like(self, ids):
        from ytmusicapi import LikeStatus

        def apply():
            for video_id in ids:
                self.api.rate_song(video_id, LikeStatus.LIKE)
            data = self.api.get_liked_songs(limit=100000)['tracks']
            if not set(ids).issubset({item.get('videoId') for item in data}):
                raise ValueError('YouTube Music could not verify saved tracks.')

        await asyncio.to_thread(apply)

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
        for batch in batches(ids, 100):
            result = await asyncio.to_thread(self.api.add_playlist_items, playlist_id, batch, duplicates=True)
            if not isinstance(result, dict) or result.get('status') != 'STATUS_SUCCEEDED':
                raise ValueError('YouTube Music did not confirm the track batch.')


def missing_occurrences(wanted, existing):
    counts = Counter(existing)
    missing = []
    for track_id in wanted:
        if counts[track_id]:
            counts[track_id] -= 1
        else:
            missing.append(track_id)
    return missing
