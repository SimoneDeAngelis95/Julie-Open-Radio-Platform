"""Exercise destructive behavior only against isolated temporary media and a test DB."""

import hashlib
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from . import backups, engine, reset
from .backup_guard import reset_requested
from .models import (
    Asset,
    BackupJob,
    Broadcast,
    BroadcastSeries,
    ListeningSample,
    Playlist,
    RadioSettings,
    RadioSnapshot,
    ResetJob,
)
from .storage import data_path, save_upload


class ResetTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.settings_override = override_settings(
            MEDIA_ROOT=root / "data", BACKUP_ROOT=root / "backups"
        )
        self.settings_override.enable()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.settings_override.disable)
        self.user = get_user_model().objects.create_user(
            "admin", password="test", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.user)

    def ready(self):
        job = reset.prepare()
        content = b"full-backup-fixture"
        backups.archive_path(job.backup).write_bytes(content)
        BackupJob.objects.filter(pk=job.backup_id).update(
            status="ready",
            sha256=hashlib.sha256(content).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=24),
        )
        reset.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")
        return job

    def populate(self):
        asset, _ = save_upload(
            SimpleUploadedFile("test.mp3", b"temporary audio"), "podcasts"
        )
        playlist = Playlist.objects.create(name="Prova")
        playlist.assets.add(asset)
        RadioSettings.objects.update_or_create(pk=1, defaults={"current_jingle": asset})
        series = BroadcastSeries.objects.create(
            asset=asset, starts_at=timezone.now(), weekdays=[1]
        )
        Broadcast.objects.create(
            asset=asset, series=series, scheduled_at=timezone.now()
        )
        ListeningSample.objects.create(minute=timezone.now(), listeners=2)
        RadioSnapshot.objects.create(
            pk=1, checked_at=timezone.now(), stream_online=True, listeners=2
        )
        return asset

    def engine_command(self, value):
        return "off" if value == "output.icecast.status" else "OK"

    def test_permissions_get_and_csrf_never_start_reset(self):
        self.assertEqual(self.client.get("/maintenance/reset/").status_code, 200)
        self.assertFalse(ResetJob.objects.exists())
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(
            self.client.post("/maintenance/reset/", {"action": "prepare"}).status_code,
            403,
        )
        self.assertNotContains(self.client.get("/"), "maintenance-menu")
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.user)
        self.assertEqual(
            secured.post("/maintenance/reset/", {"action": "prepare"}).status_code, 403
        )
        self.assertFalse(ResetJob.objects.exists())

    def test_prepare_freezes_mutations_until_cancel_but_keeps_reads(self):
        self.populate()
        job = reset.prepare()
        self.assertTrue(reset_requested())
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.post("/upload/", {}).status_code, 503)
        asset = Asset.objects.get()
        self.assertEqual(
            self.client.get(f"/library/{asset.id}/audio/").status_code, 503
        )
        with self.assertRaises(backups.BackupBusy):
            backups.enqueue("database")
        with self.assertRaises(reset.ResetError):
            reset.prepare()
        reset.cancel(job.id)
        self.assertFalse(reset_requested())
        self.assertEqual(Asset.objects.count(), 1)
        self.assertTrue(data_path(Asset.objects.get().original_path).exists())

    def test_confirmation_requires_phrase_ack_and_ready_backup(self):
        job = self.ready()
        for phrase, ack in [("RESET JULIE", False), ("reset julie", True), ("", True)]:
            with self.assertRaises(reset.ResetError):
                reset.confirm(job.id, phrase, ack)
        backups.archive_path(job.backup).unlink()
        with self.assertRaises(reset.ResetError):
            reset.confirm(job.id, "RESET JULIE", True)
        self.assertTrue(reset_requested())

    def test_confirmed_reset_erases_owned_content_preserves_backup_accounts_config(
        self,
    ):
        asset = self.populate()
        job = self.ready()
        protected = data_path("configuration/keep.txt")
        protected.parent.mkdir()
        protected.write_text("keep")
        reset.confirm(job.id, "RESET JULIE", True)
        with (
            patch(
                "library.reset.engine.command", side_effect=self.engine_command
            ) as commands,
            patch(
                "library.reset.engine.get_state",
                return_value=engine.EngineState("session", False, "", {}),
            ),
        ):
            self.assertTrue(reset.process_one())
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(Broadcast.objects.exists())
        self.assertFalse(BroadcastSeries.objects.exists())
        self.assertFalse(Playlist.objects.exists())
        self.assertFalse(ListeningSample.objects.exists())
        self.assertFalse(RadioSnapshot.objects.exists())
        self.assertFalse(data_path(asset.original_path).exists())
        self.assertTrue(backups.archive_path(job.backup).is_file())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertEqual(protected.read_text(), "keep")
        self.assertTrue(reset.stopped_marker().exists())
        self.assertFalse(reset_requested())
        self.assertEqual(data_path("playout/rotation.txt").read_text(), "0\n\n")
        self.assertFalse(RadioSettings.objects.get().jingle_enabled)
        self.assertEqual(commands.call_args_list[0].args[0], "output.icecast.stop")
        with patch(
            "library.reset.engine.command",
            side_effect=lambda c: "on" if c.endswith("status") else "OK",
        ):
            reset.resume()
        self.assertFalse(reset.stopped_marker().exists())

    def test_corrupt_backup_or_unreachable_engine_never_delete_audio(self):
        asset = self.populate()
        job = self.ready()
        reset.confirm(job.id, "RESET JULIE", True)
        backups.archive_path(job.backup).write_bytes(b"corrupt")
        with patch("library.reset.engine.command") as command:
            self.assertFalse(reset.process_one())
            command.assert_not_called()
        self.assertTrue(data_path(asset.original_path).exists())
        self.assertEqual(Asset.objects.count(), 1)
        self.assertTrue(reset_requested())
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        backups.archive_path(job.backup).write_bytes(b"full-backup-fixture")
        reset.confirm(job.id, "RESET JULIE", True)
        with patch(
            "library.reset.engine.command", side_effect=engine.EngineError("offline")
        ):
            self.assertFalse(reset.process_one())
        self.assertTrue(data_path(asset.original_path).exists())

    def test_failed_deletion_can_resume_same_confirmed_job(self):
        self.populate()
        job = self.ready()
        reset.confirm(job.id, "RESET JULIE", True)
        state = engine.EngineState("session", False, "", {})
        with (
            patch("library.reset.engine.command", side_effect=self.engine_command),
            patch("library.reset.engine.get_state", return_value=state),
            patch("library.reset.clear_audio", side_effect=OSError("disk failure")),
        ):
            self.assertFalse(reset.process_one())
        self.assertTrue(reset_requested())
        reset.confirm(job.id, "RESET JULIE", True)
        with (
            patch("library.reset.engine.command", side_effect=self.engine_command),
            patch("library.reset.engine.get_state", return_value=state),
        ):
            self.assertTrue(reset.process_one())
        self.assertFalse(reset_requested())
        self.assertFalse(Asset.objects.exists())

    def test_expiry_cancels_unconfirmed_job_and_active_backup_is_pinned(self):
        job = self.ready()
        BackupJob.objects.filter(pk=job.backup_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        backups.cleanup()
        self.assertTrue(backups.archive_path(job.backup).exists())
        ResetJob.objects.filter(pk=job.id).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        reset.process_one()
        self.assertFalse(reset_requested())
        backups.cleanup()
        self.assertFalse(backups.archive_path(job.backup).exists())

    def test_reset_gates_calendar_collector_and_symlinks_do_not_escape(self):
        from .listening import collect
        from .scheduler import tick

        reset.prepare()
        with patch("library.engine.get_state") as read:
            self.assertFalse(tick())
            self.assertFalse(collect())
            read.assert_not_called()
        external = Path(self.directory.name) / "external"
        external.mkdir()
        (external / "keep").write_text("safe")
        root = data_path("originals")
        root.symlink_to(external, target_is_directory=True)
        reset.clear_audio()
        self.assertEqual((external / "keep").read_text(), "safe")
        self.assertFalse(root.is_symlink())

    def test_crash_after_files_removed_retries_without_requiring_missing_audio(self):
        asset = self.populate()
        job = self.ready()
        reset.confirm(job.id, "RESET JULIE", True)
        original_save = ResetJob.save

        def fail_completion(instance, *args, **kwargs):
            if instance.status == "completed":
                raise OSError("commit interrupted")
            return original_save(instance, *args, **kwargs)

        state = engine.EngineState("session", False, "", {})
        with (
            patch("library.reset.engine.command", side_effect=self.engine_command),
            patch("library.reset.engine.get_state", return_value=state),
            patch.object(ResetJob, "save", fail_completion),
        ):
            self.assertFalse(reset.process_one())
        self.assertFalse(data_path(asset.original_path).exists())
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())
        self.assertTrue(reset_requested())
        reset.confirm(job.id, "RESET JULIE", True)
        with (
            patch("library.reset.engine.command", side_effect=self.engine_command),
            patch("library.reset.engine.get_state", return_value=state),
        ):
            self.assertTrue(reset.process_one())
        self.assertFalse(Asset.objects.exists())

    def test_backup_inside_audio_is_never_deleted(self):
        self.populate()
        job = self.ready()
        reset.confirm(job.id, "RESET JULIE", True)
        root = data_path("ready/exports")
        root.mkdir(parents=True)
        backup = backups.archive_path(job.backup)
        (root / backup.name).write_bytes(backup.read_bytes())
        with (
            override_settings(BACKUP_ROOT=root),
            patch("library.reset.engine.command") as command,
        ):
            self.assertFalse(reset.process_one())
            command.assert_not_called()
            self.assertTrue((root / backup.name).exists())
        self.assertTrue(Asset.objects.exists())

    def test_http_steps_never_delete_before_valid_confirmation(self):
        self.populate()
        response = self.client.post("/maintenance/reset/", {"action": "prepare"})
        self.assertEqual(response.status_code, 302)
        job = ResetJob.objects.get()
        response = self.client.post(
            "/maintenance/reset/", {"action": "confirm", "job": "not-a-uuid"}
        )
        self.assertContains(response, "Richiesta di reset non valida")
        self.assertEqual(Asset.objects.count(), 1)
        content = b"full-backup-fixture"
        backups.archive_path(job.backup).write_bytes(content)
        BackupJob.objects.filter(pk=job.backup_id).update(
            status="ready",
            sha256=hashlib.sha256(content).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=24),
        )
        reset.process_one()
        response = self.client.post(
            "/maintenance/reset/",
            {
                "action": "confirm",
                "job": str(job.id),
                "confirmation": "wrong",
                "downloaded": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")
        response = self.client.post(
            "/maintenance/reset/",
            {
                "action": "confirm",
                "job": str(job.id),
                "confirmation": "RESET JULIE",
                "downloaded": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(
            self.client.post(
                "/maintenance/reset/", {"action": "cancel", "job": str(job.id)}
            ).status_code,
            200,
        )
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")

    def test_cancel_while_worker_observes_backup_ready_is_not_resurrected(self):
        job = reset.prepare()
        BackupJob.objects.filter(pk=job.backup_id).update(status="ready")
        original = reset.set_marker

        def cancel_after_read(current):
            original(current)
            reset.cancel(current.id)

        with patch("library.reset.set_marker", side_effect=cancel_after_read):
            reset.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertFalse(reset_requested())
