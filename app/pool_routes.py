"""Persist explicitly selected cross-profile routes and their independent write journals."""
import hashlib
import json
import sys
import time
from dataclasses import replace
from importlib import import_module
from types import ModuleType
from pathlib import Path

from fastapi import HTTPException

from . import main
from .db import get_setting, set_setting


class Routes:
    def __init__(self, profiles):
        self.profiles = profiles
        self.workers = {}

    def resolve(self, source, target):
        endpoints = []
        for endpoint in (source, target):
            slot, _, provider = endpoint.partition('/')
            if slot not in self.profiles or provider not in ('spotify', 'ytmusic'):
                raise HTTPException(422, 'Tài khoản không hợp lệ.')
            endpoints.append((self.profiles[slot], provider))
        if endpoints[0][1] == endpoints[1][1]:
            raise HTTPException(422, 'Chọn hai dịch vụ khác nhau.')
        return endpoints

    def versions(self, endpoints):
        return {p: svc.direct._auth_versions([p])[p] for svc, p in endpoints}

    def worker(self, source, target, versions):
        key = hashlib.sha256(json.dumps([source, target, versions], sort_keys=True).encode()).hexdigest()
        if key in self.workers:
            return self.workers[key]
        endpoints = self.resolve(source, target)
        name = 'sync_route_' + key
        package = ModuleType(name)
        package.__path__ = [str(Path(__file__).parent)]
        sys.modules[name] = package
        config = import_module(name + '.config')
        config.settings = replace(main.settings, data_dir=main.settings.data_dir / 'routes' / key)
        for svc, p in endpoints:
            sys.modules[name + '.connectors.' + p] = getattr(svc, p)
        db = import_module(name + '.db')
        db.init_db()
        direct = import_module(name + '.sync.direct')
        direct.POLL_LOCK = main.POLL_LOCK
        def bound_versions(providers):
            current = self.versions(endpoints)
            if current != versions:
                raise ValueError('Tài khoản đã thay đổi. Hãy tạo lại lịch.')
            return {p: current[p] for p in providers}
        direct._auth_versions = bound_versions
        self.workers[key] = direct
        direct.recover()
        return direct

    def rules(self):
        return json.loads(get_setting('pool_routes') or '{}')

    def command(self, source, target, action):
        endpoints = self.resolve(source, target)
        rules = self.rules()
        key = source + '>' + target
        if action == 'stop':
            if key in rules:
                rules[key]['scheduled'] = False
            set_setting('pool_routes', json.dumps(rules))
            return {'paused': True}
        if not main.direct.ENABLED or not all(s.provider_ready(p) for s, p in endpoints):
            raise HTTPException(409, 'Cần bật chuyển nhạc và kết nối cả hai tài khoản.')
        versions = self.versions(endpoints)
        worker = self.worker(source, target, versions)
        rules[key] = dict(source=source, target=target, versions=versions,
                          scheduled=action=='schedule' or rules.get(key, {}).get('scheduled', False))
        set_setting('pool_routes', json.dumps(rules))
        return worker.submit(endpoints[0][1], endpoints[1][1], scheduled=action=='schedule')

    async def tick(self):
        for rule in self.rules().values():
            endpoints = self.resolve(rule['source'], rule['target'])
            if not all(s.provider_ready(p) for s, p in endpoints) or self.versions(endpoints) != rule['versions']:
                continue
            worker = self.worker(rule['source'], rule['target'], rule['versions'])
            if rule['scheduled'] and not worker.running() and (worker.schedule.next_check('direct') or 0)<=time.time():
                worker.submit(endpoints[0][1], endpoints[1][1], scheduled=True)
