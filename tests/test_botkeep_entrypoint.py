"""Test native Botkeep entrypoint without starting a server."""
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

def test_botkeep_entrypoint(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("SERVER_PORT", "4567")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **kw: calls.append((a, kw))))
    runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
    assert len(calls) == 1
    args, kw = calls[0]
    assert args == ("app.main:app",)
    assert kw["port"] == 4567
    assert kw["host"] == "0.0.0.0"
    assert kw["workers"] == 1
    assert kw["access_log"] is False
