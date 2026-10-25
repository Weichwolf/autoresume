import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from websockets.sync.server import unix_serve

from autoresume import (
    RpcClient, RpcError, State, Watchdog, blocked_reason, main, parse_args,
    single_instance, transient_error,
)


def snapshot(runtime="idle", goal="active", turn_status="failed", error=None):
    return {
        "thread": {"id": "t1", "status": {"type": runtime}},
        "goal": {"status": goal, "createdAt": 10, "objective": "Finish work",
                 "tokensUsed": 100, "tokenBudget": 1000} if goal else None,
        "turn": {"id": "turn1", "status": turn_status,
                 "error": error or {"codexErrorInfo": "serverOverloaded"}}
                if turn_status else None,
        "retrying": False,
    }


class FakeRpc:
    def __init__(self, snapshots):
        self.snapshots = snapshots
        self.actions = []
        self.before_read = None
        self.resume_error = None

    def threads(self):
        return [{"id": key} for key in self.snapshots]

    def snapshot(self, thread_id):
        if self.before_read:
            self.before_read(thread_id)
        return copy.deepcopy(self.snapshots[thread_id])

    def resume(self, thread_id):
        self.actions.append(("resume", thread_id))
        if self.resume_error:
            raise self.resume_error

    def call(self, method, params):
        self.actions.append((method, params["threadId"]))
        if method == "thread/resume":
            self.snapshots[params["threadId"]]["thread"]["status"] = {"type": "idle"}
        return {}


