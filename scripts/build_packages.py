#!/usr/bin/env python3
"""Assemble the three local packages (Windows / Linux / Termux-Android).

Everything heavy is downloaded fresh from its official source and
verified against a pinned SHA-256 before it goes into a zip:

  * Aria2 Next v3.1.0 engines — github.com/AnInsomniacy/aria2-next
    release assets (checksums as recorded in the project docs)
  * Python 3.12.10 embeddable (Windows) — python.org official zip;
    SHA-256 computed from the artifact fetched from that URL on
    2026-11-18 (python.org publishes an MD5 for it: fe8ef205f2e9c3ba44d0cf9954e1abd3)

Layout sources: server.py / static / aria2.conf come from the repo root
(single source of truth); launchers and readmes live in
scripts/pkgfiles/. Output zips land in dist/ with the top-level folder
names ToFetch/ , ToFetch-Linux/ , ToFetch-Android/.

Usage: python3 scripts/build_packages.py [--no-download] (reuse cache/)
"""
import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
BUILD = ROOT / "dist_build"
DIST = ROOT / "dist"
PKGF = ROOT / "scripts" / "pkgfiles"

ARIA2_BASE = "https://github.com/AnInsomniacy/aria2-next/releases/download/v3.1.0"
ASSETS = {
    "aria2-win.exe": (f"{ARIA2_BASE}/aria2-next-3.1.0-windows-x86_64.exe",
                      "12bbd48dfd2c86f55419bf92b196dd3aedd6983116b5ce10c85d27a5a76c2baa"),
    "aria2-linux-x86_64": (f"{ARIA2_BASE}/aria2-next-3.1.0-linux-x86_64",
                           "5a264fc39312cc1abc6d12cfd2d96464d27f0349fd7cb5e0cbf1ee5bd686e890"),
    "aria2-linux-aarch64": (f"{ARIA2_BASE}/aria2-next-3.1.0-linux-aarch64",
                            "38019a34ac5edd4dc7443fd7214842dc2c29b38202a1a65a6271c5e994fbd0d0"),
    "aria2-android-arm64": (f"{ARIA2_BASE}/aria2-next-3.1.0-android-arm64",
                            "7e4ce96d2e29bf4f1dff0f0d0bc737488883cc6791c1795208f7f634994c80e3"),
    "python-embed.zip": ("https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
                         "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"),
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def fetch(name: str, reuse: bool) -> Path:
    url, want = ASSETS[name]
    out = CACHE / name
    if reuse and out.exists() and sha256(out) == want:
        print(f"cache hit {name}")
        return out
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ToFetch build"})
    with urllib.request.urlopen(req, timeout=300) as r, out.open("wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    got = sha256(out)
    if got != want:
        out.unlink(missing_ok=True)
        raise SystemExit(f"CHECKSUM MISMATCH for {name}: got {got}, want {want}")
    print(f"verified {name} sha256={got[:16]}…")
    return out


def copy_core(dest: Path) -> None:
    shutil.copy2(ROOT / "server.py", dest / "server.py")
    shutil.copy2(ROOT / "aria2.conf", dest / "aria2.conf")
    shutil.copytree(ROOT / "static", dest / "static")


def zip_dir(src_dir: Path, top: str, out_zip: Path, exec_names: set) -> None:
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_dir():
                continue
            arc = f"{top}/" + p.relative_to(src_dir).as_posix()
            info = zipfile.ZipInfo(arc)
            info.external_attr = (0o755 if p.name in exec_names else 0o644) << 16
            info.create_system = 3
            zf.writestr(info, p.read_bytes())
    print(f"built {out_zip} ({out_zip.stat().st_size} bytes)")


def main() -> int:
    reuse = "--no-download" in sys.argv
    blobs = {name: fetch(name, reuse) for name in ASSETS}
    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(parents=True, exist_ok=True)

    # ---- Windows: ToFetch/
    win = BUILD / "ToFetch"
    win.mkdir(parents=True)
    copy_core(win)
    with zipfile.ZipFile(blobs["python-embed.zip"]) as zf:
        zf.extractall(win / "python")
    shutil.copy2(blobs["aria2-win.exe"], win / "aria2c.exe")
    for f in (PKGF / "windows").iterdir():
        shutil.copy2(f, win / f.name)
    zip_dir(win, "ToFetch", DIST / "ToFetch-Windows.zip", exec_names=set())

    # ---- Linux: ToFetch-Linux/
    lin = BUILD / "ToFetch-Linux"
    lin.mkdir(parents=True)
    copy_core(lin)
    shutil.copy2(blobs["aria2-linux-x86_64"], lin / "aria2c-x86_64")
    shutil.copy2(blobs["aria2-linux-aarch64"], lin / "aria2c-aarch64")
    for f in (PKGF / "linux").iterdir():
        shutil.copy2(f, lin / f.name)
    zip_dir(lin, "ToFetch-Linux", DIST / "ToFetch-Linux.zip",
            exec_names={"tofetch.sh", "tofetch-open.sh", "aria2c-x86_64", "aria2c-aarch64"})

    # ---- Termux/Android: ToFetch-Android/
    term = BUILD / "ToFetch-Android"
    term.mkdir(parents=True)
    copy_core(term)
    shutil.copy2(blobs["aria2-android-arm64"], term / "aria2c")
    for f in (PKGF / "termux").iterdir():
        shutil.copy2(f, term / f.name)
    zip_dir(term, "ToFetch-Android", DIST / "ToFetch-Android.zip",
            exec_names={"install.sh", "aria2c"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
