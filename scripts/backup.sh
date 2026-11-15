#!/usr/bin/env bash
set -euo pipefail
mkdir -p backups
stamp=$(date -u +'%Y%m%dT%H%M%SZ')
# SQLite source files are owned by uid=10001. Run as root/admin with file read privileges.
python3 - "$stamp" <<'PY'
from pathlib import Path
import sqlite3,sys
source=Path('data/music.db')
if not source.is_file():
    raise SystemExit('data/music.db does not exist')
target=Path('backups')/f'music-{sys.argv[1]}.sqlite3'
with sqlite3.connect(source) as src, sqlite3.connect(target) as dest:
    src.backup(dest)
print(target)
PY