class PolicyTests(unittest.TestCase):
    def test_only_active_goals(self):
        for status in [None, "paused", "complete", "budgetLimited", "usageLimited"]:
            with self.subTest(status=status):
                self.assertEqual(blocked_reason(snapshot(goal=status)), "no-active-goal")

    def test_stalled_goal_is_recoverable_only_after_transient_failure(self):
        for goal_status in ["blocked", "stalled"]:
            self.assertIsNone(blocked_reason(snapshot(goal=goal_status)))
            self.assertEqual(blocked_reason(snapshot(goal=goal_status, turn_status="completed")), "goal-blocked")
            self.assertEqual(blocked_reason(snapshot(goal=goal_status, error={"message": "Need missing credentials"})), "goal-blocked")

    def test_no_injection_during_work_retries_or_user_dialogs(self):
        for flags in [[], ["waitingOnApproval"], ["waitingOnUserInput"]]:
            s = snapshot(runtime="active")
            s["thread"]["status"]["activeFlags"] = flags
            self.assertEqual(blocked_reason(s), "running-or-waiting")
        self.assertEqual(blocked_reason(snapshot(turn_status="inProgress")), "running-or-waiting")
        s = snapshot()
        s["retrying"] = True
        self.assertEqual(blocked_reason(s), "codex-retrying")

    def test_idle_goal_resumes_even_without_error(self):
        for status in ["completed", None]:
            s = snapshot(turn_status=status)
            if s["turn"]:
                s["turn"]["error"] = None
            self.assertIsNone(blocked_reason(s))
            self.assertEqual(blocked_reason(s, errors_only=True), "no-transient-error")

    def test_explicit_interrupt_and_exhausted_budget_are_preserved(self):
        s = snapshot(turn_status="interrupted")
        s["turn"]["error"] = None
        self.assertEqual(blocked_reason(s), "interrupted")
        s = snapshot()
        s["goal"]["tokensUsed"] = 1000
        self.assertEqual(blocked_reason(s), "budget-exhausted")

    def test_interrupted_turn_requires_transient_error_on_same_turn(self):
        for goal in ["active", "blocked", "stalled"]:
            for errors_only in [False, True]:
                for error in [
                    {"codexErrorInfo": "serverOverloaded"},
                    {"codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}}},
                    {"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}}},
                    {"message": "Connection reset by peer"},
                ]:
                    with self.subTest(goal=goal, errors_only=errors_only, error=error):
                        s = snapshot(goal=goal, turn_status="interrupted", error=error)
                        self.assertIsNone(blocked_reason(s, errors_only=errors_only))

    def test_ambiguous_or_non_retryable_interruptions_are_skipped(self):
        for goal in ["active", "blocked", "stalled"]:
            for errors_only in [False, True]:
                for error in [
                    None, {}, {"message": "Interrupted by user"},
                    {"message": "Conversation interrupted - use /feedback if something went wrong"},
                    {"message": "Unknown error", "codexErrorInfo": "other"},
                    {"message": "Connection failed", "codexErrorInfo": "usageLimitExceeded"},
                    {"message": "Connection failed", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}},
                ]:
                    with self.subTest(goal=goal, errors_only=errors_only, error=error):
                        s = snapshot(goal=goal, turn_status="interrupted")
                        s["turn"]["error"] = error
                        self.assertIsNotNone(blocked_reason(s, errors_only=errors_only))

    def test_http_errors_are_classified_before_message_fallback(self):
        for code in [401, 403, 404]:
            error = {"message": "connection failed", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": code}}}
            self.assertFalse(transient_error(error))
            self.assertEqual(blocked_reason(snapshot(error=error)), "non-retryable-error")
        for code in [408, 429, 500, 502, 503, 504]:
            self.assertTrue(transient_error({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": code}}}))

    def test_capacity_and_connection_variants(self):
        for error in [
            {"message": "Model at capacity"}, {"message": "Connection error"},
            {"codexErrorInfo": "serverOverloaded"},
            {"codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}}},
        ]:
            self.assertTrue(transient_error(error))
            self.assertIsNone(blocked_reason(snapshot(error=error), errors_only=True))
        self.assertFalse(transient_error({"message": "timeout", "codexErrorInfo": "usageLimitExceeded"}))

    def test_content_display_error_triggers_resume(self):
        for message in ["This content can’t be shown", "This content can't be shown"]:
            for field in ["message", "additionalDetails"]:
                error = {field: message}
                self.assertTrue(transient_error(error))
                for goal in ["active", "blocked", "stalled"]:
                    for status in ["failed", "interrupted"]:
                        for errors_only in [False, True]:
                            with self.subTest(message=message, field=field, goal=goal,
                                              status=status, errors_only=errors_only):
                                s = snapshot(goal=goal, turn_status=status, error=error)
                                self.assertIsNone(blocked_reason(s, errors_only=errors_only))

    def test_content_display_error_preserves_non_retryable_errors(self):
        for info in ["usageLimitExceeded", "misalignmentPolicyViolation",
                     {"httpConnectionFailed": {"httpStatusCode": 403}}]:
            with self.subTest(info=info):
                error = {"message": "This content can’t be shown", "codexErrorInfo": info}
                self.assertFalse(transient_error(error))
                self.assertEqual(blocked_reason(snapshot(error=error)), "non-retryable-error")

    def test_cyber_policy_error_triggers_resume(self):
        for message in ["This content can’t be shown",
                        "This content was flagged for possible cybersecurity risk."]:
            error = {"message": message, "codexErrorInfo": "cyberPolicy"}
            self.assertTrue(transient_error(error))
            for goal in ["active", "blocked", "stalled"]:
                for status in ["failed", "interrupted"]:
                    for errors_only in [False, True]:
                        with self.subTest(message=message, goal=goal, status=status,
                                          errors_only=errors_only):
                            s = snapshot(goal=goal, turn_status=status, error=error)
                            self.assertIsNone(blocked_reason(s, errors_only=errors_only))


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.state = State(self.path)
        self.args = parse_args(["--jitter", "0"])
        self.now = 1000
        self.rpc = FakeRpc({"t1": snapshot()})
        self.watch = Watchdog(self.rpc, self.state, self.args, clock=lambda: self.now,
                              jitter=lambda low, high: 1)

    def test_idle_grace_and_resume_all_active_sessions(self):
        self.rpc.snapshots.update(t2=snapshot(), paused=snapshot(goal="paused"))
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [])
        self.now += 59
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [])
        self.now += 1
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [("resume", "t1"), ("resume", "t2")])

    def test_stalled_capacity_goal_is_resumed_but_task_blocker_is_not(self):
        self.args.idle_seconds = 0
        self.rpc.snapshots = {
            "capacity": snapshot(runtime="systemError", goal="blocked"),
            "task-blocker": snapshot(goal="blocked", turn_status="completed"),
        }
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [("resume", "capacity")])

    def test_technical_interruption_obeys_grace_and_backoff(self):
        self.args.errors_only = True
        self.rpc.snapshots = {
            "technical": snapshot(goal="blocked", turn_status="interrupted"),
            "manual": snapshot(turn_status="interrupted"),
        }
        self.rpc.snapshots["manual"]["turn"]["error"] = None
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [])
        self.now += 60
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [("resume", "technical")])
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [("resume", "technical")])
        self.now += 60
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [("resume", "technical"), ("resume", "technical")])

    def test_interruption_error_is_rechecked_before_resume(self):
        self.args.idle_seconds = 0
        self.rpc.snapshots["t1"] = snapshot(turn_status="interrupted")
        reads = 0

        def clear_error(_):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.rpc.snapshots["t1"]["turn"]["error"] = None

        self.rpc.before_read = clear_error
        self.assertEqual(self.watch.scan()["changed-before-resume"], 1)
        self.assertEqual(self.rpc.actions, [])

    def test_grace_restarts_after_running(self):
        self.watch.scan()
        self.now += 50
        self.rpc.snapshots["t1"]["thread"]["status"] = {"type": "active"}
        self.watch.scan()
        self.now += 50
        self.rpc.snapshots["t1"]["thread"]["status"] = {"type": "idle"}
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [])
        self.now += 60
        self.watch.scan()
        self.assertEqual(len(self.rpc.actions), 1)

    def test_persisted_backoff_and_no_immediate_duplicate(self):
        self.args.idle_seconds = 0
        self.watch.scan()
        self.assertEqual(len(self.rpc.actions), 1)
        # A daemon restart retains the 60s cooldown, even for a new watchdog.
        self.watch.state = State(self.path)
        self.now += 59
        self.watch.scan()
        self.assertEqual(len(self.rpc.actions), 1)
        self.now += 1
        self.watch.scan()
        self.assertEqual(len(self.rpc.actions), 2)
        self.assertEqual(self.watch.state.sessions["t1"]["next_attempt"], self.now + 120)

    def test_failed_new_turn_keeps_backoff(self):
        self.args.idle_seconds = 0
        self.watch.scan()
        s = self.rpc.snapshots["t1"]
        s["thread"]["status"] = {"type": "active"}
        s["turn"]["status"] = "inProgress"
        self.now += 10
        self.watch.scan()
        s["thread"]["status"] = {"type": "idle"}
        s["turn"].update(id="turn2", status="failed")
        self.now += 10
        self.watch.scan()
        self.assertEqual(len(self.rpc.actions), 1)
        self.now += 40
        self.watch.scan()
        self.assertEqual(self.state.sessions["t1"]["attempts"], 2)

    def test_backoff_resets_after_sustained_running(self):
        self.args.idle_seconds = 0
        self.watch.scan()
        self.rpc.snapshots["t1"]["thread"]["status"] = {"type": "active"}
        self.watch.scan()
        self.now += self.args.reset_after
        self.watch.scan()
        self.assertEqual(self.state.sessions["t1"]["attempts"], 0)

    def test_pause_or_turn_start_before_mutation_cancels_resume(self):
        self.args.idle_seconds = 0
        for change in ["pause", "running", "new-turn"]:
            with self.subTest(change=change):
                self.rpc.snapshots["t1"] = snapshot()
                count = 0
                def race(_):
                    nonlocal count
                    count += 1
                    if count == 2:
                        if change == "pause":
                            self.rpc.snapshots["t1"]["goal"]["status"] = "paused"
                        elif change == "running":
                            self.rpc.snapshots["t1"]["thread"]["status"] = {"type": "active"}
                        else:
                            self.rpc.snapshots["t1"]["turn"]["id"] = "new"
                self.rpc.before_read = race
                self.watch.scan()
                self.assertEqual(self.rpc.actions, [])

    def test_dormant_goal_is_loaded_then_rechecked(self):
        self.args.idle_seconds = 0
        self.rpc.snapshots["t1"] = snapshot(runtime="notLoaded")
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [("thread/resume", "t1"), ("resume", "t1")])

    def test_dormant_load_that_starts_work_needs_no_second_resume(self):
        self.args.idle_seconds = 0
        self.rpc.snapshots["t1"] = snapshot(runtime="notLoaded")
        original = self.rpc.call
        def load(method, params):
            result = original(method, params)
            self.rpc.snapshots["t1"]["thread"]["status"] = {"type": "active"}
            return result
        self.rpc.call = load
        self.watch.scan()
        self.assertEqual(self.rpc.actions, [("thread/resume", "t1")])

    def test_dry_run_never_loads_resumes_or_writes_state(self):
        self.args.dry_run = True
        self.rpc.snapshots["t1"] = snapshot(runtime="notLoaded")
        self.assertEqual(self.watch.scan()["would-resume"], 1)
        self.assertEqual(self.rpc.actions, [])
        self.assertFalse(self.path.exists())

    def test_one_broken_session_does_not_block_others(self):
        self.args.idle_seconds = 0
        self.rpc.snapshots["broken"] = snapshot()
        original = self.rpc.snapshot
        def read(thread_id):
            if thread_id == "broken":
                raise RpcError("thread/read", {"message": "deleted"})
            return original(thread_id)
        self.rpc.snapshot = read
        counts = self.watch.scan()
        self.assertEqual(counts["resumed"], 1)
        self.assertEqual(counts["rpc-error"], 1)

    def test_lost_resume_response_cannot_cause_immediate_replay(self):
        self.args.idle_seconds = 0
        self.rpc.resume_error = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.watch.scan()
        self.rpc.resume_error = None
        self.watch.state = State(self.path)
        self.watch.scan()
        self.assertEqual(len(self.rpc.actions), 1)

    def test_scope_and_errors_only(self):
        self.args.idle_seconds = 0
        self.args.thread = ["t1"]
        self.args.errors_only = True
        self.rpc.snapshots["t1"] = snapshot(turn_status="completed")
        self.rpc.snapshots["t2"] = snapshot()
        self.assertEqual(self.watch.scan()["sessions"], 1)
        self.assertEqual(self.rpc.actions, [])


