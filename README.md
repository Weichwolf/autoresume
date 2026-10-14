# codex-autoresume

Automatically resume stalled Codex sessions through the local app-server. No terminal scraping or tmux required.

Scans all non-archived sessions every 15 seconds. After 60 seconds of observed inactivity, it performs the equivalent of `/goal resume`, including for sessions not open in a terminal. Retries use persisted exponential backoff, capped at 15 minutes.

Eligible sessions have an active goal, or a goal marked **stalled** by a capacity/connection error (reported as `blocked` by the app-server). Other blocked goals are left alone.

## Install

Requires Python 3.10+ and a running Codex app-server with goal support. Tested with Codex 0.162.1 on Linux. The Python watcher should also work on macOS, but has not been tested there; service management requires Linux with systemd. The app-server protocol is experimental.

```bash
git clone https://github.com/Weichwolf/codex-autoresume.git
cd codex-autoresume
python3 -m venv .venv
.venv/bin/pip install -e .

# Preview without changing sessions
.venv/bin/codex-autoresume --dry-run --once

# Run in the foreground
.venv/bin/codex-autoresume
```

## Background service

```bash
# Install, enable login autostart, and start the user service (no sudo)
.venv/bin/codex-autoresume --install-service

# Status and logs
.venv/bin/codex-autoresume --service-status
journalctl --user -u codex-autoresume -f

# Stop, disable, and remove the service
.venv/bin/codex-autoresume --uninstall-service
```

To start at boot without logging in, also run `loginctl enable-linger "$USER"`. Each user can run an independent service for their own Codex sessions. Reinstalling updates the service paths/options and restarts it. Add `--dry-run` to preview service installation.

## Options and behavior

- `--errors-only`: only resume failed turns with capacity or connection errors. By default, any eligible idle session with an active goal is considered.
- `--thread UUID`: watch a specific session; repeatable.
- `--idle-seconds 120`: change the inactivity grace period.
- `--socket PATH` / `--codex-home PATH`: use a different local server.
- `--once`: scan once, respecting the normal grace period and cooldown.

Running turns, approval/input dialogs, manually interrupted turns, paused/completed goals, exhausted budgets, and known non-retryable errors are skipped. Goal text, budgets, and usage are preserved. State is checked again before resuming; the protocol does not provide an atomic conditional resume.

The default socket is `${CODEX_HOME:-~/.codex}/app-server-control/app-server-control.sock`. State is stored in `${XDG_STATE_HOME:-~/.local/state}/codex-autoresume/`; service configuration in `${XDG_CONFIG_HOME:-~/.config}/systemd/user/`. Separate servers such as `--no-daemon` sessions are not automatically covered. The watcher reconnects but does not start or restart Codex itself.

Service installation saves the selected options, for example:

```bash
.venv/bin/codex-autoresume --install-service --idle-seconds 120 --errors-only
```

Run `codex-autoresume --help` for all options.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use simulated sessions, an isolated Unix WebSocket server, and a fake systemctl. They do not start real Codex turns or install real services.
