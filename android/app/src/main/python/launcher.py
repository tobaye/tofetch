"""ToFetch Android launcher.

Runs inside the Chaquopy runtime: prepares TOFETCH_* environment variables
and runs the shared server.py (copied next to this file at build time).

NOTE: do NOT use runpy.run_path() here. Chaquopy serves the app's Python
files through its AssetFinder importer, and runpy cannot resolve a
'__main__' module from such a path (ImportError: can't find '__main__'
module in '.../AssetFinder/app/server.py' - found on-device 2026-11-18).
Import the module normally and call its main() instead.

Android services never receive SIGINT/SIGTERM the way a shell process
does, and signal.signal() only works on the main thread, so signal
registration is shimmed out here; service shutdown kills the process.
"""
import os
import signal
import sys
from pathlib import Path


def start(files_dir, data_dir, download_dir, aria2_bin, startup_log=None):
    os.environ["TOFETCH_HOST"] = "127.0.0.1"
    os.environ["TOFETCH_PORT"] = "8077"
    os.environ["TOFETCH_DATA_DIR"] = data_dir
    os.environ["TOFETCH_DOWNLOAD_DIR"] = download_dir
    os.environ["ARIA2_BIN"] = aria2_bin
    if startup_log:
        # Lets /api/log include the shell's startup log in its export.
        os.environ["TOFETCH_STARTUP_LOG"] = startup_log
    sys.argv = ["server"]
    signal.signal = lambda *a, **k: None  # noqa: E731 - see module docstring

    # Android gives the engine no readable system CA store, so ship the
    # Mozilla bundle (cacert.pem next to server.py) and hand its real path
    # to server.py, which passes it to aria2 as --ca-certificate.
    ca_data = None
    try:
        import pkgutil

        ca_data = pkgutil.get_data("server", "cacert.pem")
    except Exception:
        ca_data = None
    if not ca_data:
        try:
            ca_data = (Path(__file__).parent / "cacert.pem").read_bytes()
        except OSError:
            ca_data = None
    if ca_data:
        ca_path = Path(data_dir) / "cacert.pem"
        ca_path.write_bytes(ca_data)
        os.environ["ARIA2_CA_CERTIFICATE"] = str(ca_path)

    # aria2's built-in async DNS resolver finds no nameserver config on
    # Android and fails lookups with "Could not resolve host"; switch the
    # engine to the system resolver (same env-gated pattern as the CA fix).
    os.environ.setdefault("ARIA2_ASYNC_DNS", "false")

    notes = []
    try:
        import server

        notes.append(f"server={server.__file__}")
        index = server.STATIC / "index.html"
        notes.append(f"static_index_exists={index.exists()}")
        if not index.exists():
            # Sources are asset-packed, not real files: pull the UI out
            # through the module loader and point STATIC at the real copy.
            import pkgutil

            data = pkgutil.get_data("server", "static/index.html")
            if not data:
                raise RuntimeError("pkgutil.get_data returned nothing for static/index.html")
            target = Path(data_dir) / "static"
            target.mkdir(parents=True, exist_ok=True)
            (target / "index.html").write_bytes(data)
            server.STATIC = target
            notes.append("static extracted via pkgutil.get_data")
        server.main()
    except BaseException as exc:
        raise RuntimeError(" | ".join(notes) + f" || {type(exc).__name__}: {exc}") from exc
