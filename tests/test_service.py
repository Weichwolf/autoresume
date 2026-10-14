import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from autoresume import SERVICE_MARKER, SERVICE_NAME, main, parse_args, service_unit, systemd_argument


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


@unittest.skipUnless(sys.platform.startswith("linux"), "systemd management is Linux-only")
class ServiceLifecycleTests(unittest.TestCase):
    """Run the actual installer with an isolated systemctl executable and config."""

    def setUp(self):
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

    def test_macos_service_management_has_clear_error(self):
        with patch("autoresume.sys.platform", "darwin"):
            self.assertEqual(main(["--install-service"]), 1)
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
