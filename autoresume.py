#!/usr/bin/env python3
"""Watch all active Codex goals without reading or injecting terminal input."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import plistlib
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from websockets.exceptions import WebSocketException
from websockets.sync.client import unix_connect

LOG = logging.getLogger("autoresume")
VERSION = "0.1.0"
SERVICE_NAME = "codex-autoresume.service"
SERVICE_MARKER = "# Managed by codex-autoresume"
LAUNCHD_LABEL = "com.weichwolf.codex-autoresume"
TRANSIENT = {
    "serverOverloaded", "internalServerError", "rateLimitExceeded", "flexUnavailable",
    "responseStreamConnectionFailed", "responseStreamDisconnected",
    "responseTooManyFailedAttempts", "cyberPolicy",
}
TERMINAL = {
    "usageLimitExceeded", "sessionBudgetExceeded", "contextWindowExceeded",
    "unauthorized", "badRequest", "misalignmentPolicyViolation",
    "tooManyDenials", "sandboxError",
}


class RpcError(RuntimeError):
    def __init__(self, method, error):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


class RpcClient:
    """One sequential JSON-RPC client; no thread/resume subscriptions while polling."""

    def __init__(self, socket_path, timeout=15):
        self._stack = ExitStack()
        self.ws = self._stack.enter_context(unix_connect(
            str(socket_path), open_timeout=timeout, close_timeout=1,
            compression=None, max_size=32 * 1024 * 1024,
        ))
        self.timeout = timeout
        self.sequence = 0
        self.retrying = {}
        try:
            self.call("initialize", {
                "clientInfo": {"name": "codex_autoresume", "version": VERSION},
                "capabilities": {"experimentalApi": True},
            })
            self.ws.send(json.dumps({"method": "initialized"}))
        except BaseException:
            self.close()
            raise

    def close(self):
        self._stack.close()

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method}: RPC timeout")
            message = json.loads(self.ws.recv(timeout=remaining))
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RpcError(method, message["error"])
                return message["result"]
            self._notification(message)

    def _notification(self, message):
        method = message.get("method")
        params = message.get("params", {})
        thread_id = params.get("threadId")
        if method == "error" and thread_id:
            self.retrying[thread_id] = (params.get("turnId"), params.get("willRetry", False))
        elif method in {"turn/started", "turn/completed"} and thread_id:
            self.retrying.pop(thread_id, None)
        # Approval and user-input requests belong to the session's UI. This
        # observer never answers, approves, cancels, or declines them.

    def threads(self):
        cursor = None
        while True:
            page = self.call("thread/list", {
                "limit": 100, "cursor": cursor, "archived": False,
                "useStateDbOnly": True,
                "sourceKinds": ["cli", "vscode", "exec", "appServer", "subAgent",
                                "subAgentReview", "subAgentCompact", "subAgentThreadSpawn",
                                "subAgentOther", "unknown"],
            })
            yield from page["data"]
            cursor = page.get("nextCursor")
            if not cursor:
                return

    def snapshot(self, thread_id):
        thread = self.call("thread/read", {"threadId": thread_id})["thread"]
        goal = self.call("thread/goal/get", {"threadId": thread_id}).get("goal")
        turns = []
        if goal and goal["status"] in {"active", "blocked", "stalled"}:
            turns = self.call("thread/turns/list", {
                "threadId": thread_id, "limit": 1, "sortDirection": "desc",
            })["data"]
        turn = turns[0] if turns else None
        retry = self.retrying.get(thread_id)
        return {
            "thread": thread, "goal": goal, "turn": turn,
            "retrying": bool(retry and turn and retry[0] == turn["id"] and retry[1]),
        }

    def resume(self, thread_id):
        # Same goal mutation as /goal resume. Omitting objective and tokenBudget
        # preserves the existing objective, budget, and usage accounting.
        return self.call("thread/goal/set", {
            "threadId": thread_id, "status": "active", "origin": "user",
        })


def error_kind(error):
    info = (error or {}).get("codexErrorInfo")
    if isinstance(info, str):
        return info, None
    if isinstance(info, dict) and info:
        name, details = next(iter(info.items()))
        return name, details.get("httpStatusCode") if isinstance(details, dict) else None
    return None, None


def transient_error(error):
    kind, http_status = error_kind(error)
    if kind in TERMINAL:
        return False
    if http_status is not None:
        return http_status in {408, 429} or 500 <= http_status < 600
    if kind in TRANSIENT or kind == "httpConnectionFailed":
        return True
    text = " ".join(str((error or {}).get(k) or "") for k in ("message", "additionalDetails")).lower()
    return any(s in text for s in (
        "at capacity", "overloaded", "connection error", "connection failed",
        "connection reset", "connection closed", "stream disconnected",
        "timed out", "timeout", "temporarily unavailable",
        "this content can’t be shown", "this content can't be shown",
    ))


def blocked_reason(snapshot, errors_only=False):
    goal, turn = snapshot["goal"], snapshot["turn"]
    if not goal or goal["status"] not in {"active", "blocked", "stalled"}:
        return "no-active-goal"
    retryable_turn = bool(turn and turn["status"] in {"failed", "interrupted"}
                          and transient_error(turn.get("error")))
    # The UI labels a goal stalled when a provider error changes its persisted
    # status to blocked. Recover only when the latest failed or interrupted
    # turn carries a recognized retryable error, not a task blocker.
    if goal["status"] in {"blocked", "stalled"}:
        if not retryable_turn:
            return "goal-blocked"
    budget = goal.get("tokenBudget")
    if budget is not None and goal.get("tokensUsed", 0) >= budget:
        return "budget-exhausted"
    runtime = snapshot["thread"]["status"]["type"]
    if runtime == "active" or (turn and turn["status"] == "inProgress"):
        return "running-or-waiting"
    if runtime not in {"idle", "systemError", "notLoaded"}:
        return "unknown-runtime"
    if snapshot.get("retrying"):
        return "codex-retrying"
    # An interruption alone does not identify its cause. Require a transient
    # error on this same turn before treating it as a technical interruption.
    if turn and turn["status"] == "interrupted" and not retryable_turn:
        return "interrupted"
    error = (turn or {}).get("error")
    kind, status = error_kind(error)
    if kind in TERMINAL or (status is not None and 400 <= status < 500 and status not in {408, 429}):
        return "non-retryable-error"
    if errors_only and not retryable_turn:
        return "no-transient-error"
    return None


def fingerprint(snapshot):
    goal, turn = snapshot["goal"], snapshot["turn"]
    return [goal["createdAt"], turn and turn["id"], turn and turn["status"]]


class State:
    def __init__(self, path):
        self.path = path
        self.sessions = {}
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
                raise ValueError(f"Invalid state file: {path}")
            self.sessions = data["sessions"]
            for record in self.sessions.values():
                if not isinstance(record, dict):
                    raise ValueError(f"Invalid session state: {path}")
                for key in ("attempts", "next_attempt", "idle_since", "running_since"):
                    value = record.get(key)
                    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                        raise ValueError(f"Invalid {key} in state: {path}")

    def save(self):
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"version": 1, "sessions": self.sessions}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class Watchdog:
    def __init__(self, rpc, state, args, clock=time.time, jitter=random.uniform):
        self.rpc, self.state, self.args = rpc, state, args
        self.clock, self.jitter = clock, jitter

    def scan(self):
        counts = Counter()
        for thread in self.rpc.threads():
            thread_id = thread["id"]
            if self.args.thread and thread_id not in self.args.thread:
                continue
            counts["sessions"] += 1
            try:
                outcome = self.check(thread_id)
                counts[outcome] += 1
            except RpcError as error:
                # A removed thread or one unsupported history format must not
                # prevent checking every other session.
                counts["rpc-error"] += 1
                LOG.warning("Session %s: %s", thread_id, error)
        LOG.info("Scan: %s", " ".join(f"{key}={value}" for key, value in sorted(counts.items())))
        if not self.args.dry_run:
            self.state.save()
        return counts

    def check(self, thread_id):
        snapshot = self.rpc.snapshot(thread_id)
        reason = blocked_reason(snapshot, self.args.errors_only)
        now = self.clock()
        record = self.state.sessions.get(thread_id)
        if reason:
            if reason == "running-or-waiting" and record:
                record["idle_since"] = None
                if record.get("running_since") is None:
                    record["running_since"] = now
                if now - record["running_since"] >= self.args.reset_after:
                    record["attempts"] = 0
                    record["next_attempt"] = 0
            else:
                self.state.sessions.pop(thread_id, None)
            LOG.debug("Skip %s: %s", thread_id, reason)
            return reason

        key = fingerprint(snapshot)
        if record is None or record.get("goal_created_at") != key[0]:
            record = {"goal_created_at": key[0], "attempts": 0, "next_attempt": 0}
            self.state.sessions[thread_id] = record
        if record.get("fingerprint") != key or record.get("idle_since") is None:
            record["fingerprint"] = key
            record["idle_since"] = now
        record["running_since"] = None
        due = max(record["idle_since"] + self.args.idle_seconds, record.get("next_attempt", 0))
        if self.args.dry_run:
            LOG.info("DRY RUN %s: would /goal resume in %.0fs (runtime=%s, turn=%s)",
                     thread_id, max(0, due - now), snapshot["thread"]["status"]["type"],
                     (snapshot["turn"] or {}).get("status", "none"))
            return "would-resume"
        if now < due:
            return "cooldown"

        # Recheck directly before mutation. A manual pause, a new turn, or a
        # changed goal observed here cancels this attempt.
        fresh = self.rpc.snapshot(thread_id)
        if blocked_reason(fresh, self.args.errors_only) or fingerprint(fresh) != key:
            record["idle_since"] = None
            return "changed-before-resume"

        # Write the cooldown BEFORE the request. A lost response or process crash
        # must not cause an immediate duplicate resume on reconnection.
        record["attempts"] += 1
        delay = min(self.args.max_backoff, self.args.initial_backoff * 2 ** min(record["attempts"] - 1, 30))
        delay = min(self.args.max_backoff, delay * self.jitter(1 - self.args.jitter, 1 + self.args.jitter))
        record["next_attempt"] = now + delay
        record["last_attempt"] = now
        self.state.save()

        # Loading a dormant thread joins its existing history; it doesn't create
        # a new session. Recheck again because loading may restore continuation.
        if fresh["thread"]["status"]["type"] == "notLoaded":
            self.rpc.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            fresh = self.rpc.snapshot(thread_id)
            if blocked_reason(fresh, self.args.errors_only) or fingerprint(fresh) != key:
                return "changed-after-load"
        self.rpc.resume(thread_id)
        LOG.info("RESUME %s: /goal resume, attempt=%s, cooldown=%.0fs", thread_id, record["attempts"], delay)
        # The server sends the RPC response before applying runtime effects, so
        # immediate idle is not proof of failure. Later scans verify progress
        # and retry only after the persisted cooldown.
        return "resumed"


@contextmanager
def single_instance(path):
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another autoresume process already watches this socket") from error
        yield


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return value


def nonnegative(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return value


def systemd_argument(value):
    """Quote one ExecStart argument; systemd expansion isn't shell quoting."""
    value = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Service arguments must not contain control characters")
    for source, target in [("\\", "\\\\"), ('"', '\\"'), ("%", "%%"), ("$", "$$")]:
        value = value.replace(source, target)
    return '"' + value + '"'


