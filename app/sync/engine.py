import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4
from httpx import HTTPStatusError

from ..config import settings
from ..connectors import spotify, ytmusic, apple
from ..connectors.base import NotConfigured
from ..db import connect
from .safety import deletion_is_safe
from .review import plan_add

FETCHERS={'spotify':spotify.fetch,'ytmusic':ytmusic.fetch,'apple':apple.fetch}
POLL_LOCK=asyncio.Lock()

def _remote_mapping_id(item):
    # Playlist entries have an occurrence identity; the track underneath has its own identity.
    return item.raw.get('track_id',item.remote_id) if item.kind=='track' else item.remote_id

def _canonical(con,provider,item):
    base_id=_remote_mapping_id(item)
    row=con.execute('SELECT music_item_id FROM provider_mappings WHERE provider=? AND entity_kind=? AND remote_id=?', (provider,item.kind,base_id)).fetchone()
    if row:return row['music_item_id']
    # Conservative: only exact ISRC is enough to auto-merge across providers.
    row=con.execute('SELECT id FROM music_items WHERE kind=? AND isrc=? LIMIT 1',(item.kind,item.isrc)).fetchone() if item.kind=='track' and item.isrc else None
    canonical=row['id'] if row else str(uuid4())
    if not row:
        con.execute('INSERT INTO music_items(id,kind,title,artist,album,duration_ms,isrc) VALUES(?,?,?,?,?,?,?)', (canonical,item.kind,item.title,item.artist,item.album,item.duration_ms,item.isrc))
    con.execute('INSERT OR IGNORE INTO provider_mappings(provider,entity_kind,remote_id,music_item_id,confidence,match_method) VALUES(?,?,?,?,?,?)',(provider,item.kind,base_id,canonical,1.0,'same_isrc' if row else 'native_id'))
    return canonical

def _event(con,provider,scope,remote_id,kind,details):
    key=f'{provider}|{scope}|{remote_id}|{kind}|{datetime.now(timezone.utc).isoformat()}'
    event = con.execute(
        'INSERT INTO sync_events(event_key,provider,scope,remote_id,kind,details_json) VALUES(?,?,?,?,?,?)',
        (key, provider, scope, remote_id, kind, json.dumps(details, ensure_ascii=False)),
    )
    if kind == 'added':
        plan_add(con, event.lastrowid, provider, scope, remote_id)

def persist(provider: str, scopes: dict):
    if not scopes or not isinstance(scopes,dict):
        raise ValueError('Provider returned no complete scope data; refusing snapshot')
    for scope,items in scopes.items():
        if not isinstance(items,list): raise ValueError(f'Incomplete {scope} snapshot')
        if len(items)!=len(set(i.remote_id for i in items)):
            raise ValueError(f'Duplicate remote IDs in {scope}')
    with connect() as con:
        run=con.execute('INSERT INTO snapshot_runs(provider,status) VALUES(?,?)',(provider,'running')).lastrowid
        # Single transaction: either ALL scopes commit or NONE do.
        baseline = con.execute("SELECT value FROM settings WHERE key='baseline_approved'").fetchone()[0] == 'true'
        for scope,items in scopes.items():
            current={i.remote_id:i for i in items}
            old=con.execute('SELECT remote_id,present,missing_streak,payload_json FROM source_states WHERE provider=? AND scope=?',(provider,scope)).fetchall()
            old_present={r['remote_id']:r for r in old if r['present']}
            missing=set(old_present)-set(current)
            # Full-snapshot deletion guard; suspicious drop is quarantined, never applied.
            guarded=baseline and missing and not deletion_is_safe(len(old_present),len(missing),settings.max_delete_percent,settings.max_delete_absolute)
            if guarded:
                con.execute('INSERT INTO conflicts(scope,item_id,reason,details_json) VALUES(?,?,?,?)',(f'{provider}:{scope}',None,'mass_deletion_guard',json.dumps({'missing':len(missing),'previous':len(old_present)})))
            for item in items:
                cid=_canonical(con,provider,item)
                payload=json.dumps({'title':item.title,'artist':item.artist,'album':item.album,'duration_ms':item.duration_ms,'isrc':item.isrc,**item.raw},ensure_ascii=False)
                existing=con.execute('SELECT present FROM source_states WHERE provider=? AND scope=? AND remote_id=?',(provider,scope,item.remote_id)).fetchone()
                if not existing and baseline:
                    _event(con,provider,scope,item.remote_id,'added',{'item_id':cid})
                elif existing and not existing['present'] and baseline:
                    _event(con,provider,scope,item.remote_id,'added',{'item_id':cid,'restored':True})
                con.execute('''INSERT INTO source_states(provider,scope,remote_id,music_item_id,payload_json,present,missing_streak)
                  VALUES(?,?,?,?,?,1,0)
                  ON CONFLICT(provider,scope,remote_id) DO UPDATE SET music_item_id=excluded.music_item_id,payload_json=excluded.payload_json,present=1,missing_streak=0,last_seen_at=datetime('now')''',
                  (provider,scope,item.remote_id,cid,payload))
            if baseline and not guarded:
                for remote_id in missing:
                    row=old_present[remote_id]
                    streak=row['missing_streak']+1
                    if streak>=settings.delete_confirm_polls:
                        con.execute('UPDATE source_states SET present=0,missing_streak=? WHERE provider=? AND scope=? AND remote_id=?',(streak,provider,scope,remote_id))
                        if settings.delete_mode!='off':
                            _event(con,provider,scope,remote_id,'removed',{'confirmed_polls':streak,'delete_mode':settings.delete_mode,'execution':'NOT_IMPLEMENTED'})
                    else:
                        con.execute('UPDATE source_states SET missing_streak=? WHERE provider=? AND scope=? AND remote_id=?',(streak,provider,scope,remote_id))
        con.execute('UPDATE snapshot_runs SET status="complete",completed_at=datetime("now"),scope_counts_json=? WHERE id=?',(json.dumps({k:len(v) for k,v in scopes.items()}),run))
        con.execute('UPDATE accounts SET status="connected",last_success_at=datetime("now"),last_error=NULL,updated_at=datetime("now") WHERE provider=?',(provider,))
    return {'provider':provider,'scopes':{k:len(v) for k,v in scopes.items()},'baseline_approved':baseline,'remote_writes':'enabled_for_reviewed_additions' if settings.write_enabled else 'disabled'}

async def poll(provider: str):
    if provider not in FETCHERS:raise ValueError('Unknown provider')
    # Only one provider poll / SQLite merge at a time. One worker by design.
    async with POLL_LOCK:
        try:
            scopes=await FETCHERS[provider]()
            return persist(provider,scopes)
        except Exception as exc:
            # No partial snapshot is committed on error.
            if isinstance(exc, (NotConfigured, ytmusic.ImportRequestError)):
                message = str(exc)
            elif isinstance(exc, HTTPStatusError):
                message = f'Provider returned HTTP {exc.response.status_code}'
            else:
                message = f'Provider import failed ({type(exc).__name__})'
            with connect() as con:
                con.execute('INSERT INTO snapshot_runs(provider,status,error,completed_at) VALUES(?,"failed",?,datetime("now"))',(provider,message))
                con.execute('UPDATE accounts SET status="error",last_error=?,updated_at=datetime("now") WHERE provider=?',(message,provider))
            return {'provider':provider,'error':message,'remote_writes':'enabled_for_reviewed_additions' if settings.write_enabled else 'disabled'}
