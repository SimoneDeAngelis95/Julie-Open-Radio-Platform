"""Temporary suspension must not survive an engine boot or replay missed slots."""

import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from . import transmission
from .engine import EngineError, EngineState
from .models import Asset, Broadcast
from .scheduler import tick
from .storage import data_path


class TransmissionTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = override_settings(MEDIA_ROOT=Path(directory.name))
        override.enable()
        self.addCleanup(override.disable)
        self.state = EngineState("boot-one", False, "", {})
        self.user = get_user_model().objects.create_superuser(
            "operator", password="test"
        )
        self.client.force_login(self.user)
        self.now = timezone.now()
        self.asset = Asset.objects.create(
            category="podcasts",
            title="Episode",
            original_name="test.wav",
            checksum="a" * 64,
            original_path="originals/test.wav",
            status="ready",
            prepared_path="ready/test.flac",
            duration_seconds=60,
            size_bytes=8,
        )
        path = data_path(self.asset.prepared_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"prepared")
        self.state_mock = patch("library.engine.get_state", return_value=self.state)
        self.state_mock.start()
        self.addCleanup(self.state_mock.stop)

    def intent(self, session="boot-one"):
        path = transmission.marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session": session}))

    def test_suspend_stops_output_and_interrupts_podcast_without_publishing_replay(
        self,
    ):
        job = Broadcast.objects.create(
            asset=self.asset,
            scheduled_at=self.now,
            status="playing",
            engine_session="boot-one",
            started_at=self.now,
        )
        state = EngineState("boot-one", False, str(job.id), {})
        with (
            patch("library.engine.get_state", return_value=state),
            patch(
                "library.engine.command",
                side_effect=lambda value: "off" if value.endswith(".status") else "OK",
            ) as command,
        ):
            transmission.change("suspend")
        self.assertTrue(transmission.marker().exists())
        self.assertIn(
            ("output.icecast.stop",), [call.args for call in command.call_args_list]
        )
        job.refresh_from_db()
        self.assertEqual(job.status, "interrupted")
        self.asset.refresh_from_db()
        self.assertIsNone(self.asset.first_completed_at)

    def test_paused_tick_skips_due_podcast_and_never_dispatches(self):
        self.intent()
        job = Broadcast.objects.create(asset=self.asset, scheduled_at=self.now)
        with patch("library.engine.dispatch") as dispatch:
            self.assertTrue(tick(self.now))
        dispatch.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "skipped")
        self.assertIn("sospesa", job.outcome)

    def test_new_engine_boot_clears_pause_and_dispatches_future_slots(self):
        self.intent()
        due = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now - timedelta(seconds=1)
        )
        with (
            patch(
                "library.engine.get_state",
                return_value=EngineState("new-boot", False, "", {}),
            ),
            patch("library.engine.dispatch") as dispatch,
        ):
            tick(self.now)
            future = Broadcast.objects.create(
                asset=self.asset, scheduled_at=self.now + timedelta(seconds=10)
            )
            tick(self.now + timedelta(seconds=10))
        self.assertFalse(transmission.marker().exists())
        due.refresh_from_db()
        self.assertEqual(due.status, "skipped")
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[0].id, future.id)

    def test_resume_skips_recent_slots_even_inside_grace_and_keeps_future(self):
        self.intent()
        due = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now - timedelta(seconds=1)
        )
        future = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=1)
        )
        with patch(
            "library.engine.command",
            side_effect=lambda value: "on" if value.endswith(".status") else "OK",
        ):
            transmission.change("resume")
        self.assertFalse(transmission.marker().exists())
        due.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(due.status, "skipped")
        self.assertEqual(future.status, "scheduled")

    def test_output_confirmation_waits_for_engine_clock(self):
        for expected, replies in [("off", ["on", "on", "off"]), ("on", ["off", "on"])]:
            with patch("library.engine.command", side_effect=replies), patch("library.transmission.time.sleep") as sleep:
                self.assertTrue(transmission.output_confirmed(expected))
                self.assertEqual(sleep.call_count, len(replies) - 1)

    def test_output_confirmation_is_bounded_and_rejects_unknown_status(self):
        with patch("library.engine.command", return_value="on") as command, patch("library.transmission.time.sleep"):
            self.assertFalse(transmission.output_confirmed("off"))
            self.assertEqual(command.call_count, 31)
        with patch("library.engine.command", return_value="unknown"), patch("library.transmission.time.sleep") as sleep:
            self.assertFalse(transmission.output_confirmed("off"))
            sleep.assert_not_called()

    def test_uncertain_stop_or_start_keeps_pause_and_blocks_dispatch(self):
        with (
            patch("library.engine.command", side_effect=EngineError("offline")),
            self.assertRaises(EngineError),
        ):
            transmission.change("suspend")
        self.assertTrue(transmission.marker().exists())
        with (
            patch("library.engine.command", return_value="off"),
            self.assertRaises(transmission.TransmissionError),
        ):
            transmission.change("resume")
        self.assertTrue(transmission.marker().exists())

    def test_maintenance_stop_cannot_be_bypassed(self):
        path = data_path("locks/radio-stopped")
        path.parent.mkdir(parents=True)
        path.touch()
        with patch("library.engine.command") as command:
            for action in ("suspend", "resume"):
                with self.assertRaises(transmission.TransmissionError):
                    transmission.change(action)
        command.assert_not_called()

    def test_controls_require_superuser_post_confirmation_and_csrf(self):
        url = "/maintenance/status/transmission/"
        self.assertEqual(self.client.get(url).status_code, 405)
        with patch("library.transmission.change") as change:
            self.client.post(url, {"action": "suspend"})
            change.assert_not_called()
            self.client.post(url, {"action": "suspend", "confirm": "yes"})
            change.assert_called_once_with("suspend")
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post(url, {"action": "suspend", "confirm": "yes"}).status_code, 403
        )
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(
            self.client.post(url, {"action": "suspend", "confirm": "yes"}).status_code,
            302,
        )

    def test_real_post_can_suspend_while_audio_worker_holds_shared_gate(self):
        from .backup_guard import data_gate

        with (
            data_gate() as worker_gate,
            patch(
                "library.engine.command",
                side_effect=lambda command: (
                    "off" if command.endswith(".status") else "OK"
                ),
            ),
        ):
            self.assertIsNotNone(worker_gate)
            response = self.client.post(
                "/maintenance/status/transmission/",
                {"action": "suspend", "confirm": "yes"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(transmission.marker().exists())

    def test_control_commands_and_scheduler_are_serialized(self):
        with transmission.control_gate(exclusive=True):
            self.assertFalse(tick(self.now))
            with self.assertRaises(transmission.TransmissionError):
                transmission.change("suspend")

    def test_get_only_renders_controls_and_does_not_touch_engine(self):
        with patch("library.engine.command") as command:
            self.assertContains(
                self.client.get("/maintenance/status/"), "Sospendi la trasmissione"
            )
            self.intent()
            self.assertContains(
                self.client.get("/maintenance/status/"), "Riprendi la trasmissione"
            )
        command.assert_not_called()
