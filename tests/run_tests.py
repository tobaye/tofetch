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
  T17 changePosition reorders the waiting queue (POS_SET to front)
  T18 stats: a completed download is counted once (bytes + task count),
      re-harvesting the same task does not double-count
  T19 settings export/import round-trip; bad import is rejected
  T20 dir clamping: addUri dir outside the download root is dropped,
      a subdir is honoured, /api/torrent with an escaping dir is a 400
  T21 request-body cap: an oversized JSON body gets a 413

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


    def do_HEAD(self):
        p = self.root / self.path.lstrip("/")
        if not p.is_file():
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # aria2 opens many parallel ranged connections; the default backlog
    # of 5 can drop loopback connects late in a long suite run.
    request_queue_size = 128

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


        # ---- v1.3.0 batch (fresh instances, small files only) ----
        def raw_req(url, body=None, method=None):
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method=method or ("POST" if body is not None else "GET"))
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode(errors="replace")

        # T17 changePosition: third queued task jumps to the front
        work17 = Path(tempfile.mkdtemp(prefix="tofetch-test17-"))
        td17 = ToFetch(work17, aria2, free_port(), free_port())
        try:
            td17.wait_engine()
            td17.rpc("changeGlobalOption", [{"max-concurrent-downloads": "1",
                                             "max-overall-download-limit": "3M"}])
            td17.rpc("addUri", [[f"{base}/big.bin"], {"out": "hold.bin"}])
            g17a = td17.rpc("addUri", [[f"{base}/small.bin"], {"out": "q1.bin"}])
            g17b = td17.rpc("addUri", [[f"{base}/small.bin"], {"out": "q2.bin"}])
            time.sleep(1.5)
            td17.rpc("changePosition", [g17b, 0, "POS_SET"])
            time.sleep(0.5)
            waiting = td17.rpc("tellWaiting", [0, 10, ["gid"]])
            ok17 = bool(waiting) and waiting[0]["gid"] == g17b
            report("T17 changePosition moves a waiting task to the front", ok17,
                   f"(queue={[w['gid'][-4:] for w in waiting]})")
        finally:
            td17.stop()
            shutil.rmtree(work17, ignore_errors=True)

        # T18 stats: completion counted exactly once
        work18 = Path(tempfile.mkdtemp(prefix="tofetch-test18-"))
        td18 = ToFetch(work18, aria2, free_port(), free_port())
        try:
            td18.wait_engine()
            gid18 = td18.rpc("addUri", [[f"{base}/small.bin"], {}])
            td18.wait_complete(gid18, 30)
            stats = {}
            for _ in range(20):
                td18.rpc("tellStopped", [0, 100, ["gid", "status", "totalLength",
                                                  "completedLength", "files", "dir",
                                                  "errorMessage"]])
                _, body18 = raw_req(td18.ui + "/api/stats")
                stats = json.loads(body18)
                if stats.get("total_tasks", 0) >= 1:
                    break
                time.sleep(0.5)
            once = (stats.get("total_bytes") == 1024 * 1024
                    and stats.get("total_tasks") == 1
                    and stats.get("today") == 1024 * 1024)
            td18.rpc("tellStopped", [0, 100, ["gid", "status", "totalLength",
                                              "completedLength", "files", "dir",
                                              "errorMessage"]])
            time.sleep(0.5)
            _, body18b = raw_req(td18.ui + "/api/stats")
            stats2 = json.loads(body18b)
            report("T18 stats count a completion once, no double-count",
                   once and stats2.get("total_tasks") == 1
                   and stats2.get("total_bytes") == 1024 * 1024,
                   f"(stats={stats})")
        finally:
            td18.stop()
            shutil.rmtree(work18, ignore_errors=True)

        # T19 settings export/import round-trip + rejection
        work19 = Path(tempfile.mkdtemp(prefix="tofetch-test19-"))
        td19 = ToFetch(work19, aria2, free_port(), free_port())
        try:
            td19.wait_engine()
            td19.api("/api/settings", {"global": {"max-concurrent-downloads": "9"}})
            _, exp19 = raw_req(td19.ui + "/api/settings/export")
            exported = json.loads(exp19)
            code_i, _ = raw_req(td19.ui + "/api/settings/import",
                                json.dumps({"global": {"max-concurrent-downloads": "6"},
                                            "prefs": {"webhook_url": "https://example.invalid/h"}}).encode())
            _, got19 = raw_req(td19.ui + "/api/settings")
            gotj = json.loads(got19)
            code_bad, _ = raw_req(td19.ui + "/api/settings/import",
                                  json.dumps({"global": {"bogus-key": "1"}}).encode())
            ok19 = (exported.get("global", {}).get("max-concurrent-downloads") == "9"
                    and code_i == 200
                    and gotj.get("global", {}).get("max-concurrent-downloads") == "6"
                    and gotj.get("prefs", {}).get("webhook_url") == "https://example.invalid/h"
                    and code_bad == 400)
            report("T19 settings export/import round-trips, bad import 400", ok19,
                   f"(import={code_i}, bad={code_bad})")
        finally:
            td19.stop()
            shutil.rmtree(work19, ignore_errors=True)

        # T20 dir clamping on addUri and /api/torrent
        work20 = Path(tempfile.mkdtemp(prefix="tofetch-test20-"))
        td20 = ToFetch(work20, aria2, free_port(), free_port())
        try:
            td20.wait_engine()
            dl20 = str(work20 / "downloads")
            g_out = td20.rpc("addUri", [[f"{base}/small.bin"],
                                        {"out": "esc.bin", "dir": "/tmp/tf-outside"}])
            st_out = td20.rpc("tellStatus", [g_out, ["dir"]])
            try:
                td20.rpc("remove", [g_out, True])
            except Exception:
                try:
                    td20.rpc("removeDownloadResult", [g_out])
                except Exception:
                    pass
            g_sub = td20.rpc("addUri", [[f"{base}/small.bin"],
                                        {"out": "sub.bin", "dir": "sub1"}])
            st_sub = td20.rpc("tellStatus", [g_sub, ["dir"]])
            td20.wait_complete(g_sub, 30)
            import base64 as _b64
            code_t, body_t = raw_req(td20.ui + "/api/torrent", json.dumps(
                {"data": _b64.b64encode(b"x").decode(), "dir": "../escape"}).encode())
            ok20 = (st_out.get("dir") == dl20
                    and st_sub.get("dir") == dl20 + "/sub1"
                    and (work20 / "downloads" / "sub1" / "sub.bin").exists()
                    and code_t == 400 and "outside" in body_t)
            report("T20 dir clamped to download root on all entry points", ok20,
                   f"(outside->{st_out.get('dir')}, torrent={code_t})")
        finally:
            td20.stop()
            shutil.rmtree(work20, ignore_errors=True)

        # T21 body cap: >2MB JSON body is a 413, normal calls still work
        work21 = Path(tempfile.mkdtemp(prefix="tofetch-test21-"))
        td21 = ToFetch(work21, aria2, free_port(), free_port())
        try:
            td21.wait_engine()
            big_body = json.dumps({"global": {"bt-tracker": "x" * (3 * 1024 * 1024)}}).encode()
            code21, _ = raw_req(td21.ui + "/api/settings", big_body)
            code_ok, _ = raw_req(td21.ui + "/api/settings",
                                 json.dumps({"task": {"max-tries": "3"}}).encode())
            report("T21 oversized body gets 413, normal body still works",
                   code21 == 413 and code_ok == 200,
                   f"(big={code21}, normal={code_ok})")
        finally:
            td21.stop()
            shutil.rmtree(work21, ignore_errors=True)

        # T14 export/import: an unfinished task survives the round-trip
        work14 = Path(tempfile.mkdtemp(prefix="tofetch-test14-"))
        td14 = ToFetch(work14, aria2, free_port(), free_port())
        try:
            td14.wait_engine()
            td14.rpc("changeGlobalOption", [{"max-overall-download-limit": "200K"}])
            gid14 = td14.rpc("addUri", [[f"{base}/big.bin"], {"out": "exp.bin"}])
            time.sleep(2)
            with urllib.request.urlopen(td14.ui + "/api/export", timeout=10) as r:
                export = json.loads(r.read().decode())
            tasks14 = export.get("tasks") or []
            has14 = any(any("big.bin" in u for u in t.get("uris", []))
                        for t in tasks14)
            td14.rpc("remove", [gid14, True])
            res14 = td14.api("/api/import", {"tasks": tasks14})
            ok14 = has14 and res14.get("added", 0) >= 1 and bool(res14.get("gids"))
            if ok14:
                st14 = td14.status(res14["gids"][0])
                ok14 = st14["status"] in ("active", "waiting", "paused")
            report("T14 export/import round-trips an unfinished task", ok14,
                   f"(exported={len(tasks14)}, added={res14.get('added')})")
        finally:
            td14.stop()
            shutil.rmtree(work14, ignore_errors=True)

        # T15 probe: live file reports its size, 404 is flagged, magnet refused
        work15 = Path(tempfile.mkdtemp(prefix="tofetch-test15-"))
        td15 = ToFetch(work15, aria2, free_port(), free_port())
        try:
            td15.wait_engine()
            res15 = td15.api("/api/probe", {"urls": [
                f"{base}/small.bin", f"{base}/nope.bin", "magnet:?xt=urn:btih:abc"]},
                timeout=60)
            by_url = {r["url"]: r for r in res15.get("results", [])}
            live = by_url.get(f"{base}/small.bin", {})
            dead = by_url.get(f"{base}/nope.bin", {})
            mag = by_url.get("magnet:?xt=urn:btih:abc", {})
            ok15 = (live.get("ok") and live.get("size") == 1024 * 1024
                    and not dead.get("ok") and dead.get("status") == 404
                    and not mag.get("ok"))
            report("T15 probe: live size, 404 flagged, magnet unsupported", ok15,
                   f"(live={live.get('ok')}/{live.get('size')}/{live.get('error')}, "
                   f"dead={dead.get('status')}/{dead.get('error')}, mag={mag.get('error')})")
        finally:
            td15.stop()
            shutil.rmtree(work15, ignore_errors=True)

        # T16 multi-source: dead first URI + live second URI still completes
        work16 = Path(tempfile.mkdtemp(prefix="tofetch-test16-"))
        td16 = ToFetch(work16, aria2, free_port(), free_port())
        try:
            td16.wait_engine()
            gid16 = td16.rpc("addUri", [[f"{base}/missing.bin", f"{base}/small.bin"],
                                        {"out": "multi.bin"}])
            td16.wait_complete(gid16, 45)
            ok16 = sha256(work16 / "downloads" / "multi.bin") == small_hash
            report("T16 multi-source failover completes byte-exact", ok16)
        finally:
            td16.stop()
            shutil.rmtree(work16, ignore_errors=True)


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

    # T22: an unauthenticated POST's body must be drained before the
    # 401 goes out - leftover bytes would be parsed as the next request
    # on the same keep-alive connection and corrupt it (the browser
    # extension bug: a stale-session 401 poisoned the login retry).
    import http.client
    work22 = Path(tempfile.mkdtemp(prefix="tofetch-test22-"))
    td22 = ToFetch(work22, aria2, free_port(), free_port(), extra_env={
        "TOFETCH_PASSWORD": "test-pw-22",
    })
    try:
        port22 = int(td22.ui.rsplit(":", 1)[1])
        up = False
        for _ in range(50):
            try:
                urllib.request.urlopen(td22.ui + "/api/state", timeout=3)
            except urllib.error.HTTPError as e:
                up = e.code == 401
                if up:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        conn = http.client.HTTPConnection("127.0.0.1", port22, timeout=10)

        def one(method, path, body=None, cookie=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            conn.request(method, path, data, headers)
            r = conn.getresponse()
            return r.status, r.getheader("Set-Cookie"), r.read()

        c1, _, _ = one("POST", "/api/settings", {"global": {}})
        c2, set_cookie, _ = one("POST", "/api/login", {"password": "test-pw-22"})
        cookie22 = (set_cookie or "").split(";")[0]
        c3, _, raw3 = one("GET", "/api/state", None, cookie22)
        report("T22 unauthenticated body drained: keep-alive login after 401 works",
               up and c1 == 401 and c2 == 200 and c3 == 200 and b'"engine"' in raw3,
               f"(up={up}, c1={c1}, c2={c2}, c3={c3})")
        conn.close()
    finally:
        td22.stop()
        shutil.rmtree(work22, ignore_errors=True)

    # T23: Windows integration against the fake registry - verified
    # assoc/autostart toggles; /api/exit refuses on a WAN-bound
    # instance (the main td) and really exits a loopback-bound one.
    work23 = Path(tempfile.mkdtemp(prefix="tofetch-test23-"))
    dummy_exe = ROOT / "ToFetch.exe"
    dummy_exe.write_bytes(b"test stand-in for the control panel")
    td23 = ToFetch(work23, aria2, free_port(), free_port(), extra_env={
        "TOFETCH_PLATFORM": "windows",
        "TOFETCH_FAKE_REGISTRY": str(work23 / "reg.json"),
        "TOFETCH_HOST": "127.0.0.1",
    })
    try:
        td23.wait_engine()

        def win_state():
            with urllib.request.urlopen(td23.ui + "/api/win/state", timeout=10) as r:
                return json.loads(r.read().decode())

        st0 = win_state()
        r_on = td23.api("/api/win/assoc", {"on": True})
        r_auto = td23.api("/api/win/autostart", {"on": True})
        st1 = win_state()
        r_off = td23.api("/api/win/assoc", {"on": False})
        r_auto_off = td23.api("/api/win/autostart", {"on": False})
        toggles_ok = (st0["magnet"] is False and st0["autostart"] is False
                      and r_on.get("ok") and r_auto.get("ok")
                      and st1["magnet"] and st1["torrent"] and st1["autostart"]
                      and r_off.get("ok") and r_auto_off.get("ok"))
        try:
            td.api("/api/exit", {"mode": "stop"})
            gate_ok = False
        except Exception:
            gate_ok = True  # 403 on the 0.0.0.0-bound main instance
        td23.api("/api/exit", {"mode": "stop"})
        try:
            rc23 = td23.proc.wait(timeout=15)
        except Exception:
            rc23 = None
        report("T23 win toggles verified; exit gated to local, loopback exits",
               toggles_ok and gate_ok and rc23 == 0,
               f"(toggles={toggles_ok}, gate={gate_ok}, rc={rc23})")
    finally:
        td23.stop()
        dummy_exe.unlink(missing_ok=True)
        shutil.rmtree(work23, ignore_errors=True)

    # T24: the download-dir setting re-points the root live: state
    # follows, a new download lands in the new root, an unusable path
    # is refused, and clearing the setting reverts to the launch dir.
    work24 = Path(tempfile.mkdtemp(prefix="tofetch-test24-"))
    # T24 gets its OWN fixture server: by this point in the suite the
    # shared one has served hundreds of ranged connects and loopback
    # drops late in long runs (see QuietServer's note) kept erroring
    # this test's download leg — the dir assertions are what matters
    # here, so they get an isolated source.
    src24 = work24 / "src"
    src24.mkdir()
    (src24 / "small.bin").write_bytes(b"s" * 65536)

    class _H24(RangeHandler):
        pass
    _H24.root = src24
    httpd24 = QuietServer(("127.0.0.1", 0), _H24)
    threading.Thread(target=httpd24.serve_forever, daemon=True).start()
    base24 = f"http://127.0.0.1:{httpd24.server_address[1]}"
    td24 = ToFetch(work24, aria2, free_port(), free_port())
    try:
        td24.wait_engine()
        root_a = str((work24 / "downloads").resolve())
        root_b = str((work24 / "elsewhere").resolve())
        st0 = td24.api("/api/state")
        td24.api("/api/settings", {"global": {"dir": root_b}})
        st1 = td24.api("/api/state")
        gid24 = td24.rpc("addUri", [[f"{base24}/small.bin"], {"out": "t24.bin"}])
        landed = False
        task_dir = ""
        try:
            task_dir = td24.wait_complete(gid24, 30).get("dir", "")
            landed = (Path(root_b) / "t24.bin").exists()
        except Exception:
            # Late in a long suite run the loopback fixture can drop a
            # connect (see QuietServer's note) and error the task; the
            # dir assertions are what this test is about, so retry the
            # download leg once before judging.
            try:
                gid24 = td24.rpc("addUri", [[f"{base24}/small.bin"], {"out": "t24.bin"}])
                task_dir = td24.wait_complete(gid24, 30).get("dir", "")
                landed = (Path(root_b) / "t24.bin").exists()
            except Exception as e:
                task_dir = f"wait failed twice: {e}"
        bad_refused = False
        try:
            td24.api("/api/settings", {"global": {"dir": "/proc/tofetch-nope/x"}})
        except Exception:
            bad_refused = True
        td24.api("/api/settings", {"global": {"dir": ""}})
        st2 = td24.api("/api/state")
        report("T24 download-dir setting re-points root, bad path refused",
               st0.get("download_dir") == root_a and st1.get("download_dir") == root_b
               and landed and bad_refused and st2.get("download_dir") == root_a,
               f"(a={st0.get('download_dir')}, b={st1.get('download_dir')}, "
               f"landed={landed}, task_dir={task_dir}, bad={bad_refused}, "
               f"back={st2.get('download_dir')})")
    finally:
        td24.stop()
        httpd24.shutdown()
        shutil.rmtree(work24, ignore_errors=True)

    # T25: exit must also stop an ADOPTED engine. Backend A spawns the
    # engine; A's python is hard-killed, orphaning the engine; backend
    # B (same data dir + RPC port) adopts it ("aria2 already running,
    # using it"); /api/exit on B must leave no engine behind. (Field
    # bug 2026-11-19: exiting ToFetch left aria2 running and the
    # program folder undeletable.)
    work25 = Path(tempfile.mkdtemp(prefix="tofetch-test25-"))
    rpc25 = free_port()
    td25a = ToFetch(work25 / "a", aria2, free_port(), rpc25)
    engine_pid = None
    rc25 = None
    engine_gone = False
    try:
        td25a.wait_engine()
        pat25 = "conf-path=" + str(work25 / "a" / "data")
        out = subprocess.run(["pgrep", "-f", pat25],
                             capture_output=True, text=True).stdout.strip()
        engine_pid = out.splitlines()[0] if out else None
        td25a.proc.kill()  # hard kill: no cleanup runs, engine orphaned
        td25a.proc.wait(timeout=10)
        time.sleep(0.5)
        td25b = ToFetch(work25 / "b", aria2, free_port(), rpc25, extra_env={
            "TOFETCH_DATA_DIR": str(work25 / "a" / "data"),  # same RPC secret
            "TOFETCH_HOST": "127.0.0.1",
        })
        try:
            td25b.wait_engine()
            td25b.api("/api/exit", {"mode": "stop"})
            rc25 = td25b.proc.wait(timeout=20)
        finally:
            td25b.stop()
        end = time.time() + 15
        while time.time() < end:
            alive = subprocess.run(["pgrep", "-f", pat25],
                                   capture_output=True, text=True).stdout.strip()
            if not alive:
                engine_gone = True
                break
            time.sleep(0.5)
        report("T25 exit stops an adopted engine too",
               bool(engine_pid) and rc25 == 0 and engine_gone,
               f"(engine_pid={engine_pid}, rc={rc25}, engine_gone={engine_gone})")
    finally:
        td25a.stop()
        shutil.rmtree(work25, ignore_errors=True)

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
