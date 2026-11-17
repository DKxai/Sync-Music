"""Reviewed links between exact provider identifiers and outgoing add plans."""

import hashlib
import json
import re
import sqlite3

from ..db import connect
from .matcher import normalized, preview_score

ENTITY_KINDS = {'liked': 'track', 'followed_artist': 'artist'}
ADD_PROVIDERS = {'spotify': 'ytmusic', 'ytmusic': 'spotify'}
SAFE_REMOTE_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def lookup(con, provider: str, kind: str, remote_id: str):
    return con.execute(
        """SELECT m.remote_id, c.id, c.title, c.artist, c.duration_ms, c.isrc
           FROM provider_mappings m JOIN music_items c ON c.id=m.music_item_id
           WHERE m.provider=? AND m.entity_kind=? AND m.remote_id=?""",
        (provider, kind, remote_id),
    ).fetchone()


def candidates(provider: str, kind: str, remote_id: str) -> dict:
    target = ADD_PROVIDERS[provider]
    with connect() as con:
        source = lookup(con, provider, kind, remote_id)
        if source is None:
            raise ValueError('Source ID not found in the imported catalog')
        if not normalized(source['title']):
            return {'source': dict(source), 'target_provider': target, 'candidates': []}
        pool = con.execute(
            """SELECT m.remote_id, c.id, c.title, c.artist, c.duration_ms, c.isrc
               FROM provider_mappings m JOIN music_items c ON c.id=m.music_item_id
               WHERE m.provider=? AND m.entity_kind=?
                 AND (c.id=? OR c.title=? COLLATE NOCASE)
               ORDER BY (c.id=?) DESC, m.remote_id LIMIT 50""",
            (target, kind, source['id'], source['title'], source['id']),
        )
        ranked = []
        for match in pool:
            score = preview_score(source['title'], source['artist'], source['duration_ms'],
                                  match['title'], match['artist'], match['duration_ms'])
            ranked.append({**dict(match), 'score': score,
                           'same_isrc': bool(source['isrc'] and source['isrc'] == match['isrc'])})
    ranked.sort(key=lambda candidate: (candidate['same_isrc'], candidate['score']), reverse=True)
    return {'source': dict(source), 'target_provider': target, 'candidates': ranked[:10]}


def approve(provider: str, kind: str, source_id: str, target_id: str, *, target_provider=None) -> dict:
    source_pattern = re.compile(r'[A-Za-z0-9._-]{1,128}') if provider == 'apple' else SAFE_REMOTE_ID
    if not source_pattern.fullmatch(source_id) or not SAFE_REMOTE_ID.fullmatch(target_id):
        raise ValueError('Expected provider-native IDs')
    target = target_provider or ADD_PROVIDERS[provider]
    directions = [(provider, target, kind, source_id, target_id),
                  (target, provider, kind, target_id, source_id)]
    try:
        with connect() as con:
            for direction in directions:
                con.execute(
                    """INSERT INTO reviewed_links
                       (source_provider,target_provider,entity_kind,source_id,target_id)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(source_provider,target_provider,entity_kind,source_id)
                       DO NOTHING""", direction,
                )
                row = con.execute(
                    """SELECT target_id FROM reviewed_links WHERE source_provider=?
                       AND target_provider=? AND entity_kind=? AND source_id=?""", direction[:4]
                ).fetchone()
                if row['target_id'] != direction[4]:
                    raise ValueError('This identifier already has a different reviewed link')
            con.execute("DELETE FROM settings WHERE key LIKE 'direct_observation:%'")
            con.execute("DELETE FROM sync_schedule WHERE name='direct'")
    except sqlite3.IntegrityError as exc:
        raise ValueError('Both IDs must exist in their imported catalogs and form a one-to-one link') from exc
    return {'source_provider': provider, 'target_provider': target, 'kind': kind,
            'source_id': source_id, 'target_id': target_id}


def plan_add(con, event_id: int, provider: str, scope: str, remote_id: str) -> None:
    target = ADD_PROVIDERS.get(provider)
    kind = ENTITY_KINDS.get(scope)
    if not target or not kind:
        return
    link = con.execute(
        """SELECT target_id FROM reviewed_links WHERE source_provider=? AND target_provider=?
           AND entity_kind=? AND source_id=?""",
        (provider, target, kind, remote_id),
    ).fetchone()
    if not link:
        return
    payload = {'source_provider': provider, 'source_id': remote_id, 'scope': scope,
               'target_id': link['target_id']}
    identity = [event_id, provider, remote_id, target, link['target_id'], scope, 'add']
    fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    con.execute(
        """INSERT INTO sync_operations(event_id,target_provider,operation,payload_json,idempotency_key)
           VALUES (?,?,'add',?,?) ON CONFLICT(idempotency_key) DO NOTHING""",
        (event_id, target, json.dumps(payload), fingerprint),
    )
