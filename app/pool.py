"""Mount isolated account pairs in one Botkeep process."""
import os
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

from fastapi import FastAPI

from . import main
from .config import settings


def build():
    slots = list(dict.fromkeys(s.strip() for s in os.getenv("SYNC_PROFILES", "secondary").split(",") if s.strip()))
    if any(not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", s) for s in slots):
        raise ValueError("Invalid SYNC_PROFILES name")
    mounts = []
    links = [("/", "Chính")] + [(f"/profiles/{s}/", s) for s in slots]
    for slug in slots:
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
        mounts.append((prefix, service))
    services = [main, *(service for _, service in mounts)]
    for (url, label), service in zip(links, services):
        nav = ' · '.join(f'<a href="{path}">{title}</a>' if path != url else f'<strong>{title}</strong>'
                         for path, title in links)
        service.HTML = service.HTML.replace('<section id="providers">', f'<nav aria-label="Hồ sơ">Hồ sơ: {nav}</nav><section id="providers">')

    @asynccontextmanager
    async def lifespan(_):
        async with AsyncExitStack() as stack:
            for service in services:
                await stack.enter_async_context(service.lifespan(service.app))
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    for prefix, service in mounts:
        app.mount(prefix, service.app)
    app.mount("/", main.app)
    return app


app = build()
