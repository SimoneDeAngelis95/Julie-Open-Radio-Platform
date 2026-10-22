"""Upload plus booking, repeated episodes and safe changes to future appointments."""

from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from . import test_scheduler
from .models import Asset, Broadcast
from .scheduler import finish, tick


class UploadCalendarTests(TestCase):
    setUp = test_scheduler.CalendarTests.setUp
    tearDown = test_scheduler.CalendarTests.tearDown
    job = test_scheduler.CalendarTests.job
    payload = test_scheduler.CalendarTests.payload

    def upload(self, **overrides):
        values = {
            "category": "podcasts",
            "schedule": "on",
            "scheduled_at": timezone.localtime(self.now + timedelta(days=2)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "files": SimpleUploadedFile("nuova-puntata.mp3", b"new episode"),
        }
        values.update(overrides)
        return self.client.post(
            "/upload/", values, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

    def test_upload_creates_one_asset_and_waiting_appointment_then_becomes_ready(self):
        response = self.upload()
        self.assertEqual(response.json(), {"redirect": "/calendar/"})
        job = Broadcast.objects.select_related("asset").get()
        self.assertEqual(job.asset.status, "pending")
        self.assertEqual(job.calendar_label, "In preparazione")
        self.assertContains(self.client.get("/calendar/"), "In preparazione")
        job.asset.status = "ready"
        job.asset.save()
        self.assertEqual(job.calendar_label, "Programmato")
        self.assertEqual(Broadcast.objects.count(), 1)

    def test_upload_rejects_music_multiple_files_missing_date_and_past(self):
        for overrides in [
            {"category": "music"},
            {
                "files": [
                    SimpleUploadedFile("a.mp3", b"a"),
                    SimpleUploadedFile("b.mp3", b"b"),
                ]
            },
            {"scheduled_at": ""},
            {
                "scheduled_at": timezone.localtime(
                    self.now - timedelta(days=1)
                ).strftime("%Y-%m-%dT%H:%M")
            },
        ]:
            response = self.upload(**overrides)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(Broadcast.objects.count(), 0)
        self.assertEqual(Asset.objects.count(), 1)

    def test_plain_upload_still_accepts_multiple_files_without_booking(self):
        response = self.upload(
            schedule="",
            files=[
                SimpleUploadedFile("a.mp3", b"a"),
                SimpleUploadedFile("b.mp3", b"b"),
            ],
        )
        self.assertEqual(response.json(), {"redirect": "/"})
        self.assertEqual(Broadcast.objects.count(), 0)
        self.assertEqual(Asset.objects.count(), 3)

    def test_duplicate_upload_reuses_asset_for_another_appointment(self):
        self.upload()
        self.upload(
            scheduled_at=timezone.localtime(self.now + timedelta(days=3)).strftime(
                "%Y-%m-%dT%H:%M"
            )
        )
        jobs = list(Broadcast.objects.all())
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].asset_id, jobs[1].asset_id)
        self.assertEqual(Asset.objects.count(), 2)
        self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(Broadcast.objects.count(), 2)

    def test_trashed_duplicate_does_not_resurrect_or_schedule(self):
        self.asset.trashed_at = self.now
        self.asset.save()
        response = self.upload(files=SimpleUploadedFile("duplicate.mp3", b"episode"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Cestino", response.json()["error"])
        self.assertEqual(Broadcast.objects.count(), 0)
        self.asset.refresh_from_db()
        self.assertIsNotNone(self.asset.trashed_at)

    def test_late_validation_preserves_uploaded_original_without_booking(self):
        with patch(
            "library.appointments.validate_asset",
            side_effect=ValidationError("Contenuto non disponibile"),
        ):
            response = self.upload()
        self.assertEqual(response.status_code, 400)
        self.assertIn("File conservato", response.json()["error"])
        self.assertTrue(
            Asset.objects.filter(original_name="nuova-puntata.mp3").exists()
        )
        self.assertEqual(Broadcast.objects.count(), 0)

    def test_repeat_prefills_episode_and_creates_independent_history(self):
        previous = self.job(
            status="completed",
            started_at=self.now,
            ended_at=self.now + timedelta(seconds=120),
            outcome="Trasmissione terminata.",
        )
        page = self.client.get(f"/calendar/new/?asset={self.asset.id}")
        self.assertEqual(
            str(page.context["form"].initial["asset"].id), str(self.asset.id)
        )
        self.assertContains(self.client.get(f"/calendar/{previous.id}/"), "Programma di nuovo")
        for days in [1, 2]:
            self.assertEqual(
                self.client.post(
                    "/calendar/new/", self.payload(self.now + timedelta(days=days))
                ).status_code,
                302,
            )
        self.assertEqual(Broadcast.objects.filter(asset=self.asset).count(), 3)
        self.assertEqual(Asset.objects.count(), 1)
        previous.refresh_from_db()
        self.assertEqual(previous.status, "completed")
        self.assertEqual(previous.outcome, "Trasmissione terminata.")

    def test_edit_moves_only_selected_appointment_and_ignores_episode_tampering(self):
        first = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=1)
        )
        second = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=2)
        )
        target = self.now + timedelta(days=3)
        data = self.payload(target)
        data["asset"] = "not-an-asset"
        self.assertEqual(
            self.client.post(f"/calendar/{first.id}/edit/", data).status_code, 302
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(
            first.scheduled_at.strftime("%Y-%m-%d %H:%M"),
            target.strftime("%Y-%m-%d %H:%M"),
        )
        self.assertEqual(first.asset_id, second.asset_id)
        self.assertEqual(second.scheduled_at, self.now + timedelta(days=2))

    def test_edit_excludes_itself_but_rejects_another_overlap(self):
        first = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=1)
        )
        second = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=2)
        )
        self.assertEqual(
            self.client.post(
                f"/calendar/{first.id}/edit/", self.payload(first.scheduled_at)
            ).status_code,
            302,
        )
        self.assertContains(
            self.client.post(
                f"/calendar/{first.id}/edit/", self.payload(second.scheduled_at)
            ),
            "sovrappone",
        )

    def test_started_and_finished_appointments_cannot_be_edited(self):
        for status in [
            "dispatching",
            "playing",
            "completed",
            "interrupted",
            "skipped",
            "cancelled",
        ]:
            job = self.job(status=status)
            self.client.post(
                f"/calendar/{job.id}/edit/", self.payload(self.now + timedelta(days=1))
            )
            job.refresh_from_db()
            self.assertEqual(job.scheduled_at, self.now)
            self.assertEqual(job.status, status)

    def test_pending_or_failed_at_airtime_skips_with_specific_reason(self):
        for status, expected in [("pending", "preparazione"), ("error", "fallita")]:
            self.asset.status = status
            self.asset.save()
            job = self.job()
            tick(self.now)
            job.refresh_from_db()
            self.assertEqual(job.status, "skipped")
            self.assertIn(expected, job.outcome)
        self.sender.assert_not_called()

    def test_reschedule_racing_dispatch_cannot_air_at_old_time(self):
        job = self.job()
        later = self.now + timedelta(days=1)

        def moved(_):
            Broadcast.objects.filter(pk=job.pk).update(scheduled_at=later)
            return True

        with patch("library.scheduler.eligible", side_effect=moved):
            tick(self.now)
        self.sender.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "scheduled")
        self.assertEqual(job.scheduled_at, later)

    def test_stale_skip_cannot_overwrite_a_changed_appointment(self):
        job = self.job()
        Broadcast.objects.filter(pk=job.pk).update(
            scheduled_at=self.now + timedelta(days=1)
        )
        finish(job, "skipped", "old state", self.now)
        job.refresh_from_db()
        self.assertEqual(job.status, "scheduled")

    def test_unknown_duration_conflicts_become_visible_after_preparation(self):
        self.asset.duration_seconds = None
        self.asset.status = "pending"
        self.asset.save()
        Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=1)
        )
        Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=1, seconds=60)
        )
        self.asset.duration_seconds = 120
        self.asset.status = "ready"
        self.asset.save()
        self.assertContains(self.client.get("/calendar/"), "si sovrappone")

    def test_edit_csrf_auth_and_invalid_episode_link(self):
        from django.test import Client

        job = self.job()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(
            csrf.post(
                f"/calendar/{job.id}/edit/", self.payload(self.now + timedelta(days=1))
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/calendar/new/?asset=invalid").status_code, 404
        )
        self.client.logout()
        self.assertEqual(self.client.get(f"/calendar/{job.id}/edit/").status_code, 302)
