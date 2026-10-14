import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from autoresume import (LAUNCHD_LABEL, SERVICE_MARKER, SERVICE_NAME, launchd_plist,
                        main, parse_args, service_unit, systemd_argument)


class ServiceUnitTests(unittest.TestCase):
    def test_current_interpreter_paths_and_options_are_persisted(self):
        args = parse_args(["--install-service", "--socket", "/tmp/socket with spaces",
                           "--state-dir", "/tmp/state", "--thread", "t1", "--thread", "t2",
                           "--errors-only", "--idle-seconds", "120"])
        unit = service_unit(args, python="/tmp/venv/bin/python", script="/tmp/repo dir/autoresume.py")
        command = shlex.split(next(line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("ExecStart=")))
        self.assertEqual(command[:2], ["/tmp/venv/bin/python", "/tmp/repo dir/autoresume.py"])
        self.assertEqual(command[command.index("--socket") + 1], "/tmp/socket with spaces")
        self.assertEqual(command[command.index("--idle-seconds") + 1], "120.0")
        self.assertEqual(command.count("--thread"), 2)
        self.assertIn("--errors-only", command)
        self.assertNotIn("--install-service", command)
        self.assertNotIn("--dry-run", command)

    def test_systemd_expansion_characters_are_escaped(self):
        self.assertEqual(systemd_argument('/tmp/a %h $HOME " \\'),
                         '"/tmp/a %%h $$HOME \\" \\\\"')
        for value in ["a\nb", "a\rb", "a\x00b"]:
            with self.assertRaises(ValueError):
                systemd_argument(value)

    def test_oneshot_and_service_management_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--install-service", "--once"])


