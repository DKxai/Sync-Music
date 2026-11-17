"""Single-process API, scheduler and manual sync queue."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from .connectors import apple, google_oauth, spotify
from .connectors.base import NotConfigured
from .dashboard import HTML
from .dashboard_session import admin, router as dashboard_session_router
from .provider_tokens import has_token
from .sync.engine import POLL_LOCK, poll
from .sync import direct, review, transfer, writer

PROVIDERS = ('spotify', 'ytmusic', 'apple')
scheduler = AsyncIOScheduler(timezone='UTC')
_jobs: dict[str, dict] = {}
_active: dict[str, str] = {}
_tasks: set[asyncio.Task] = set()

def timestamp():
    return datetime.now(timezone.utc).isoformat()

async def _run_job(job_id: str, provider: str):
    job = _jobs[job_id]
    job['state'] = 'running'
    job['started_at'] = timestamp()
    try:
        result = await poll(provider)
        job['result'] = result
        job['state'] = 'failed' if 'error' in result else 'completed'
    except Exception as exc:
        job['result'] = {'error': f'{type(exc).__name__}: {str(exc)[:250]}'}
        job['state'] = 'failed'
    finally:
        job['finished_at'] = timestamp()
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
    _jobs[job_id] = {'job_id': job_id, 'provider': provider, 'trigger': trigger,
                     'state': 'queued', 'requested_at': timestamp()}
    _active[provider] = job_id
    task = asyncio.create_task(_run_job(job_id, provider))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {'job_id': job_id, 'provider': provider, 'already_running': False}

async def scheduled_poll(provider: str):
    if provider_ready(provider):
        submit(provider, 'scheduled')


def provider_ready(provider: str) -> bool:
    if provider == 'ytmusic':
        return settings.ytm_auth_path.is_file() or (google_oauth.configured() and has_token('ytmusic'))
    if provider == 'apple':
        return apple.developer_configured() and (settings.apple_user_token_path.is_file() or has_token('apple'))
    return bool(settings.spotify_client_id and settings.spotify_redirect_uri and has_token('spotify'))

async def scheduled_direct_sync():
    rule = direct.rule()
    if rule and rule['scheduled'] and all(provider_ready(rule[key]) for key in ('source', 'target')):
        direct.submit(rule['source'], rule['target'])


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate()
    init_db()
    direct.recover()
    for provider, minutes in [('spotify', settings.spotify_minutes),
                              ('ytmusic', settings.ytm_minutes),
                              ('apple', settings.apple_minutes)]:
        scheduler.add_job(scheduled_poll, 'interval', minutes=minutes, args=[provider],
                          id=provider, coalesce=True, max_instances=1, misfire_grace_time=60)
    scheduler.add_job(prune_history, 'cron', hour=3, minute=25, id='maintenance',
                      coalesce=True, max_instances=1)
    if settings.write_enabled:
        scheduler.add_job(execute_pending, 'interval', minutes=1, id='sync_writer',
                          coalesce=True, max_instances=1, misfire_grace_time=60)
    if direct.ENABLED:
        scheduler.add_job(scheduled_direct_sync, 'interval', minutes=10, id='direct_sync',
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
            'provider_ready': {provider: provider_ready(provider) for provider in PROVIDERS},
            'spotify_redirect_uri': settings.spotify_redirect_uri,
            'ytmusic_oauth_configured': google_oauth.configured(),
            'apple_oauth_configured': apple_authorization.configured(),
            'spotify_oauth_configured': bool(settings.spotify_client_id and settings.spotify_redirect_uri)}

class DirectDirection(BaseModel):
    source: Literal['spotify', 'ytmusic']
    target: Literal['spotify', 'ytmusic']


class DirectRule(DirectDirection):
    scheduled: bool


def validate_direction(direction):
    if not direct.ENABLED:
        raise HTTPException(409, 'Enable AUTO_TRANSFER_ENABLED on the server.')
    if direction.source == direction.target:
        raise HTTPException(422, 'Choose two different services.')
    if not all(provider_ready(provider) for provider in (direction.source, direction.target)):
        raise HTTPException(409, 'Connect both music services first.')


@app.get('/v1/direct-sync', dependencies=[Depends(admin)])
def direct_status():
    return direct.status()


@app.put('/v1/direct-sync/rule', dependencies=[Depends(admin)])
def direct_rule(payload: DirectRule):
    if payload.scheduled:
        validate_direction(payload)
    elif payload.source == payload.target:
        raise HTTPException(422, 'Choose two different services.')
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

@app.get('/v1/sync/jobs/{job_id}', dependencies=[Depends(admin)])
def sync_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, 'Job not found (in-memory history expired)')
    return _jobs[job_id]

@app.post('/v1/sync/all', status_code=202, dependencies=[Depends(admin)])
async def sync_all():
    return {'jobs': [submit(p) for p in PROVIDERS]}

@app.post('/v1/sync/{provider}', status_code=202, dependencies=[Depends(admin)])
async def sync_now(provider: Literal['spotify', 'ytmusic', 'apple']):
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
    except ValueError:
        raise HTTPException(400, 'Invalid or expired OAuth state')
    except Exception:
        raise HTTPException(502, 'Spotify token exchange failed')
    submit('spotify', 'oauth')
    return HTMLResponse(
        '<!doctype html><html lang="vi"><meta charset="utf-8"><title>Spotify đã kết nối</title>'
        '<h1>Spotify đã kết nối</h1><p>Máy chủ đã xếp hàng nhập dữ liệu lần đầu.</p>'
        '<a href="/">Về dashboard</a></html>',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'},
    )


AUTH_HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}


class GooglePoll(BaseModel):
    handle: str = Field(min_length=16, max_length=80)


class AppleGrant(BaseModel):
    state: str = Field(min_length=16, max_length=80)
    user_token: str = Field(min_length=16, max_length=8192)


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
def export_library(provider: Literal['spotify', 'ytmusic', 'apple']):
    try:
        library = transfer.export_library(provider)
    except transfer.TransferError as exc:
        raise HTTPException(409, detail=str(exc))
    return JSONResponse(
        library.model_dump(),
        headers={'Cache-Control': 'no-store', 'Content-Disposition': f'attachment; filename="{provider}-library.json"'},
    )


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
    try:
        async with POLL_LOCK:
            return await transfer.execute(payload.library, payload.target, title, payload.plan)
    except transfer.TransferError as exc:
        raise HTTPException(409, detail=str(exc))
