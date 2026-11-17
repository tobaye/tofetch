#!/usr/bin/env python3
"""ToFetch Windows smoke test — run with the PACKAGED python.exe on Windows.

Covers what can be verified without a Unix kill model:
  W1 the packaged server starts, engine answers
  W2 a download completes byte-exact and can be fetched back
  W3 a broken engine path is surfaced with a reason

Crash-recovery cases stay in the Linux suite (tests/run_tests.py): Windows
has no SIGKILL/SIGTERM choreography equivalent for this harness.

Env: TOFETCH_PKG_DIR = folder with python/python.exe, aria2c.exe,
     server.py, aria2.conf, static/  (assembled exactly like the shipped zip)

CI note: the final cleanup taskkills aria2c.exe by image name — fine on an
ephemeral CI runner, do not run this on a machine whose aria2 you care about.
"""
import hashlib
import json
import os
import random
import shutil
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_tests as rt  # RangeHandler / QuietServer / free_port (platform-neutral)

PKG = Path(os.environ["TOFETCH_PKG_DIR"]).resolve()
RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


def start_server(work: Path, aria2: str, ui: int, rpc: int):
    env = dict(os.environ)
    env.update({"ARIA2_BIN": aria2, "TOFETCH_HOST": "127.0.0.1",
                "TOFETCH_PORT": str(ui), "ARIA2_RPC_PORT": str(rpc),
                "TOFETCH_DOWNLOAD_DIR": str(work / "dl"),
                "TOFETCH_DATA_DIR": str(work / "data")})
    log = (work / "server-out.log").open("wb")
    return subprocess.Popen([str(PKG / "python" / "python.exe"), str(PKG / "server.py")],
                            env=env, stdout=log, stderr=subprocess.STDOUT)


def get_state(ui: int):
    with urllib.request.urlopen(f"http://127.0.0.1:{ui}/api/state", timeout=5) as r:
        return json.loads(r.read())


def wait_engine(ui: int, t=30):
    end = time.time() + t
    while time.time() < end:
        try:
            if get_state(ui).get("engine"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def rpc(ui: int, method, params):
    req = urllib.request.Request(f"http://127.0.0.1:{ui}/api/rpc",
                                 data=json.dumps({"method": method, "params": params}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["result"]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="tofetch-winsmoke-"))
    src = work / "src"; src.mkdir(parents=True)
    blob = random.Random(3).randbytes(2 * 1024 * 1024)
    (src / "w.bin").write_bytes(blob)
    want = hashlib.sha256(blob).hexdigest()
    rt.RangeHandler.root = src
    httpd = rt.QuietServer(("127.0.0.1", 0), rt.RangeHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    ui, rpcp = rt.free_port(), rt.free_port()
    proc = start_server(work, str(PKG / "aria2c.exe"), ui, rpcp)
    try:
        report("W1 packaged server + engine up", wait_engine(ui))
        gid = rpc(ui, "addUri", [[f"{base}/w.bin"], {}])
        end = time.time() + 40
        status = ""
        while time.time() < end:
            status = rpc(ui, "tellStatus", [gid, ["status"]])["status"]
            if status in ("complete", "error"):
                break
            time.sleep(0.5)
        got = work / "dl" / "w.bin"
        ok = status == "complete" and got.exists() and \
            hashlib.sha256(got.read_bytes()).hexdigest() == want
        if ok:
            with urllib.request.urlopen(f"http://127.0.0.1:{ui}/files/w.bin", timeout=10) as r:
                ok = hashlib.sha256(r.read()).hexdigest() == want
        report("W2 download byte-exact + fetch-back", ok)
    finally:
        proc.terminate()
    # W3 broken engine surfaces a reason
    work3 = Path(tempfile.mkdtemp(prefix="tofetch-winsmoke3-"))
    ui3, rpc3 = rt.free_port(), rt.free_port()
    proc3 = start_server(work3, str(PKG / "no-such-aria2c.exe"), ui3, rpc3)
    try:
        st = {}
        end = time.time() + 25
        while time.time() < end:
            try:
                st = get_state(ui3)
                break
            except Exception:
                time.sleep(0.5)
        ok3 = st.get("engine") is False and bool(st.get("engine_error"))
        report("W3 engine failure surfaced with reason", ok3,
               f"({(st.get('engine_error') or '')[:40]}...)")
        if not ok3:
            print("W3 diagnostics: proc3 poll =", proc3.poll(), "| state =", st)
            for cand in (work3 / "server-out.log", work3 / "data" / "tofetch.log"):
                try:
                    print(f"--- {cand.name} tail ---")
                    print(cand.read_bytes()[-1200:].decode(errors="replace"))
                except OSError as e:
                    print(f"--- {cand.name}: unreadable ({e})")
    finally:
        proc3.terminate()
        httpd.shutdown()
    subprocess.run(["taskkill", "/F", "/IM", "aria2c.exe"], capture_output=True)
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(work3, ignore_errors=True)
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
