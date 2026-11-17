# ToFetch

A tiny download manager you actually own.

ToFetch is a thin, self-hosted shell around [aria2](https://aria2.github.io/)
— the battle-tested download engine that has powered tools like Motrix for
two decades. All downloading (HTTP / HTTPS / FTP / BitTorrent / magnet) is
done by `aria2c`; ToFetch only adds:

- a single-file web UI (no build step, no CDN, no framework)
- a small Python backend that serves the UI, holds the aria2 RPC secret
  server-side, and can start `aria2c` for you
- password login, task list with progress/speed/ETA, pause & resume,
  global speed limits, and click-to-download of finished files

It was written to be **independent of any upstream project**: the backend
uses only the Python 3 standard library, the frontend is one HTML file with
vanilla JS, and the engine is a single static binary. If every repository
mentioned here disappeared tomorrow, a copy of this repo plus an `aria2c`
binary would still run exactly as it does today.

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/tobaye/tofetch.git
cd tofetch
# edit docker-compose.yml and set TOFETCH_PASSWORD first!
docker compose up -d
```

Open `http://<your-server>:8077`.

### Bare metal

You need Python 3.10+ and an `aria2c` binary:

- Debian/Ubuntu: `sudo apt install aria2`
- or a static build of [Aria2 Next](https://github.com/AnInsomniacy/aria2-next/releases)
  (a maintained aria2 fork; drop the binary somewhere and point
  `ARIA2_BIN` at it — handy to keep a copy with your backups)

```bash
TOFETCH_PASSWORD='pick-a-password' python3 server.py
```

Open `http://localhost:8077`. `server.py` starts `aria2c` automatically
when it is not already running (`ARIA2_AUTOSTART=0` to disable).

## Configuration

All through environment variables:

| Variable              | Default        | Meaning                                  |
|-----------------------|----------------|------------------------------------------|
| `TOFETCH_PORT`        | `8077`         | web UI port                              |
| `TOFETCH_HOST`        | `0.0.0.0`      | bind address                             |
| `TOFETCH_PASSWORD`    | *(empty)*      | web UI password; empty = no login — LAN only, never expose |
| `TOFETCH_DOWNLOAD_DIR`| `./downloads`  | where files land                         |
| `TOFETCH_DATA_DIR`    | `./data`       | session file, generated RPC secret, aria2.conf copy |
| `ARIA2_BIN`           | from `PATH`    | path to `aria2c`                         |
| `ARIA2_RPC_PORT`      | `6800`         | aria2 RPC port (localhost only)          |
| `ARIA2_SECRET`        | generated once | aria2 RPC secret; stored in the data dir |
| `ARIA2_AUTOSTART`     | `1`            | start `aria2c` if it is not answering    |

Download behaviour (trackers, connections, seeding ratio…) lives in
`aria2.conf` — it is copied to the data dir on first run; edit that copy.

## Security notes

- The aria2 RPC port is bound to localhost and protected by a secret that
  never leaves the server; the browser only talks to ToFetch's own API.
- Always set `TOFETCH_PASSWORD` if the UI is reachable from anywhere you
  do not fully trust. For internet exposure, put it behind HTTPS
  (a reverse proxy / Cloudflare Tunnel) as well.
- Finished files can be downloaded through the UI (`/files/...`), limited
  to the download directory.

## Layout

```
server.py          the whole backend (Python stdlib only)
static/index.html  the whole frontend (one file, vanilla JS)
aria2.conf         download behaviour defaults
Dockerfile         python:3.12-slim + aria2 from Debian
docker-compose.yml one-service deployment
```

## Known limitations

- If the process is killed hard (power loss, OS killing the app), a task's
  downloaded progress can roll back to the engine's last checkpoint —
  measured at 26 MB -> 8 MB in a forced-kill test. The task itself is not
  lost (the session is saved on every add and every 5 s) and the finished
  file is unaffected; it just re-downloads a stretch. Graceful shutdown
  (Ctrl+C) saves everything exactly.

## License

MIT — see [LICENSE](LICENSE). aria2 / Aria2 Next are GPLv2 and are *not*
included in this repository; you install the engine separately, exactly
like a browser is separate from a web page.
