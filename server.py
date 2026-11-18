#!/usr/bin/env python3
"""ToFetch - a tiny self-hosted download manager.

Architecture: aria2c does all the downloading (HTTP/FTP/BitTorrent/magnet).
This file is only a thin shell around it:

  * serves the single-file web UI in ./static
  * proxies JSON-RPC calls to aria2, injecting the RPC secret server-side
    (the secret never reaches the browser)
  * optionally starts aria2c itself, so the whole thing runs as one command
  * serves finished files back to the browser from the download directory

Only the Python standard library is used, on purpose: nothing here can be
broken by a package registry, a renamed dependency or an abandoned upstream.

Configuration (environment variables):
  TOFETCH_PORT          web UI port                (default 8077)
  TOFETCH_HOST          bind address               (default 0.0.0.0)
  TOFETCH_PASSWORD      web UI password; empty = no login (LAN only!)
  TOFETCH_LOGIN_MAX_FAILS    failed logins per client before a lock (default 5)
  TOFETCH_LOGIN_LOCK_SECONDS login lock duration in seconds (default 900)
  TOFETCH_DOWNLOAD_DIR  where files are downloaded (default ./downloads)
  TOFETCH_DATA_DIR      session/config state       (default ./data)
  ARIA2_BIN             path to aria2c             (default: find in PATH)
  ARIA2_RPC_PORT        aria2 RPC port             (default 6800)
  ARIA2_SECRET          aria2 RPC secret           (default: stored in data dir)
  ARIA2_AUTOSTART       "1" = start aria2c if it is not already answering
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

# Windows consoles/pipes may default to a legacy code page (cp1252/cp936...);
# our log/UI strings are Chinese, and a UnicodeEncodeError while *reporting*
# an error must never kill the server. Force UTF-8 with replacement.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _env(name, default=""):
    """Read a TOFETCH_* environment variable."""
    return os.environ.get("TOFETCH_" + name, default)


PORT = int(_env("PORT", "8077"))
HOST = _env("HOST", "0.0.0.0")
PASSWORD = _env("PASSWORD", "")
DOWNLOAD_DIR = Path(_env("DOWNLOAD_DIR", str(ROOT / "downloads"))).resolve()
DATA_DIR = Path(_env("DATA_DIR", str(ROOT / "data"))).resolve()
RPC_PORT = int(os.environ.get("ARIA2_RPC_PORT", "6800"))
RPC_URL = f"http://127.0.0.1:{RPC_PORT}/jsonrpc"
AUTOSTART = os.environ.get("ARIA2_AUTOSTART", "1") == "1"

SESSIONS = set()  # live login tokens (in memory only)
LOGIN_MAX_FAILS = int(_env("LOGIN_MAX_FAILS", "5"))
LOGIN_LOCK_SECONDS = int(_env("LOGIN_LOCK_SECONDS", "900"))
LOGIN_FAILS = {}  # client key -> (consecutive failures, locked-until epoch)
ENGINE_ERROR = ""  # why the engine is not running, surfaced in /api/state

# ------------------------------------------------------------- settings
# User-tunable engine settings, persisted in the data dir so they survive
# restarts (aria2 itself forgets changeGlobalOption on exit). Two groups:
#   "global": applied to the engine via changeGlobalOption
#   "task":   defaults merged into every addUri/addTorrent call
SETTINGS_PATH = DATA_DIR / "settings.json"
GLOBAL_OPT_KEYS = {
    "max-overall-download-limit", "max-overall-upload-limit",
    "max-concurrent-downloads", "max-connection-per-server", "split",
    "all-proxy", "bt-tracker", "seed-ratio", "seed-time",
}
TASK_OPT_KEYS = {"max-tries", "retry-wait"}
# Per-task options the add dialog may pass through (advanced section).
ADD_OPT_KEYS = {
    "split", "max-connection-per-server", "max-download-limit",
    "user-agent", "referer", "header", "max-tries", "retry-wait",
    "all-proxy", "checksum", "pause",
}


def load_settings() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    out = {"global": {}, "task": {}, "schedule": {}, "prefs": {}}
    if isinstance(raw, dict):
        for group, keys in (("global", GLOBAL_OPT_KEYS), ("task", TASK_OPT_KEYS)):
            vals = raw.get(group)
            if isinstance(vals, dict):
                out[group] = {k: str(v) for k, v in vals.items() if k in keys}
        if isinstance(raw.get("schedule"), dict):
            out["schedule"] = raw["schedule"]
        if isinstance(raw.get("prefs"), dict):
            out["prefs"] = {k: v for k, v in raw["prefs"].items()
                            if isinstance(v, (str, bool, int, float)) and len(str(v)) <= 500}
    return out


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def _clean_schedule(sched: dict):
    """Validate a schedule dict from the client; None means reject."""
    import re as _re
    hhmm = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    out = {}
    windows = sched.get("speed_windows")
    if windows:
        if not isinstance(windows, list) or len(windows) > 8:
            return None
        cleaned_windows = []
        for w in windows:
            if not isinstance(w, dict):
                return None
            frm, to = str(w.get("from", "")), str(w.get("to", ""))
            if not (hhmm.match(frm) and hhmm.match(to)):
                return None
            try:
                down = int(w.get("down", 0))
                up = int(w.get("up", 0))
            except (TypeError, ValueError):
                return None
            if not (0 <= down <= 10 ** 6 and 0 <= up <= 10 ** 6):
                return None
            cleaned_windows.append({"from": frm, "to": to, "down": down, "up": up})
        out["speed_windows"] = cleaned_windows
    for key in ("pause_at", "resume_at"):
        v = str(sched.get(key, "")).strip()
        if v:
            if not hhmm.match(v):
                return None
            out[key] = v
    url = str(sched.get("tracker_url", "")).strip()
    if url:
        if len(url) > 500 or not url.startswith(("http://", "https://")):
            return None
        out["tracker_url"] = url
        try:
            hours = float(sched.get("tracker_hours") or 24)
        except (TypeError, ValueError):
            return None
        if not (1 <= hours <= 168):
            return None
        out["tracker_hours"] = hours
    return out


def _valid_setting(key: str, value: str) -> bool:
    if len(value) > 8000:
        return False
    if key in ("all-proxy", "bt-tracker"):
        return True
    if key == "seed-ratio":
        try:
            return 0.0 <= float(value) <= 100.0
        except ValueError:
            return False
    try:
        n = int(value)
    except ValueError:
        return False
    ranges = {
        "max-overall-download-limit": (0, 10 ** 12),
        "max-overall-upload-limit": (0, 10 ** 12),
        "max-concurrent-downloads": (1, 50),
        "max-connection-per-server": (1, 64),
        "split": (1, 64),
        "seed-time": (0, 60 * 24 * 30),
        "max-tries": (0, 100),
        "retry-wait": (0, 3600),
    }
    lo, hi = ranges.get(key, (0, 10 ** 9))
    return lo <= n <= hi


def log_event(msg: str) -> None:
    """Print and append to the on-disk diagnostic log (data dir)."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(f"[tofetch] {msg}", flush=True)
    except Exception:
        pass  # logging must never be fatal
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logf = DATA_DIR / "tofetch.log"
        if logf.exists() and logf.stat().st_size > 512 * 1024:
            tail = logf.read_bytes()[-256 * 1024:]
            logf.write_bytes(tail)
        with logf.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_or_create_secret() -> str:
    env = os.environ.get("ARIA2_SECRET")
    if env:
        return env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = DATA_DIR / "rpc-secret.txt"
    if f.exists():
        return f.read_text().strip()
    s = secrets.token_hex(16)
    f.write_text(s)
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return s


