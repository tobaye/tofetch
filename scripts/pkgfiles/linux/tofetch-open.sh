#!/usr/bin/env bash
# Open magnet links / .torrent files with this ToFetch package (Linux).
# Registered via tofetch.desktop; ensures the server is running first.
cd "$(dirname "$0")"
TARGET="${1:-}"

if ! curl -s --max-time 2 http://127.0.0.1:8077/api/state >/dev/null 2>&1; then
  nohup ./tofetch.sh >/dev/null 2>&1 &
  for i in $(seq 1 30); do
    curl -s --max-time 2 http://127.0.0.1:8077/api/state >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

if [ -n "$TARGET" ]; then
  TARGET="$TARGET" python3 - <<'PYEOF'
import base64, json, os, urllib.request
from pathlib import Path

t = os.environ.get("TARGET", "")
try:
    if t.lower().endswith(".torrent") and Path(t).is_file():
        body = {"data": base64.b64encode(Path(t).read_bytes()).decode()}
        path = "/api/torrent"
    else:
        body = {"method": "addUri", "params": [[t], {}]}
        path = "/api/rpc"
    req = urllib.request.Request(
        "http://127.0.0.1:8077" + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()
except Exception:
    pass  # the UI opens anyway; the user can add it manually
PYEOF
fi
xdg-open "http://127.0.0.1:8077/" >/dev/null 2>&1 || true
