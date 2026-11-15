import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import db
from app.connectors.base import Item
from app.sync import review, writer
from app.sync.engine import persist


def setup_catalog(monkeypatch, tmp_path, previously_liked=False):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(writer, 'settings', replace(writer.settings, write_enabled=True))
    db.init_db()
    source = Item('playlist:SP', 'S#1', 'Song', raw={'track_id': 'S'})
    target = Item('playlist:YT', 'Y#1', 'Song', raw={'track_id': 'Y'})
    persist('spotify', {'playlist:SP': [source], 'liked': []})
    likes = [Item('liked', 'Y', 'Song')] if previously_liked else []
    persist('ytmusic', {'playlist:YT': [target], 'liked': likes})
    review.approve('spotify', 'track', 'S', 'Y')
    db.set_setting('baseline_approved', 'true')
    persist('spotify', {'playlist:SP': [source], 'liked': [Item('liked', 'S', 'Song')]})


@pytest.mark.parametrize(
    'origin_present,target_present,previously_liked,expected',
    [(True, False, False, 'applied'), (False, False, False, 'needs_review'),
     (True, False, True, 'needs_review'), (True, True, False, 'already_present')],
)
def test_writer_checks_live_state(monkeypatch, tmp_path, origin_present, target_present,
                                  previously_liked, expected):
    setup_catalog(monkeypatch, tmp_path, previously_liked)
    origin = SimpleNamespace(contains=AsyncMock(return_value=origin_present))
    target = SimpleNamespace(contains=AsyncMock(side_effect=[target_present, True]),
                             add_to_library=AsyncMock())
    monkeypatch.setattr(writer, 'ADAPTERS', {'spotify': origin, 'ytmusic': target})
    asyncio.run(writer.execute_next())
    with db.connect() as con:
        assert con.execute('SELECT status FROM sync_operations').fetchone()[0] == expected
        assert con.execute('SELECT COUNT(*) FROM sync_operations').fetchone()[0] == 1
    if expected == 'applied':
        target.add_to_library.assert_awaited_once_with('liked', 'Y')
        persist('ytmusic', {'liked': [Item('liked', 'Y', 'Song')]})
        with db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM sync_operations').fetchone()[0] == 1
    else:
        target.add_to_library.assert_not_awaited()