class StateTests(unittest.TestCase):
    def test_corrupt_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "state.json"
            for content in ["broken", '{"version": 2, "sessions": {}}',
                            '{"version": 1, "sessions": {"t": {"attempts": -1}}}']:
                p.write_text(content)
                with self.assertRaises(ValueError):
                    State(p)

    def test_second_writer_cannot_take_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "lock"
            with single_instance(p):
                with self.assertRaises(RuntimeError):
                    with single_instance(p):
                        self.fail("second writer acquired lock")


class ProtocolTests(unittest.TestCase):
    """Exercise real Unix WebSockets against an isolated app-server fixture."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ar-")
        self.addCleanup(self.temp.cleanup)
        self.sock = Path(self.temp.name) / "server.sock"
        self.methods = []
        self.mutations = []
        self.responses = []
        self.runtime = "idle"
        self.goal_status = "active"
        self.turn = snapshot()["turn"]
        self.disconnect_once = False
        self.resumed = threading.Event()
        self.server = unix_serve(self.handler, str(self.sock))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)

    def handler(self, ws):
        for raw in ws:
            request = json.loads(raw)
            method, params = request["method"], request.get("params", {})
            self.methods.append(method)
            if method == "initialized":
                continue
            if method == "initialize":
                result = {"userAgent": "test"}
            elif method == "thread/list":
                if self.disconnect_once:
                    self.disconnect_once = False
                    ws.close()
                    return
                if params.get("cursor"):
                    result = {"data": [{"id": "t2"}], "nextCursor": None}
                else:
                    result = {"data": [{"id": "t1"}], "nextCursor": "page2"}
            elif method == "thread/read":
                result = {"thread": {"id": params["threadId"], "status": {"type": self.runtime}}}
            elif method == "thread/goal/get":
                result = {"goal": snapshot(goal=self.goal_status)["goal"]}
            elif method == "thread/turns/list":
                result = {"data": [self.turn]}
            elif method == "thread/goal/set":
                self.mutations.append(params)
                self.runtime = "active"
                self.resumed.set()
                result = {"goal": snapshot()["goal"]}
            elif method == "timeout":
                continue
            elif method == "fail":
                ws.send(json.dumps({"id": request["id"], "error": {"code": -1, "message": "no"}}))
                continue
            else:
                result = {}
            # Unrelated notifications must not be mistaken for RPC responses.
            ws.send(json.dumps({"method": "error", "params": {
                "threadId": "elsewhere", "turnId": "turn9", "willRetry": True,
            }}))
            ws.send(json.dumps({"id": request["id"], "result": result}))

    def test_initialize_pagination_notifications_and_exact_resume_request(self):
        rpc = RpcClient(self.sock)
        try:
            self.assertEqual([t["id"] for t in rpc.threads()], ["t1", "t2"])
            self.assertEqual(rpc.retrying["elsewhere"], ("turn9", True))
            rpc.resume("t1")
            self.assertEqual(self.mutations, [{"threadId": "t1", "status": "active", "origin": "user"}])
            with self.assertRaises(RpcError):
                rpc.call("fail", {})
        finally:
            rpc.close()

    def test_cli_live_once_against_fake_server(self):
        result = main(["--socket", str(self.sock), "--state-dir", self.temp.name,
                       "--once", "--idle-seconds", "0", "--thread", "t1"])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.mutations), 1)

    def test_cli_recovers_interrupted_goal_with_connection_error(self):
        self.goal_status = "blocked"
        self.turn = snapshot(turn_status="interrupted", error={
            "message": "Stream disconnected",
            "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 502}},
        })["turn"]
        result = main(["--socket", str(self.sock), "--state-dir", self.temp.name,
                       "--once", "--idle-seconds", "0", "--thread", "t1", "--errors-only"])
        self.assertEqual(result, 0)
        self.assertEqual(self.mutations, [{"threadId": "t1", "status": "active", "origin": "user"}])

    def test_cli_dry_run_never_mutates_server(self):
        result = main(["--socket", str(self.sock), "--state-dir", self.temp.name,
                       "--once", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertEqual(self.mutations, [])
        self.assertNotIn("thread/resume", self.methods)
        self.assertEqual(list(Path(self.temp.name).glob("*.json")), [])

    def test_rpc_deadline(self):
        rpc = RpcClient(self.sock, timeout=0.1)
        try:
            with self.assertRaises(TimeoutError):
                rpc.call("timeout", {})
        finally:
            rpc.close()

    def test_blocked_goal_snapshot_includes_the_failure_for_recovery(self):
        self.goal_status = "blocked"
        self.runtime = "systemError"
        rpc = RpcClient(self.sock)
        try:
            current = rpc.snapshot("t1")
            self.assertEqual(current["turn"]["status"], "failed")
            self.assertIsNone(blocked_reason(current))
        finally:
            rpc.close()

    def test_daemon_reconnects_and_stops_cleanly_on_sigterm(self):
        self.disconnect_once = True
        process = subprocess.Popen([
            sys.executable, "-m", "autoresume", "--socket", str(self.sock),
            "--state-dir", self.temp.name, "--interval", "0.05",
            "--idle-seconds", "0", "--thread", "t1",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(self.resumed.wait(timeout=5), "no resume after reconnect")
        finally:
            process.terminate()
            try:
                _, output = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("Scan failed", output)
        self.assertIn("RESUME t1", output)
        self.assertGreaterEqual(self.methods.count("initialize"), 2)


if __name__ == "__main__":
    unittest.main()
