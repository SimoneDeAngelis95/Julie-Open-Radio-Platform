"""Backup isolation, coherent pinned files, failure recovery and guarded downloads."""

import hashlib
import io
import json
import os
import tarfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from . import backups, tests
from .backup_guard import data_gate
from .forms import BackupForm
from .models import BackupJob
from .storage import data_path


class BackupTests(TestCase):
    def setUp(self):
        tests.LibraryTests.setUp(self)
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        base = Path(self.directory.name)
        self.config = base / "config-source"
        self.code = base / "code"
        self.config.mkdir()
        self.code.mkdir()
        for name in [
            ".env",
            ".env.dashboard",
            "compose.yaml",
            "compose.dashboard.yaml",
            "config/radio.liq",
            "config/icecast.xml",
            "icecast/Dockerfile",
        ]:
            path = self.config / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private-fixture-secret")
        (self.code / "manage.py").write_text("# fixture only\n")
        (self.code / "private.sqlite3").write_bytes(b"excluded")
        self.backup_settings = override_settings(
            BACKUP_ROOT=base / "exports",
            BACKUP_CONFIG_ROOT=self.config,
            BACKUP_CODE_ROOT=self.code,
        )
        self.backup_settings.enable()

    def tearDown(self):
        self.backup_settings.disable()
        tests.LibraryTests.tearDown(self)

    def audio(self):
        source = data_path("originals/example.wav")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"original audio fixture")
        prepared = data_path("ready/music/example.flac")
        prepared.parent.mkdir(parents=True, exist_ok=True)
        prepared.write_bytes(b"prepared audio fixture")
        return source, prepared

    def test_only_superusers_can_prepare_view_and_download(self):
        self.assertEqual(Client().get("/backups/").status_code, 302)
        self.assertEqual(self.client.get("/backups/").status_code, 200)
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        for path in ["/backups/", "/backups/status/"]:
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(
            self.client.post("/backups/", {"kind": "audio"}).status_code, 403
        )
        self.assertFalse(BackupJob.objects.exists())

    def test_all_kinds_need_no_password_and_keep_csrf_protection(self):
        for kind in BackupJob.Kind.values:
            self.assertTrue(BackupForm({"kind": kind}).is_valid())
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post("/backups/", {"kind": "audio"}).status_code, 403)
        response = self.client.get("/backups/")
        self.assertNotContains(response, 'name="password"')
        self.assertEqual(
            self.client.post("/backups/", {"kind": "config"}).status_code, 302
        )

    def test_single_active_job_without_secret_file(self):
        job = backups.enqueue("config")
        self.assertFalse(backups.secret_path(job.id).exists())
        with self.assertRaises(backups.BackupBusy):
            backups.enqueue("audio")
        self.assertEqual(BackupJob.objects.count(), 1)

    def test_hardlink_snapshot_survives_replacement_and_deletion(self):
        source, prepared = self.audio()
        snapshot = data_path("backup-snapshots/test")
        entries = dict(backups.pin_audio(snapshot))
        source.unlink()
        replacement = prepared.with_suffix(".part")
        replacement.write_bytes(b"new audio")
        os.replace(replacement, prepared)
        self.assertEqual(
            entries["originals/example.wav"].read_bytes(), b"original audio fixture"
        )
        self.assertEqual(
            entries["ready/music/example.flac"].read_bytes(), b"prepared audio fixture"
        )
        self.assertEqual(prepared.read_bytes(), b"new audio")

    def test_symlinks_are_rejected_without_reading_target(self):
        source, _ = self.audio()
        (source.parent / "link.wav").symlink_to(source)
        with self.assertRaises(backups.BackupError):
            backups.pin_audio(data_path("backup-snapshots/test"))

    def test_guard_blocks_mutations_only_during_snapshot_and_worker_pauses(self):
        with data_gate(exclusive=True) as lock:
            self.assertIsNotNone(lock)
            self.assertEqual(
                self.client.post("/settings/", {"jingle_every": 3}).status_code, 503
            )
            self.assertEqual(self.client.get("/playlists/").status_code, 200)
            self.assertEqual(self.client.get("/backups/").status_code, 200)
            with (
                patch("library.management.commands.audio_worker.logging.basicConfig"),
                # The test owns an atomic transaction; a worker's connection
                # cleanup must not close that transaction's PostgreSQL connection.
                patch("library.management.commands.audio_worker.close_old_connections"),
                patch("library.management.commands.audio_worker.run_one") as process,
            ):
                call_command("audio_worker", once=True)
                process.assert_not_called()
        self.assertEqual(self.client.get("/settings/").status_code, 200)

    def test_audio_archive_manifest_download_ranges_and_expiration(self):
        self.audio()
        job = backups.enqueue("audio")
        self.assertTrue(backups.process_one())
        job.refresh_from_db()
        self.assertEqual(job.status, "ready", job.progress)
        archive = backups.archive_path(job)
        with tarfile.open(archive) as package:
            document = json.load(package.extractfile("manifest.json"))
            self.assertEqual(document["id"], str(job.id))
            for entry in document["files"]:
                content = package.extractfile(entry["path"]).read()
                self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
        self.assertEqual(backups.sha256(archive), job.sha256)
        url = f"/backups/{job.id}/download/"
        full = self.client.get(url)
        content = b"".join(full.streaming_content)
        self.assertEqual(len(content), job.size_bytes)
        self.assertIn("no-store", full["Cache-Control"])
        response = self.client.get(url, HTTP_RANGE="bytes=0-31")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), content[:32])
        self.assertEqual(
            self.client.get(url, HTTP_RANGE="bytes=999999999-").status_code, 416
        )
        self.assertEqual(self.client.head(url).content, b"")
        job.expires_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["expires_at"])
        self.assertEqual(self.client.get(url).status_code, 404)
        backups.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "expired")
        self.assertFalse(archive.exists())
        self.assertTrue(data_path("originals/example.wav").exists())

    def test_failure_removes_secrets_partial_files_and_marker(self):
        self.audio()
        job = backups.enqueue("full")
        with patch(
            "library.backups.preflight",
            side_effect=backups.BackupError("Spazio insufficiente"),
        ):
            backups.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "error")
        self.assertFalse(backups.archive_path(job).exists())
        self.assertFalse(backups.secret_path(job.id).exists())
        self.assertFalse(data_path("locks/backup-requested").exists())
        self.assertTrue(data_path("originals/example.wav").exists())

    def test_recovery_marks_interrupted_job_failed_and_cleans_only_its_snapshot(self):
        self.audio()
        job = backups.enqueue("config")
        job.status = "running"
        job.save(update_fields=["status"])
        snapshot = data_path(f"backup-snapshots/{job.id}")
        backups.pin_audio(snapshot)
        data_path("locks").mkdir(exist_ok=True)
        data_path("locks/backup-requested").write_text(str(job.id))
        backups.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "error")
        self.assertFalse(snapshot.exists())
        self.assertFalse(backups.secret_path(job.id).exists())
        self.assertTrue(data_path("originals/example.wav").exists())

    def test_full_archive_streams_audio_and_coordinates_component_manifests(self):
        self.audio()
        job = backups.enqueue("full")

        def fake_dump(path):
            path.write_bytes(b"PGDMP-fixture")

        with (
            patch("library.backups.preflight"),
            patch("library.backups.dump_database", side_effect=fake_dump),
        ):
            backups.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "ready", job.progress)
        with zipfile.ZipFile(backups.archive_path(job)) as package:
            manifest = json.loads(package.read("manifest.json"))
            self.assertTrue(manifest["coordinated"])
            for entry in manifest["files"]:
                self.assertEqual(
                    hashlib.sha256(package.read(entry["path"])).hexdigest(),
                    entry["sha256"],
                )
            with tarfile.open(fileobj=io.BytesIO(package.read("audio.tar"))) as audio:
                self.assertEqual(
                    json.load(audio.extractfile("manifest.json"))["id"], str(job.id)
                )
        self.assertFalse(data_path(f"backup-snapshots/{job.id}").exists())

    def test_config_is_directly_readable_and_manifest_verified(self):
        job = backups.enqueue("config")
        backups.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "ready", job.progress)
        self.assertTrue(backups.archive_path(job).name.endswith(".tar.gz"))
        with tarfile.open(backups.archive_path(job), "r:gz") as package:
            self.assertEqual(
                package.extractfile(".env").read(), b"private-fixture-secret"
            )
            self.assertNotIn("dashboard/private.sqlite3", package.getnames())
            manifest = json.load(package.extractfile("manifest.json"))
            for entry in manifest["files"]:
                self.assertEqual(
                    hashlib.sha256(
                        package.extractfile(entry["path"]).read()
                    ).hexdigest(),
                    entry["sha256"],
                )

    def test_real_postgresql_dump_when_using_postgres(self):
        if connection.settings_dict["ENGINE"] != "django.db.backends.postgresql":
            self.skipTest("Actual PostgreSQL is verified on Vault-5")
        destination = Path(self.directory.name) / "database.dump"
        backups.dump_database(destination)
        self.assertTrue(destination.read_bytes().startswith(b"PGDMP"))

    def test_restart_removes_pins_left_after_a_successful_backup(self):
        self.audio()
        job = backups.enqueue("audio")
        backups.process_one()
        job.refresh_from_db()
        snapshot = data_path(f"backup-snapshots/{job.id}")
        backups.pin_audio(snapshot)
        backups.process_one()
        self.assertFalse(snapshot.exists())
        self.assertTrue(backups.archive_path(job).exists())
        self.assertTrue(data_path("originals/example.wav").exists())

    def test_stale_queued_request_does_not_retain_its_password(self):
        job = backups.enqueue("config")
        BackupJob.objects.filter(pk=job.id).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        backups.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "error")
        self.assertFalse(backups.secret_path(job.id).exists())
