"""Single-process API, scheduler and manual sync queue."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
from typing import Literal
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from .config import settings, validate
from .db import init_db, connect, get_setting, set_setting, prune_history
from .connectors import spotify
from .connectors.base import NotConfigured
from .dashboard import HTML
from .sync.engine import poll

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
    submit(provider, 'scheduled')

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate()
    if settings.write_enabled:
        raise RuntimeError('Remote writes have NOT been implemented. Keep WRITE_ENABLED=false.')
    init_db()
    for provider, minutes in [('spotify', settings.spotify_minutes),
                              ('ytmusic', settings.ytm_minutes),
                              ('apple', settings.apple_minutes)]:
        scheduler.add_job(scheduled_poll, 'interval', minutes=minutes, args=[provider],
                          id=provider, coalesce=True, max_instances=1, misfire_grace_time=60)
    scheduler.add_job(prune_history, 'cron', hour=3, minute=25, id='maintenance',
                      coalesce=True, max_instances=1)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        for task in list(_tasks):
            task.cancel()
        if _tasks:
            await asyncio.gather(*_tasks, return_exceptions=True)

app = FastAPI(title='Music Sync Hub', version='0.2.0', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

def admin(x_api_key: str | None = Header(default=None)):
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail='Invalid admin API key')

@app.get('/', response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTML

@app.get('/health')
def health():
    return {'ok': True, 'service': 'music-sync-hub', 'stage': 'read-only-import'}

@app.get('/v1/config', dependencies=[Depends(admin)])
def config():
    return {'poll_minutes': {'spotify': settings.spotify_minutes, 'ytmusic': settings.ytm_minutes,
                              'apple': settings.apple_minutes},
            'delete_mode': settings.delete_mode, 'confirm_polls': settings.delete_confirm_polls,
            'max_delete_percent': settings.max_delete_percent,
            'max_delete_absolute': settings.max_delete_absolute,
            'write_enabled': False, 'baseline_approved': get_setting('baseline_approved') == 'true'}

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
        providers = [r['provider'] for r in con.execute('SELECT provider FROM accounts WHERE status="connected" AND last_success_at IS NOT NULL')]
    if set(providers) != set(PROVIDERS):
        raise HTTPException(409, detail=f'Complete snapshots needed for all services; ready: {providers}')
    set_setting('baseline_approved', 'true')
    return {'approved': True, 'remote_writes_enabled': False}

@app.post('/v1/auth/spotify/start', dependencies=[Depends(admin)])
def spotify_start():
    try:
        return {'authorization_url': spotify.auth_url()}
    except NotConfigured as exc:
        raise HTTPException(409, detail=str(exc))

@app.get('/v1/auth/spotify/callback', response_class=HTMLResponse)
async def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        raise HTTPException(400, 'Spotify authorization denied or missing parameters')
    try:
        await spotify.redeem(code, state)
    except ValueError:
        raise HTTPException(400, 'Invalid or expired OAuth state')
    except Exception:
        raise HTTPException(502, 'Spotify token exchange failed')
    return '<html><body><h2>Spotify connected</h2><p>You can close this page.</p></body></html>'
