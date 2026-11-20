"""Mount isolated account pairs in one Botkeep process."""
import json
import os
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

from fastapi import FastAPI, Depends, HTTPException
from typing import Literal
from .pool_routes import Routes
from .pool_ui import panel

from .db import get_setting, set_setting
from . import main
from .config import settings


def build():
    slots = list(dict.fromkeys(s.strip() for s in os.getenv("SYNC_PROFILES", "secondary").split(",") if s.strip()))
    if any(not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", s) for s in slots):
        raise ValueError("Invalid SYNC_PROFILES name")
    profiles = {'main': main}
    routes = Routes(profiles)
    registry = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    template = main.HTML
    async def attach(slug, stack):
        if slug in profiles:
            return
        prefix = f"/profiles/{slug}"
        # The existing single-account engine owns module-level tasks and OAuth grants.
        # Load one private module namespace per profile so those never cross accounts.
        name = "sync_profile_" + slug.replace("-", "_")
        package = ModuleType(name)
        package.__path__ = [str(Path(__file__).parent)]
        sys.modules[name] = package
        config = import_module(name + ".config")
        redirect = urlsplit(settings.spotify_redirect_uri)
        config.settings = replace(settings, data_dir=settings.data_dir / "profiles" / slug,
                                  ytm_auth_path=settings.ytm_auth_path.parent / slug / "ytmusic_browser.json",
                                  apple_user_token_path=settings.apple_user_token_path.parent / slug / "apple_user_token.txt",
                                  spotify_redirect_uri=redirect._replace(path=prefix + redirect.path).geturl()
                                  if settings.spotify_redirect_uri else "")
        service = import_module(name + ".main")
        # Spotify quotas belong to the app. All profiles must observe the same
        # request spacing and persisted deadline, even with separate libraries.
        for fn in ("pace", "adjust_pace", "record_rate_limit", "api_retry_at"):
            setattr(service.spotify, fn, getattr(main.spotify, fn))
        session = import_module(name + ".dashboard_session")
        session.COOKIE += "_" + slug
        service.HTML = service.HTML.replace("fetch(path,options)", f"fetch('{prefix}'+path,options)").replace(
            "href=result.connect_url", f"href='{prefix}'+result.connect_url")
        service.apple_authorization.HTML = service.apple_authorization.HTML.replace("fetch('/v1/", f"fetch('{prefix}/v1/")
        service.PROFILE_HOME = prefix + "/"
        service.POLL_LOCK = service.direct.POLL_LOCK = main.POLL_LOCK
        import_module(service.__package__ + '.sync.engine').POLL_LOCK = main.POLL_LOCK
        service.app.dependency_overrides[service.admin] = main.admin
        service.PROFILE_HOME = '/'
        await stack.enter_async_context(service.lifespan(service.app))
        registry.mount('/' + slug, service.app)
        profiles[slug] = service

    def render():
        main.HTML = template.replace('<section id="direct">', panel(profiles) + '<section id="direct">')

    @asynccontextmanager
    async def lifespan(application):
        async with AsyncExitStack() as stack:
            application.state.stack = stack
            await stack.enter_async_context(main.lifespan(main.app))
            for slug in dict.fromkeys(slots + json.loads(get_setting('pool_slots') or '[]')):
                await attach(slug, stack)
            render()
            scheduler = main.AsyncIOScheduler(timezone='UTC')
            scheduler.add_job(routes.tick, 'interval', seconds=30, max_instances=1, coalesce=True)
            scheduler.start()
            try:
                yield
            finally:
                scheduler.shutdown(wait=False)
                for worker in routes.workers.values():
                    await worker.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    @app.post('/v1/pool/route', dependencies=[Depends(main.admin)])
    async def route(source: str, target: str, action: Literal['run','schedule','stop']):
        return routes.command(source, target, action)

    @app.post('/v1/pool/undo', dependencies=[Depends(main.admin)])
    async def undo_route(source: str, target: str, transfer_id: str):
        rules = routes.rules()
        rule = rules.get(source + '>' + target)
        endpoints = routes.resolve(source, target)
        if not rule or rule['versions'] != routes.versions(endpoints):
            raise HTTPException(409, 'Tài khoản đã thay đổi.')
        worker = routes.worker(source, target, rule['versions'])
        try:
            result = worker.submit_undo(transfer_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        routes.command(source,target,'stop')
        return result

    @app.get('/v1/pool/routes', dependencies=[Depends(main.admin)])
    async def route_status():
        rows = []
        for rule in routes.rules().values():
            try:
                endpoints = routes.resolve(rule['source'], rule['target'])
                valid = routes.versions(endpoints) == rule['versions']
                worker = routes.worker(rule['source'], rule['target'], rule['versions']) if valid else None
                job = worker.status()['job'] if worker else None
                rows.append(dict(source=rule['source'], target=rule['target'], scheduled=rule['scheduled'],
                                 state=(job or {}).get('state','idle') if valid else 'account_changed',
                                 undo=worker.undo.history()[:1] if worker else [],
                                 error=((job or {}).get('result') or {}).get('error')))
            except HTTPException:
                continue
        return rows
    @app.post('/v1/pool/accounts', dependencies=[Depends(main.admin)])
    async def add_slot(name: str):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', name) or name in profiles:
            raise HTTPException(422, 'Tên đã tồn tại hoặc không hợp lệ. Dùng chữ thường, số và dấu gạch ngang.')
        await attach(name, app.state.stack)
        saved = json.loads(get_setting('pool_slots') or '[]')
        set_setting('pool_slots',json.dumps([*saved,name]))
        render()
        return {'slot':name}

    app.mount('/profiles', registry)
    app.mount("/", main.app)
    return app


app = build()
