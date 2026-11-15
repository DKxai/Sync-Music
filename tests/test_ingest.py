from pathlib import Path
import sqlite3

from app import db
from app.connectors.base import Item
from app.sync.engine import persist


def _temporary_db(monkeypatch, tmp_path):
    path=tmp_path/'test.db'
    monkeypatch.setattr(db,'DB_PATH',path)
    with sqlite3.connect(path) as con:
        con.executescript((Path(__file__).parents[1]/'db'/'schema.sql').read_text())
    return path


def test_baseline_does_not_infer_deletion(monkeypatch,tmp_path):
    _temporary_db(monkeypatch,tmp_path)
    persist('spotify',{'liked':[Item('liked','S1','Song One'),Item('liked','S2','Song Two')]})
    persist('spotify',{'liked':[Item('liked','S1','Song One')]})
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM sync_events').fetchone()[0]==0
        # Deliberately keep records during initial multi-source union stage.
        assert con.execute('SELECT COUNT(*) FROM source_states WHERE present=1').fetchone()[0]==2


def test_confirmed_removal_is_detected_not_applied(monkeypatch,tmp_path):
    _temporary_db(monkeypatch,tmp_path)
    persist('spotify',{'liked':[Item('liked','S1','Song One'),Item('liked','S2','Song Two')]})
    db.set_setting('baseline_approved','true')
    persist('spotify',{'liked':[Item('liked','S1','Song One')]})
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM sync_events WHERE kind="removed"').fetchone()[0]==0
    persist('spotify',{'liked':[Item('liked','S1','Song One')]})
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM sync_events WHERE kind="removed"').fetchone()[0]==1
        assert con.execute('SELECT COUNT(*) FROM sync_operations').fetchone()[0]==0


def test_mass_delete_guard(monkeypatch,tmp_path):
    _temporary_db(monkeypatch,tmp_path)
    persist('spotify',{'liked':[Item('liked',str(i),f'Song {i}') for i in range(100)]})
    db.set_setting('baseline_approved','true')
    persist('spotify',{'liked':[Item('liked',str(i),f'Song {i}') for i in range(10)]})
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0]==1
        assert con.execute('SELECT COUNT(*) FROM source_states WHERE present=0').fetchone()[0]==0
