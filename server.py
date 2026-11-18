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
import time
import urllib.request
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
        "--daemon=false",
    ]
    # Optional CA bundle override (Android: the engine has no system CA
    # store to read, so the app ships one and points aria2 at it).
    ca_bundle = os.environ.get("ARIA2_CA_CERTIFICATE")
    if ca_bundle and Path(ca_bundle).exists():
        args.append(f"--ca-certificate={ca_bundle}")
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


# ------------------------------------------------------------------- the app

ALLOWED_METHODS = {
    "addUri", "addTorrent", "addMetalink",
    "tellActive", "tellWaiting", "tellStopped", "tellStatus",
    "pause", "unpause", "pauseAll", "unpauseAll",
    "remove", "removeDownloadResult", "purgeDownloadResult",
    "getGlobalStat", "getGlobalOption", "changeGlobalOption",
    "getVersion", "getSessionInfo", "saveSession", "getFiles", "getUris",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ToFetch/1.0.1"
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
        if path == "/api/log":
            if not self._authed():
                return self._need_auth()
            parts = []
            for name in ("tofetch.log", "aria2.log"):
                f = DATA_DIR / name
                if f.exists():
                    data = f.read_bytes()[-60000:].decode(errors="replace")
                    parts.append(f"===== {name} =====\n{data}")
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
                    return self._send(200, resp.read())
            except Exception as e:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"aria2 unreachable: {e}"})
        short = method.split(".", 1)[-1]
        if short not in ALLOWED_METHODS:
            return self._json(HTTPStatus.FORBIDDEN, {"error": f"method not allowed: {method}"})
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
