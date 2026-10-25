# codex-autoresume

Automatically resume stalled Codex sessions through the local app-server. No terminal scraping or tmux required.

Scans all non-archived sessions every 15 seconds. After 60 seconds of observed inactivity, it performs the equivalent of `/goal resume`, including for sessions not open in a terminal. Retries use persisted exponential backoff, capped at 15 minutes.

Eligible sessions have an active goal, or a goal marked **stalled** by a capacity/connection error (reported as `blocked` by the app-server). Other blocked goals are left alone.

## Install

Requires Python 3.10+ and a running Codex app-server with goal support. Service management supports Linux with systemd and macOS with launchd. Tested with Codex 0.162.1. The app-server protocol is experimental.

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
# Linux
journalctl --user -u codex-autoresume -f
# macOS
tail -f ~/.local/state/codex-autoresume/service.log

# Stop, disable, and remove the service
.venv/bin/codex-autoresume --uninstall-service
```

On Linux, use `loginctl enable-linger "$USER"` to start at boot without logging in. On macOS, the LaunchAgent starts at login and restarts the watcher if it exits; it runs in the user's GUI session. Each user can run an independent service for their own Codex sessions. Reinstalling updates the service paths/options and restarts it. Add `--dry-run` to preview service installation. Keep the repository and virtual environment at their installed paths.

## Options and behavior

- `--errors-only`: only resume failed or interrupted turns with recognized retryable errors. By default, any eligible idle session with an active goal is considered.
- `--thread UUID`: watch a specific session; repeatable.
- `--idle-seconds 120`: change the inactivity grace period.
- `--socket PATH` / `--codex-home PATH`: use a different local server.
- `--once`: scan once, respecting the normal grace period and cooldown.

Interrupted turns are eligible only when the same turn carries a recognized retryable error. This includes stalled goals and `--errors-only` mode. The `interrupted` status or the generic "Conversation interrupted" message alone is not enough to trigger a retry.

For the exact error text "This content can’t be shown" (also with a straight apostrophe), the watcher first calls `thread/revert` before the oldest turn, removing the entire conversation history, then performs `/goal resume` on the same thread. Local files and the existing goal, token budget, and usage accounting are retained. This requires persistent paginated history and a server supporting `thread/revert`; unsupported threads are skipped without falling back to a plain resume. The normal idle grace and backoff apply. Recovery state, including a goal snapshot, is saved before reverting so a disconnect between revert and resume can be recovered after restart, including in `--errors-only` mode. `--dry-run` previews this action without changing history.

Running turns, approval/input dialogs, interruptions without a recognized transient error, paused/completed goals, exhausted budgets, and known non-retryable errors are skipped. Goal text, budgets, and usage are preserved. State is checked again before resuming; the protocol does not provide an atomic conditional resume.

The default socket is `${CODEX_HOME:-~/.codex}/app-server-control/app-server-control.sock`. State is stored in `${XDG_STATE_HOME:-~/.local/state}/codex-autoresume/`; service configuration in `${XDG_CONFIG_HOME:-~/.config}/systemd/user/` on Linux or `~/Library/LaunchAgents/com.weichwolf.codex-autoresume.plist` on macOS. macOS logs are written to `service.log` in the selected state directory. Separate servers such as `--no-daemon` sessions are not automatically covered. The watcher reconnects but does not start or restart Codex itself.

Service installation saves the selected options, for example:

```bash
.venv/bin/codex-autoresume --install-service --idle-seconds 120 --errors-only
```

Run `codex-autoresume --help` for all options.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use simulated sessions, an isolated Unix WebSocket server, and fake systemctl/launchctl executables. Both service lifecycles are tested on either supported platform. Tests do not start real Codex turns or install real services.
