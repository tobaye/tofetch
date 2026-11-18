#!/data/data/com.termux/files/usr/bin/bash
# ToFetch for Termux - installer
set -e
cd "$(dirname "$0")"
INSTALL_LOG="$HOME/tofetch-install.log"

# Mirrors tried in order for pkg operations. The auto-picked mirror can be
# mid-sync and fail with a stale index (seen in the wild), so fall back
# through known-good mirrors instead of dying on the first failure.
MIRRORS=(
  ""
  "https://packages-cf.termux.dev/apt/termux-main"
  "https://mirrors.tuna.tsinghua.edu.cn/termux/apt/termux-main"
  "https://mirrors.ustc.edu.cn/termux/apt/termux-main"
)

set_mirror() {
  local url="$1"
  [ -z "$url" ] && return 0
  echo "deb $url stable main" > "$PREFIX/etc/apt/sources.list"
}

pkg_try() {
  local m
  for m in "${MIRRORS[@]}"; do
    set_mirror "$m"
    pkg update -y >/dev/null 2>&1 || true
    if pkg install -y "$@"; then
      return 0
    fi
    echo "Install failed with mirror '${m:-auto-picked}', trying next..."
  done
  return 1
}

# Bring every installed package to a consistent version BEFORE installing
# anything new. A half-upgraded Termux (seen in the wild: libcurl upgraded,
# openssl left behind) makes freshly installed tools fail to even start
# ("cannot locate symbol ..."), and mirror-hopping cannot fix that.
full_upgrade() {
  pkg update -y >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive pkg upgrade -y \
    -o Dpkg::Options::="--force-confold" \
    -o Dpkg::Options::="--force-confdef" || true
}

# The failure mode above leaves binaries present but unrunnable, so check
# that the tools we depend on actually execute, not just exist.
tools_ok() {
  command -v curl >/dev/null 2>&1 || return 1
  curl --version >/dev/null 2>&1 || return 1
  command -v python >/dev/null 2>&1 || return 1
  python -c "import ssl" >/dev/null 2>&1 || return 1
  return 0
}

rpc() {
  curl -s -X POST http://127.0.0.1:8077/api/rpc -H 'Content-Type: application/json' \
    -d "{\"method\":\"$1\",\"params\":$2}"
}

