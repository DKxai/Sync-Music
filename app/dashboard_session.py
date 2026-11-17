"""Persistent dashboard sign-in with a server-side session token."""
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import settings
from .db import connect

COOKIE = '__Host-sync_music'
LIFETIME = 14 * 86400
router = APIRouter()


def _digest(token: str) -> str:
    return hmac.new(settings.api_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def _same_origin(request: Request) -> bool:
    origin = request.headers.get('origin', '')
    if origin:
        parsed = urlsplit(origin)
        return parsed.scheme == 'https' and parsed.netloc == request.headers.get('host')
    return request.headers.get('sec-fetch-site') == 'same-origin'


def _valid_key(value):
    return bool(value and hmac.compare_digest(value.encode(), settings.api_key.encode()))


def admin(request: Request, x_api_key: str | None = Header(default=None)):
    if _valid_key(x_api_key):
        return
    token = request.cookies.get(COOKIE)
    if token and len(token) <= 100:
        with connect() as con:
            found = con.execute(
                'SELECT 1 FROM dashboard_sessions WHERE token_hash=? AND expires_at>?',
                (_digest(token), int(time.time())),
            ).fetchone()
        if found:
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and not _same_origin(request):
                raise HTTPException(403, 'Dashboard changes require a same-origin request.')
            return
    raise HTTPException(401, 'Sign in to Sync-Music.')


@router.post('/v1/session')
def login(x_api_key: str | None = Header(default=None)):
    if not _valid_key(x_api_key):
        raise HTTPException(401, 'Invalid admin API key.')
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connect() as con:
        con.execute('DELETE FROM dashboard_sessions WHERE expires_at<=?', (now,))
        con.execute('INSERT INTO dashboard_sessions VALUES (?,?)', (_digest(token), now + LIFETIME))
    response = JSONResponse({'connected': True}, headers={'Cache-Control': 'no-store'})
    response.set_cookie(COOKIE, token, max_age=LIFETIME, secure=True, httponly=True,
                        samesite='strict', path='/')
    return response


@router.delete('/v1/session', dependencies=[Depends(admin)])
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        with connect() as con:
            con.execute('DELETE FROM dashboard_sessions WHERE token_hash=?', (_digest(token),))
    response = JSONResponse({'connected': False}, headers={'Cache-Control': 'no-store'})
    response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
    return response
