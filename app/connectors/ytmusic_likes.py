"""Read the authenticated LM playlist, including both YouTube pagination protocols."""
from urllib.parse import urlencode

from ytmusicapi.continuations import get_continuation_token
from ytmusicapi.parsers import playlists

from .base import ProviderResponseError


class LikedPlaylistError(ProviderResponseError):
    pass


def incomplete(code, page):
    return LikedPlaylistError(
        f'YouTube Music chưa trả đủ bài đã thích (trang {page}, {code}). '
        'Dữ liệu đã nhập trước đó được giữ lại.'
    )


def signed_in(response):
    context = response.get('responseContext', {})
    flags = [p.get('value') for service in context.get('serviceTrackingParams', [])
             for p in service.get('params', []) if p.get('key') == 'logged_in']
    if context.get('mainAppWebResponseContext', {}).get('loggedOut') is True or '0' in flags:
        raise LikedPlaylistError('YouTube Music yêu cầu đăng nhập lại. Hãy cập nhật tệp phiên hoặc cấp lại quyền Google.')


def first_shelf(response):
    contents = response.get('contents', {})
    layout = contents.get('twoColumnBrowseResultsRenderer') or contents.get('singleColumnBrowseResultsRenderer') or {}
    panels = [layout.get('secondaryContents', {})]
    tabs = [item.get('tabRenderer', {}) for item in layout.get('tabs', [])]
    panels.extend(tab.get('content', {}) for tab in tabs if len(tabs) == 1 or tab.get('selected'))
    shelves = []
    for panel in panels:
        for section in panel.get('sectionListRenderer', {}).get('contents', []):
            if 'musicPlaylistShelfRenderer' in section:
                shelves.append(section['musicPlaylistShelfRenderer'])
            elif section.get('musicShelfRenderer', {}).get('playlistId') in ('LM', 'VLLM'):
                shelves.append(section['musicShelfRenderer'])
    if len(shelves) != 1:
        raise incomplete('LM_SHELF', 1)
    return shelves[0]


def next_shelf(response, page):
    legacy = response.get('continuationContents', {}).get('musicPlaylistShelfContinuation')
    if isinstance(legacy, dict):
        return legacy
    pages = [action['appendContinuationItemsAction'] for key in ('onResponseReceivedActions', 'onResponseReceivedEndpoints')
             for action in response.get(key, []) if 'appendContinuationItemsAction' in action]
    if len(pages) != 1:
        raise incomplete('LM_NEXT_PAGE', page)
    return {'contents': pages[0].get('continuationItems')}


def parse_track(entry, page):
    try:
        parsed = playlists.parse_playlist_items([entry])
        if len(parsed) == 1 and (parsed[0].get('videoId') or not
                                 entry['musicResponsiveListItemRenderer'].get('playlistItemData', {}).get('videoId')):
            return parsed[0]
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        pass
    # Optional menu, thumbnail and album fields change independently of track identity.
    row = entry['musicResponsiveListItemRenderer']
    video_id = row.get('playlistItemData', {}).get('videoId')
    columns = row.get('flexColumns', [])
    runs = [run for column in columns for run in column.get('musicResponsiveListItemFlexColumnRenderer', {})
            .get('text', {}).get('runs', [])]
    for run in runs:
        watch_id = run.get('navigationEndpoint', {}).get('watchEndpoint', {}).get('videoId')
        if watch_id and (not video_id or video_id == watch_id) and run.get('text'):
            artists = [dict(name=artist['text']) for artist in runs if artist.get('text')
                       and artist.get('navigationEndpoint', {}).get('browseEndpoint', {}).get('browseId', '').startswith('UC')]
            return {'videoId': watch_id, 'title': run['text'], 'artists': artists}
    raise incomplete('LM_TRACK', page)


def read_liked_tracks(api, limit=None):
    page = 1
    tracks = []
    seen = set()
    response = api._send_request('browse', {'browseId': 'VLLM'})
    while True:
        if not isinstance(response, dict):
            raise incomplete('LM_RESPONSE', page)
        try:
            signed_in(response)
            shelf = first_shelf(response) if page == 1 else next_shelf(response, page)
            rows = shelf.get('contents')
            if not isinstance(rows, list):
                raise incomplete('LM_ROWS', page)
            inline = bool(rows and 'continuationItemRenderer' in rows[-1])
            entries = rows[:-1] if inline else rows
            if page == 1 and not inline and not shelf.get('continuations') and len(entries) == 1:
                style = entries[0].get('messageRenderer', {}).get('style', {})
                if style.get('value') == 'RENDER_STYLE_EMPTY_STATE':
                    return []
            if any('musicResponsiveListItemRenderer' not in entry for entry in entries):
                raise incomplete('LM_ROW_TYPE', page)
            tracks.extend(parse_track(entry, page) for entry in entries)
            legacy = shelf.get('continuations', [])
            if not inline and not legacy:
                return tracks
            if inline and not legacy:
                token = get_continuation_token(rows)
            elif not inline and isinstance(legacy, list) and len(legacy) == 1:
                token = legacy[0].get('nextContinuationData', {}).get('continuation')
            else:
                raise incomplete('LM_PAGING', page)
            if not isinstance(token, str) or not token or token in seen:
                raise incomplete('LM_CURSOR', page)
        except ProviderResponseError:
            raise
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise incomplete('LM_FORMAT', page) from None
        if limit is not None and len(tracks) >= limit:
            return tracks
        seen.add(token)
        page += 1
        if inline:
            response = api._send_request('browse', {'continuation': token})
        else:
            params = urlencode({'ctoken': token, 'continuation': token})
            response = api._send_request('browse', {'browseId': 'VLLM'}, '&' + params)
