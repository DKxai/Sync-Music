"""Bounded retries for read-only requests and shared YouTube Music cooldowns."""
import time
from email.utils import parsedate_to_datetime
from math import ceil
from urllib.parse import urlsplit

from requests import ConnectionError, Session, Timeout
from requests.structures import CaseInsensitiveDict

from . import requests_log
from ..db import connect, get_setting


class RateLimited(Exception):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__('YouTube Music đang giới hạn yêu cầu.')


def retry_seconds(value, default=60):
    if value:
        try:
            return max(1, int(value))
        except ValueError:
            try:
                return max(1, ceil(parsedate_to_datetime(value).timestamp() - time.time()))
            except (ValueError, TypeError, OverflowError):
                pass
    return default


def api_retry_at():
    deadline = int(get_setting('ytmusic_api_retry_at') or 0)
    return deadline if deadline > time.time() else None


class MusicSession(Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', 30)
        if 'json' in kwargs:
            # requests serializes json as plain UTF-8. Some browser exports and
            # ytmusicapi defaults advertise gzip even for that uncompressed body.
            headers = CaseInsensitiveDict(kwargs.get('headers') or {})
            headers.pop('Content-Encoding', None)
            kwargs['headers'] = headers
        path = urlsplit(url).path
        read_only = method.upper() == 'GET' or path in {
            '/youtubei/v1/browse', '/youtubei/v1/search', '/youtubei/v1/account/account_menu',
        }
        operation = 'search' if path.endswith('/search') else 'read' if read_only else 'write'
        for attempt in range(2):
            if deadline := api_retry_at():
                raise RateLimited(deadline)
            try:
                response = super().request(method, url, **kwargs)
            except (Timeout, ConnectionError):
                requests_log.record('ytmusic', operation, 0)
                if not read_only or attempt:
                    raise
                time.sleep(1)
                continue
            requests_log.record('ytmusic', operation, response.status_code)
            if response.status_code == 429:
                delay = retry_seconds(response.headers.get('Retry-After'))
                with connect() as con:
                    row = con.execute(
                        """INSERT INTO settings(key,value) VALUES ('ytmusic_api_retry_at',?)
                           ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(settings.value AS INTEGER),
                           CAST(excluded.value AS INTEGER)) RETURNING value""",
                        (str(ceil(time.time() + delay)),),
                    ).fetchone()
                response.close()
                raise RateLimited(int(row[0]))
            if (read_only and not attempt and response.status_code in (502, 503, 504)
                    and 'Retry-After' not in response.headers):
                response.close()
                time.sleep(1)
                continue
            return response
