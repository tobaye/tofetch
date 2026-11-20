#!/usr/bin/env python3
"""Open magnet links / .torrent files with this ToFetch package.

Registered (per-user, no admin) as the handler for magnet: links and
.torrent files by 关联-注册.bat. Ensures the local server is running
(starts it hidden if not), submits the link/torrent, opens the UI.
Standard library only, like the rest of ToFetch.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8077"


def state():
    try:
        with urllib.request.urlopen(BASE + "/api/state", timeout=2) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ensure_running() -> bool:
    if state() is not None:
        return True
    download_dir = Path.home() / "Downloads" / "ToFetch"
    download_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "ARIA2_BIN": str(HERE / "aria2c.exe"),
        "TOFETCH_PORT": "8077",
        "TOFETCH_HOST": "127.0.0.1",
        "TOFETCH_DOWNLOAD_DIR": str(download_dir),
    })
    try:
        subprocess.Popen(
            [str(HERE / "python" / "pythonw.exe"), str(HERE / "server.py")],
            env=env, cwd=str(HERE),
            creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_GROUP
        )
    except OSError:
        return False
    for _ in range(40):
        if state() is not None:
            return True
        time.sleep(0.5)
    return False


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    if len(sys.argv) < 2:
        return
    target = sys.argv[1]
    if not ensure_running():
        return
    try:
        if target.lower().endswith(".torrent") and Path(target).is_file():
            data = base64.b64encode(Path(target).read_bytes()).decode()
            post("/api/torrent", {"data": data})
        else:
            post("/api/rpc", {"method": "addUri", "params": [[target], {}]})
    except Exception:
        pass  # the UI opening below still lets the user add it manually
    webbrowser.open(BASE + "/")


if __name__ == "__main__":
    main()
