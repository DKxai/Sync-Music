import asyncio
from app import main


def test_submit_deduplicates(monkeypatch):
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