def service_command(args, python=None, script=None):
    # Keep the venv interpreter path: resolve() would follow its symlink to the
    # base Python interpreter, which doesn't have this package's dependencies.
    command = [os.path.abspath(python or sys.executable),
               str(Path(script or __file__).absolute()),
               "--socket", str(args.socket), "--state-dir", str(args.state_dir.absolute())]
    for name in ["interval", "idle_seconds", "initial_backoff", "max_backoff",
                 "reset_after", "jitter", "rpc_timeout"]:
        command.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
    for thread_id in args.thread or []:
        command.extend(["--thread", thread_id])
    for name in ["errors_only", "verbose"]:
        if getattr(args, name):
            command.append("--" + name.replace("_", "-"))
    return command


def service_unit(args, python=None, script=None):
    command = service_command(args, python, script)
    return "\n".join([
        SERVICE_MARKER, "[Unit]", "Description=Resume stalled Codex sessions with active goals",
        "", "[Service]", "Type=simple",
        "ExecStart=" + " ".join(systemd_argument(argument) for argument in command),
        "Restart=on-failure", "RestartSec=15", "UMask=0077", "",
        "[Install]", "WantedBy=default.target", "",
    ])


def run_systemctl(*arguments, check=True):
    result = subprocess.run(["systemctl", "--user", *arguments],
                            capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"systemctl --user {' '.join(arguments)} failed: {detail}")
    return result


