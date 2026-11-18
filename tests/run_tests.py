#!/usr/bin/env python3
"""ToFetch regression suite — self-contained, stdlib only, no external network.

Builds a throwaway workspace with generated source files served by a local
Range-capable HTTP server, starts ToFetch against it, and exercises the
flows that must never silently break:

  T1 basic download completes byte-exact + /files/ fetch-back
  T2 pause actually freezes progress, resume finishes
  T3 crash (kill -9) after an explicit session save: task returns, file intact
  T4 engine failure is surfaced with a reason (no silent engine:false)
  T5 second instance on the same port exits politely (no traceback)
  T6 diagnostics log endpoint returns content
  T7 crash right after add: task survives (add persists the session
     immediately; this gated the suite from the start as a
     known-gap soft check and was promoted once the fix landed).
  T8 login throttle: 5 wrong passwords lock the client out; even the
     right password gets 429 until the lock expires, then login works.

Usage: python3 tests/run_tests.py
Env:   TOFETCH_TEST_ARIA2=/path/to/aria2c   (default: tools/aria2c, else PATH)
Exit:  0 when all hard tests pass.
"""
import hashlib
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys

# Test output may include Chinese engine-error text; never die on encoding.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = []


def report(name, ok, detail="", hard=True):
    RESULTS.append((name, ok, detail, hard))
    tag = "PASS" if ok else ("FAIL" if hard else "SOFT-FAIL (known gap)")
    print(f"[{tag}] {name} {detail}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------ source server
class RangeHandler(BaseHTTPRequestHandler):
    root = Path(".")

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.root / self.path.lstrip("/")
        if not p.is_file():
            self.send_error(404)
            return
        size = p.stat().st_size
        rng = self.headers.get("Range")
        lo, hi, code = 0, size - 1, 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            lo = int(a or 0)
            hi = int(b) if b else size - 1
            hi = min(hi, size - 1)
            code = 206
        try:
            self.send_response(code)
            if code == 206:
                self.send_header("Content-Range", f"bytes {lo}-{hi}/{size}")
            self.send_header("Content-Length", str(hi - lo + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with p.open("rb") as f:
                f.seek(lo)
                left = hi - lo + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # aria2 drops range connections routinely; not an error here


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


# ------------------------------------------------------------ tofetch client
class ToFetch:
    def __init__(self, work: Path, aria2: str, ui_port: int, rpc_port: int, extra_env=None):
        self.work = work
        self.ui = f"http://127.0.0.1:{ui_port}"
        env = dict(os.environ)
        env.update({
            "ARIA2_BIN": aria2,
            "TOFETCH_PORT": str(ui_port),
            "ARIA2_RPC_PORT": str(rpc_port),
            "TOFETCH_DOWNLOAD_DIR": str(work / "downloads"),
            "TOFETCH_DATA_DIR": str(work / "data"),
        })
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def api(self, path, body=None, timeout=10):
        req = urllib.request.Request(
            self.ui + path,
            data=json.dumps(body or {}).encode() if body is not None or path != "/api/state" else None,
            headers={"Content-Type": "application/json"},
            method="POST" if path != "/api/state" else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def rpc(self, method, params=None):
        j = self.api("/api/rpc", {"method": method, "params": params or []})
        if "error" in j and isinstance(j["error"], dict):
            raise RuntimeError(j["error"].get("message", "rpc error"))
        return j.get("result")

    def wait_engine(self, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            try:
                st = self.api("/api/state")
                if st.get("engine"):
                    return st
            except Exception:
                pass
            time.sleep(0.4)
        raise RuntimeError("engine did not come up")

    def state(self):
        return self.api("/api/state")

    def status(self, gid):
        return self.rpc("tellStatus", [gid, ["status", "completedLength", "totalLength"]])

    def wait_complete(self, gid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            st = self.status(gid)
            if st["status"] == "complete":
                return st
            if st["status"] == "error":
                raise RuntimeError(f"task errored: {st}")
            time.sleep(0.5)
        raise RuntimeError(f"task did not complete in {timeout}s: {self.status(gid)}")

    def _sweep_engine(self):
        # kill this instance's engine by its unique conf path (orphan-safe)
        subprocess.run(["pkill", "-9", "-f", f"conf-path={self.work}/data/aria2.conf"],
                       capture_output=True)

    def kill9(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)
        self._sweep_engine()
        time.sleep(1)

    _rpc_port = 0

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._sweep_engine()


def main() -> int:
    aria2 = os.environ.get("TOFETCH_TEST_ARIA2") or (
        str(ROOT / "tools" / "aria2c") if (ROOT / "tools" / "aria2c").exists()
        else shutil.which("aria2c") or ""
    )
    if not aria2 or not Path(aria2).exists():
        print("aria2 binary not found; set TOFETCH_TEST_ARIA2", file=sys.stderr)
        return 2
    print(f"engine: {aria2}")
    print(f"python: {sys.version.split()[0]}")

    work = Path(tempfile.mkdtemp(prefix="tofetch-test-"))
    src = work / "src"
    src.mkdir(parents=True)
    rng = random.Random(42)
    small = src / "small.bin"
    small.write_bytes(rng.randbytes(1 * 1024 * 1024))
    big = src / "big.bin"
    big.write_bytes(rng.randbytes(48 * 1024 * 1024))
    small_hash, big_hash = sha256(small), sha256(big)

    RangeHandler.root = src
    httpd = QuietServer(("127.0.0.1", 0), RangeHandler)
    src_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{src_port}"

    ui_port, rpc_port = free_port(), free_port()
    td = ToFetch(work, aria2, ui_port, rpc_port)
    td._rpc_port = rpc_port
    try:
        td.wait_engine()
        report("T0 engine starts and state answers", True)

        # T1 basic download + fetch-back
        gid = td.rpc("addUri", [[f"{base}/small.bin"], {}])
        td.wait_complete(gid, 30)
        got = work / "downloads" / "small.bin"
        ok = got.exists() and sha256(got) == small_hash
        with urllib.request.urlopen(f"{td.ui}/files/small.bin", timeout=10) as r:
            fetched = r.read()
        ok = ok and hashlib.sha256(fetched).hexdigest() == small_hash
        report("T1 download completes byte-exact + fetch-back", ok)

        # T2 pause freezes, resume finishes (throttled so it stays in flight)
        td.rpc("changeGlobalOption", [{"max-overall-download-limit": "3M"}])
        gid2 = td.rpc("addUri", [[f"{base}/big.bin"], {"out": "big2.bin"}])
        time.sleep(3)
        td.rpc("pause", [gid2])
        time.sleep(1.5)
        c1 = int(td.status(gid2)["completedLength"])
        time.sleep(2)
        c2 = int(td.status(gid2)["completedLength"])
        frozen = c1 == c2 and 0 < c1 < 48 * 1024 * 1024
        td.rpc("changeGlobalOption", [{"max-overall-download-limit": "0"}])
        td.rpc("unpause", [gid2])
        td.wait_complete(gid2, 60)
        ok = frozen and sha256(work / "downloads" / "big2.bin") == big_hash
        report("T2 pause freezes progress, resume completes intact", ok,
               f"(frozen at {c1} B)")

        # T3 crash after explicit save: task returns and finishes intact
        detail3 = ""
        try:
            td.rpc("changeGlobalOption", [{"max-overall-download-limit": "3M"}])
            gid3 = td.rpc("addUri", [[f"{base}/big.bin"], {"out": "big3.bin"}])
            time.sleep(3)
            before = int(td.status(gid3)["completedLength"])
            td.rpc("saveSession")
            td.kill9()
            td = ToFetch(work, aria2, ui_port, rpc_port)
            td._rpc_port = rpc_port
            td.wait_engine()
            st3 = td.status(gid3)
            back = st3["status"] in ("active", "paused", "waiting", "complete")
            intact = False
            if back:
                td.rpc("changeGlobalOption", [{"max-overall-download-limit": "0"}])
                try:
                    td.rpc("unpause", [gid3])
                except Exception:
                    pass
                td.wait_complete(gid3, 60)
                intact = sha256(work / "downloads" / "big3.bin") == big_hash
            detail3 = f"(pre-crash {before} B, back={back}, intact={intact})"
            ok3 = back and intact
        except Exception as e:
            ok3, detail3 = False, f"(error: {e})"
        report("T3 crash after save: task returns, completes intact", ok3, detail3)

        # S1 soft: crash immediately after add (known 30 s session-save gap)
        td.rpc("changeGlobalOption", [{"max-overall-download-limit": "3M"}])
        gid4 = td.rpc("addUri", [[f"{base}/big.bin"], {"out": "big4.bin"}])
        td.kill9()
        td = ToFetch(work, aria2, ui_port, rpc_port)
        td._rpc_port = rpc_port
        td.wait_engine()
        try:
            td.status(gid4)
            survived = True
        except Exception:
            survived = False
        report("T7 crash right after add: task survives", survived)
        try:
            td.rpc("remove", [gid4], True)
        except Exception:
            pass
        td.rpc("changeGlobalOption", [{"max-overall-download-limit": "0"}])

        # T6 diagnostics endpoint
        with urllib.request.urlopen(f"{td.ui}/api/log", timeout=10) as r:
            log_body = r.read().decode(errors="replace")
        report("T6 diagnostics log endpoint returns content",
               "aria2" in log_body and len(log_body) > 20)

        # T9 settings: round-trip, engine apply, validation
        def settings_call(method, body=None):
            req = urllib.request.Request(
                td.ui + "/api/settings",
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"}, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, {}
        code1, _ = settings_call("POST", {"global": {"max-concurrent-downloads": "7"},
                                          "task": {"max-tries": "4", "retry-wait": "30"}})
        _, got_settings = settings_call("GET")
        engine_opt = td.rpc("getGlobalOption", [])
        code_bad_key, _ = settings_call("POST", {"global": {"bogus-key": "1"}})
        code_bad_val, _ = settings_call("POST", {"task": {"max-tries": "9999"}})
        report("T9 settings persist, apply to engine, reject bad input",
               code1 == 200
               and got_settings.get("global", {}).get("max-concurrent-downloads") == "7"
               and got_settings.get("task", {}).get("max-tries") == "4"
               and str(engine_opt.get("max-concurrent-downloads")) == "7"
               and code_bad_key == 400 and code_bad_val == 400,
               f"(post={code1}, bad_key={code_bad_key}, bad_val={code_bad_val})")
        settings_call("POST", {"global": {"max-concurrent-downloads": "5"}})

        # T10 remove-files: task and its file both go away
        gid10 = td.rpc("addUri", [[f"{base}/small.bin"], {"out": "t10.bin"}])
        td.wait_complete(gid10, 30)
        f10 = work / "downloads" / "t10.bin"
        existed = f10.exists()
        rm = td.api("/api/remove-files", {"gid": gid10})
        report("T10 remove-files deletes the downloaded file",
               existed and rm.get("deleted", 0) >= 1 and not f10.exists(),
               f"(deleted={rm.get('deleted')})")

        # T11 history: finished tasks are harvested from tellStopped polls
        td.rpc("tellStopped", [0, 100, ["gid", "status", "totalLength",
                                        "completedLength", "files", "dir",
                                        "errorMessage"]])
        def history_call(path, body=None):
            req = urllib.request.Request(
                td.ui + path,
                data=json.dumps(body or {}).encode() if body is not None else None,
                headers={"Content-Type": "application/json"},
                method="POST" if body is not None else "GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        entries = history_call("/api/history").get("entries", [])
        hist_ok = any(e.get("name") == "t10.bin" and e.get("status") == "complete"
                      for e in entries)
        history_call("/api/history-clear", {})
        entries2 = history_call("/api/history").get("entries", [])
        report("T11 history records finished tasks and clears",
               hist_ok and entries2 == [],
               f"(entries={len(entries)})")
    finally:
        td.stop()
        httpd.shutdown()

    # T4 engine failure surfaces a reason (separate instance, bogus engine)
    work4 = Path(tempfile.mkdtemp(prefix="tofetch-test4-"))
    td4 = ToFetch(work4, "/nonexistent/aria2c", free_port(), free_port())
    try:
        time.sleep(2.5)
        st = td4.state()
        report("T4 engine failure surfaced with reason",
               st.get("engine") is False and bool(st.get("engine_error")),
               f"({(st.get('engine_error') or '')[:40]}...)")
    finally:
        td4.stop()
        shutil.rmtree(work4, ignore_errors=True)

    # T5 second instance on the same port exits politely
    work5 = Path(tempfile.mkdtemp(prefix="tofetch-test5-"))
    p1 = ToFetch(work5, aria2, ui_port, free_port())
    try:
        p1.wait_engine()
        env = dict(os.environ)
        env.update({"ARIA2_BIN": aria2, "TOFETCH_PORT": str(ui_port),
                    "ARIA2_RPC_PORT": str(free_port()),
                    "TOFETCH_DOWNLOAD_DIR": str(work5 / "downloads"),
                    "TOFETCH_DATA_DIR": str(work5 / "data")})
        out = subprocess.run([sys.executable, str(ROOT / "server.py")], env=env,
                             capture_output=True, text=True, timeout=30)
        report("T5 double start exits politely",
               out.returncode == 0 and "已经在运行" in out.stdout
               and "Traceback" not in (out.stdout + out.stderr))
    finally:
        p1.stop()
        shutil.rmtree(work5, ignore_errors=True)

    # T8 login throttle: repeated wrong passwords lock the client out;
    # even the right password is refused (429) until the lock expires.
    work8 = Path(tempfile.mkdtemp(prefix="tofetch-test8-"))
    td8 = ToFetch(work8, aria2, free_port(), free_port(), extra_env={
        "TOFETCH_PASSWORD": "test-pw-8",
        "TOFETCH_LOGIN_LOCK_SECONDS": "4",
    })
    try:
        def login_code(pw):
            req = urllib.request.Request(
                td8.ui + "/api/login",
                data=json.dumps({"password": pw}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        up = False
        for _ in range(50):
            try:
                urllib.request.urlopen(td8.ui + "/api/state", timeout=3)
            except urllib.error.HTTPError as e:
                up = e.code == 401
                if up:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        wrong = [login_code("nope") for _ in range(5)]
        locked = login_code("test-pw-8")
        time.sleep(4.5)
        after = login_code("test-pw-8")
        report("T8 login throttle: 5 failures lock, 429 while locked, recovers",
               up and all(c == 401 for c in wrong) and locked == 429 and after == 200,
               f"(wrong={wrong}, locked={locked}, after={after})")
    finally:
        td8.stop()
        shutil.rmtree(work8, ignore_errors=True)

    # T12 scheduler: a speed window covering "now" applies its limit to
    # the engine; clearing the schedule restores the default (0).
    work12 = Path(tempfile.mkdtemp(prefix="tofetch-test12-"))
    td12 = ToFetch(work12, aria2, free_port(), free_port(),
                   extra_env={"TOFETCH_SCHED_TICK": "1"})
    try:
        td12.wait_engine()
        now = time.localtime()
        frm = time.strftime("%H:%M", time.localtime(time.time() - 3600))
        to = time.strftime("%H:%M", time.localtime(time.time() + 3600))
        td12.api("/api/settings", {"schedule": {
            "speed_windows": [{"from": frm, "to": to, "down": 321, "up": 0}]}})
        applied = False
        for _ in range(30):
            opt = td12.rpc("getGlobalOption", [])
            if str(opt.get("max-overall-download-limit")) == str(321 * 1024):
                applied = True
                break
            time.sleep(0.5)
        td12.api("/api/settings", {"schedule": {}})
        restored = False
        for _ in range(30):
            opt = td12.rpc("getGlobalOption", [])
            if str(opt.get("max-overall-download-limit")) == "0":
                restored = True
                break
            time.sleep(0.5)
        try:
            td12.api("/api/settings", {"schedule": {"pause_at": "99:99"}})
            rejected = False
        except Exception:
            rejected = True
        report("T12 speed window applies and restores; bad schedule rejected",
               applied and restored and rejected,
               f"(applied={applied}, restored={restored}, rejected={rejected})")
    finally:
        td12.stop()
        shutil.rmtree(work12, ignore_errors=True)

    # final sweep: no test engine may outlive the suite
    subprocess.run(["pkill", "-9", "-f", "conf-path=" + str(Path(tempfile.gettempdir())) + "/tofetch-test"],
                   capture_output=True)
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "conf-path=" + str(Path(tempfile.gettempdir())) + "/tofetch-test"],
                          capture_output=True, text=True).stdout.strip()
    print(f"cleanup: {'no stray test engines' if not left else 'STRAYS LEFT: ' + left}")

    hard_failed = [r for r in RESULTS if not r[1] and r[3]]
    soft_failed = [r for r in RESULTS if not r[1] and not r[3]]
    print(f"\n{len(RESULTS) - len(hard_failed) - len(soft_failed)}/{len(RESULTS)} passed"
          f"; hard failures: {len(hard_failed)}, soft (known gaps): {len(soft_failed)}")
    shutil.rmtree(work, ignore_errors=True)
    return 1 if hard_failed else 0


if __name__ == "__main__":
    sys.exit(main())
