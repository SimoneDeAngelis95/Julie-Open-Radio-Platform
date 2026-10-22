"""Restore only into temporary media and the Django test database, never the live engine."""

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import backups, engine, reset, restore, restore_data
from .backup_guard import reset_requested
from .models import (
    Asset,
    BackupJob,
    Broadcast,
    BroadcastSeries,
    Playlist,
    RadioSettings,
    RestoreJob,
)
from .restore_archive import RestoreError
from .storage import data_path, save_upload


def copy_sql():
    lines = ["-- SQL outside COPY must never execute", "DROP TABLE auth_user;"]
    for model in restore_data.MODELS:
        fields = list(model._meta.concrete_fields)
        lines.append(
            f"COPY public.{model._meta.db_table} ({', '.join(f.column for f in fields)}) FROM stdin;"
        )
        for instance in model.objects.all():
            values = []
            for field in fields:
                value = getattr(instance, field.attname)
                if value is None:
                    values.append(r"\N")
                    continue
                if field.get_internal_type() == "JSONField":
                    value = json.dumps(value)
                elif isinstance(value, bool):
                    value = "t" if value else "f"
                else:
                    value = str(value)
                values.append(
                    value.replace("\\", "\\\\")
                    .replace("\n", r"\n")
                    .replace("\r", r"\r")
                    .replace("\t", r"\t")
                )
            lines.append("\t".join(values))
        lines.append(r"\.")
    return "\n".join(lines) + "\n"


def full_archive(target, entries, dump=b"PGDMP-fixture"):
    """Use the production TAR writer and manifests; only the local dump is substituted."""
    job = BackupJob(kind="full", started_at=timezone.now())
    components = {"database.dump": dump}
    for name, files, compressed in [
        ("audio.tar", entries, False),
        ("configuration.tar.gz", [], True),
    ]:
        buffer = io.BytesIO()
        backups.write_tar(job, buffer, files, compressed=compressed)
        components[name] = buffer.getvalue()
    manifest = backups.manifest(
        job,
        [
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in components.items()
        ],
    )
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in components.items():
            archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest))


class RestoreFixtures:
    def setup_restore(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.override = override_settings(
            MEDIA_ROOT=base / "data", BACKUP_ROOT=base / "exports"
        )
        self.override.enable()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.override.disable)
        self.user = get_user_model().objects.create_superuser(
            "admin", password="current-password"
        )
        self.client.force_login(self.user)
        source, _ = save_upload(
            SimpleUploadedFile("source.mp3", b"archived-original"),
            "music",
            title="Dal backup",
            artist="Artista scelto",
        )
        self.source_id = source.id
        playlist = Playlist.objects.create(name="Classica")
        playlist.assets.add(source)
        RadioSettings.objects.update_or_create(
            pk=1, defaults={"selected_playlist": playlist}
        )
        self.sql = copy_sql()
        entries = [(source.original_path, data_path(source.original_path))]
        self.input = base / "full.zip"
        full_archive(self.input, entries)
        restore_data.clear_content()
        self.current, _ = save_upload(
            SimpleUploadedFile("current.mp3", b"current-original"),
            "music",
            title="Attuale",
        )

    def ready(self):
        job = restore.receive(SimpleUploadedFile("full.zip", self.input.read_bytes()))

        def export(dump, destination):
            destination.write_text(self.sql)

        with patch("library.restore_data.export_copy", side_effect=export):
            self.assertTrue(restore.process_one())
        job.refresh_from_db()
        self.assertEqual(job.status, "ready", job.message)
        return job

    def protected(self):
        job = self.ready()
        restore.protect(job.id)
        job.refresh_from_db()
        content = b"current-safety-backup"
        backups.archive_path(job.safety_backup).write_bytes(content)
        BackupJob.objects.filter(pk=job.safety_backup_id).update(
            status="ready",
            sha256=hashlib.sha256(content).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=24),
        )
        restore.process_one()
        job.refresh_from_db()
        self.assertEqual(job.status, "protected")
        return job

    def command(self, value):
        return "off" if value == "output.icecast.status" else "OK"

    def apply(self):
        with (
            patch("library.reset.engine.command", side_effect=self.command),
            patch(
                "library.reset.engine.get_state",
                return_value=engine.EngineState("test", False, "", {}),
            ),
        ):
            return restore.process_one()


