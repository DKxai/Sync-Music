import sqlite3
from pathlib import Path

def test_schema_is_valid():
    schema=(Path(__file__).parents[1]/'db'/'schema.sql').read_text()
    con=sqlite3.connect(':memory:')
    con.executescript(schema)
    tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'music_items','provider_mappings','source_states','snapshot_runs','sync_events','sync_operations','conflicts','oauth_states'} <= tables
    con.close()
