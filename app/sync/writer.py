"""Apply reviewed Spotify–YouTube Music additions with live read-back."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..config import settings
from ..connectors import spotify, ytmusic
from ..db import connect
from .review import ADD_PROVIDERS, ENTITY_KINDS, SAFE_REMOTE_ID, lookup

ADAPTERS = {'spotify': spotify, 'ytmusic': ytmusic}
MAX_ATTEMPTS = 3


def _claim():
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        if not con.execute("SELECT 1 FROM settings WHERE key='baseline_approved' AND value='true'").fetchone():
            return None
        row = con.execute(
            """SELECT o.* FROM sync_operations o
               WHERE o.operation='add'
                 AND (o.status='planned' OR
                   (o.status='retry' AND o.completed_at <= datetime('now')) OR o.status='running')
               ORDER BY o.id LIMIT 1"""
        ).fetchone()
        if row:
            con.execute("UPDATE sync_operations SET status='running', attempt_count=attempt_count+1 WHERE id=?",
                        (row['id'],))
        return row


def _finish(operation_id: int, status: str, error: str | None = None,
            after_seconds: int = 0, mirror: tuple | None = None) -> None:
    """Record the verified target state and receipt in one transaction."""
    with connect() as con:
        if mirror:
            provider, scope, remote_id, catalog = mirror
            con.execute(
                """INSERT OR IGNORE INTO source_states
                   (provider,scope,remote_id,music_item_id,payload_json) VALUES (?,?,?,?,?)""",
                (provider, scope, remote_id, catalog['id'],
                 json.dumps({'title': catalog['title'], 'artist': catalog['artist'],
                             'duration_ms': catalog['duration_ms'], 'isrc': catalog['isrc']})),
            )
        con.execute(
            """UPDATE sync_operations SET status=?, last_error=?,
               completed_at=datetime('now', ?) WHERE id=?""",
            (status, error, f'+{after_seconds} seconds', operation_id),
        )


def _preflight(operation, payload):
    source = payload['source_provider']
    target = operation['target_provider']
    scope, src, dst = payload['scope'], payload['source_id'], payload['target_id']
    kind = ENTITY_KINDS.get(scope)
    if (not kind or ADD_PROVIDERS.get(source) != target
            or not SAFE_REMOTE_ID.fullmatch(src) or not SAFE_REMOTE_ID.fullmatch(dst)):
        return None
    with connect() as con:
        row = con.execute(
            """SELECT 1 FROM sync_events e
               JOIN source_states s ON s.provider=e.provider AND s.scope=e.scope AND s.remote_id=e.remote_id
               JOIN reviewed_links r ON r.source_provider=e.provider AND r.source_id=e.remote_id
               WHERE e.id=? AND e.kind='added' AND e.provider=? AND e.scope=? AND e.remote_id=?
                 AND r.target_provider=? AND r.target_id=? AND r.entity_kind=?
                 AND s.present=1 AND s.missing_streak=0
                 AND NOT EXISTS (SELECT 1 FROM sync_events newer
                     WHERE newer.provider=e.provider AND newer.scope=e.scope AND newer.remote_id=e.remote_id
                       AND newer.id>e.id AND newer.kind IN ('added','removed'))""",
            (operation['event_id'], source, scope, src, target, dst, kind),
        ).fetchone()
        if not row:
            return None
        statuses = con.execute(
            """SELECT COUNT(*) FROM accounts WHERE provider IN (?,?)
               AND status='connected' AND last_success_at IS NOT NULL""", (source, target)
        ).fetchone()[0]
        if statuses != 2:
            return None
        catalog = lookup(con, target, kind, dst)
        known_target = con.execute(
            'SELECT 1 FROM source_states WHERE provider=? AND scope=? AND remote_id=?',
            (target, scope, dst),
        ).fetchone() is not None
        return catalog, known_target


def _retry_delay(error: httpx.HTTPStatusError) -> int:
    value = error.response.headers.get('Retry-After', '')
    try:
        seconds = int(value)
    except ValueError:
        try:
            seconds = int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            seconds = 120
    return max(60, seconds)


async def execute_next() -> None:
    if not settings.write_enabled:
        return
    operation = _claim()
    if operation is None:
        return
    operation_id = operation['id']
    if operation['attempt_count'] >= MAX_ATTEMPTS:
        _finish(operation_id, 'needs_review', 'Retry limit reached. Check provider membership.')
        return
    try:
        payload = json.loads(operation['payload_json'])
        preflight = _preflight(operation, payload)
        if not preflight:
            _finish(operation_id, 'needs_review', 'Mapping, source event or account status has changed')
            return
        catalog, known_target = preflight
        src, dst, scope = payload['source_id'], payload['target_id'], payload['scope']
        target = operation['target_provider']
        origin_adapter = ADAPTERS[payload['source_provider']]
        target_adapter = ADAPTERS[target]
        if not await origin_adapter.contains(scope, src):
            _finish(operation_id, 'needs_review', 'Source item is no longer present')
            return
        if await target_adapter.contains(scope, dst):
            _finish(operation_id, 'already_present')
            return
        if known_target:
            _finish(operation_id, 'needs_review', 'Target used to contain this item. Possible independent removal.')
            return
        await target_adapter.add_to_library(scope, dst)
        if await target_adapter.contains(scope, dst):
            _finish(operation_id, 'applied', mirror=(target, scope, dst, catalog))
        else:
            _finish(operation_id, 'retry', 'Provider has not confirmed the addition', 120)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 429 or code >= 500:
            _finish(operation_id, 'retry', f'Provider returned HTTP {code}', _retry_delay(exc))
        else:
            _finish(operation_id, 'needs_review', f'Provider returned HTTP {code}')
    except httpx.RequestError:
        _finish(operation_id, 'retry', 'Provider connection interrupted', 120)
    except Exception as exc:
        _finish(operation_id, 'needs_review', f'Provider verification failed ({type(exc).__name__})')
