"""Regressions confirmed during the October code audit."""

import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings

from . import backups, maintenance
from .appointments import validate_slot
from .backup_guard import data_gate
from .models import Broadcast
from .recurrence import ROME
from .test_scheduler import CalendarTests


class AuditTests(CalendarTests):
    def test_spring_slot_uses_elapsed_audio_duration_across_clock_change(self):
        start = datetime(2027, 3, 28, 1, 30, tzinfo=ROME)
        Broadcast.objects.create(
            asset=self.asset, scheduled_at=datetime(2027, 3, 28, 4, 0, tzinfo=ROME)
        )
        with self.assertRaises(ValidationError):
            validate_slot(start, 7200)

    def test_autumn_slot_does_not_reserve_an_extra_hour(self):
        start = datetime(2027, 10, 31, 1, 30, tzinfo=ROME)
        Broadcast.objects.create(
            asset=self.asset, scheduled_at=datetime(2027, 10, 31, 3, 0, tzinfo=ROME)
        )
        validate_slot(start, 7200)

    def test_backup_refuses_missing_source_instead_of_creating_incomplete_archive(self):
        with override_settings(BACKUP_CODE_ROOT=Path(self.temp.name) / "missing"):
            # Supply a real minimal config, then verify the source directory guard.
            source = Path(self.temp.name) / "config"
            for name in [
                ".env",
                ".env.dashboard",
                "compose.yaml",
                "compose.dashboard.yaml",
                "config/radio.liq",
                "config/icecast.xml",
                "icecast/Dockerfile",
            ]:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            with (
                override_settings(BACKUP_CONFIG_ROOT=source),
                self.assertRaises(backups.BackupError),
            ):
                backups.config_entries()

    def test_expansion_waits_for_backup_and_failure_does_not_escape(self):
        with (
            patch.object(maintenance, "close_old_connections"),
            patch.object(maintenance.connections, "close_all"),
            patch.object(maintenance, "extend_series") as expand,
        ):
            with data_gate(exclusive=True):
                self.assertFalse(maintenance.maintenance_pass())
                expand.assert_not_called()
            expand.side_effect = RuntimeError("fixture maintenance error")
            with self.assertLogs("library.maintenance", level="ERROR"):
                self.assertFalse(maintenance.maintenance_pass())

    def test_slow_expansion_runs_in_its_own_thread(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(2)

        with (
            patch.object(maintenance, "close_old_connections"),
            patch.object(maintenance.connections, "close_all"),
            patch.object(maintenance, "extend_series", side_effect=slow),
        ):
            thread = maintenance.start_maintenance(stop)
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(thread.is_alive())
                # Dispatch still advances while the calendar expansion is blocked.
                from .scheduler import tick

                self.assertTrue(tick(self.now))
            finally:
                stop.set()
                release.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


def load_tests(loader, suite, pattern):
    import unittest

    return unittest.TestSuite(
        AuditTests(name) for name in AuditTests.__dict__ if name.startswith("test_")
    )
