"""Single-process API, scheduler and manual sync queue."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import time
import json
from typing import Literal
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import httpx

from .config import settings, validate
from .db import init_db, connect, get_setting, prune_history
from . import apple_authorization
from .connectors import apple, google_oauth, requests_log, spotify, ytmusic
from .connectors.base import Item, NotConfigured, ProviderResponseError
from .dashboard import HTML
from .dashboard_session import admin, router as dashboard_session_router
from .provider_tokens import has_token, invalidate_account
from .sync.engine import POLL_LOCK, _canonical, poll
from .sync import direct, direct_results, review, schedule, transfer, writer

PROFILE_HOME = '/'
PROVIDERS = ('spotify', 'ytmusic', 'apple')
_jobs: dict[str, dict] = {}
_active: dict[str, str] = {}
_tasks: set[asyncio.Task] = set()

def timestamp():
    return datetime.now(timezone.utc).isoformat()


def persist_import(job):
    with connect() as con:
        con.execute(
            """INSERT INTO import_jobs VALUES (?,?,?,?) ON CONFLICT(job_id)
               DO UPDATE SET state=excluded.state,payload_json=excluded.payload_json""",
            (job['job_id'], job['provider'], job['state'], json.dumps(job)),
        )


def start_import(job):
    job_id, provider = job['job_id'], job['provider']
    _jobs[job_id] = job
    _active[provider] = job_id
    task = asyncio.create_task(_run_job(job_id, provider))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def recover_imports():
    _jobs.clear()
    _active.clear()
    with connect() as con:
        rows = con.execute(
            """SELECT payload_json FROM import_jobs WHERE state IN ('queued','running') OR job_id IN (
                SELECT job_id FROM import_jobs ORDER BY rowid DESC LIMIT 30)
               ORDER BY rowid DESC""",
        ).fetchall()
    for row in reversed(rows):
        job = json.loads(row['payload_json'])
        if job['state'] in ('queued', 'running'):
            start_import(job)
        else:
            _jobs[job['job_id']] = job

async def _run_job(job_id: str, provider: str):
    job = _jobs[job_id]
    job['state'] = 'running'
    job.setdefault('started_at', timestamp())
    persist_import(job)
    try:
        while True:
            result = await poll(provider)
            retry_at = result.get('retry_at')
            if retry_at is None:
                break
            job['retry_at'] = retry_at
            persist_import(job)
            # poll() releases its lock before returning a cooldown.
            await asyncio.sleep(max(0, retry_at - time.time()))
        if job['trigger'] == 'scheduled':
            schedule.record(provider, poll_minutes()[provider], unchanged=result.get('unchanged', False))
        job['result'] = result
        job['state'] = 'failed' if 'error' in result else 'completed'
    except Exception as exc:
        job['result'] = {'error': f'{type(exc).__name__}: {str(exc)[:250]}'}
        job['state'] = 'failed'
    finally:
        if job['state'] in ('completed', 'failed'):
            job.pop('retry_at', None)
            job['finished_at'] = timestamp()
            persist_import(job)
        if _active.get(provider) == job_id:
            del _active[provider]
        # Keep up to 30 recent jobs in RAM (past results persist in SQLite).
        for old_id in list(_jobs)[:-30]:
            if old_id not in _active.values():
                _jobs.pop(old_id, None)

def submit(provider: str, trigger='manual'):
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider')
    if provider in _active:
        return {'job_id': _active[provider], 'provider': provider, 'already_running': True}
    job_id = uuid4().hex
    job = {'job_id': job_id, 'provider': provider, 'trigger': trigger,
           'state': 'queued', 'requested_at': timestamp()}
    persist_import(job)
    start_import(job)
    return {'job_id': job_id, 'provider': provider, 'already_running': False}

def poll_minutes():
    return {'spotify': settings.spotify_minutes, 'ytmusic': settings.ytm_minutes,
            'apple': settings.apple_minutes}


def active_direct_schedule():
    rule = direct.rule() if direct.ENABLED else None
    if rule and rule['source'] == direct.ALL and not provider_ready('apple'):
        rule = {**rule, 'source': direct.BOTH, 'target': direct.BOTH}
    return rule if direct_schedule_ready(rule) else None


async def scheduled_checks():
    rule = active_direct_schedule()
    managed = {provider for pair in direct.directions(rule['source'], rule['target'])
               for provider in pair} if rule else set()
    now = time.time()
    if rule and (schedule.next_check('direct') or 0) <= now:
        direct.submit(rule['source'], rule['target'], scheduled=True)
    for provider in PROVIDERS:
        if provider not in managed and provider_ready(provider) and (schedule.next_check(provider) or 0) <= now:
            submit(provider, 'scheduled')


def provider_ready(provider: str) -> bool:
    if provider == 'ytmusic':
        mode = ytmusic.auth_mode()
        browser = mode != 'oauth' and settings.ytm_auth_path.is_file()
        oauth = mode != 'browser' and google_oauth.configured() and has_token('ytmusic')
        return browser or oauth
    if provider == 'apple':
        if not apple.developer_configured():
            return False
        if has_token('apple'):
            return True
        try:
            return bool(settings.apple_user_token_path.read_text().strip())
        except (OSError, UnicodeError):
            return False
    return bool(settings.spotify_client_id and settings.spotify_redirect_uri and has_token('spotify'))

def direct_schedule_ready(rule):
    if not rule or not rule['scheduled']:
        return False
    pairs = direct.directions(rule['source'], rule['target'])
    return bool(pairs) and all(provider_ready(provider) for pair in pairs for provider in pair)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate()
    init_db()
    scheduler = AsyncIOScheduler(timezone='UTC')
    spotify.enable_pacing(True)
    direct.recover()
    recover_imports()
    scheduler.add_job(scheduled_checks, 'interval', seconds=30, id='library_checks',
                      coalesce=True, max_instances=1, misfire_grace_time=60)
    scheduler.add_job(requests_log.flush, 'interval', minutes=1, id='request_metrics',
                      coalesce=True, max_instances=1)
    scheduler.add_job(prune_history, 'cron', hour=3, minute=25, id='maintenance',
                      coalesce=True, max_instances=1)
    if settings.write_enabled:
        scheduler.add_job(execute_pending, 'interval', minutes=1, id='sync_writer',
                          coalesce=True, max_instances=1, misfire_grace_time=60)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await direct.shutdown()
        for task in list(_tasks):
            task.cancel()
        if _tasks:
            await asyncio.gather(*_tasks, return_exceptions=True)
        spotify.enable_pacing(False)
        await asyncio.to_thread(requests_log.flush)

app = FastAPI(title='Music Sync Hub', version='0.4.0', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.include_router(dashboard_session_router)

@app.get('/', response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(HTML, headers={'Cache-Control': 'no-store'})

@app.get('/health')
def health():
    return {'ok': True, 'service': 'music-sync-hub', 'stage': 'reviewed-additions' if settings.write_enabled else 'read-only-import'}

@app.get('/v1/config', dependencies=[Depends(admin)])
def config():
    return {'poll_minutes': {'spotify': settings.spotify_minutes, 'ytmusic': settings.ytm_minutes,
                              'apple': settings.apple_minutes},
            'delete_mode': settings.delete_mode, 'confirm_polls': settings.delete_confirm_polls,
            'max_delete_percent': settings.max_delete_percent,
            'max_delete_absolute': settings.max_delete_absolute,
            'write_enabled': settings.write_enabled, 'baseline_approved': get_setting('baseline_approved') == 'true',
            'playlist_transfer_enabled': transfer.PLAYLIST_TRANSFER_ENABLED,
            'provider_ready': {provider: provider_ready(provider) for provider in PROVIDERS},
            'spotify_retry_at': spotify.api_retry_at(),
            'spotify_limit_reason': get_setting('spotify_limit_reason'),
            'ytmusic_retry_at': ytmusic.api_retry_at(),
            'next_checks': {name: schedule.next_check(name) for name in (*PROVIDERS, 'direct')},
            'spotify_redirect_uri': settings.spotify_redirect_uri,
            'ytmusic_oauth_configured': google_oauth.configured(),
            'ytmusic_auth_mode': ytmusic.auth_mode(),
            'ytmusic_browser_available': settings.ytm_auth_path.is_file(),
            'apple_favorites_configured': bool(settings.apple_favorites_playlist_id),
            'apple_oauth_configured': apple_authorization.configured(),
            'spotify_oauth_configured': bool(settings.spotify_client_id and settings.spotify_redirect_uri)}

class DirectDirection(BaseModel):
    source: Literal['spotify', 'ytmusic', 'apple', 'both', 'all']
    target: Literal['spotify', 'ytmusic', 'both', 'all']


class DirectRule(DirectDirection):
    scheduled: bool


def validate_direction(direction):
    if not direct.ENABLED:
        raise HTTPException(409, 'Enable AUTO_TRANSFER_ENABLED on the server.')
    if not direct.directions(direction.source, direction.target):
        raise HTTPException(422, 'Choose a valid sync direction.')
    if not all(provider_ready(provider) for pair in direct.directions(direction.source, direction.target)
               for provider in pair):
        raise HTTPException(409, 'Kết nối các dịch vụ trong hướng đồng bộ đã chọn.')


@app.get('/v1/direct-sync', dependencies=[Depends(admin)])
def direct_status():
    return direct.status()


DirectItemStatus = Literal['pending', 'deferred', 'unmatched', 'failed', 'synced']


@app.get('/v1/direct-sync/groups', dependencies=[Depends(admin)])
def direct_groups(
    status: DirectItemStatus | None = None,
    job_id: str | None = Query(None, max_length=32),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    return direct_results.list_groups(status, offset, limit, job_id=job_id)


@app.get('/v1/direct-sync/items', dependencies=[Depends(admin)])
def direct_items(
    status: DirectItemStatus | None = None,
    job_id: str | None = Query(None, max_length=32),
    source: Literal['spotify', 'ytmusic', 'apple'] | None = None,
    target: Literal['spotify', 'ytmusic'] | None = None,
    collection_key: str | None = Query(None, max_length=512),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
):
    return direct_results.list_items(status, offset, limit, source=source,
                                     target=target, collection_key=collection_key, job_id=job_id)


@app.get('/v1/direct-sync/history', dependencies=[Depends(admin)])
def direct_history():
    return direct_results.history()


@app.get('/v1/requests', dependencies=[Depends(admin)])
def request_counts():
    requests_log.flush()
    return requests_log.recent()


class DirectMatch(BaseModel):
    source: Literal['spotify', 'ytmusic', 'apple']
    target: Literal['spotify', 'ytmusic']
    source_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)


@app.get('/v1/direct-sync/candidates', dependencies=[Depends(admin)])
def direct_candidates(source: Literal['spotify', 'ytmusic', 'apple'], target: Literal['spotify', 'ytmusic'],
                      source_id: str = Query(min_length=1, max_length=128)):
    fields = ('id', 'title', 'artist', 'duration_ms', 'isrc')
    return [dict(zip(fields, row)) for row in direct_results.options(source, target, source_id)]


@app.post('/v1/direct-sync/candidates', dependencies=[Depends(admin)])
async def choose_direct_candidate(payload: DirectMatch):
    async with POLL_LOCK:
        if not direct.directions(payload.source, payload.target):
            raise HTTPException(422, 'Chọn hai dịch vụ khác nhau.')
        options = direct_results.options(payload.source, payload.target, payload.source_id)
        candidate = next((row for row in options if row[0] == payload.target_id), None)
        if candidate is None:
            raise HTTPException(409, 'Kết quả tra cứu đã thay đổi. Hãy đồng bộ lại để tải ứng viên mới.')
        track_id, title, artist, duration, isrc = candidate
        with connect() as con:
            _canonical(con, payload.target, Item('catalog', track_id, title or '', artist=artist,
                                                 duration_ms=duration, isrc=isrc))
        try:
            return review.approve(payload.source, 'track', payload.source_id, track_id, target_provider=payload.target)
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc


@app.put('/v1/direct-sync/rule', dependencies=[Depends(admin)])
def direct_rule(payload: DirectRule):
    if payload.scheduled:
        validate_direction(payload)
    elif not direct.directions(payload.source, payload.target):
        raise HTTPException(422, 'Choose a valid sync direction.')
    direct.save_rule(payload.source, payload.target, payload.scheduled)
    return direct.status()


@app.post('/v1/direct-sync/run', status_code=202, dependencies=[Depends(admin)])
async def direct_run(payload: DirectDirection):
    validate_direction(payload)
    return direct.submit(payload.source, payload.target)


@app.post('/v1/direct-sync/{transfer_id}/undo', status_code=202, dependencies=[Depends(admin)])
async def direct_undo(transfer_id: str):
    try:
        return direct.submit_undo(transfer_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/v1/accounts', dependencies=[Depends(admin)])
def accounts():
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT provider,status,last_success_at,last_error FROM accounts ORDER BY provider')]

@app.get('/v1/snapshots', dependencies=[Depends(admin)])
def snapshots(limit: int = Query(20, ge=1, le=100)):
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT id,provider,started_at,completed_at,status,scope_counts_json,error FROM snapshot_runs ORDER BY id DESC LIMIT ?', (limit,))]

@app.get('/v1/events', dependencies=[Depends(admin)])
def events(limit: int = Query(50, ge=1, le=200)):
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT id,provider,scope,remote_id,kind,status,created_at,details_json FROM sync_events ORDER BY id DESC LIMIT ?', (limit,))]

@app.get('/v1/conflicts', dependencies=[Depends(admin)])
def conflicts():
    with connect() as con:
        return [dict(r) for r in con.execute('SELECT * FROM conflicts ORDER BY id DESC LIMIT 200')]

@app.get('/v1/sync/status', dependencies=[Depends(admin)])
def sync_status():
    return {p: _jobs[_active[p]] if p in _active else next((v for v in reversed(list(_jobs.values())) if v['provider'] == p), {'state':'idle'}) for p in PROVIDERS}

@app.get('/v1/overview', dependencies=[Depends(admin)])
def overview():
    return {'accounts': accounts(), 'jobs': sync_status(), 'config': config(), 'direct': direct.status()}


@app.get('/v1/sync/jobs/{job_id}', dependencies=[Depends(admin)])
def sync_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, 'Job not found (in-memory history expired)')
    return _jobs[job_id]

@app.post('/v1/sync/all', status_code=202, dependencies=[Depends(admin)])
async def sync_all():
    return {'jobs': [submit(provider) for provider in PROVIDERS if provider_ready(provider)]}

@app.post('/v1/sync/{provider}', status_code=202, dependencies=[Depends(admin)])
async def sync_now(provider: Literal['spotify', 'ytmusic', 'apple']):
    if not provider_ready(provider):
        raise HTTPException(409, "Hãy kết nối dịch vụ trước khi nhập thư viện.")
    return submit(provider)

@app.post('/v1/baseline/approve', dependencies=[Depends(admin)])
def approve_baseline():
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        providers = [r['provider'] for r in con.execute('SELECT provider FROM accounts WHERE status="connected" AND last_success_at IS NOT NULL')]
        if set(providers) != set(PROVIDERS):
            raise HTTPException(409, detail=f'Complete snapshots needed for all services; ready: {providers}')
        con.execute("UPDATE settings SET value='true', updated_at=datetime('now') WHERE key='baseline_approved'")
    return {'approved': True, 'remote_writes_enabled': settings.write_enabled}

@app.post('/v1/auth/spotify/start', dependencies=[Depends(admin)])
def spotify_start():
    try:
        return {'authorization_url': spotify.auth_url()}
    except NotConfigured as exc:
        raise HTTPException(409, detail=str(exc))

@app.get('/v1/auth/spotify/callback', response_class=HTMLResponse)
async def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state or state.startswith('apple-'):
        raise HTTPException(400, 'Spotify authorization denied or missing parameters')
    try:
        async with POLL_LOCK:
            await spotify.redeem(code, state)
    except ProviderResponseError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    except ValueError:
        raise HTTPException(400, 'Invalid or expired OAuth state')
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise HTTPException(429, 'Spotify đang giới hạn yêu cầu cấp token. Thử lại sau.') from exc
        raise HTTPException(502, f'Spotify từ chối cấp token (HTTP {exc.response.status_code}).') from exc
    except Exception:
        raise HTTPException(502, 'Spotify token exchange failed')
    submit('spotify', 'oauth')
    return HTMLResponse(
        '<!doctype html><html lang="vi"><meta charset="utf-8"><title>Spotify đã kết nối</title>'
        '<h1>Đã cấp quyền Spotify</h1><p>Máy chủ đang xử lý lượt nhập thư viện. '
        'Nếu Spotify giới hạn yêu cầu, lượt nhập sẽ tự tiếp tục sau thời gian chờ.</p>'
        f'<a href="{PROFILE_HOME}">Về dashboard</a></html>',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'},
    )


AUTH_HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}


class GooglePoll(BaseModel):
    handle: str = Field(min_length=16, max_length=80)


class AppleGrant(BaseModel):
    state: str = Field(min_length=16, max_length=80)
    user_token: str = Field(min_length=16, max_length=8192)


@app.delete('/v1/auth/{provider}/connection', dependencies=[Depends(admin)])
async def disconnect(provider: Literal['spotify', 'ytmusic']):
    if POLL_LOCK.locked() or direct.running() or _active:
        raise HTTPException(409, 'Chờ lượt đang chạy kết thúc trước khi gỡ kết nối.')
    async with POLL_LOCK:
        with connect() as con:
            if provider == 'ytmusic':
                settings.ytm_auth_path.unlink(missing_ok=True)
                google_oauth.cancel()
                con.execute("DELETE FROM settings WHERE key IN ('ytmusic_auth_mode','ytmusic_browser_fingerprint')")
            else:
                con.execute("DELETE FROM oauth_states WHERE state NOT LIKE 'apple-%'")
            con.execute('DELETE FROM encrypted_tokens WHERE provider=?', (provider,))
            invalidate_account(con, provider)
            con.execute("UPDATE accounts SET status='disconnected' WHERE provider=?", (provider,))
            con.execute('UPDATE direct_sync_rule SET scheduled=0')
            con.execute('DELETE FROM direct_playlist_links WHERE source=? OR target=?', (provider, provider))
    return {'disconnected': True}


@app.post('/v1/auth/ytmusic/check', dependencies=[Depends(admin)])
async def ytmusic_check():
    try:
        return await asyncio.to_thread(ytmusic.check_connection)
    except NotConfigured as exc:
        raise HTTPException(409, detail=str(exc)) from None
    except ytmusic.RateLimited as exc:
        raise HTTPException(429, detail='YouTube Music đang giới hạn yêu cầu. Hãy chờ tới lượt thử lại.',
                            headers={'Retry-After': str(max(1, int(exc.retry_at - time.time()) + 1))}) from None
    except ProviderResponseError as exc:
        raise HTTPException(502, detail=str(exc)) from None


class YouTubeAuthMode(BaseModel):
    mode: Literal['browser', 'oauth']


@app.put('/v1/auth/ytmusic/mode', dependencies=[Depends(admin)])
async def ytmusic_mode(payload: YouTubeAuthMode):
    if POLL_LOCK.locked() or direct.running() or _active:
        raise HTTPException(409, 'Chờ lượt đang chạy kết thúc trước khi đổi cách kết nối.')
    async with POLL_LOCK:
        try:
            fingerprint = await asyncio.to_thread(ytmusic.validate_mode, payload.mode)
        except NotConfigured as exc:
            raise HTTPException(409, str(exc)) from None
        except ytmusic.RateLimited as exc:
            raise HTTPException(429, 'YouTube Music đang giới hạn yêu cầu.',
                                headers={'Retry-After': str(max(1, int(exc.retry_at - time.time()) + 1))}) from None
        except ProviderResponseError as exc:
            raise HTTPException(502, str(exc)) from None
        with connect() as con:
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('ytmusic_auth_mode',?)", (payload.mode,))
            con.execute("DELETE FROM settings WHERE key='ytmusic_browser_fingerprint'")
            if fingerprint:
                con.execute("INSERT INTO settings(key,value) VALUES ('ytmusic_browser_fingerprint',?)", (fingerprint,))
            invalidate_account(con, 'ytmusic')
            con.execute('UPDATE direct_sync_rule SET scheduled=0')
    submit('ytmusic', 'auth_mode')
    return {'mode': payload.mode}


@app.post('/v1/auth/ytmusic/start', dependencies=[Depends(admin)])
async def ytmusic_start():
    try:
        result = await google_oauth.start()
    except NotConfigured as exc:
        raise HTTPException(409, detail=str(exc))
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(502, detail='Google authorization unavailable. Check your OAuth client.')
    return JSONResponse(result, headers=AUTH_HEADERS)


@app.post('/v1/auth/ytmusic/poll', dependencies=[Depends(admin)])
async def ytmusic_poll(grant: GooglePoll):
    try:
        async with POLL_LOCK:
            result = await google_oauth.poll(grant.handle)
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(502, detail='Google token exchange failed.')
    if result['status'] == 'connected':
        submit('ytmusic', 'oauth')
    return JSONResponse(result, headers=AUTH_HEADERS)


@app.post('/v1/auth/apple/start', dependencies=[Depends(admin)])
def apple_start():
    try:
        connect_url = apple_authorization.start()
    except NotConfigured as exc:
        raise HTTPException(409, detail=str(exc))
    except (OSError, ValueError):
        raise HTTPException(502, detail='Apple developer token unavailable. Check the MusicKit key.')
    return JSONResponse({'connect_url': connect_url}, headers=AUTH_HEADERS)


@app.get('/v1/auth/apple/connect', response_class=HTMLResponse)
def apple_connect():
    return HTMLResponse(apple_authorization.HTML, headers=AUTH_HEADERS)


@app.post('/v1/auth/apple/complete')
async def apple_complete(grant: AppleGrant):
    try:
        async with POLL_LOCK:
            await apple_authorization.complete(grant.state, grant.user_token)
    except ValueError:
        raise HTTPException(400, detail='Apple authorization invalid or expired.')
    except (httpx.HTTPError, NotConfigured):
        raise HTTPException(502, detail='Could not verify Apple Music library access.')
    submit('apple', 'oauth')
    return JSONResponse({'connected': True}, headers=AUTH_HEADERS)


async def execute_pending():
    async with POLL_LOCK:
        if not (direct.ENABLED and (direct.running() or direct_schedule_ready(direct.rule()))):
            await writer.execute_next()


class MatchApproval(BaseModel):
    source_provider: Literal['spotify', 'ytmusic']
    kind: Literal['track', 'artist']
    source_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)


@app.get('/v1/matches/candidates', dependencies=[Depends(admin)])
def match_candidates(provider: Literal['spotify', 'ytmusic'], kind: Literal['track', 'artist'],
                     remote_id: str = Query(min_length=1, max_length=128)):
    try:
        return review.candidates(provider, kind, remote_id)
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))


@app.post('/v1/matches/approve', dependencies=[Depends(admin)])
def approve_match(match: MatchApproval):
    try:
        return review.approve(match.source_provider, match.kind, match.source_id, match.target_id)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc))


@app.get('/v1/operations', dependencies=[Depends(admin)])
def operations(limit: int = Query(30, ge=1, le=100)):
    with connect() as con:
        return [dict(row) for row in con.execute(
            '''SELECT id,event_id,target_provider,operation,payload_json,status,attempt_count,last_error,
                      created_at,completed_at FROM sync_operations ORDER BY id DESC LIMIT ?''', (limit,)
        )]


class TransferPreview(BaseModel):
    library: transfer.LibraryExport
    target: Literal['spotify', 'ytmusic']


class TransferExecution(TransferPreview):
    title: str = Field(min_length=1, max_length=100)
    plan: str = Field(pattern=r'^[a-f0-9]{64}$')


@app.get('/v1/transfers/export/{provider}', dependencies=[Depends(admin)])
def export_library(provider: Literal['spotify', 'ytmusic', 'apple'],
                   playlist_id: str | None = Query(None, min_length=1, max_length=160)):
    try:
        library = transfer.export_playlist(provider, playlist_id) if playlist_id else transfer.export_library(provider)
    except transfer.TransferError as exc:
        raise HTTPException(409, detail=str(exc))
    return JSONResponse(
        library.model_dump(),
        headers={'Cache-Control': 'no-store', 'Content-Disposition': f'attachment; filename="{provider}-library.json"'},
    )


@app.get('/v1/transfers/collections/{provider}', dependencies=[Depends(admin)])
def transfer_collections(provider: Literal['spotify', 'ytmusic', 'apple']):
    try:
        return JSONResponse(transfer.list_collections(provider), headers={'Cache-Control': 'no-store'})
    except transfer.TransferError as exc:
        raise HTTPException(409, detail=str(exc))


@app.post('/v1/transfers/preview', dependencies=[Depends(admin)])
def preview_transfer(payload: TransferPreview):
    try:
        plan = transfer.preview(payload.library, payload.target)
    except transfer.TransferError as exc:
        raise HTTPException(409, detail=str(exc))
    return JSONResponse(plan, headers={'Cache-Control': 'no-store'})


@app.post('/v1/transfers/execute', dependencies=[Depends(admin)])
async def execute_transfer(payload: TransferExecution):
    title = payload.title.strip()
    if not title:
        raise HTTPException(422, detail='Enter a playlist title.')
    if not provider_ready(payload.target):
        raise HTTPException(409, detail='Connect the destination service first.')
    retry_at = (spotify.api_retry_at if payload.target == 'spotify' else ytmusic.api_retry_at)()
    if retry_at:
        return JSONResponse({'detail': 'Dịch vụ đích đang giới hạn yêu cầu.', 'retry_at': retry_at},
                            status_code=409)
    if POLL_LOCK.locked():
        raise HTTPException(409, detail='Máy chủ đang xử lý lượt nhập hoặc đồng bộ khác. Hãy thử lại sau.')
    try:
        async with POLL_LOCK:
            return await transfer.execute(payload.library, payload.target, title, payload.plan)
    except transfer.TransferError as exc:
        raise HTTPException(409, detail=str(exc))