class ServiceLifecycleTests(unittest.TestCase):
    """Run the actual installer with an isolated systemctl executable and config."""

    def setUp(self):
        platform = patch("autoresume.sys.platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "config"
        self.log = root / "commands.jsonl"
        self.unit = self.config / "systemd/user" / SERVICE_NAME
        binary = root / "bin"
        binary.mkdir()
        systemctl = binary / "systemctl"
        systemctl.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with Path(os.environ['SYSTEMCTL_TEST_LOG']).open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('SYSTEMCTL_TEST_FAIL') in sys.argv[1:]:
    print('simulated failure', file=sys.stderr)
    sys.exit(1)
if 'status' in sys.argv:
    print('service status')
    sys.exit(int(os.environ.get('SYSTEMCTL_TEST_STATUS', '0')))
""")
        systemctl.chmod(0o755)
        env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": str(binary) + os.pathsep + os.environ.get("PATH", os.defpath),
            "SYSTEMCTL_TEST_LOG": str(self.log),
        })
        env.start()
        self.addCleanup(env.stop)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_install_enables_starts_and_can_update_settings(self):
        self.assertEqual(main(["--install-service", "--idle-seconds", "120"]), 0)
        self.assertIn(SERVICE_MARKER, self.unit.read_text())
        self.assertIn('"--idle-seconds" "120.0"', self.unit.read_text())
        self.assertEqual(self.commands(), [
            ["--user", "show", "--property=Version", "--value"],
            ["--user", "daemon-reload"], ["--user", "enable", SERVICE_NAME],
            ["--user", "restart", SERVICE_NAME], ["--user", "is-active", "--quiet", SERVICE_NAME],
        ])
        self.assertEqual(main(["--install-service", "--idle-seconds", "180"]), 0)
        self.assertIn('"--idle-seconds" "180.0"', self.unit.read_text())

    def test_remove_stops_disables_and_unlinks(self):
        main(["--install-service"])
        self.log.unlink()
        self.assertEqual(main(["--uninstall-service"]), 0)
        self.assertFalse(self.unit.exists())
        self.assertEqual(self.commands(), [
            ["--user", "show", "--property=Version", "--value"],
            ["--user", "disable", "--now", SERVICE_NAME], ["--user", "daemon-reload"],
        ])
        self.assertEqual(main(["--uninstall-service"]), 0)

    def test_dry_run_has_no_service_or_file_side_effects(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--install-service", "--dry-run"]), 0)
        self.assertIn("ExecStart=", output.getvalue())
        self.assertFalse(self.config.exists())
        self.assertEqual(self.commands(), [])
        self.assertEqual(main(["--uninstall-service", "--dry-run"]), 0)

    def test_unmanaged_unit_is_not_overwritten_or_removed(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("[Service]\nExecStart=/bin/true\n")
        for flag in ["--install-service", "--uninstall-service"]:
            self.assertEqual(main([flag]), 1)
            self.assertEqual(self.unit.read_text(), "[Service]\nExecStart=/bin/true\n")

    def test_unreachable_manager_does_not_write_unit(self):
        with patch.dict(os.environ, {"SYSTEMCTL_TEST_FAIL": "show"}):
            self.assertEqual(main(["--install-service"]), 1)
        self.assertFalse(self.unit.exists())

    def test_failed_start_is_reported_and_can_be_retried(self):
        with patch.dict(os.environ, {"SYSTEMCTL_TEST_FAIL": "restart"}):
            self.assertEqual(main(["--install-service"]), 1)
        self.assertTrue(self.unit.exists())
        self.assertEqual(main(["--install-service"]), 0)

    def test_status_preserves_inactive_exit_code(self):
        with redirect_stdout(io.StringIO()) as output:
            with patch.dict(os.environ, {"SYSTEMCTL_TEST_STATUS": "3"}):
                self.assertEqual(main(["--service-status"]), 3)
        self.assertIn("service status", output.getvalue())

    def test_unsupported_platform_has_clear_error(self):
        with patch("autoresume.sys.platform", "win32"):
            self.assertEqual(main(["--install-service"]), 1)
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.config.exists())


class LaunchdLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unit = self.root / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        self.log = self.root / "commands.jsonl"
        self.loaded = self.root / "loaded"
        binary = self.root / "bin"
        binary.mkdir()
        launchctl = binary / "launchctl"
        launchctl.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['LAUNCHCTL_TEST_LOG']).open('a') as f:
    f.write(json.dumps(args) + '\\n')
loaded = Path(os.environ['LAUNCHCTL_TEST_LOADED'])
if os.environ.get('LAUNCHCTL_TEST_FAIL') == args[0]:
    print('simulated failure', file=sys.stderr)
    sys.exit(1)
if args[0] == 'print':
    if args[1].endswith('codex-autoresume') and not loaded.exists():
        sys.exit(113)
    print('service status')
if args[0] == 'bootstrap':
    loaded.touch()
if args[0] == 'bootout':
    loaded.unlink()
""")
        launchctl.chmod(0o755)
        for context in [patch("autoresume.sys.platform", "darwin"),
                        patch("autoresume.Path.home", return_value=self.root),
                        patch.dict(os.environ, {
                            "PATH": str(binary) + os.pathsep + os.environ.get("PATH", os.defpath),
                            "LAUNCHCTL_TEST_LOG": str(self.log),
                            "LAUNCHCTL_TEST_LOADED": str(self.loaded),
                        })]:
            context.start()
            self.addCleanup(context.stop)

    def run_cli(self, *arguments):
        return main([*arguments, "--state-dir", str(self.root / "state")])

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_install_reinstall_and_remove(self):
        self.assertEqual(self.run_cli("--install-service", "--idle-seconds", "120"), 0)
        self.assertTrue(self.loaded.exists())
        unit = plistlib.loads(self.unit.read_bytes())
        self.assertEqual(unit["ManagedBy"], SERVICE_MARKER)
        self.assertTrue(unit["RunAtLoad"])
        self.assertTrue(unit["KeepAlive"])
        command = unit["ProgramArguments"]
        self.assertEqual(command[command.index("--idle-seconds") + 1], "120.0")
        self.assertEqual(self.unit.stat().st_mode & 0o777, 0o600)
        target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
        self.assertIn(["enable", target], self.commands())
        self.assertEqual(self.run_cli("--install-service", "--idle-seconds", "180"), 0)
        self.assertIn(["bootout", target], self.commands())
        self.assertIn("180.0", plistlib.loads(self.unit.read_bytes())["ProgramArguments"])
        self.assertEqual(self.run_cli("--uninstall-service"), 0)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.loaded.exists())
        self.assertEqual(self.run_cli("--uninstall-service"), 0)

    def test_dry_run_has_no_side_effects(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.run_cli("--install-service", "--dry-run"), 0)
        self.assertEqual(plistlib.loads(output.getvalue().encode())["Label"], LAUNCHD_LABEL)
        self.assertEqual(self.run_cli("--uninstall-service", "--dry-run"), 0)
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.unit.parent.exists())
        self.assertFalse((self.root / "state").exists())

    def test_unmanaged_plist_and_symlink_are_preserved(self):
        self.unit.parent.mkdir(parents=True)
        content = plistlib.dumps({"Label": LAUNCHD_LABEL})
        self.unit.write_bytes(content)
        for flag in ["--install-service", "--uninstall-service"]:
            self.assertEqual(self.run_cli(flag), 1)
            self.assertEqual(self.unit.read_bytes(), content)
        self.unit.unlink()
        self.unit.symlink_to(self.root / "missing")
        self.assertEqual(self.run_cli("--install-service"), 1)
        self.assertTrue(self.unit.is_symlink())

    def test_unreachable_manager_and_failed_bootstrap(self):
        with patch.dict(os.environ, {"LAUNCHCTL_TEST_FAIL": "print"}):
            self.assertEqual(self.run_cli("--install-service"), 1)
        self.assertFalse(self.unit.exists())
        with patch.dict(os.environ, {"LAUNCHCTL_TEST_FAIL": "bootstrap"}):
            self.assertEqual(self.run_cli("--install-service"), 1)
        self.assertTrue(self.unit.exists())
        self.assertEqual(self.run_cli("--install-service"), 0)

    def test_failed_stop_preserves_plist(self):
        self.assertEqual(self.run_cli("--install-service"), 0)
        with patch.dict(os.environ, {"LAUNCHCTL_TEST_FAIL": "bootout"}):
            self.assertEqual(self.run_cli("--uninstall-service"), 1)
        self.assertTrue(self.unit.exists())
        self.assertTrue(self.loaded.exists())

    def test_status_returns_missing_service_exit_code(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cli("--service-status"), 113)
            self.assertEqual(self.run_cli("--install-service"), 0)
            self.assertEqual(self.run_cli("--service-status"), 0)

    def test_plist_preserves_literal_paths_and_selected_options(self):
        args = parse_args(["--errors-only", "--socket", '/tmp/a %h $HOME " &',
                           "--thread", "t1", "--verbose"])
        unit = plistlib.loads(launchd_plist(args, python="/tmp/venv/bin/python",
                                          script="/tmp/repo dir/autoresume.py"))
        command = unit["ProgramArguments"]
        self.assertEqual(command[:2], ["/tmp/venv/bin/python", "/tmp/repo dir/autoresume.py"])
        self.assertEqual(command[command.index("--socket") + 1], '/tmp/a %h $HOME " &')
        self.assertIn("--errors-only", command)
        self.assertIn("--verbose", command)
        self.assertNotIn("--install-service", command)


if __name__ == "__main__":
    unittest.main()
