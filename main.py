"""Botkeep native Python entrypoint: no Docker daemon required."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
# Botkeep's Environment tab provides credentials; set only local paths here.
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("YTM_AUTH_PATH", str(ROOT / "private" / "ytmusic_browser.json"))
os.environ.setdefault("APPLE_KEY_PATH", str(ROOT / "private" / "apple_music_key.p8"))
os.environ.setdefault("APPLE_USER_TOKEN_PATH", str(ROOT / "private" / "apple_user_token.txt"))
os.environ.setdefault("PYTHONUNBUFFERED", "1")
sys.dont_write_bytecode = True

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or "8787")
    if not 1 <= port <= 65535:
        raise ValueError("SERVER_PORT must be in 1..65535")
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=port,
        workers=1, access_log=False, log_level="warning"
    )