class RestoreTests(RestoreFixtures, TestCase):
    def setUp(self):
        self.setup_restore()

    def test_validation_reads_archive_but_does_not_change_station_or_execute_sql(self):
        self.sql = self.sql.replace(
            "COPY public.library_asset (description,",
            'COPY "public"."library_asset" ("description",',
        )
        job = self.ready()
        self.assertEqual(Asset.objects.get().title, "Attuale")
        self.assertTrue(
            get_user_model()
            .objects.get(pk=self.user.pk)
            .check_password("current-password")
        )
        self.assertFalse(reset_requested())
        with self.assertRaises(reset.ResetError):
            reset.prepare()
        self.assertEqual(job.summary["counts"]["library_asset"], 1)
        self.assertEqual(job.summary["files"], 1)

    def test_full_restore_preserves_account_and_current_technical_configuration(self):
        job = self.protected()
        self.assertTrue(reset_requested())
        self.assertEqual(self.client.post("/upload/", {}).status_code, 503)
        with self.assertRaises(RestoreError):
            restore.confirm(job.id, "RIPRISTINA JULIE", False)
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        self.assertTrue(self.apply())
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(Asset.objects.get().id, self.source_id)
        self.assertEqual(Asset.objects.get().artist, "Artista scelto")
        self.assertEqual(
            data_path(Asset.objects.get().original_path).read_bytes(),
            b"archived-original",
        )
        self.assertFalse(data_path(self.current.original_path).exists())
        self.assertEqual(RadioSettings.objects.get().selected_playlist.name, "Classica")
        self.assertTrue(
            get_user_model()
            .objects.get(pk=self.user.pk)
            .check_password("current-password")
        )
        self.assertTrue(backups.archive_path(job.safety_backup).is_file())
        self.assertFalse(reset_requested())
        self.assertTrue(reset.stopped_marker().exists())

    def test_db_failure_restores_current_files_and_retry_uses_same_validated_input(
        self,
    ):
        job = self.protected()
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        with patch(
            "library.restore_data.import_content", side_effect=RuntimeError("rollback")
        ):
            self.assertFalse(self.apply())
        self.assertEqual(Asset.objects.get().title, "Attuale")
        self.assertTrue(data_path(self.current.original_path).exists())
        self.assertTrue(reset_requested())
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        self.assertTrue(self.apply())
        self.assertEqual(Asset.objects.get().title, "Dal backup")

    def test_crash_after_database_commit_only_finalizes_and_never_rolls_back_files(
        self,
    ):
        job = self.protected()
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        with patch(
            "library.restore.finalize", side_effect=OSError("interrupted after commit")
        ):
            self.assertFalse(self.apply())
        job.refresh_from_db()
        self.assertEqual(job.status, "applied")
        self.assertEqual(Asset.objects.get().title, "Dal backup")
        self.assertTrue(restore.process_one())
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertFalse(reset_requested())

    def test_resume_after_audio_swap_before_database_commit(self):
        job = self.protected()
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        restore.swap_forward(job)
        self.assertTrue(self.apply())
        self.assertEqual(Asset.objects.get().title, "Dal backup")

    def test_cancellation_unfreezes_and_preserves_all_current_data(self):
        job = self.protected()
        restore.cancel(job.id)
        self.assertFalse(reset_requested())
        self.assertEqual(Asset.objects.get().title, "Attuale")
        self.assertTrue(data_path(self.current.original_path).exists())
        self.assertFalse(restore.workspace(job).exists())

    def test_corrupt_component_rejected_before_any_current_changes(self):
        with zipfile.ZipFile(self.input) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries["database.dump"] = b"corrupted"
        with zipfile.ZipFile(self.input, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        job = restore.receive(SimpleUploadedFile("bad.zip", self.input.read_bytes()))
        self.assertFalse(restore.process_one())
        job.refresh_from_db()
        self.assertEqual(job.status, "invalid")
        self.assertEqual(Asset.objects.get().title, "Attuale")
        self.assertFalse(reset_requested())

    def test_missing_audio_and_unsupported_new_schema_are_rejected(self):
        for sql in (
            self.sql.replace(str(self.source_id) + ".mp3", "missing.mp3"),
            self.sql.replace("artist_edited,", "future_field,"),
        ):
            job = restore.receive(
                SimpleUploadedFile("full.zip", self.input.read_bytes())
            )
            with patch(
                "library.restore_data.export_copy",
                side_effect=lambda dump, dest, content=sql: dest.write_text(content),
            ):
                self.assertFalse(restore.process_one())
            job.refresh_from_db()
            self.assertEqual(job.status, "invalid")
        self.assertEqual(Asset.objects.get().title, "Attuale")

    def test_only_admin_and_explicit_confirmation_can_queue_restore(self):
        self.assertEqual(self.client.get("/maintenance/restore/").status_code, 200)
        self.assertFalse(RestoreJob.objects.exists())
        job = self.protected()
        response = self.client.post(
            "/maintenance/restore/",
            {
                "action": "confirm",
                "job": str(job.id),
                "confirmation": "wrong",
                "downloaded": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, "protected")
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(
            self.client.post(
                "/maintenance/restore/", {"action": "confirm"}
            ).status_code,
            403,
        )
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.user)
        self.assertEqual(
            secured.post("/maintenance/restore/", {"action": "upload"}).status_code, 403
        )

    def test_active_restore_blocks_reset_and_safety_backup_does_not_expire(self):
        job = self.protected()
        with self.assertRaises(reset.ResetError):
            reset.prepare()
        BackupJob.objects.filter(pk=job.safety_backup_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        backups.cleanup()
        self.assertTrue(backups.archive_path(job.safety_backup).exists())

    def test_changed_staging_after_validation_never_stops_radio(self):
        job = self.protected()
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        (restore.workspace(job) / "content.sqlite").write_bytes(b"tamper")
        with patch("library.reset.engine.command") as command:
            self.assertFalse(restore.process_one())
            command.assert_not_called()
        self.assertEqual(Asset.objects.get().title, "Attuale")

    def test_post_upload_and_cancel_http_flow(self):
        with self.input.open("rb") as archive:
            response = self.client.post(
                "/maintenance/restore/", {"action": "upload", "archive": archive}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RestoreJob.objects.get().status, "checking")
        self.assertEqual(
            self.client.post(
                "/maintenance/restore/", {"action": "cancel", "job": "bad"}
            ).status_code,
            200,
        )

    def test_copy_escape_decoding(self):
        self.assertEqual(
            restore_data.decode_copy(r"voce\tuno\nrighe\\fine"),
            "voce\tuno\nrighe\\fine",
        )
        self.assertIsNone(restore_data.decode_copy(r"\N"))
        self.assertEqual(restore_data.decode_copy(r"\303\250"), "è")
        self.assertEqual(restore_data.decode_copy(r"\\N"), r"\N")

    def test_tar_paths_links_and_oversized_pax_are_rejected(self):
        from .restore_archive import read_tar

        for name, kind in [
            ("../escape", tarfile.REGTYPE),
            ("originals/link", tarfile.SYMTYPE),
        ]:
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as archive:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            data.seek(0)
            with self.assertRaises(RestoreError):
                read_tar(
                    data,
                    len(data.getvalue()),
                    "test",
                    Path(self.directory.name) / "extract",
                )
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XHDTYPE
        header.size = 100_000_000
        with self.assertRaises(RestoreError):
            read_tar(io.BytesIO(header.tobuf()), 200_000_000, "test")

    def test_scheduler_does_not_dispatch_while_restored_radio_is_stopped(self):
        from .scheduler import tick

        reset.stopped_marker().parent.mkdir(parents=True, exist_ok=True)
        reset.stopped_marker().touch()
        with patch("library.engine.get_state") as state:
            self.assertFalse(tick())
            state.assert_not_called()

    def test_original_permissions_are_private_and_ready_files_remain_readable(self):
        job = self.protected()
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        self.assertTrue(self.apply())
        original = data_path(Asset.objects.get().original_path)
        self.assertEqual(original.stat().st_mode & 0o777, 0o600)
        self.assertEqual(original.parent.stat().st_mode & 0o777, 0o700)

    def test_old_rotation_is_cleared_before_the_engine_can_prefetch_after_flush(self):
        path = data_path("playout/rotation.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("0\n\nold-track\n")

        def command(value):
            if value == "music.flush_and_skip":
                self.assertEqual(path.read_text(), "0\n\n")
            return self.command(value)

        with (
            patch("library.reset.engine.command", side_effect=command),
            patch(
                "library.reset.engine.get_state",
                return_value=engine.EngineState("test", False, "", {}),
            ),
        ):
            reset.stop_output()

    def test_cleanup_failure_after_success_never_turns_into_a_second_import(self):
        job = self.protected()
        restore.confirm(job.id, "RIPRISTINA JULIE", True)
        with patch(
            "library.restore.cleanup_job", side_effect=OSError("cleanup interrupted")
        ):
            self.assertFalse(self.apply())
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        with patch("library.restore_data.import_content") as import_content:
            restore.process_one()
            import_content.assert_not_called()
        self.assertEqual(Asset.objects.get().title, "Dal backup")
        self.assertFalse(restore.workspace(job).exists())

    def test_sequences_are_adjusted_only_after_content_commit(self):
        job = self.protected()
        restore.confirm(job.id, "RIPRISTINA JULIE", True)

        def sequences(style, models):
            self.assertEqual(RestoreJob.objects.get(pk=job.id).status, "applied")
            return []

        with patch.object(
            connection.ops, "sequence_reset_sql", side_effect=sequences
        ) as sequence_reset:
            self.assertTrue(self.apply())
            sequence_reset.assert_called_once()


class PostgreSQLRestoreTests(RestoreFixtures, TransactionTestCase):
    def test_real_full_backup_round_trip_in_isolated_postgresql_database(self):
        import subprocess
        import time

        from . import audio
        from .worker import run_one

        if connection.vendor != "postgresql" or not shutil.which("pg_restore"):
            self.skipTest(
                "Requires PostgreSQL tools and the isolated PostgreSQL test database"
            )
        self.setup_restore()
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))
        base = Path(self.directory.name)
        source = base / "tone.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                str(source),
            ],
            check=True,
        )
        data_path(self.current.original_path).write_bytes(source.read_bytes())
        self.current.checksum = backups.sha256(data_path(self.current.original_path))
        self.current.size_bytes = source.stat().st_size
        self.current.save()
        self.assertTrue(run_one())
        self.current.refresh_from_db()
        self.assertEqual(self.current.status, "ready")
        audio_digest = backups.sha256(data_path(self.current.prepared_path))
        playlist = Playlist.objects.create(name="Rotazione ripristinata")
        playlist.assets.add(self.current)
        RadioSettings.objects.update_or_create(
            pk=1, defaults={"selected_playlist": playlist}
        )
        podcast, _ = save_upload(
            SimpleUploadedFile("podcast.mp3", source.read_bytes()),
            "podcasts",
            title="Puntata ripristinata",
        )
        self.assertTrue(run_one())
        podcast.refresh_from_db()
        self.assertEqual(podcast.status, "ready")
        appointment = timezone.now() + timedelta(days=3)
        series = BroadcastSeries.objects.create(
            asset=podcast,
            starts_at=appointment,
            weekdays=[timezone.localtime(appointment).weekday()],
        )
        Broadcast.objects.create(
            asset=podcast,
            series=series,
            scheduled_at=appointment,
            occurrence_date=timezone.localdate(appointment),
        )

        config = base / "config"
        code = base / "code"
        code.mkdir()
        (code / "manage.py").write_text("# fixture")
        for name in (
            ".env",
            ".env.dashboard",
            "compose.yaml",
            "compose.dashboard.yaml",
            "config/radio.liq",
            "config/icecast.xml",
            "icecast/Dockerfile",
        ):
            path = config / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture only")
        with override_settings(BACKUP_CONFIG_ROOT=config, BACKUP_CODE_ROOT=code):
            source_job = backups.enqueue("full")
            self.assertTrue(backups.process_one())
            source_job.refresh_from_db()
            self.assertEqual(source_job.status, "ready", source_job.progress)
            self.current.title = "Dopo il backup"
            self.current.save()
            self.user.set_password("destination-password")
            self.user.save()
            with backups.archive_path(source_job).open("rb") as content:
                job = restore.receive(SimpleUploadedFile("full.zip", content.read()))
            self.assertTrue(restore.process_one())
            job.refresh_from_db()
            self.assertEqual(job.status, "ready", job.message)
            restore.protect(job.id)
            self.assertTrue(backups.process_one())
            restore.process_one()
            job.refresh_from_db()
            self.assertEqual(job.status, "protected", job.message)
            restore.confirm(job.id, "RIPRISTINA JULIE", True)
            self.assertTrue(self.apply())
        restored = Asset.objects.get(pk=self.current.pk)
        self.assertEqual(restored.title, "Attuale")
        self.assertEqual(
            RadioSettings.objects.get().selected_playlist.name, "Rotazione ripristinata"
        )
        self.assertEqual(
            BroadcastSeries.objects.get().weekdays,
            [timezone.localtime(appointment).weekday()],
        )
        self.assertEqual(Broadcast.objects.get().asset.title, "Puntata ripristinata")
        self.assertEqual(
            backups.sha256(data_path(restored.prepared_path)), audio_digest
        )
        self.assertGreater(
            float(
                audio.probe(data_path(restored.prepared_path), time.monotonic() + 30)[
                    "format"
                ]["duration"]
            ),
            2,
        )
        self.assertTrue(
            get_user_model()
            .objects.get(pk=self.user.pk)
            .check_password("destination-password")
        )
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertTrue(backups.archive_path(job.safety_backup).is_file())
        self.assertFalse(reset_requested())
