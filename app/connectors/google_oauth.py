"""Google device authorization for a configured limited-input OAuth client."""

import asyncio
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..provider_tokens import save_token
from .base import NotConfigured
from .ytmusic_transport import retry_seconds

DEVICE_URL = 'https://oauth2.googleapis.com/device/code'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/youtube'
_lock = asyncio.Lock()


@dataclass
class Grant:
    handle: str
    device_code: str
    user_code: str
    verification_url: str
    expires_at: float
    next_poll: float
    interval: int


_grant: Grant | None = None


def configured() -> bool:
    return bool(settings.ytm_client_id and settings.ytm_client_secret)


async def start() -> dict:
    global _grant
    if not configured():
        raise NotConfigured('Set YTM_CLIENT_ID and YTM_CLIENT_SECRET for a limited-input Google OAuth client')
    async with _lock:
        if _grant is None or _grant.expires_at <= time.monotonic():
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(DEVICE_URL, data={
                    'client_id': settings.ytm_client_id, 'scope': SCOPE,
                })
                response.raise_for_status()
            data = response.json()
            verification_url = data.get('verification_url') or data['verification_uri']
            parsed = urlparse(verification_url)
            if parsed.scheme != 'https' or parsed.hostname not in {
                'google.com', 'www.google.com', 'accounts.google.com',
            }:
                raise ValueError('Unexpected Google verification URL')
            now = time.monotonic()
            interval = max(5, int(data.get('interval', 5)))
            _grant = Grant(secrets.token_urlsafe(24), data['device_code'], data['user_code'],
                           verification_url, now + min(1800, int(data['expires_in'])),
                           now + interval, interval)
        return {'handle': _grant.handle, 'user_code': _grant.user_code,
                'verification_url': _grant.verification_url, 'interval': _grant.interval}


async def poll(handle: str) -> dict:
    global _grant
    async with _lock:
        now = time.monotonic()
        grant = _grant
        if grant is None or not secrets.compare_digest(grant.handle, handle):
            return {'status': 'expired'}
        if now >= grant.expires_at:
            _grant = None
            return {'status': 'expired'}
        if now < grant.next_poll:
            return {'status': 'pending', 'interval': grant.interval}
        grant.next_poll = now + grant.interval
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(TOKEN_URL, data={
                    'client_id': settings.ytm_client_id,
                    'client_secret': settings.ytm_client_secret,
                    'device_code': grant.device_code,
                    'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                })
        except httpx.TransportError:
            delay = min(60, grant.interval * 2)
            grant.next_poll = time.monotonic() + delay
            return {'status': 'pending', 'interval': delay}
        if response.status_code == 429 or response.status_code >= 500:
            delay = max(grant.interval, retry_seconds(response.headers.get('Retry-After'), grant.interval * 2))
            grant.next_poll = time.monotonic() + delay
            return {'status': 'pending', 'interval': min(delay, max(1, int(grant.expires_at - time.monotonic()) + 1))}
        data = response.json()
        error = data.get('error')
        if error in ('authorization_pending', 'slow_down'):
            if error == 'slow_down':
                grant.interval += 5
                grant.next_poll = time.monotonic() + grant.interval
            return {'status': 'pending', 'interval': grant.interval}
        if error in ('access_denied', 'expired_token'):
            _grant = None
            return {'status': 'denied' if error == 'access_denied' else 'expired'}
        response.raise_for_status()
        if not data.get('refresh_token') or not data.get('access_token'):
            _grant = None
            raise ValueError('Google did not issue a refreshable token')
        data['expires_at'] = int(time.time()) + int(data['expires_in'])
        data['client_id'] = settings.ytm_client_id
        save_token('ytmusic', data, reconnect=True)
        _grant = None
        return {'status': 'connected'}


def cancel():
    global _grant
    _grant = None
