import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import audio, publication, reprocessing, restore_data
from .audio_settings import AudioPreferencesForm, effective, enqueue
from .models import Asset, AudioReprocessItem, AudioReprocessJob, RadioSettings
from .onboarding import prepare
from .storage import data_path, save_upload
from .worker import lock_file, run_one


class AudioSettingsTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        override = override_settings(
            MEDIA_ROOT=self.root, BACKUP_ROOT=self.root / "backups"
        )
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_superuser("admin", password="test")
        self.client.force_login(self.user)

    def config(self):
        return RadioSettings.objects.get_or_create(pk=1)[0]

    def asset(self, category="music"):
        item, _ = save_upload(
            SimpleUploadedFile("audio.wav", b"original " + category.encode()), category
        )
        relative = f"ready/{category}/{item.id}.flac"
        path = data_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old prepared audio")
        item.status = Asset.Status.READY
        item.prepared_path = relative
        item.duration_seconds = 3
        item.save()
        return item

    def enqueue(self):
        with transaction.atomic():
            return enqueue(self.config())

    def values(self, **changes):
        return {
            "action": "save",
            "audio_target_lufs": -16,
            "audio_ceiling_dbtp": -1.5,
            **changes,
        }

    def fake_prepare(self, original, workspace, target, ceiling):
        path = workspace / "prepared.flac"
        path.write_bytes(b"new prepared audio")
        return {
            "path": path,
            "duration": 3,
            "tags": {},
            "before": {"input_i": -20, "input_tp": -5},
            "after": {"input_i": target, "input_tp": ceiling - 1},
            "peak_constrained": False,
            "deadline": 9999999999,
        }

    @staticmethod
    def fake_tags(source, destination, metadata, deadline):
        destination.write_bytes(source.read_bytes())

    def test_access_read_only_get_and_csrf(self):
        count = RadioSettings.objects.count()
        response = self.client.get("/maintenance/audio/")
        self.assertContains(response, "Impostazioni audio")
        self.assertContains(response, 'value="-20.0"')
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(RadioSettings.objects.count(), count)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post("/maintenance/audio/", self.values()).status_code, 403
        )
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.client.get("/maintenance/audio/").status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get("/maintenance/audio/").status_code, 302)

    def test_invalid_values_are_rejected_and_save_does_not_enqueue(self):
        for target, ceiling in [
            (-31, -1.5),
            (-9, -1.5),
            (-16, 0),
            (-16, -7),
            ("nan", -1.5),
            (-16, "inf"),
        ]:
            self.assertFalse(
                AudioPreferencesForm(
                    self.values(audio_target_lufs=target, audio_ceiling_dbtp=ceiling)
                ).is_valid()
            )
        self.asset()
        self.assertEqual(
            self.client.post("/maintenance/audio/", self.values()).status_code, 302
        )
        self.assertEqual(effective(), (-16, -1.5))
        self.assertFalse(AudioReprocessJob.objects.exists())

    def test_reprocess_requires_confirmation_and_saved_values(self):
        self.asset()
        self.client.post("/maintenance/audio/", self.values())
        response = self.client.post(
            "/maintenance/audio/", self.values(action="reprocess")
        )
        self.assertContains(response, "Conferma la rielaborazione")
        response = self.client.post(
            "/maintenance/audio/",
            self.values(action="reprocess", confirm="yes", audio_target_lufs=-18),
        )
        self.assertContains(response, "impostazioni sono cambiate")
        self.assertFalse(AudioReprocessJob.objects.exists())
        self.assertEqual(
            self.client.post(
                "/maintenance/audio/", self.values(action="reprocess", confirm="yes")
            ).status_code,
            302,
        )
        self.assertEqual(AudioReprocessItem.objects.count(), 1)
        response = self.client.post(
            "/maintenance/audio/", self.values(audio_target_lufs=-18)
        )
        self.assertContains(response, "Attendi la fine")
        self.assertEqual(effective(), (-16, -1.5))

    def test_legacy_settings_and_original_preparation_use_saved_values(self):
        self.assertEqual(effective(), (-20, -1.5))
        self.client.post("/maintenance/audio/", self.values())
        item = self.asset()
        item.status = Asset.Status.PENDING
        item.save()
        with (
            patch(
                "library.worker.audio.prepare", side_effect=self.fake_prepare
            ) as prepare_mock,
            patch("library.worker.audio.apply_tags", side_effect=self.fake_tags),
        ):
            self.assertTrue(run_one())
        self.assertEqual(prepare_mock.call_args.args[2:], (-16, -1.5))

    def test_success_preserves_old_files_and_editorial_choices(self):
        item = self.asset("jingles")
        config = self.config()
        config.current_jingle = item
        config.save()
        old_path = item.prepared_path
        job = self.enqueue()

        def concurrent_edit(*args):
            Asset.objects.filter(pk=item.pk).update(
                title="Edited during conversion", is_active=False
            )
            return self.fake_prepare(*args)

        with (
            patch("library.reprocessing.audio.prepare", side_effect=concurrent_edit),
            patch(
                "library.reprocessing.audio.apply_tags", side_effect=self.fake_tags
            ) as tagged,
        ):
            self.assertTrue(reprocessing.run_one())
        item.refresh_from_db()
        self.assertEqual(item.status, Asset.Status.READY)
        self.assertNotEqual(item.prepared_path, old_path)
        self.assertEqual(data_path(old_path).read_bytes(), b"old prepared audio")
        self.assertEqual(
            data_path(item.prepared_path).read_bytes(), b"new prepared audio"
        )
        self.assertEqual(tagged.call_args.args[2]["title"], "Edited during conversion")
        self.assertFalse(item.is_active)
        self.assertEqual(RadioSettings.objects.get().current_jingle_id, item.pk)
        self.assertEqual(job.items.get().previous_paths, [old_path])
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")

    def test_failure_keeps_radio_and_replay_unchanged(self):
        item = self.asset("podcasts")
        item.replay_path = f"ready/replay/{item.id}.mp3"
        path = data_path(item.replay_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"old replay")
        item.replay_status = "ready"
        item.published_at = timezone.now()
        item.save()
        job = self.enqueue()
        original_path = item.prepared_path
        with (
            patch("library.reprocessing.audio.prepare", side_effect=self.fake_prepare),
            patch(
                "library.reprocessing.publication.encode_mp3",
                side_effect=audio.AudioError("Encoding failed"),
            ),
        ):
            self.assertTrue(reprocessing.run_one())
        item.refresh_from_db()
        self.assertEqual(item.prepared_path, original_path)
        self.assertEqual(path.read_bytes(), b"old replay")
        self.assertTrue(publication.publicly_available(item))
        self.assertEqual(job.items.get().status, "error")

    def test_success_updates_existing_replay_without_republishing_private_podcast(self):
        item = self.asset("podcasts")
        item.auto_publish = False
        item.replay_status = "ready"
        item.replay_path = f"ready/replay/{item.id}.mp3"
        path = data_path(item.replay_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"old replay")
        item.save()
        self.enqueue()

        def encode(source, workspace):
            destination = workspace / "replay.mp3"
            destination.write_bytes(b"new replay")
            return destination, 3, 9999999999

        with (
            patch("library.reprocessing.audio.prepare", side_effect=self.fake_prepare),
            patch("library.reprocessing.audio.apply_tags", side_effect=self.fake_tags),
            patch("library.reprocessing.publication.encode_mp3", side_effect=encode),
        ):
            reprocessing.run_one()
        item.refresh_from_db()
        self.assertEqual(data_path(item.replay_path).read_bytes(), b"new replay")
        self.assertEqual(path.read_bytes(), b"old replay")
        self.assertFalse(publication.publicly_available(item))

    def test_trash_during_conversion_wins_and_failure_after_file_write_keeps_old(self):
        item = self.asset()
        old_path = item.prepared_path
        self.enqueue()

        def trash(*args):
            Asset.objects.filter(pk=item.pk).update(
                trashed_at=timezone.now(), is_active=False
            )
            return self.fake_prepare(*args)

        with patch("library.reprocessing.audio.prepare", side_effect=trash):
            reprocessing.run_one()
        self.assertEqual(AudioReprocessItem.objects.get().status, "skipped")
        Asset.objects.filter(pk=item.pk).update(trashed_at=None)
        self.enqueue()
        with (
            patch("library.reprocessing.audio.prepare", side_effect=self.fake_prepare),
            patch("library.reprocessing.audio.apply_tags", side_effect=self.fake_tags),
            patch.object(Asset, "save", side_effect=OSError("database commit failed")),
        ):
            reprocessing.run_one()
        item.refresh_from_db()
        self.assertEqual(item.prepared_path, old_path)
        self.assertEqual(
            list(data_path("ready/music").iterdir()), [data_path(old_path)]
        )

    def test_recovery_respects_live_locks_and_deleted_assets(self):
        item = self.asset()
        job = self.enqueue()
        task = job.items.get()
        task.status = "working"
        task.save()
        handle = lock_file(item.id)
        try:
            reprocessing.recover()
            task.refresh_from_db()
            self.assertEqual(task.status, "working")
        finally:
            handle.close()
        reprocessing.recover()
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        item.delete()
        reprocessing.run_one()
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_backup_restore_defaults_and_audio_preferences_validation(self):
        columns = [
            "id",
            "jingle_enabled",
            "jingle_every",
            "current_jingle_id",
            "pending_jingle_id",
            "selected_playlist_id",
        ]
        record = restore_data.typed_row(
            RadioSettings, columns, ["1", "t", "3", None, None, None]
        )
        self.assertIsNone(record["audio_target_lufs"])
        columns += ["audio_target_lufs", "audio_ceiling_dbtp"]
        record = restore_data.typed_row(
            RadioSettings, columns, ["1", "t", "3", None, None, None, "-16", "-1.5"]
        )
        self.assertEqual(record["audio_target_lufs"], -16)
        self.assertNotIn(AudioReprocessJob, restore_data.MODELS)

    def test_old_audio_cleanup_waits_for_engine_and_never_deletes_current_files(self):
        from .engine import EngineError

        item = self.asset()
        old = item.prepared_path
        job = self.enqueue()
        task = job.items.get()
        task.status = "done"
        task.previous_paths = [old]
        task.finished_at = timezone.now() - timezone.timedelta(days=2)
        task.save()
        with patch("library.engine.media_in_use", side_effect=EngineError("offline")):
            reprocessing.cleanup_previous()
        self.assertTrue(data_path(old).exists())
        with patch("library.engine.media_in_use", return_value=(set(), set())):
            reprocessing.cleanup_previous()
        self.assertTrue(data_path(old).exists())

    def test_real_audio_and_replay_round_trip_keeps_old_versions_readable(self):
        import hashlib
        import subprocess

        item = self.asset("podcasts")
        original = data_path(item.original_path)
        source = self.root / "fixture.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                str(source),
            ],
            check=True,
        )
        original.write_bytes(source.read_bytes())
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        item.status = "pending"
        item.save()
        self.assertTrue(run_one())
        self.assertTrue(publication.run_one())
        item.refresh_from_db()
        old_flac, old_mp3 = item.prepared_path, item.replay_path
        self.assertTrue(old_mp3)
        self.client.post("/maintenance/audio/", self.values())
        job = self.enqueue()
        # Simulate existing players holding the previous version open.
        with data_path(old_flac).open("rb") as listener:
            self.assertTrue(reprocessing.run_one())
            self.assertEqual(listener.read(4), b"fLaC")
        item.refresh_from_db()
        self.assertEqual(job.items.get().status, "done")
        self.assertNotEqual(item.prepared_path, old_flac)
        self.assertNotEqual(item.replay_path, old_mp3)
        self.assertTrue(data_path(old_mp3).exists())
        self.assertEqual(
            hashlib.sha256(original.read_bytes()).hexdigest(), original_hash
        )
        self.assertAlmostEqual(item.loudness_lufs, -16, delta=0.3)
        self.assertTrue(publication.publicly_available(item))

    def test_cleanup_only_removes_unreferenced_versions_after_retention(self):
        item = self.asset()
        old = item.prepared_path
        job = self.enqueue()
        with (
            patch("library.reprocessing.audio.prepare", side_effect=self.fake_prepare),
            patch("library.reprocessing.audio.apply_tags", side_effect=self.fake_tags),
        ):
            reprocessing.run_one()
        task = job.items.get()
        with patch("library.engine.media_in_use", return_value=(set(), set())):
            reprocessing.cleanup_previous()
        self.assertTrue(data_path(old).exists())
        task.finished_at = timezone.now() - timezone.timedelta(days=2)
        task.save()
        with patch("library.engine.media_in_use", return_value=({str(item.id)}, set())):
            reprocessing.cleanup_previous()
        self.assertTrue(data_path(old).exists())
        with patch("library.engine.media_in_use", return_value=(set(), set())):
            reprocessing.cleanup_previous()
        self.assertFalse(data_path(old).exists())
        item.refresh_from_db()
        self.assertTrue(data_path(item.prepared_path).exists())

    def test_bootstrap_setup_saves_explicit_audio_choices(self):
        get_user_model().objects.all().delete()
        prepare()
        code = data_path("installation-code.txt").read_text().strip()
        response = Client().post(
            "/setup/",
            {
                "code": code,
                "radio_name": "Radio",
                "username": "first",
                "password": "A-radio-password-2026!",
                "password_repeat": "A-radio-password-2026!",
                "start": "empty",
                "audio_target_lufs": -18,
                "audio_ceiling_dbtp": -2,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(effective(), (-18, -2))


class PostgreSQLAudioPreferencesTests(TransactionTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = override_settings(MEDIA_ROOT=Path(directory.name))
        override.enable()
        self.addCleanup(override.disable)

    def test_two_concurrent_requests_start_only_one_job(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from django.db import connection, connections

        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL row locking")
        user = get_user_model().objects.create_superuser(
            "audio-concurrency", password="test"
        )
        RadioSettings.objects.update_or_create(
            pk=1, defaults={"audio_target_lufs": -16, "audio_ceiling_dbtp": -1.5}
        )
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(user)
        barrier = Barrier(2)

        def request(client):
            try:
                barrier.wait(timeout=10)
                return client.post(
                    "/maintenance/audio/",
                    {
                        "action": "reprocess",
                        "confirm": "yes",
                        "audio_target_lufs": -16,
                        "audio_ceiling_dbtp": -1.5,
                    },
                ).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(request, clients))
        self.assertEqual(sorted(statuses), [200, 302])
        self.assertEqual(AudioReprocessJob.objects.filter(status="active").count(), 1)
