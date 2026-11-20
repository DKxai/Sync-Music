"""Destination operations for additive Spotify and YouTube Music transfers."""
from itertools import count

import httpx

from ..connectors import spotify, ytmusic
from ..connectors.base import ProviderResponseError
from .matcher import catalog_title
from .undo import fingerprint


def batches(ids, size):
    for offset in range(0, len(ids), size):
        yield ids[offset:offset + size]


def artist_names(values):
    if not isinstance(values, list):
        return ''
    return ', '.join(value['name'] for value in values if isinstance(value, dict)
                     and isinstance(value.get('name'), str) and value['name'])


class PlaylistConflict(ValueError):
    pass


class WriteVerificationError(ValueError):
    pass


class SpotifyDestination:
    def __init__(self, http):
        self.http = http
        self._profile = None
        self._search_results = {}

    async def request(self, method, path, **kwargs):
        result = await spotify.request(self.http, method, f'{spotify.API}{path}', **kwargs)
        retry_after = result.headers.get('Retry-After', '')
        if (spotify.rate_limit_waiter.get() is None and result.status_code == 429
                and method == 'GET' and retry_after in ('1', '2', '3', '4', '5')):
            await asyncio.sleep(int(retry_after))
            result = await self.http.request(method, f'{spotify.API}{path}', **kwargs)
        result.raise_for_status()
        return result

    async def search(self, track):
        query = f'isrc:{track.isrc}' if track.isrc else f'{catalog_title(track.title)} {track.artist or ""}'.strip()
        return await self._search(query)

    async def search_fallback(self, track):
        title = catalog_title(track.title)
        if not title or not (track.artist or track.isrc):
            return []
        query = f'{title} {track.artist or ""}'.strip() if track.isrc else title
        return await self._search(query)

    async def _search(self, query):
        if query in self._search_results:
            return self._search_results[query]
        result = await self.request('GET', '/search', params={'q': query, 'type': 'track', 'limit': 10})
        tracks = spotify.response_object(result, 'tra cứu bài hát').get('tracks')
        if not isinstance(tracks, dict) or not isinstance(tracks.get('items'), list):
            raise ProviderResponseError('Spotify tra cứu bài hát: phản hồi thiếu danh sách kết quả.')
        candidates = [
            (item['id'], item.get('name'), artist_names(item.get('artists')),
             item.get('duration_ms'), (item.get('external_ids') or {}).get('isrc'))
            for item in tracks['items'] if isinstance(item, dict) and item.get('id') and item.get('is_playable') is not False
        ]
        if len(self._search_results) >= 256:
            self._search_results.pop(next(iter(self._search_results)))
        self._search_results[query] = candidates
        return candidates

    async def account(self):
        if self._profile is None:
            self._profile = (await self.request('GET', '/me')).json()
        return self._profile.get('account_id') or self._profile.get('id')

    async def _saved(self, ids):
        uris = ','.join(f'spotify:track:{value}' for value in ids)
        response = await self.request('GET', '/me/library/contains', params={'uris': uris})
        return response.json()

    async def recover_likes(self, journal):
        for entry, ids, guard in journal.pending('likes'):
            if guard.get('accepted') and await self._saved(ids) == [True] * len(ids):
                journal.confirm(entry)

    async def apply_likes(self, ids, journal):
        for batch in batches(ids, 40):
            while True:
                saved = await self._saved(batch)
                if not isinstance(saved, list) or len(saved) != len(batch) or any(type(value) is not bool for value in saved):
                    raise WriteVerificationError('Spotify chưa xác minh được các bài đã thích.')
                missing = [value for value, present in zip(batch, saved) if not present]
                if not missing:
                    break
                entry = journal.record('likes', missing)
                uris = ','.join(f'spotify:track:{value}' for value in missing)
                try:
                    with journal.attempt(entry):
                        await self.request('PUT', '/me/library', params={'uris': uris})
                except httpx.HTTPStatusError as exc:
                    if await spotify.wait_after_rate_limit(exc.response):
                        continue
                    raise
                journal.checkpoint(entry, {'accepted': True})
                if await self._saved(missing) != [True] * len(missing):
                    raise WriteVerificationError('Spotify chưa xác minh được lượt thích sau khi ghi.')
                journal.confirm(entry)
                break

    async def undo_likes(self, ids):
        for batch in batches(ids, 40):
            uris = ','.join(f'spotify:track:{value}' for value in batch)
            if await self._saved(batch) == [False] * len(batch):
                continue
            await self.request('DELETE', '/me/library', params={'uris': uris})
            if await self._saved(batch) != [False] * len(batch):
                raise ValueError('Spotify could not verify removed likes.')

    async def _playlist_snapshot(self, playlist_id):
        path = f'/playlists/{playlist_id}'
        account = await self.account()
        params = {'fields': 'snapshot_id,public,collaborative,owner(id)'}
        details = (await self.request('GET', path, params=params)).json()
        if not details.get('snapshot_id') or not details.get('owner') or details.get('public') is None:
            details = (await self.request('GET', path)).json()
        owner = (details.get('owner') or {}).get('id')
        if not owner or owner not in (self._profile.get('id'), account):
            raise PlaylistConflict('Spotify không xác nhận playlist thuộc tài khoản đang kết nối.')
        if details.get('public') is True or details.get('collaborative'):
            raise PlaylistConflict('Playlist Spotify đang công khai hoặc bật cộng tác. Chỉ ghi vào playlist riêng tư.')
        if details.get('public') is not False or not details.get('snapshot_id'):
            raise PlaylistConflict('Spotify thiếu thông tin quyền riêng tư hoặc phiên bản của playlist.')
        return details['snapshot_id']

    async def playlist_value(self, playlist_id):
        before = await self._playlist_snapshot(playlist_id)
        for _ in range(2):
            ids = await self.playlist_tracks(playlist_id)
            after = await self._playlist_snapshot(playlist_id)
            if before == after:
                return ids, after
            before = after
        raise PlaylistConflict('Playlist Spotify thay đổi trong lúc đọc. Hệ thống sẽ thử lại ở lượt sau.')

    async def undo_playlist(self, playlist_id, ids, guard):
        try:
            current, snapshot = await self.playlist_value(playlist_id)
        except PlaylistConflict:
            return False
        if fingerprint(current) == guard['before']:
            return True
        if snapshot != guard['snapshot'] or fingerprint(current) != guard['after'] or not guard['unique']:
            return False
        items = [{'uri': f'spotify:track:{value}'} for value in dict.fromkeys(ids)]
        await self.request('DELETE', f'/playlists/{playlist_id}/items',
                           json={'snapshot_id': snapshot, 'items': items})
        try:
            observed, restored_snapshot = await self.playlist_value(playlist_id)
        except PlaylistConflict:
            return False
        return restored_snapshot if fingerprint(observed) == guard['before'] else False

    async def create_playlist(self, title):
        result = await self.request('POST', '/me/playlists', json={'name': title, 'public': False})
        playlist_id = spotify.response_object(result, 'tạo playlist').get('id')
        if not isinstance(playlist_id, str) or not playlist_id:
            raise ProviderResponseError('Spotify tạo playlist: phản hồi thiếu mã playlist.')
        return playlist_id

    async def playlist_tracks(self, playlist_id):
        tracks = []
        url = f'/playlists/{playlist_id}/items'
        for offset in count(step=50):
            response = await self.request('GET', url, params={'offset': offset, 'limit': 50,
                                                             'fields': 'items(item(id),track(id)),next'})
            data = response.json()
            if 'next' not in data or not isinstance(data.get('items'), list):
                raise PlaylistConflict('Spotify trả về danh sách bài hát không đầy đủ.')
            for row in data['items']:
                track = row.get('item') or row.get('track') or {}
                tracks.append(track.get('id'))
            if not data.get('next'):
                return tracks
            if len(data['items']) != 50:
                raise PlaylistConflict('Spotify trả về một trang playlist không đầy đủ.')

    async def insert(self, playlist_id, ids, position=None):
        body = {'uris': [f'spotify:track:{value}' for value in ids]}
        if position is not None:
            body['position'] = position
        response = await self.request('POST', f'/playlists/{playlist_id}/items', json=body)
        return spotify.response_object(response, 'thêm bài vào playlist').get('snapshot_id')

    async def append(self, playlist_id, ids):
        return await self.insert(playlist_id, ids)


