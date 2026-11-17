"""ToFetch Android launcher.

Runs inside the Chaquopy runtime: prepares TOFETCH_* environment variables
and executes the shared server.py (copied next to this file at build time)
as __main__. Android services never receive SIGINT/SIGTERM the way a shell
process does, and signal.signal() only works on the main thread, so signal
registration is shimmed out here; service shutdown kills the process.
"""
import os
import runpy
import signal
import sys


def start(files_dir, data_dir, download_dir, aria2_bin):
    os.environ["TOFETCH_HOST"] = "127.0.0.1"
    os.environ["TOFETCH_PORT"] = "8077"
    os.environ["TOFETCH_DATA_DIR"] = data_dir
    os.environ["TOFETCH_DOWNLOAD_DIR"] = download_dir
    os.environ["ARIA2_BIN"] = aria2_bin
    here = os.path.dirname(os.path.abspath(__file__))
    server_py = os.path.join(here, "server.py")
    sys.argv = [server_py]
    signal.signal = lambda *a, **k: None  # noqa: E731 - see module docstring
    runpy.run_path(server_py, run_name="__main__")