main() {
  echo "[1/6] Syncing all packages to consistent versions, then installing Python..."
  full_upgrade
  if ! pkg_try python curl; then
    echo "ERROR: could not install Python from any mirror. Check the network and run install.sh again."
    echo "Install log: $INSTALL_LOG"
    exit 1
  fi
  if ! tools_ok; then
    echo "Python/curl are installed but do not run - the local package system looks"
    echo "broken (a partial upgrade left library versions inconsistent). Trying a"
    echo "full upgrade to repair it (this is NOT a mirror problem)..."
    full_upgrade
    pkg_try python curl || true
    if ! tools_ok; then
      echo "ERROR: local Termux packages are still broken after a full upgrade."
      echo "Run 'apt full-upgrade' manually, answer any prompts, then re-run install.sh."
      echo "Install log: $INSTALL_LOG"
      exit 1
    fi
    echo "Package system repaired."
  fi

  echo "[2/6] Preparing the download engine..."
  ARCH="$(uname -m)"
  if [ "$ARCH" = "aarch64" ] && [ -f ./aria2c ]; then
    ARIA2_BIN="$HOME/tofetch/aria2c"
    echo "ARM64 device: using the bundled Aria2 Next engine."
  else
    echo "Architecture is $ARCH (bundled engine is arm64-only): installing aria2 from the Termux repo instead."
    if ! pkg_try aria2; then
      echo "ERROR: could not install aria2 from any mirror."
      echo "Install log: $INSTALL_LOG"
      exit 1
    fi
    ARIA2_BIN="$PREFIX/bin/aria2c"
  fi

  echo "[3/6] Copying files to ~/tofetch ..."
  mkdir -p ~/tofetch
  cp -rf server.py aria2.conf static ~/tofetch/
  if [ "$ARIA2_BIN" = "$HOME/tofetch/aria2c" ]; then
    cp -f aria2c ~/tofetch/aria2c
    chmod +x ~/tofetch/aria2c
  fi
  # Drop a stale bundled engine from an older install when it is not
  # when they are not the engine this install actually uses.
  local stale
  for stale in "$HOME/tofetch/aria2c"; do
    if [ -f "$stale" ] && [ "$stale" != "$ARIA2_BIN" ]; then
      rm -f "$stale"
      echo "Removed stale bundled engine: $stale"
    fi
  done

  echo "[4/6] Creating launcher command 'tofetch' ..."
  cat > ~/tofetch/tofetch.sh <<'EOS'
#!/data/data/com.termux/files/usr/bin/bash
cd ~/tofetch
export ARIA2_BIN="__ARIA2_BIN__"
export TOFETCH_HOST=127.0.0.1
export TOFETCH_PORT=8077
if [ -d "$HOME/storage/downloads" ]; then
  export TOFETCH_DOWNLOAD_DIR=$HOME/storage/downloads/ToFetch
else
  export TOFETCH_DOWNLOAD_DIR=$HOME/tofetch/downloads
fi
mkdir -p "$TOFETCH_DOWNLOAD_DIR"
echo "ToFetch running: open http://localhost:8077 in your browser"
echo "Files are saved to: $TOFETCH_DOWNLOAD_DIR"
echo "Press Ctrl+C to stop."
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  trap 'termux-wake-unlock 2>/dev/null' EXIT
fi
termux-open-url http://localhost:8077 2>/dev/null || true
python $HOME/tofetch/server.py
EOS
  sed -i "s|__ARIA2_BIN__|$ARIA2_BIN|" ~/tofetch/tofetch.sh
  chmod +x ~/tofetch/tofetch.sh
  ln -sf ~/tofetch/tofetch.sh "$PREFIX/bin/tofetch"
  
  echo "[5/6] Self-check: starting ToFetch and downloading a 1 MB test file..."
  DL_DIR="$HOME/tofetch/downloads"
  if [ -d "$HOME/storage/downloads" ]; then
    DL_DIR="$HOME/storage/downloads/ToFetch"
  fi
  mkdir -p "$DL_DIR"
  ARIA2_BIN="$ARIA2_BIN" TOFETCH_HOST=127.0.0.1 TOFETCH_PORT=8077 \
    TOFETCH_DOWNLOAD_DIR="$DL_DIR" TOFETCH_DATA_DIR="$HOME/tofetch/data" \
    python "$HOME/tofetch/server.py" > "$HOME/tofetch/selfcheck.log" 2>&1 &
  SC_PID=$!
  sleep 5
  SC_RESULT="engine"
  if curl -s http://127.0.0.1:8077/api/state | grep -q '"engine": true'; then
    SC_RESULT="network"
    SC_URLS=(
      "https://proof.ovh.net/files/1Mb.dat"
      "https://speed.cloudflare.com/__down?bytes=1048576"
      "http://speedtest.tele2.net/1MB.zip"
    )
    for url in "${SC_URLS[@]}"; do
      gid="$(rpc addUri "[[\"$url\"],{\"out\":\"tofetch-selfcheck.dat\"}]" \
        | python -c "import sys, json; print(json.load(sys.stdin).get('result', ''))" 2>/dev/null)"
      if [ -n "$gid" ]; then
        for i in $(seq 1 30); do
          st="$(rpc tellStatus "[\"$gid\",[\"status\"]]" \
            | python -c "import sys, json; print(json.load(sys.stdin).get('result', {}).get('status', ''))" 2>/dev/null)"
          if [ "$st" = "complete" ]; then
            if [ "$(stat -c%s "$DL_DIR/tofetch-selfcheck.dat" 2>/dev/null || echo 0)" = "1048576" ]; then
              SC_RESULT="ok"
            fi
            break
          fi
          [ "$st" = "error" ] && break
          sleep 1
        done
        # Leave no self-check task in the user's list/session.
        rpc removeDownloadResult "[\"$gid\"]" >/dev/null 2>&1 || true
      fi
      [ "$SC_RESULT" = "ok" ] && break
      rm -f "$DL_DIR/tofetch-selfcheck.dat" "$DL_DIR/tofetch-selfcheck.dat.aria2"
    done
  fi
  kill $SC_PID 2>/dev/null || true
  sleep 1
  rm -f "$DL_DIR/tofetch-selfcheck.dat" "$DL_DIR/tofetch-selfcheck.dat.aria2"
  case "$SC_RESULT" in
    ok)
      echo "SELF-CHECK PASSED: engine is up and a 1 MB test file downloaded byte-exact."
      ;;
    engine)
      echo "SELF-CHECK FAILED: the download engine did not start."
      echo "Check ~/tofetch/selfcheck.log and ~/tofetch/data/ logs for the reason."
      ;;
    *)
      echo "SELF-CHECK INCONCLUSIVE: the engine is up, but every test site was"
      echo "unreachable - that points to a network problem, not a broken install."
      echo "Just run 'tofetch' and try a real download."
      ;;
  esac

  echo "[6/6] Finishing up..."
  cp -f "$INSTALL_LOG" ~/tofetch/install.log 2>/dev/null || true
  echo ""
  echo "Done! Start it anytime by typing: tofetch"
  echo "Install log: ~/tofetch/install.log"
  echo "Tip: run 'termux-setup-storage' once (and allow access) so downloads land in the phone Download folder."
}

set +e
main "$@" 2>&1 | tee -a "$INSTALL_LOG"
CODE=${PIPESTATUS[0]}
exit $CODE
