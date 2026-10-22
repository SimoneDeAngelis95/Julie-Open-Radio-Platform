"""Calendar outcomes, crash recovery and user-visible validation."""

import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .engine import EngineError, EngineState
from .models import Asset, Broadcast
from .scheduler import tick
from .storage import data_path, save_upload


class CalendarTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.temp.name))
        self.override.enable()
        self.user = get_user_model().objects.create_user(
            "editor", password="test", is_staff=True
        )
        self.client.force_login(self.user)
        self.asset = save_upload(
            SimpleUploadedFile("episode.mp3", b"episode"), "podcasts"
        )[0]
        self.asset.status = Asset.Status.READY
        self.asset.duration_seconds = 120
        self.asset.prepared_path = "ready/podcast.flac"
        data_path("ready").mkdir()
        data_path(self.asset.prepared_path).write_bytes(b"prepared")
        self.asset.save()
        self.now = timezone.now()
        self.state = EngineState("boot1", False, "", {})
        self.poll = patch("library.scheduler.engine.get_state", return_value=self.state)
        self.poller = self.poll.start()
        self.send = patch("library.scheduler.engine.dispatch", return_value="accepted")
        self.sender = self.send.start()

    def tearDown(self):
        self.poll.stop()
        self.send.stop()
        self.override.disable()
        self.temp.cleanup()

    def job(self, **kwargs):
        return Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now, **kwargs
        )

    def test_dispatch_is_once_and_reconciles_real_start_and_end(self):
        job = self.job()
        tick(self.now)
        tick(self.now + timedelta(seconds=1))
        self.assertEqual(self.sender.call_count, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "dispatching")
        started = self.now.timestamp() + 3
        ended = started + 120
        self.poller.return_value = EngineState(
            "boot1", False, "", {str(job.id): {"started": started, "completed": ended}}
        )
        tick(self.now + timedelta(seconds=124))
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.started_at.timestamp(), started)
        self.assertEqual(job.ended_at.timestamp(), ended)

    def test_live_and_busy_skip_without_dispatch(self):
        for live, current in [(True, ""), (False, "someone")]:
            job = self.job()
            self.poller.return_value = EngineState("boot1", live, current, {})
            tick(self.now)
            job.refresh_from_db()
            self.assertEqual(job.status, "skipped")
        self.sender.assert_not_called()

    def test_live_racing_dispatch_is_recorded_as_skipped(self):
        job = self.job()
        tick(self.now)
        self.poller.return_value = EngineState(
            "boot1", True, "", {str(job.id): {"live": self.now.timestamp()}}
        )
        tick(self.now)
        job.refresh_from_db()
        self.assertEqual(job.status, "skipped")

    def test_interrupted_and_restart_are_not_replayed(self):
        for restarted in [False, True]:
            job = self.job(
                status="playing",
                dispatched_at=self.now,
                started_at=self.now,
                engine_session="boot1",
            )
            events = (
                {}
                if restarted
                else {str(job.id): {"interrupted": self.now.timestamp()}}
            )
            self.poller.return_value = EngineState(
                "boot2" if restarted else "boot1", True, "", events
            )
            tick(self.now)
            job.refresh_from_db()
            self.assertEqual(job.status, "interrupted")
        self.sender.assert_not_called()

    def test_uncertain_dispatch_is_not_retried_and_times_out_safely(self):
        job = self.job()
        self.sender.side_effect = EngineError("lost reply")
        tick(self.now)
        with patch("library.scheduler.engine.command", return_value="aborted") as abort:
            tick(self.now + timedelta(seconds=31))
            abort.assert_called_once_with(f"julie.abort {job.id}")
        job.refresh_from_db()
        self.assertEqual(job.status, "error")
        self.assertEqual(self.sender.call_count, 1)

    def test_downtime_does_not_replay_past_appointments(self):
        job = self.job()
        self.poller.side_effect = EngineError("offline")
        tick(self.now + timedelta(seconds=31))
        job.refresh_from_db()
        self.assertEqual(job.status, "skipped")
        self.sender.assert_not_called()

    def test_trashed_disabled_or_missing_audio_cannot_air(self):
        for field, value in [
            ("trashed_at", self.now),
            ("is_active", False),
            ("prepared_path", "missing.flac"),
        ]:
            job = self.job()
            original = getattr(self.asset, field)
            setattr(self.asset, field, value)
            self.asset.save()
            tick(self.now)
            job.refresh_from_db()
            self.assertEqual(job.status, "skipped")
            setattr(self.asset, field, original)
            self.asset.save()
        self.sender.assert_not_called()

    def test_simultaneous_due_appointments_only_dispatch_one(self):
        self.job()
        self.job()
        tick(self.now)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(Broadcast.objects.filter(status="skipped").count(), 1)

    def payload(self, moment):
        return {
            "asset": self.asset.id,
            "scheduled_at": timezone.localtime(moment).strftime("%Y-%m-%dT%H:%M"),
        }

    def test_create_overlap_validation_cancel_and_history(self):
        start = self.now + timedelta(days=1)
        self.assertEqual(
            self.client.post("/calendar/new/", self.payload(start)).status_code, 302
        )
        job = Broadcast.objects.get()
        response = self.client.post(
            "/calendar/new/", self.payload(start + timedelta(seconds=60))
        )
        self.assertContains(response, "sovrappone")
        self.assertEqual(Broadcast.objects.count(), 1)
        self.assertContains(self.client.get("/calendar/"), "episode")
        self.assertEqual(
            self.client.get(f"/calendar/{job.id}/cancel/").status_code, 405
        )
        self.client.post(f"/calendar/{job.id}/cancel/")
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertContains(self.client.get("/calendar/"), "Annullato")

    def test_form_rejects_past_unavailable_and_dst_ambiguity(self):
        self.assertContains(
            self.client.post(
                "/calendar/new/", self.payload(self.now - timedelta(days=1))
            ),
            "futuro",
        )
        response = self.client.post(
            "/calendar/new/",
            {"asset": self.asset.id, "scheduled_at": "2026-10-25T02:30"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Broadcast.objects.count(), 0)
        self.asset.is_active = False
        self.asset.save()
        self.client.post("/calendar/new/", self.payload(self.now + timedelta(days=1)))
        self.assertEqual(Broadcast.objects.count(), 0)

    def test_auth_csrf_and_cannot_cancel_running(self):
        job = self.job(status="playing", started_at=self.now)
        self.client.post(f"/calendar/{job.id}/cancel/")
        job.refresh_from_db()
        self.assertEqual(job.status, "playing")
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(
            csrf.post(
                "/calendar/new/", self.payload(self.now + timedelta(days=1))
            ).status_code,
            403,
        )
        self.client.logout()
        self.assertEqual(self.client.get("/calendar/").status_code, 302)

    def test_missing_end_cannot_leave_calendar_busy_forever(self):
        job = self.job(
            status="playing",
            started_at=self.now,
            dispatched_at=self.now,
            engine_session="boot1",
        )
        self.poller.return_value = EngineState("boot1", False, str(job.id), {})
        with patch("library.scheduler.engine.command", return_value="aborted") as abort:
            tick(self.now + timedelta(seconds=151))
            abort.assert_called_once_with(f"julie.abort {job.id}")
        job.refresh_from_db()
        self.assertEqual(job.status, "interrupted")

    def test_protocol_preserves_event_history_and_trusted_uri(self):
        from . import engine

        self.poll.stop()
        self.send.stop()
        job = self.job()
        self.asset.title = "Podcast | parte 1\nTitolo"
        self.asset.save()
        response = f"JULIE1|boot1|false|{job.id}\n{job.id}|accepted|100\n{job.id}|started|103\n{job.id}|completed|223"
        with patch("library.engine.command", return_value=response):
            state = engine.get_state()
        self.assertEqual(state.events[str(job.id)]["started"], 103)
        self.assertEqual(state.events[str(job.id)]["completed"], 223)
        with patch("library.engine.command", return_value="accepted") as send:
            engine.dispatch(job)
        value = send.call_args.args[0]
        self.assertIn('julie_kind="podcast"', value)
        self.assertNotIn("\n", value)
