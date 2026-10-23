# SPDX-License-Identifier: GPL-3.0-only
"""Use temporary Git repositories and simulated Docker; never contact a station."""

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

MODULE = Path(__file__).with_name("update.py")
spec = importlib.util.spec_from_file_location("update", MODULE)
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class SimulatedDocker(update.Updater):
    def __init__(self, root, release):
        super().__init__(root, release, backup=True)
        self.calls = []
        self.fail_at = None
        self.backup_info = None
        self.remaining_container = False
        self.config = {
            "services": {
                "dashboard": {
                    "image": "julie-dashboard:local",
                    "volumes": [
                        {
                            "type": "bind",
                            "target": "/data",
                            "source": str(root / "audio/library"),
                        }
                    ],
                },
                "dashboard-backup": {
                    "volumes": [
                        {
                            "type": "bind",
                            "target": "/backups",
                            "source": str(root / "backups/exports"),
                        }
                    ]
                },
            }
        }

    def run(self, args, **options):
        if args[0] in ("docker", "sudo"):
            return ""
        return super().run(args, **options)

    def docker(self, *args):
        self.calls.append(("docker", *args))
        if self.remaining_container and args[:2] == ("ps", "-aq"):
            return "oneoff-container"
        if args[:2] == ("image", "inspect"):
            return "sha256:old-image"
        return ""

    def compose(self, *args, **options):
        self.calls.append(args)
        if self.fail_at == "health" and args[-1] == update.HEALTH_CODE:
            raise update.UpdateError("simulated health failure")
        if self.fail_at == "busy" and args[-1] == update.BUSY_CODE:
            raise update.UpdateError("maintenance already active")
        if self.fail_at == "build" and args[0] == "build":
            raise update.UpdateError("simulated build failure")
        if self.fail_at == "migrate" and "migrate" in args:
            raise update.UpdateError("simulated migration failure")
        if args[0] == "config":
            return json.dumps(self.config)
        if args[0] == "ps":
            return "\n".join(update.APPS + ["dashboard-db", "liquidsoap", "icecast"])
        if args[0] == "images":
            return "sha256:old-image"
        if args[-1] == update.BUSY_CODE:
            return "JULIE_UPDATE_IDLE"
        if args[-1] == update.BACKUP_CODE:
            if self.fail_at == "config_changed":
                self.config["services"]["dashboard"]["environment"] = {"CHANGED": "yes"}
            if self.fail_at == "backup":
                raise update.UpdateError("simulated backup failure")
            return "JULIE_UPDATE_BACKUP " + json.dumps(self.backup_info)
        return ""


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Update Tests")
        self.git("config", "user.email", "tests@example.invalid")
        (self.root / ".gitignore").write_text(
            ".env*\nconfig/icecast.xml\naudio/\nbackups/\n.update.lock\n"
        )
        (self.root / "VERSION").write_text("0.1.0\n")
        (self.root / "config").mkdir()
        (self.root / "config/radio.liq").write_text("old engine\n")
        (self.root / "compose.yaml").write_text("old topology\n")
        (self.root / "README.md").write_text("old docs\n")
        self.git("add", ".")
        self.git("commit", "-m", "old version")
        self.old = self.git("rev-parse", "HEAD")
        (self.root / "VERSION").write_text("0.1.1\n")
        (self.root / "README.md").write_text("new docs\n")
        self.git("add", ".")
        self.git("commit", "-m", "new version")
        self.target = self.git("rev-parse", "HEAD")
        self.git("tag", "v0.1.1")
        self.git("checkout", "-b", "installed", self.old)
        self.git("remote", "add", "origin", str(self.root))
        (self.root / ".env").write_text("KEEP=local\n")
        (self.root / ".env.dashboard").write_text("KEEP=database-credentials\n")
        (self.root / "config/icecast.xml").write_text("KEEP=stream-credentials\n")
        (self.root / "audio/library/originals").mkdir(parents=True)
        (self.root / "audio/library/originals/track.flac").write_bytes(
            b"original audio"
        )
        (self.root / "backups/exports").mkdir(parents=True)
        self.updater = SimulatedDocker(self.root, "v0.1.1")
        self.backup = self.root / "backups/exports/julie-full-test.zip"
        with zipfile.ZipFile(self.backup, "w") as archive:
            for name in [
                "database.dump",
                "audio.tar",
                "configuration.tar.gz",
                "README.txt",
            ]:
                archive.writestr(name, b"fixture")
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": "julie-backup-v1",
                        "kind": "full",
                        "coordinated": True,
                        "id": "backup-test",
                    }
                ),
            )
        self.updater.backup_info = {
            "name": self.backup.name,
            "id": "backup-test",
            "size": self.backup.stat().st_size,
            "sha256": hashlib.sha256(self.backup.read_bytes()).hexdigest(),
        }

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True, stderr=subprocess.DEVNULL
        ).strip()

    def assert_data_intact(self):
        self.assertEqual(
            (self.root / "audio/library/originals/track.flac").read_bytes(),
            b"original audio",
        )
        self.assertEqual(
            (self.root / ".env.dashboard").read_text(), "KEEP=database-credentials\n"
        )
        self.assertEqual(
            (self.root / "config/icecast.xml").read_text(), "KEEP=stream-credentials\n"
        )
        for call in self.updater.calls:
            self.assertNotIn("down", call)
            if call[0] == "stop":
                self.assertEqual(list(call[1:]), update.APPS)

    def test_success_retains_verified_backup_and_never_restarts_audio_or_database(self):
        self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        checkpoint = self.updater.checkpoint
        self.assertEqual(
            (checkpoint / "full-backup.zip").read_bytes(), self.backup.read_bytes()
        )
        self.assertEqual(
            json.loads((checkpoint / "state.json").read_text())["phase"], "completed"
        )
        self.assertFalse((self.root / "backups/updates/INCOMPLETE").exists())
        backup_index = next(
            i for i, c in enumerate(self.updater.calls) if c[-1] == update.BACKUP_CODE
        )
        build_index = next(
            i for i, c in enumerate(self.updater.calls) if c[0] == "build"
        )
        migration_index = next(
            i for i, c in enumerate(self.updater.calls) if "migrate" in c
        )
        self.assertLess(backup_index, build_index)
        self.assertLess(build_index, migration_index)
        self.assert_data_intact()

    def test_backup_failure_prevents_checkout_build_and_migration(self):
        self.updater.fail_at = "backup"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertFalse(
            any(c[0] == "build" or "migrate" in c for c in self.updater.calls)
        )
        self.assertFalse((self.root / "backups/updates/INCOMPLETE").exists())
        self.assert_data_intact()

    def test_corrupted_backup_prevents_migration(self):
        self.updater.backup_info["sha256"] = "0" * 64
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertFalse(self.updater.migration_started)
        self.assert_data_intact()

    def test_build_failure_restores_old_code_and_old_image(self):
        self.updater.fail_at = "build"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertEqual(self.git("branch", "--show-current"), "installed")
        self.assertIn(
            (
                "docker",
                "image",
                "tag",
                self.updater.recovery_image,
                "julie-dashboard:local",
            ),
            self.updater.calls,
        )
        self.assertFalse(self.updater.migration_started)
        self.assert_data_intact()

    def test_migration_failure_keeps_new_code_and_backup_without_database_rollback(
        self,
    ):
        self.updater.fail_at = "migrate"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        self.assertTrue(self.updater.migration_started)
        self.assertTrue((self.root / "backups/updates/INCOMPLETE").exists())
        self.assertEqual(
            json.loads((self.updater.checkpoint / "state.json").read_text())["phase"],
            "manual_recovery_required",
        )
        self.assertFalse(any(c[0] == "up" for c in self.updater.calls))
        self.assert_data_intact()

    def test_dirty_repository_is_never_overwritten(self):
        (self.root / "README.md").write_text("user edits")
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(self.updater.calls)
        self.assertEqual((self.root / "README.md").read_text(), "user edits")

    def test_engine_release_requires_manual_migration(self):
        self.git("checkout", "main")
        (self.root / "config/radio.liq").write_text("different engine")
        self.git("add", ".")
        self.git("commit", "-m", "engine change")
        self.git("tag", "-f", "v0.1.1")
        self.git("checkout", "installed")
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(self.updater.calls)
        self.assert_data_intact()

    def test_target_cannot_track_audio_directory(self):
        self.git("checkout", "main")
        self.git("add", "-f", "audio/library/originals/track.flac")
        self.git("commit", "-m", "bad release")
        self.git("tag", "-f", "v0.1.1")
        self.git("checkout", "installed")
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(self.updater.calls)

    def test_previous_incomplete_update_blocks_new_attempt(self):
        (self.root / "backups/updates").mkdir()
        (self.root / "backups/updates/INCOMPLETE").write_text("previous attempt")
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(self.updater.calls)

    def test_existing_target_is_a_noop(self):
        self.git("checkout", "--detach", self.target)
        self.updater.execute()
        self.assertFalse(self.updater.calls)
        self.assertIsNone(self.updater.checkpoint)

    def test_low_space_is_refused_even_without_backup(self):
        self.updater.backup_enabled = False
        with patch.object(update.shutil, "disk_usage") as usage:
            usage.return_value.free = 0
            with self.assertRaises(update.UpdateError):
                self.updater.execute()
        self.assertFalse(any(c[0] == "stop" for c in self.updater.calls))
        self.assert_data_intact()

    def test_main_without_backup_preserves_data(self):
        self.updater.branch = "main"
        self.updater.release = "main"
        self.updater.backup_enabled = False
        self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        self.assertFalse((self.updater.checkpoint / "full-backup.zip").exists())
        state = json.loads((self.updater.checkpoint / "state.json").read_text())
        self.assertFalse(state["backup_requested"])
        self.assertEqual(state["phase"], "completed")
        self.assertFalse(any(update.BACKUP_CODE in call for call in self.updater.calls))
        self.assert_data_intact()

    def test_dev_with_backup(self):
        self.git("branch", "dev", self.target)
        self.updater.branch = "dev"
        self.updater.release = "dev"
        self.updater.execute()
        self.assertTrue((self.updater.checkpoint / "full-backup.zip").is_file())
        self.assert_data_intact()

    def test_missing_branch_does_not_use_stale_remote_ref(self):
        self.git("update-ref", "refs/remotes/origin/dev", self.target)
        self.updater.branch = "dev"
        self.updater.release = "dev"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(any(c[0] == "stop" for c in self.updater.calls))

    def test_failure_without_backup_before_migration_recovers_old_code(self):
        self.updater.backup_enabled = False
        self.updater.fail_at = "build"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assert_data_intact()

    def test_failure_without_backup_after_migration_requires_review(self):
        self.updater.backup_enabled = False
        self.updater.fail_at = "health"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        self.assertTrue((self.root / "backups/updates/INCOMPLETE").exists())
        state = json.loads((self.updater.checkpoint / "state.json").read_text())
        self.assertEqual(state["phase"], "manual_recovery_required")
        self.assertFalse(state["backup_requested"])
        self.assert_data_intact()

    def test_cli_target_and_backup_options(self):
        parser = update.argument_parser()
        args = parser.parse_args([])
        self.assertFalse(args.backup)
        self.assertFalse(args.dev)
        self.assertIsNone(args.tag)
        args = parser.parse_args(["-d", "-b"])
        self.assertTrue(args.dev and args.backup)
        args = parser.parse_args(["-t", "v0.1.1", "-b"])
        self.assertEqual(args.tag, "v0.1.1")
        self.assertTrue(args.backup)
        with self.assertRaises(SystemExit):
            parser.parse_args(["-d", "-t", "v0.1.1"])

    def test_branch_name_is_not_accepted(self):
        self.updater.release = "main"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(self.updater.calls)

    def test_busy_maintenance_is_refused_before_stopping_services(self):
        self.updater.fail_at = "busy"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(any(c[0] == "stop" for c in self.updater.calls))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assert_data_intact()

    def test_health_failure_retains_new_schema_state_for_manual_recovery(self):
        self.updater.fail_at = "health"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertTrue(self.updater.migration_started)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)
        self.assertEqual(self.updater.calls[-1][0], "stop")
        self.assertTrue((self.root / "backups/updates/INCOMPLETE").exists())
        self.assert_data_intact()

    def test_failed_oneoff_is_stopped_before_resuming_old_apps(self):
        self.updater.fail_at = "backup"
        self.updater.remaining_container = True
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        stops = [
            i for i, c in enumerate(self.updater.calls) if c[:2] == ("docker", "stop")
        ]
        resumes = [i for i, c in enumerate(self.updater.calls) if c[0] == "up"]
        self.assertEqual(len(stops), 1)
        self.assertLess(stops[0], resumes[0])
        self.assert_data_intact()

    def test_downgrade_is_refused(self):
        self.git("tag", "v0.1.0", self.old)
        self.git("checkout", "--detach", self.target)
        self.updater.release = "v0.1.0"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(self.updater.calls)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.target)

    def test_changed_compose_settings_prevent_automatic_restart(self):
        self.updater.fail_at = "config_changed"
        with self.assertRaises(update.UpdateError):
            self.updater.execute()
        self.assertFalse(self.updater.migration_started)
        self.assertFalse(any(c[0] == "up" for c in self.updater.calls))
        self.assertTrue((self.root / "backups/updates/INCOMPLETE").exists())
        self.assert_data_intact()

    def test_private_backup_umask_keeps_checked_out_source_readable(self):
        for name in (".env", ".env.dashboard", "config/icecast.xml"):
            (self.root / name).chmod(0o600)
        previous = os.umask(0o077)
        try:
            self.updater.execute()
            self.assertEqual((self.root / "README.md").stat().st_mode & 0o777, 0o644)
            for name in (".env", ".env.dashboard", "config/icecast.xml"):
                self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)
            for name in ("full-backup.zip", "local-config.tar.gz", "source.bundle"):
                self.assertEqual(
                    (self.updater.checkpoint / name).stat().st_mode & 0o777, 0o600
                )
            probe = self.updater.checkpoint / "private-probe"
            probe.touch()
            self.assertEqual(probe.stat().st_mode & 0o777, 0o600)
        finally:
            os.umask(previous)


if __name__ == "__main__":
    unittest.main()