RPC_SECRET = load_or_create_secret()


def rpc_call(method: str, params: list, timeout: float = 15.0):
    """One JSON-RPC call to aria2 with the secret token injected."""
    payload = {
        "jsonrpc": "2.0",
        "id": "tofetch",
        "method": method,
        "params": [f"token:{RPC_SECRET}"] + params,
    }
    req = urllib.request.Request(
        RPC_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def rpc_alive() -> bool:
    try:
        rpc_call("aria2.getVersion", [], timeout=3)
        return True
    except Exception:
        return False


def apply_global_settings() -> None:
    """Push the persisted global options into the running engine."""
    settings = load_settings()
    if settings["global"] and rpc_alive():
        try:
            rpc_call("aria2.changeGlobalOption", [settings["global"]], timeout=5)
            log_event("applied saved global settings")
        except Exception:
            pass


def with_task_defaults(options: dict) -> dict:
    """Merge the persisted per-task defaults (retry policy) into add options."""
    merged = dict(options or {})
    for k, v in load_settings()["task"].items():
        merged.setdefault(k, v)
    return merged


# -------------------------------------------------------------- history
# Finished/failed tasks are harvested from tellStopped responses (the UI
# polls them anyway) into a persistent history file, so the record of what
# was downloaded survives restarts and removeDownloadResult.
HISTORY_PATH = DATA_DIR / "history.json"
HISTORY_CAP = 500
_history_lock = threading.Lock()


def _task_display_name(task: dict) -> str:
    bt = task.get("bittorrent") or {}
    info = bt.get("info") or {}
    if info.get("name"):
        return str(info["name"])
    files = task.get("files") or []
    if files:
        p = files[0].get("path") or ""
        if p:
            return p.rsplit("/", 1)[-1]
        uris = files[0].get("uris") or []
        if uris:
            return str(uris[0].get("uri", ""))
    return ""


def _record_history(tasks) -> None:
    fresh = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        if t.get("status") not in ("complete", "error", "removed"):
            continue
        gid = t.get("gid")
        if not gid:
            continue
        files = t.get("files") or []
        fresh.append({
            "gid": gid,
            "name": _task_display_name(t),
            "status": t.get("status"),
            "total": int(t.get("totalLength") or 0),
            "done": int(t.get("completedLength") or 0),
            "dir": t.get("dir", ""),
            "path": (files[0].get("path") if files else "") or "",
            "error": t.get("errorMessage", ""),
            "at": int(time.time()),
        })
    if not fresh:
        return
    with _history_lock:
        try:
            entries = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []
        known = {e.get("gid") for e in entries}
        add = [e for e in fresh if e["gid"] not in known]
        if not add:
            return
        entries = (add + entries)[:HISTORY_CAP]
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            tmp.replace(HISTORY_PATH)
        except OSError:
            pass
    _fire_webhooks(add)


def _fire_webhooks(entries) -> None:
    """POST newly finished/failed tasks to the user's webhook URL, if set."""
    url = str(load_settings().get("prefs", {}).get("webhook_url", "")).strip()
    if not url or not entries:
        return

    def send(entry):
        try:
            payload = json.dumps({
                "event": entry.get("status"), "name": entry.get("name"),
                "total": entry.get("total"), "path": entry.get("path"),
                "gid": entry.get("gid"), "at": entry.get("at"),
            }).encode()
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass

    for e in entries:
        threading.Thread(target=send, args=(e,), daemon=True).start()


def _harvest_history(node) -> None:
    """Walk an RPC result structure; record any terminal tasks found."""
    if isinstance(node, dict):
        if "gid" in node and "status" in node:
            _record_history([node])
        for v in node.values():
            _harvest_history(v)
    elif isinstance(node, list):
        for v in node:
            _harvest_history(v)


def load_history() -> list:
    try:
        entries = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return entries if isinstance(entries, list) else []
    except Exception:
        return []


# ------------------------------------------------------------ scheduler
# One daemon thread drives the time-based features from the persisted
# schedule settings: speed-limit windows, daily pause/resume, and
# tracker-list refresh from a URL.
_sched_state = {"window_active": False, "fired": {}, "tracker_last": 0.0}


def _in_window(hhmm: str, start: str, end: str) -> bool:
    if not (start and end):
        return False
    if start <= end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end  # window wraps midnight


def _scheduler_tick() -> None:
    if not rpc_alive():
        return
    sched = load_settings().get("schedule") or {}
    hhmm = time.strftime("%H:%M")
    active = None
    for w in sched.get("speed_windows") or []:
        if isinstance(w, dict) and _in_window(hhmm, str(w.get("from", "")), str(w.get("to", ""))):
            active = w
            break
    if active and not _sched_state["window_active"]:
        try:
            rpc_call("aria2.changeGlobalOption", [{
                "max-overall-download-limit": str(int(active.get("down", 0)) * 1024),
                "max-overall-upload-limit": str(int(active.get("up", 0)) * 1024),
            }], timeout=5)
            _sched_state["window_active"] = True
            log_event(f"speed window {active.get('from')}–{active.get('to')} entered")
        except Exception:
            pass
    elif not active and _sched_state["window_active"]:
        restore = dict(load_settings()["global"])
        restore.setdefault("max-overall-download-limit", "0")
        restore.setdefault("max-overall-upload-limit", "0")
        try:
            rpc_call("aria2.changeGlobalOption", [restore], timeout=5)
        except Exception:
            pass
        _sched_state["window_active"] = False
        log_event("speed window left, saved limits restored")
    today = time.strftime("%Y-%m-%d")
    for key, method in (("pause_at", "aria2.pauseAll"), ("resume_at", "aria2.unpauseAll")):
        at = sched.get(key)
        if at and hhmm >= str(at) and _sched_state["fired"].get(key) != today:
            try:
                rpc_call(method, [], timeout=5)
                _sched_state["fired"][key] = today
                log_event(f"scheduled {key} fired at {hhmm}")
            except Exception:
                pass
    url = sched.get("tracker_url")
    if url:
        try:
            hours = float(sched.get("tracker_hours") or 24)
        except (TypeError, ValueError):
            hours = 24.0
        if time.time() - _sched_state["tracker_last"] > hours * 3600:
            _sched_state["tracker_last"] = time.time()
            try:
                req = urllib.request.Request(str(url), headers={"User-Agent": "ToFetch"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    text = r.read().decode(errors="replace")
                trackers = ",".join(x.strip() for x in text.replace("\r", "\n").replace("\n", ",").split(",") if x.strip())
                if trackers:
                    settings = load_settings()
                    settings["global"]["bt-tracker"] = trackers
                    save_settings(settings)
                    rpc_call("aria2.changeGlobalOption", [{"bt-tracker": trackers}], timeout=5)
                    log_event("tracker list refreshed from URL")
            except Exception:
                pass


def _scheduler_loop() -> None:
    try:
        tick = float(os.environ.get("TOFETCH_SCHED_TICK", "20"))
    except ValueError:
        tick = 20.0
    while True:
        time.sleep(max(1.0, tick))
        try:
            _scheduler_tick()
        except Exception:
            pass


# ---------------------------------------------------------------- aria2 child

_aria2_proc = None


def start_aria2() -> None:
    global _aria2_proc, ENGINE_ERROR
    if rpc_alive():
        ENGINE_ERROR = ""
        log_event("aria2 already running, using it")
        return
    import shutil

    binary = os.environ.get("ARIA2_BIN") or shutil.which("aria2c")
    if not binary:
        ENGINE_ERROR = "找不到 aria2 引擎（PATH 里没有 aria2c，也没设 ARIA2_BIN）"
        log_event(ENGINE_ERROR + "; UI will run without an engine")
        return
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session = DATA_DIR / "session.txt"
    session.touch(exist_ok=True)
    conf = DATA_DIR / "aria2.conf"
    if not conf.exists():
        conf.write_text((ROOT / "aria2.conf").read_text() if (ROOT / "aria2.conf").exists() else "")
    args = [
        binary,
        f"--conf-path={conf}" if conf.read_text().strip() else "--no-conf",
        "--enable-rpc=true",
        f"--rpc-listen-port={RPC_PORT}",
        "--rpc-listen-all=false",
        f"--rpc-secret={RPC_SECRET}",
        f"--dir={DOWNLOAD_DIR}",
        f"--input-file={session}",
        f"--save-session={session}",
        "--save-session-interval=5",
        "--continue=true",
        "--summary-interval=0",
        "--daemon=false",
    ]
    # Optional CA bundle override (Android: the engine has no system CA
    # store to read, so the app ships one and points aria2 at it).
    ca_bundle = os.environ.get("ARIA2_CA_CERTIFICATE")
    if ca_bundle and Path(ca_bundle).exists():
        args.append(f"--ca-certificate={ca_bundle}")
    # Optional: use the OS resolver instead of aria2's built-in async one.
    # On Android the async resolver (c-ares) finds no usable nameserver
    # config and lookups fail with "Could not resolve host", while the
    # system resolver (getaddrinfo) goes through Android's resolver.
    if os.environ.get("ARIA2_ASYNC_DNS", "").strip().lower() in ("false", "0", "no", "off"):
        args.append("--async-dns=false")
    aria2_log = DATA_DIR / "aria2.log"
    try:
        if aria2_log.exists() and aria2_log.stat().st_size > 1024 * 1024:
            aria2_log.write_bytes(aria2_log.read_bytes()[-256 * 1024:])
    except OSError:
        pass
    try:
        logf = aria2_log.open("ab")
    except OSError:
        logf = subprocess.DEVNULL
    try:
        _aria2_proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)
    except OSError as exc:
        ENGINE_ERROR = f"引擎启动失败：{binary} 无法执行（{exc.strerror or exc}）。常见原因：二进制与设备架构不匹配（如 x86_64 机器上放了 arm64 版）"
        log_event(ENGINE_ERROR)
        return
    for _ in range(50):
        if rpc_alive():
            ENGINE_ERROR = ""
            log_event(f"aria2 started (pid {_aria2_proc.pid})")
            return
        if _aria2_proc.poll() is not None:
            break
        time.sleep(0.2)
    tail = ""
    try:
        tail = aria2_log.read_bytes()[-400:].decode(errors="replace").strip().replace("\n", " ")
    except OSError:
        pass
    ENGINE_ERROR = f"引擎未能启动（进程已退出）。{tail}"
    log_event(ENGINE_ERROR)


def stop_aria2() -> None:
    global _aria2_proc
    try:
        rpc_call("aria2.saveSession", [], timeout=3)
    except Exception:
        pass
    if _aria2_proc and _aria2_proc.poll() is None:
        _aria2_proc.terminate()
        try:
            _aria2_proc.wait(timeout=5)
        except Exception:
            _aria2_proc.kill()



# ------------------------------------------------------- tasks export/import
_EXPORT_KEYS = ["gid", "status", "dir", "files", "bittorrent"]


def _task_export_entry(st):
    uris = []
    for f in st.get("files") or []:
        for u in f.get("uris") or []:
            uri = u.get("uri")
            if uri and uri not in uris:
                uris.append(uri)
    bt = st.get("bittorrent") or {}
    if bt.get("infoHash") and not any(u.startswith("magnet:") for u in uris):
        uris.insert(0, "magnet:?xt=urn:btih:" + bt["infoHash"])
    if not uris:
        return None
    entry = {"uris": uris}
    try:
        rel = Path(st.get("dir") or "").resolve().relative_to(DOWNLOAD_DIR)
        if str(rel) not in (".", ""):
            entry["dir"] = str(rel)
    except Exception:
        pass
    return entry


def collect_export_tasks():
    """All non-complete tasks as portable {uris, dir} entries."""
    tasks, seen = [], set()
    for method, params in (("aria2.tellActive", []),
                           ("aria2.tellWaiting", [0, 1000]),
                           ("aria2.tellStopped", [0, 1000])):
        try:
            resp = rpc_call(method, params + [_EXPORT_KEYS], timeout=10)
        except Exception:
            continue
        for st in resp.get("result") or []:
            if st.get("status") == "complete" or st.get("gid") in seen:
                continue
            seen.add(st.get("gid"))
            entry = _task_export_entry(st)
            if entry:
                tasks.append(entry)
    return tasks


def import_tasks(items):
    added, skipped, gids = 0, 0, []
    for item in items[:200]:
        if not isinstance(item, dict):
            skipped += 1
            continue
        uris = [u for u in (item.get("uris") or [])
                if isinstance(u, str) and u.strip()]
        if not uris:
            skipped += 1
            continue
        options = {}
        sub = str(item.get("dir") or "").strip()
        if sub:
            target = (DOWNLOAD_DIR / sub).resolve()
            try:
                target.relative_to(DOWNLOAD_DIR)
                options["dir"] = str(target)
            except Exception:
                pass  # a dir outside the download root is dropped, not fatal
        extra = item.get("options")
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k in ADD_OPT_KEYS:
                    options[k] = v
        try:
            resp = rpc_call("aria2.addUri", [uris, with_task_defaults(options)])
            if isinstance(resp, dict) and resp.get("error"):
                skipped += 1
                continue
            added += 1
            gids.append(resp.get("result"))
        except Exception:
            skipped += 1
    if added:
        try:
            rpc_call("aria2.saveSession", [], timeout=3)
        except Exception:
            pass
    return added, skipped, gids


# ------------------------------------------------------------- link probing
_PROBE_UA = "Mozilla/5.0 (compatible; ToFetch probe)"


def _filename_from_cd(cd):
    import re as _re
    m = _re.search(r"filename\*=UTF-8''([^;]+)", cd or "")
    if m:
        return unquote(m.group(1)).strip().strip('"')
    m = _re.search(r'filename="?([^";]+)"?', cd or "")
    return m.group(1).strip() if m else ""


def _probe_one(url):
    out = {"url": url, "ok": False, "status": 0, "size": 0,
           "name": "", "error": ""}
    if not url.startswith(("http://", "https://")):
        out["error"] = "unsupported scheme"
        return out
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url, method=method, headers={"User-Agent": _PROBE_UA})
            if method == "GET":
                req.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(req, timeout=8) as resp:
                out["status"] = resp.status
                out["ok"] = 200 <= resp.status < 400
                out["name"] = _filename_from_cd(
                    resp.headers.get("Content-Disposition") or "")
                cr = resp.headers.get("Content-Range") or ""
                tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
                if tail.isdigit():
                    out["size"] = int(tail)
                elif method == "HEAD":
                    cl = resp.headers.get("Content-Length") or ""
                    if cl.isdigit():
                        out["size"] = int(cl)
                return out
        except urllib.error.HTTPError as e:
            out["status"] = e.code
            if e.code in (405, 501) and method == "HEAD":
                continue
            out["error"] = f"HTTP {e.code}"
            return out
        except Exception as e:
            if method == "HEAD":
                continue
            out["error"] = str(e)[:120]
            return out
    return out


# ------------------------------------------------------------------- the app

ALLOWED_METHODS = {
    "addUri", "addTorrent", "addMetalink",
    "tellActive", "tellWaiting", "tellStopped", "tellStatus",
    "pause", "unpause", "pauseAll", "unpauseAll",
    "remove", "removeDownloadResult", "purgeDownloadResult",
    "getGlobalStat", "getGlobalOption", "changeGlobalOption", "changeOption",
    "getVersion", "getSessionInfo", "saveSession", "getFiles", "getUris",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ToFetch/1.2.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console quiet
        sys.stderr.write("[tofetch] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj).encode(), extra=extra)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _client_key(self) -> str:
        # Reverse proxies (cloudflared) connect from localhost and carry
        # the real client in CF-Connecting-IP / X-Forwarded-For. Only trust
        # those headers from a loopback peer; otherwise use the peer itself.
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):
            for hdr in ("CF-Connecting-IP", "X-Forwarded-For"):
                v = self.headers.get(hdr)
                if v:
                    return v.split(",")[0].strip()
        return peer

    def _authed(self) -> bool:
        if not PASSWORD:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get("tofetch_session")
        return bool(morsel and morsel.value in SESSIONS)

    def _need_auth(self):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "login required", "login": True})

    # -- routes --------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file(STATIC / "index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            if not self._authed():
                return self._need_auth()
            alive = rpc_alive()
            return self._json(200, {
                "engine": alive,
                "engine_error": "" if alive else ENGINE_ERROR,
                "password": bool(PASSWORD),
                "download_dir": str(DOWNLOAD_DIR),
            })
        if path == "/api/settings":
            if not self._authed():
                return self._need_auth()
            return self._json(200, load_settings())
        if path == "/api/history":
            if not self._authed():
                return self._need_auth()
            return self._json(200, {"entries": load_history()})
        if path == "/api/export":
            if not self._authed():
                return self._need_auth()
            payload = {"app": "tofetch", "version": 1,
                       "exported_at": int(time.time()),
                       "tasks": collect_export_tasks()}
            body = json.dumps(payload, ensure_ascii=False, indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             "attachment; filename=tofetch-tasks.json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/log":
            if not self._authed():
                return self._need_auth()
            parts = []
            for name in ("tofetch.log", "aria2.log"):
                f = DATA_DIR / name
                if f.exists():
                    data = f.read_bytes()[-60000:].decode(errors="replace")
                    parts.append(f"===== {name} =====\n{data}")
            # The Android shell passes its startup log path in; include it
            # so this one export carries the full diagnostics picture.
            startup_log = os.environ.get("TOFETCH_STARTUP_LOG")
            if startup_log and Path(startup_log).exists():
                data = Path(startup_log).read_bytes()[-60000:].decode(errors="replace")
                parts.append(f"===== startup.log =====\n{data}")
            body = ("\n\n".join(parts) or "no logs yet").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=tofetch-diagnostics.txt")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/files/"):
            if not self._authed():
                return self._need_auth()
            return self._serve_download(unquote(path[len("/files/"):]))
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            body = self._read_json()
            if not PASSWORD:
                return self._json(200, {"ok": True})
            key = self._client_key()
            now = time.time()
            fails, locked_until = LOGIN_FAILS.get(key, (0, 0.0))
            if locked_until > now:
                mins = max(1, int((locked_until - now) // 60) + 1)
                return self._json(HTTPStatus.TOO_MANY_REQUESTS,
                                  {"error": f"登录失败次数过多，已锁定，请约 {mins} 分钟后再试"})
            if hmac.compare_digest(str(body.get("password", "")), PASSWORD):
                LOGIN_FAILS.pop(key, None)
                token = secrets.token_hex(24)
                SESSIONS.add(token)
                return self._json(200, {"ok": True}, extra={
                    "Set-Cookie": f"tofetch_session={token}; Path=/; HttpOnly; SameSite=Lax",
                })
            fails += 1
            if fails >= LOGIN_MAX_FAILS:
                LOGIN_FAILS[key] = (0, now + LOGIN_LOCK_SECONDS)
                log_event(f"login locked for {key}: {LOGIN_MAX_FAILS} failed attempts")
            else:
                LOGIN_FAILS[key] = (fails, 0.0)
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "wrong password"})
        if path == "/api/logout":
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            morsel = cookie.get("tofetch_session")
            if morsel:
                SESSIONS.discard(morsel.value)
            return self._json(200, {"ok": True})
        if not self._authed():
            return self._need_auth()
        if path == "/api/settings":
            body = self._read_json()
            settings = load_settings()
            for group, keys in (("global", GLOBAL_OPT_KEYS), ("task", TASK_OPT_KEYS)):
                vals = body.get(group)
                if isinstance(vals, dict):
                    for k, v in vals.items():
                        if k not in keys:
                            return self._json(HTTPStatus.BAD_REQUEST, {"error": f"unknown setting: {k}"})
                        v = str(v).strip()
                        if v == "":
                            settings[group].pop(k, None)  # empty = back to engine default
                        elif _valid_setting(k, v):
                            settings[group][k] = v
                        else:
                            return self._json(HTTPStatus.BAD_REQUEST, {"error": f"bad value for {k}"})
            sched = body.get("schedule")
            if isinstance(sched, dict):
                cleaned = _clean_schedule(sched)
                if cleaned is None:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad schedule"})
                settings["schedule"] = cleaned
            prefs = body.get("prefs")
            if isinstance(prefs, dict):
                for k, v in prefs.items():
                    if not isinstance(v, (str, bool, int, float)) or len(str(v)) > 500:
                        return self._json(HTTPStatus.BAD_REQUEST, {"error": f"bad pref: {k}"})
                    if v == "":
                        settings["prefs"].pop(k, None)
                    else:
                        settings["prefs"][k] = v
            save_settings(settings)
            apply_global_settings()
            return self._json(200, {"ok": True, "settings": settings})
        if path == "/api/history-clear":
            with _history_lock:
                try:
                    HISTORY_PATH.unlink()
                except OSError:
                    pass
            return self._json(200, {"ok": True})
        if path == "/api/import":
            body = self._read_json()
            items = body.get("tasks")
            if not isinstance(items, list):
                return self._json(HTTPStatus.BAD_REQUEST,
                                  {"error": "tasks list required"})
            added, skipped, gids = import_tasks(items)
            return self._json(200, {"added": added, "skipped": skipped,
                                    "gids": gids})
        if path == "/api/probe":
            body = self._read_json()
            urls = body.get("urls")
            if not isinstance(urls, list) or not urls:
                return self._json(HTTPStatus.BAD_REQUEST,
                                  {"error": "urls required"})
            urls = [str(u) for u in urls[:20]]
            with ThreadPoolExecutor(max_workers=6) as ex:
                results = list(ex.map(_probe_one, urls))
            return self._json(200, {"results": results})
        if path == "/api/remove-files":
            return self._remove_files()
        if path == "/api/rpc":
            return self._proxy_rpc()
        if path == "/api/torrent":
            body = self._read_json()
            data = body.get("data") or ""
            try:
                base64.b64decode(data, validate=True)
            except Exception:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad torrent data"})
            options = {}
            if body.get("dir"):
                options["dir"] = str((DOWNLOAD_DIR / body["dir"]).resolve())
            extra = body.get("options")
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if k in ADD_OPT_KEYS:
                        options[k] = v
            options = with_task_defaults(options)
            try:
                resp = rpc_call("aria2.addTorrent", [data, [], options])
                if isinstance(resp, dict) and resp.get("error"):
                    return self._json(HTTPStatus.BAD_GATEWAY,
                                      {"error": str(resp["error"].get("message", resp["error"]))})
                try:
                    rpc_call("aria2.saveSession", [], timeout=3)
                except Exception:
                    pass
                return self._json(200, {"gid": resp.get("result")})
            except Exception as e:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"aria2: {e}"})
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # -- pieces --------------------------------------------------------
    def _remove_files(self):
        """Remove a task AND delete its downloaded files from disk.

        Only paths inside the download dir are touched; the .aria2 control
        file next to each download goes with it.
        """
        body = self._read_json()
        gid = str(body.get("gid", ""))
        if not gid:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "gid required"})
        try:
            resp = rpc_call("aria2.tellStatus", [gid])
            status = resp.get("result") or {}
        except Exception as e:
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"aria2: {e}"})
        paths = [f.get("path") for f in status.get("files") or [] if f.get("path")]
        if status.get("status") in ("complete", "error", "removed"):
            _record_history([status])  # keep a history trace before it goes
        try:
            if status.get("status") in ("complete", "error", "removed"):
                rpc_call("aria2.removeDownloadResult", [gid])
            else:
                rpc_call("aria2.remove", [gid])
        except Exception:
            pass
        deleted, skipped = 0, 0
        for p in paths:
            for target in (Path(p), Path(p + ".aria2")):
                try:
                    resolved = target.resolve()
                    if DOWNLOAD_DIR not in resolved.parents:
                        skipped += 1
                        continue
                    if resolved.is_file():
                        resolved.unlink()
                        deleted += 1
                except OSError:
                    skipped += 1
        try:
            rpc_call("aria2.saveSession", [], timeout=3)
        except Exception:
            pass
        log_event(f"remove-files gid={gid}: deleted={deleted} skipped={skipped}")
        return self._json(200, {"ok": True, "deleted": deleted, "skipped": skipped})

    def _proxy_rpc(self):
        body = self._read_json()
        method = str(body.get("method", ""))
        params = body.get("params") or []
        if method == "system.multicall":
            calls = []
            for item in params[0] if params else []:
                name = str(item.get("methodName", ""))
                short = name.split(".", 1)[-1]
                if short not in ALLOWED_METHODS:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": f"method not allowed: {name}"})
                calls.append({"methodName": name, "params": [f"token:{RPC_SECRET}"] + list(item.get("params") or [])})
            payload = {"jsonrpc": "2.0", "id": "tofetch", "method": "system.multicall", "params": [calls]}
            try:
                req = urllib.request.Request(RPC_URL, data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                try:
                    _harvest_history(json.loads(data.decode()))
                except Exception:
                    pass
                return self._send(200, data)
            except Exception as e:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"aria2 unreachable: {e}"})
        short = method.split(".", 1)[-1]
        if short not in ALLOWED_METHODS:
            return self._json(HTTPStatus.FORBIDDEN, {"error": f"method not allowed: {method}"})
        if short == "addUri":
            # Merge the persisted per-task defaults (retry policy) under
            # whatever options the caller supplied.
            params = list(params)
            uris = params[0] if len(params) > 0 else []
            opts = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            params = [uris, with_task_defaults(opts)]
        try:
            result = rpc_call(f"aria2.{short}", list(params))
            if isinstance(result, dict) and result.get("error"):
                return self._json(HTTPStatus.BAD_GATEWAY,
                                  {"error": str(result["error"].get("message", result["error"]))})
            if short in ("addUri", "addTorrent", "addMetalink"):
                # Persist the session immediately: the periodic save alone
                # leaves a window where a hard kill loses the new task.
                try:
                    rpc_call("aria2.saveSession", [], timeout=3)
                except Exception:
                    pass
            if short == "tellStopped":
                try:
                    _harvest_history(result.get("result"))
                except Exception:
                    pass
            return self._json(200, {"result": result.get("result")})
        except urllib.error.HTTPError as e:
            try:
                return self._send(e.code, e.read())
            except Exception:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": "aria2 error"})
        except Exception as e:
            return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"aria2 unreachable: {e}"})

    def _serve_file(self, path: Path, ctype: str):
        try:
            data = path.read_bytes()
        except OSError:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        self._send(200, data, ctype)

    def _serve_download(self, rel: str):
        target = (DOWNLOAD_DIR / rel).resolve()
        if target != DOWNLOAD_DIR and DOWNLOAD_DIR not in target.parents:
            return self._json(HTTPStatus.FORBIDDEN, {"error": "outside download dir"})
        if not target.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        size = target.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range") or ""
        status = 200
        if range_header.startswith("bytes="):
            try:
                first, _, last = range_header[6:].partition("-")
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:  # suffix range
                    start = max(0, size - int(last))
                end = min(end, size - 1)
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with target.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


class DualStackServer(ThreadingHTTPServer):
    """IPv6 socket that also accepts IPv4-mapped clients (for HOST='::')."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def _already_running() -> bool:
    """True if a ToFetch instance already answers on our port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=2) as r:
            return b'"engine"' in r.read()
    except Exception:
        return False


def main():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if AUTOSTART:
        start_aria2()
    apply_global_settings()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    server_cls = DualStackServer if ":" in HOST else ThreadingHTTPServer
    try:
        server = server_cls((HOST, PORT), Handler)
    except OSError:
        if _already_running():
            print(f"ToFetch 已经在运行了，直接打开 http://localhost:{PORT} 就行，不用重复启动。")
        else:
            print(f"端口 {PORT} 已被其他程序占用，ToFetch 无法启动。换个端口试试：TOFETCH_PORT=8078")
        return

    def shutdown(signum, frame):
        log_event("shutting down")
        stop_aria2()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    log_event(f"UI on http://{HOST}:{PORT}  downloads -> {DOWNLOAD_DIR}")
    if not PASSWORD:
        print("[tofetch] WARNING: TOFETCH_PASSWORD is empty - no login. Do not expose this port to the internet.")
    server.serve_forever()


if __name__ == "__main__":
    main()