def launchd_plist(args, python=None, script=None):
    log_path = args.state_dir.absolute() / "service.log"
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL, "ManagedBy": SERVICE_MARKER,
        "ProgramArguments": service_command(args, python, script),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
        "Umask": 0o077, "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    })


def run_launchctl(*arguments, check=True):
    result = subprocess.run(["launchctl", *arguments],
                            capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"launchctl {' '.join(arguments)} failed: {detail}")
    return result


def bootstrap_launchd(domain, unit_path):
    # bootout returns before launchd finishes removing the old service.
    deadline, delay = time.monotonic() + 10, 0.1
    while True:
        result = run_launchctl("bootstrap", domain, str(unit_path), check=False)
        if not result.returncode:
            return
        remaining = deadline - time.monotonic()
        if result.returncode != 5 or remaining <= 0:
            raise RuntimeError(f"launchctl bootstrap {domain} {unit_path} failed: {result.stderr.strip()}")
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 1)


def manage_launchd(args):
    unit_path = Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LAUNCHD_LABEL}"
    if args.dry_run:
        if args.install_service:
            print(launchd_plist(args).decode(), end="")
        LOG.info("DRY RUN: would %s %s (%s)",
                 "install/start" if args.install_service else "remove" if args.uninstall_service else "inspect",
                 target, unit_path)
        return 0
    if not shutil.which("launchctl"):
        raise RuntimeError("launchctl was not found")
    if args.service_status:
        result = run_launchctl("print", target, check=False)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    run_launchctl("print", domain)
    if unit_path.exists() or unit_path.is_symlink():
        if unit_path.is_symlink() or plistlib.loads(unit_path.read_bytes()).get("ManagedBy") != SERVICE_MARKER:
            raise RuntimeError(f"Existing service {unit_path} is not managed by this script; move it aside first")
    loaded = run_launchctl("print", target, check=False)
    if loaded.returncode not in {0, 113}:
        raise RuntimeError(f"Cannot inspect {target}: {loaded.stderr.strip()}")
    if not loaded.returncode:
        if not unit_path.exists():
            raise RuntimeError(f"Loaded service {target} has no managed plist at {unit_path}")
        run_launchctl("bootout", target)
    if args.install_service:
        content = launchd_plist(args)
        args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".codex-autoresume-", dir=unit_path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(temporary, unit_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        run_launchctl("enable", target)
        bootstrap_launchd(domain, unit_path)
        run_launchctl("print", target)
        LOG.info("Installed and started %s (%s)", LAUNCHD_LABEL, unit_path)
    elif unit_path.exists():
        unit_path.unlink()
        LOG.info("Stopped and removed %s", unit_path)
    else:
        LOG.info("No service installed at %s", unit_path)
    return 0


def manage_service(args):
    if sys.platform == "darwin":
        return manage_launchd(args)
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Service management requires Linux with systemd or macOS with launchd")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser().absolute()
    unit_path = config_home / "systemd/user" / SERVICE_NAME
    if args.dry_run:
        if args.install_service:
            print(service_unit(args), end="")
            LOG.info("DRY RUN: would write %s, daemon-reload, enable and start/restart the user service", unit_path)
        elif args.uninstall_service:
            LOG.info("DRY RUN: would stop/disable %s, remove %s and daemon-reload", SERVICE_NAME, unit_path)
        else:
            LOG.info("DRY RUN: would run systemctl --user status --no-pager %s", SERVICE_NAME)
        return 0
    if not shutil.which("systemctl"):
        raise RuntimeError("systemctl was not found; run the watcher directly or install systemd")
    if args.service_status:
        result = run_systemctl("status", "--no-pager", SERVICE_NAME, check=False)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode

    # Check the user manager before creating or changing files. No sudo, global
    # units, environment import, or login/linger configuration is needed.
    run_systemctl("show", "--property=Version", "--value")
    if unit_path.exists() or unit_path.is_symlink():
        if unit_path.is_symlink() or SERVICE_MARKER not in unit_path.read_text().splitlines():
            raise RuntimeError(f"Existing service {unit_path} is not managed by this script; move it aside first")
    if args.install_service:
        text = service_unit(args)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".codex-autoresume-", dir=unit_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            os.replace(temporary, unit_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        run_systemctl("daemon-reload")
        run_systemctl("enable", SERVICE_NAME)
        # restart starts an inactive service and applies new arguments to a
        # running one. enable --now alone wouldn't reload a running process.
        run_systemctl("restart", SERVICE_NAME)
        run_systemctl("is-active", "--quiet", SERVICE_NAME)
        LOG.info("Installed and started %s (%s)", SERVICE_NAME, unit_path)
    elif unit_path.exists():
        run_systemctl("disable", "--now", SERVICE_NAME)
        unit_path.unlink()
        run_systemctl("daemon-reload")
        LOG.info("Stopped and removed %s", unit_path)
    else:
        LOG.info("No service installed at %s", unit_path)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser())
    parser.add_argument("--socket", type=Path, help="Override the local app-server Unix socket")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "codex-autoresume")
    parser.add_argument("--thread", action="append", help="Only watch this thread UUID; repeatable; default: all")
    parser.add_argument("--dry-run", action="store_true", help="Read sessions and log plans; no resume or state writes")
    parser.add_argument("--once", action="store_true", help="Scan once; normal idle grace still applies")
    parser.add_argument("--errors-only", action="store_true", help="Only resume failed or interrupted turns with recognized retryable errors")
    parser.add_argument("--interval", type=positive, default=15, help="Scan interval in seconds (default: 15)")
    parser.add_argument("--idle-seconds", type=nonnegative, default=60, help="Continuous observed idle grace (default: 60)")
    parser.add_argument("--initial-backoff", type=positive, default=60)
    parser.add_argument("--max-backoff", type=positive, default=900)
    parser.add_argument("--reset-after", type=positive, default=300, help="Reset backoff after this many seconds running")
    parser.add_argument("--jitter", type=nonnegative, default=0.2, help="Backoff randomization fraction (default: 0.2)")
    parser.add_argument("--rpc-timeout", type=positive, default=15)
    parser.add_argument("--verbose", action="store_true")
    service = parser.add_mutually_exclusive_group()
    service.add_argument("--install-service", action="store_true", help="Install, enable and start the user service with these options")
    service.add_argument("--uninstall-service", action="store_true", help="Stop and remove the user service")
    service.add_argument("--service-status", action="store_true", help="Show user service status")
    args = parser.parse_args(argv)
    if args.jitter >= 1:
        parser.error("--jitter must be less than 1")
    if args.max_backoff < args.initial_backoff:
        parser.error("--max-backoff must be at least --initial-backoff")
    if args.once and (args.install_service or args.uninstall_service or args.service_status):
        parser.error("--once cannot be combined with service management")
    args.socket = (args.socket or args.codex_home / "app-server-control/app-server-control.sock").expanduser().absolute()
    args.state_dir = args.state_dir.expanduser()
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if args.install_service or args.uninstall_service or args.service_status:
        try:
            return manage_service(args)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            LOG.error("%s", error)
            return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tag = hashlib.sha256(str(args.socket).encode()).hexdigest()[:16]
    rpc = None
    try:
        # A dry run may run alongside the real service; it never mutates sessions.
        suffix = "-dry-run" if args.dry_run else ""
        with single_instance(args.state_dir / f"{tag}{suffix}.lock"):
            state = State(args.state_dir / f"{tag}.json")
            watchdog = Watchdog(None, state, args)
            LOG.info("Watching %s; mode=%s; scope=%s", args.socket,
                     "dry-run" if args.dry_run else "resume", args.thread or "all active goals and goals stalled by transient errors")
            while not stop.is_set():
                try:
                    if rpc is None:
                        rpc = RpcClient(args.socket, args.rpc_timeout)
                        watchdog.rpc = rpc
                    counts = watchdog.scan()
                    if args.once:
                        return 1 if counts["rpc-error"] else 0
                except (OSError, WebSocketException, RpcError, ValueError, KeyError) as error:
                    LOG.error("Scan failed: %s", error)
                    if rpc is not None:
                        rpc.close()
                        rpc = None
                    if args.once:
                        return 1
                stop.wait(args.interval)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        LOG.error("%s", error)
        return 1
    finally:
        if rpc is not None:
            rpc.close()


if __name__ == "__main__":
    raise SystemExit(main())
