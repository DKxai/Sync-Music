import asyncio
from dataclasses import replace

from app import db
from app import main


def test_submit_deduplicates(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'music.db')
    monkeypatch.setattr(db, 'settings', replace(db.settings, data_dir=tmp_path))
    db.init_db()
    async def fake_poll(provider):
        await asyncio.sleep(0.05)
        return {'provider': provider}
    monkeypatch.setattr(main, 'poll', fake_poll)
    async def scenario():
        main._jobs.clear()
        main._active.clear()
        a = main.submit('spotify')
        b = main.submit('spotify')
        assert a['job_id'] == b['job_id']
        assert b['already_running'] is True
        while main._active:
            await asyncio.sleep(0.01)
        assert main._jobs[a['job_id']]['state'] == 'completed'
    asyncio.run(scenario())


def test_pending_import_resumes_from_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'music.db')
    monkeypatch.setattr(db, 'settings', replace(db.settings, data_dir=tmp_path))
    db.init_db()
    called = []

    async def fake_poll(provider):
        called.append(provider)
        return {'provider': provider}

    monkeypatch.setattr(main, 'poll', fake_poll)
    main.persist_import({'job_id': 'queued-job', 'state': 'running', 'provider': 'ytmusic',
                         'trigger': 'manual', 'requested_at': '2026-11-17'})

    async def scenario():
        main.recover_imports()
        await asyncio.gather(*main._tasks)
        assert main._jobs['queued-job']['state'] == 'completed'

    asyncio.run(scenario())
    assert called == ['ytmusic']
