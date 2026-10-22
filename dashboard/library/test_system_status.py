import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import BackupJob, RadioSettings, RadioSnapshot


class SystemStatusTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(
            MEDIA_ROOT=Path(self.directory.name), BACKUP_ROOT=Path(self.directory.name)
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = get_user_model().objects.create_superuser("admin", password="test")
        self.client.force_login(self.user)

    def test_only_administrators_can_read_and_post_is_rejected(self):
        self.assertEqual(self.client.post("/maintenance/status/").status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get("/maintenance/status/").status_code, 302)
        staff = get_user_model().objects.create_user(
            "staff", password="test", is_staff=True
        )
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/maintenance/status/").status_code, 302)

    def test_empty_page_has_no_side_effects_and_no_false_healthy_status(self):
        settings_count = RadioSettings.objects.count()
        response = self.client.get("/maintenance/status/")
        self.assertContains(response, "Stato non aggiornato")
        self.assertContains(response, "Nessun backup richiesto")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(RadioSettings.objects.count(), settings_count)
        self.assertFalse(RadioSnapshot.objects.exists())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_fresh_stale_and_unknown_stream_are_distinct(self):
        snapshot = RadioSnapshot.objects.create(
            pk=1, checked_at=timezone.now(), stream_online=None, engine_online=True
        )
        self.assertContains(
            self.client.get("/maintenance/status/"), "Stato non disponibile"
        )
        snapshot.checked_at = timezone.now() - timedelta(minutes=2)
        snapshot.save()
        self.assertContains(
            self.client.get("/maintenance/status/"), "Stato non aggiornato"
        )
        snapshot.checked_at = timezone.now()
        snapshot.stream_online = True
        snapshot.save()
        self.assertContains(self.client.get("/maintenance/status/"), "Online")

    def test_disk_failure_is_unknown_and_low_disk_is_reported(self):
        with patch("library.system_views.shutil.disk_usage", side_effect=OSError):
            self.assertContains(
                self.client.get("/maintenance/status/"), "Spazio non verificabile"
            )
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")
        with patch(
            "library.system_views.shutil.disk_usage",
            return_value=usage(10 * 1024**3, 9 * 1024**3, 1024),
        ):
            self.assertContains(
                self.client.get("/maintenance/status/"), "Spazio in esaurimento"
            )

    def test_failed_attempt_does_not_hide_previous_success(self):
        BackupJob.objects.create(
            kind="full",
            status="expired",
            finished_at=timezone.now() - timedelta(days=2),
        )
        BackupJob.objects.create(kind="audio", status="error")
        response = self.client.get("/maintenance/status/")
        self.assertContains(response, "Non riuscito")
        self.assertContains(response, "Ultimo backup riuscito")
        self.assertContains(response, "download di questa copia è scaduto")