class YouTubeDestination:
    def __init__(self, api):
        self.api = api
        self._likes = None

    async def search(self, track):
        results = await ytmusic.call('tra cứu bài hát', self.api.search,
                                    f'{catalog_title(track.title)} {track.artist or ""}', filter='songs', limit=8)
        return self._candidates(results)

    async def search_fallback(self, track):
        results = await ytmusic.call('tra cứu video âm nhạc', self.api.search,
                                    f'{catalog_title(track.title)} {track.artist or ""}', filter='videos', limit=8)
        return self._candidates(results, official_video=True)

    @staticmethod
    def _candidates(results, *, official_video=False):
        if not isinstance(results, list):
            raise ProviderResponseError('YouTube Music trả về kết quả tìm kiếm không hợp lệ.')
        return [
            (item['videoId'], item.get('title'), artist_names(item.get('artists')),
             ytmusic.duration_ms(item), None)
            for item in results if isinstance(item, dict) and item.get('videoId')
            and (not official_video or item.get('videoType') in ('MUSIC_VIDEO_TYPE_ATV', 'MUSIC_VIDEO_TYPE_OMV'))
        ]

    async def account(self):
        return await ytmusic.call('đọc thông tin tài khoản', ytmusic.destination_account, self.api)

    async def owns_playlist(self, playlist_id):
        playlists = await ytmusic.call('đọc danh sách playlist', self.api.get_library_playlists, limit=None)
        return any(item.get('playlistId') == playlist_id and item.get('owned') for item in playlists)

    async def apply_likes(self, ids, journal):
        from ytmusicapi import LikeStatus

        if self._likes is None:
            self._likes = await self.liked_ids()
        entries = []
        for video_id in ids:
            if video_id in self._likes:
                continue
            entry = journal.record('likes', [video_id])
            await ytmusic.call('ghi lượt thích', self.api.rate_song, video_id, LikeStatus.LIKE)
            journal.checkpoint(entry, {'accepted': True})
            entries.append((entry, video_id))
        if not entries:
            return journal.stats['liked_added']
        observed = await self.liked_ids()
        self._likes = observed
        for entry, video_id in entries:
            if video_id in observed:
                journal.confirm(entry)
        if any(video_id not in observed for _, video_id in entries):
            raise WriteVerificationError('YouTube Music chưa xác minh được lượt thích sau khi ghi.')
        return journal.stats['liked_added']

    async def recover_likes(self, journal):
        pending = journal.pending('likes')
        if not pending:
            return
        self._likes = await self.liked_ids()
        for entry, ids, guard in pending:
            if guard.get('accepted') and all(video_id in self._likes for video_id in ids):
                journal.confirm(entry)

    async def undo_likes(self, ids):
        from ytmusicapi import LikeStatus

        existing = await self.liked_ids()
        for video_id in ids:
            if video_id in existing:
                await ytmusic.call('hoàn tác lượt thích', self.api.rate_song, video_id, LikeStatus.INDIFFERENT)
        if set(ids).intersection(await self.liked_ids()):
            raise ValueError('YouTube Music could not verify removed likes.')

    async def liked_ids(self):
        tracks = await ytmusic.call('đọc lượt thích', ytmusic.liked_tracks, self.api)
        return {item.get('videoId') for item in tracks}

    async def undo_playlist(self, playlist_id, ids, guard):
        tracks = await self._playlist_rows(playlist_id)
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
            await ytmusic.call('hoàn tác bài trong playlist', self.api.remove_playlist_items, playlist_id, remove)
            observed = await self._playlist_rows(playlist_id)
            return not {item.get('setVideoId') for item in observed}.intersection(guard['item_ids'])
        return True

    async def create_playlist(self, title):
        result = await ytmusic.call('tạo playlist', self.api.create_playlist, title, 'Synced by Sync-Music',
                                    privacy_status='PRIVATE')
        if not isinstance(result, str) or not result:
            raise ValueError('YouTube Music did not confirm playlist creation.')
        return result

    async def _playlist_rows(self, playlist_id):
        playlist = await ytmusic.call('đọc playlist', self.api.get_playlist, playlist_id, limit=None)
        if not isinstance(playlist, dict) or not isinstance(playlist.get('tracks'), list):
            raise ProviderResponseError('YouTube Music đọc playlist: phản hồi thiếu danh sách bài hát.')
        return playlist['tracks']

    async def playlist_tracks(self, playlist_id):
        return [row.get('videoId') for row in await self._playlist_rows(playlist_id)]

    async def append(self, playlist_id, ids):
        result = await ytmusic.call('thêm bài vào playlist', self.api.add_playlist_items, playlist_id, ids,
                                    duplicates=True)
        if not isinstance(result, dict) or result.get('status') != 'STATUS_SUCCEEDED':
            raise WriteVerificationError('YouTube Music chưa xác nhận lô bài đã thêm.')
        rows = result.get('playlistEditResults') or []
        if (len(rows) == len(ids) and all(isinstance(row, dict) and row.get('setVideoId')
                and row.get('videoId') == video_id for row, video_id in zip(rows, ids))):
            return [row['setVideoId'] for row in rows]
        return None

    async def verify_playlist(self, playlist_id, expected, receipts):
        rows = await self._playlist_rows(playlist_id)
        if [row.get('videoId') for row in rows] != expected:
            return False
        seen = [row.get('setVideoId') for row in rows]
        return bool(receipts and seen[-len(receipts):] == receipts and len(set(receipts)) == len(receipts))
