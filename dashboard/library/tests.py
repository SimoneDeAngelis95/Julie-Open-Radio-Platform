"""User-visible behavior and failure handling, not implementation snapshots."""

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from . import audio
from .models import Asset
from .storage import data_path, save_upload
from .worker import recover_interrupted, run_one


class LibraryTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.directory.name))
        self.override.enable()
        self.user = get_user_model().objects.create_user(
            "simone", password="test-pass", is_staff=True
        )
        self.client.force_login(self.user)

    def tearDown(self):
        self.override.disable()
        self.directory.cleanup()

    def upload(self, content=b"fake audio", name="brano.mp3", category="music"):
        return self.client.post(
            reverse("library:upload"),
            {
                "category": category,
                "files": SimpleUploadedFile(name, content, content_type="audio/mpeg"),
            },
        )

    def asset(self, content=b"fake audio"):
        return save_upload(SimpleUploadedFile("brano.mp3", content), "music")[0]

    def test_pages_and_audio_require_an_administrator(self):
        asset = self.asset()
        self.client.logout()
        for url in [
            "/",
            "/upload/",
            "/api/library/status/",
            reverse("library:audio", args=[asset.id]),
        ]:
            self.assertEqual(self.client.get(url).status_code, 302)
        ordinary = get_user_model().objects.create_user("listener", password="test")
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_upload_is_durable_and_not_published(self):
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get()
        self.assertEqual(data_path(asset.original_path).read_bytes(), b"fake audio")
        self.assertEqual(asset.checksum, hashlib.sha256(b"fake audio").hexdigest())
        self.assertEqual(asset.status, Asset.Status.PENDING)
        self.assertEqual(
            self.client.get(reverse("library:audio", args=[asset.id])).status_code, 404
        )

    def test_duplicates_do_not_add_another_job(self):
        self.upload()
        self.upload(name="renamed.mp3")
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(len(list(data_path("originals").iterdir())), 1)
        self.upload(category="podcasts")
        self.assertEqual(Asset.objects.count(), 2)

    def test_multiple_uploads_and_invalid_extension(self):
        files = [
            SimpleUploadedFile("a.mp3", b"one"),
            SimpleUploadedFile("b.flac", b"two"),
        ]
        self.assertEqual(
            self.client.post(
                "/upload/", {"category": "music", "files": files}
            ).status_code,
            302,
        )
        self.assertEqual(Asset.objects.count(), 2)
        response = self.upload(name="script.html")
        self.assertContains(response, "formato non supportato")
        self.assertEqual(Asset.objects.count(), 2)

    def test_failed_original_remains_available_for_retry(self):
        asset = self.asset()
        with self.assertLogs("library.worker", level="ERROR"):
            self.assertTrue(run_one())
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.ERROR)
        self.assertTrue(data_path(asset.original_path).is_file())
        self.assertFalse(asset.prepared_path)
        self.client.post(reverse("library:retry", args=[asset.id]))
        asset.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.PENDING)

    def test_edit_and_retry_require_csrf(self):
        asset = self.asset()
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(
            protected.post(
                reverse("library:detail", args=[asset.id]), {"title": "Changed"}
            ).status_code,
            403,
        )
        self.assertEqual(
            protected.post(reverse("library:retry", args=[asset.id])).status_code, 403
        )
        self.assertEqual(
            self.client.get(reverse("library:retry", args=[asset.id])).status_code, 405
        )

    def test_metadata_edit_preserves_processing_state(self):
        asset = self.asset()
        Asset.objects.filter(pk=asset.id).update(status=Asset.Status.PROCESSING)
        self.client.post(
            reverse("library:detail", args=[asset.id]),
            {"title": "Titolo nuovo", "artist": "Artista", "composer": "", "album": ""},
        )
        asset.refresh_from_db()
        self.assertEqual(asset.title, "Titolo nuovo")
        self.assertTrue(asset.metadata_edited)
        self.assertEqual(asset.status, Asset.Status.PROCESSING)

    def test_audio_range_and_missing_file(self):
        asset = self.asset()
        path = data_path("ready/music/test.flac")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"0123456789")
        Asset.objects.filter(pk=asset.id).update(
            status="ready", prepared_path="ready/music/test.flac"
        )
        url = reverse("library:audio", args=[asset.id])
        response = self.client.get(url, HTTP_RANGE="bytes=2-5")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"2345")
        response = self.client.get(url, HTTP_RANGE="bytes=-3")
        self.assertEqual(b"".join(response.streaming_content), b"789")
        self.assertEqual(self.client.get(url, HTTP_RANGE="bytes=100-").status_code, 416)
        path.unlink()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_search_filters_and_polling(self):
        self.asset()
        response = self.client.get("/?q=brano&category=music")
        self.assertContains(response, "brano")
        self.assertEqual(
            self.client.get("/api/library/status/?category=jingles").json()["assets"],
            [],
        )
        self.assertEqual(
            self.client.get("/api/library/status/").json()["counts"]["waiting"], 1
        )

    def test_interrupted_job_recovers_but_active_job_does_not(self):
        from .worker import lock_file

        asset = self.asset()
        Asset.objects.filter(pk=asset.id).update(status="processing")
        handle = lock_file(asset.id)
        recover_interrupted()
        asset.refresh_from_db()
        self.assertEqual(asset.status, "processing")
        handle.close()
        recover_interrupted()
        asset.refresh_from_db()
        self.assertEqual(asset.status, "pending")

    def test_ajax_upload_validation_and_success(self):
        response = self.client.post(
            "/upload/",
            {"category": "music", "files": SimpleUploadedFile("bad.exe", b"abc")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/upload/",
            {"category": "music", "files": SimpleUploadedFile("good.mp3", b"abc")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.json()["redirect"], "/")

    def test_normalization_and_source_tags_end_to_end(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        source = Path(self.directory.name) / "fixture.wav"
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
                "-metadata",
                "title=Titolo sorgente",
                "-metadata",
                "artist=Artista sorgente",
                str(source),
            ],
            check=True,
        )
        asset, _ = save_upload(
            SimpleUploadedFile("test.wav", source.read_bytes()), "music"
        )
        self.assertTrue(run_one())
        asset.refresh_from_db()
        self.assertEqual(asset.status, "ready")
        self.assertEqual(asset.title, "Titolo sorgente")
        self.assertEqual(asset.artist, "Artista sorgente")
        self.assertLessEqual(asset.true_peak_dbtp, -1.5)
        self.assertAlmostEqual(asset.loudness_lufs, -20, delta=0.3)
        self.assertAlmostEqual(asset.duration_seconds, 3, delta=0.1)
        self.assertTrue(data_path(asset.prepared_path).is_file())
        self.assertEqual(
            data_path(asset.original_path).read_bytes(), source.read_bytes()
        )
        self.assertFalse(run_one())

    def test_metadata_edits_during_conversion_win_over_tags(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        source = Path(self.directory.name) / "fixture.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=3",
                "-metadata",
                "title=Old title",
                str(source),
            ],
            check=True,
        )
        asset, _ = save_upload(
            SimpleUploadedFile("test.wav", source.read_bytes()), "music"
        )
        real_prepare = audio.prepare

        def concurrent_edit(*args, **kwargs):
            result = real_prepare(*args, **kwargs)
            Asset.objects.filter(pk=asset.id).update(
                title="Titolo scelto", metadata_edited=True
            )
            return result

        with patch("library.worker.audio.prepare", side_effect=concurrent_edit):
            run_one()
        asset.refresh_from_db()
        self.assertEqual(asset.status, "ready")
        self.assertEqual(asset.title, "Titolo scelto")


class RotationTests(TestCase):
    setUp = LibraryTests.setUp
    tearDown = LibraryTests.tearDown
    upload = LibraryTests.upload
    asset = LibraryTests.asset
    """Selection eligibility and safe replacement are the playout contract."""

    def ready(self, category="music", title="Brano", content=b"audio", active=True):
        asset, _ = save_upload(SimpleUploadedFile(title + ".mp3", content), category)
        relative = f"ready/{category}/{asset.id}.flac"
        path = data_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"verified test placeholder")
        Asset.objects.filter(pk=asset.id).update(
            status="ready", prepared_path=relative, is_active=active
        )
        asset.refresh_from_db()
        return asset

    def test_snapshot_only_includes_ready_active_music(self):
        from .playout import publish_snapshot

        a = self.ready()
        inactive = self.ready(title="Inactive", content=b"inactive", active=False)
        podcast = self.ready(category="podcasts", title="Podcast", content=b"podcast")
        self.asset(b"pending")
        publish_snapshot()
        content = data_path("playout/rotation.txt").read_text()
        self.assertIn(str(a.id), content)
        for asset in [inactive, podcast]:
            self.assertNotIn(str(asset.id), content)
        self.assertEqual(content.splitlines()[:2], ["0", ""])
        data_path(a.prepared_path).unlink()
        publish_snapshot()
        self.assertNotIn(str(a.id), data_path("playout/rotation.txt").read_text())

    def test_single_jingle_and_disabled_snapshot(self):
        from .models import RadioSettings
        from .playout import publish_snapshot

        jingle = self.ready(category="jingles", title="Jingle", content=b"jingle")
        extra = self.ready(category="jingles", title="Old", content=b"old")
        RadioSettings.objects.filter(pk=1).update(current_jingle=jingle, jingle_every=5)
        publish_snapshot()
        content = data_path("playout/rotation.txt").read_text()
        self.assertTrue(content.startswith("5\n"))
        self.assertIn(str(jingle.id), content)
        self.assertNotIn(str(extra.id), content)
        RadioSettings.objects.filter(pk=1).update(jingle_enabled=False)
        publish_snapshot()
        self.assertEqual(data_path("playout/rotation.txt").read_text(), "0\n\n")

    def test_jingles_are_not_in_library_or_generic_upload(self):
        jingle = self.ready(
            category="jingles", title="Hidden jingle", content=b"jingle"
        )
        self.assertNotContains(self.client.get("/"), "Hidden jingle")
        self.assertEqual(
            self.client.get("/api/library/status/").json()["counts"]["total"], 0
        )
        self.assertNotIn(
            '<option value="jingles"', self.client.get("/upload/").content.decode()
        )
        self.assertEqual(self.upload(category="jingles").status_code, 200)
        self.assertEqual(Asset.objects.count(), 1)

    def test_settings_keep_current_while_replacement_is_pending_or_failed(self):
        from .models import RadioSettings

        current = self.ready(category="jingles", title="Current", content=b"old")
        RadioSettings.objects.filter(pk=1).update(current_jingle=current)
        self.client.post(
            "/settings/",
            {
                "jingle_enabled": "on",
                "jingle_every": 3,
                "replacement": SimpleUploadedFile("new.mp3", b"invalid"),
            },
        )
        config = RadioSettings.objects.get(pk=1)
        self.assertEqual(config.current_jingle, current)
        self.assertIsNotNone(config.pending_jingle)
        with self.assertLogs("library.worker", level="ERROR"):
            run_one()
        config.refresh_from_db()
        self.assertEqual(config.current_jingle, current)
        self.assertEqual(config.pending_jingle.status, "error")
        self.assertContains(self.client.get("/settings/"), "Riprova il nuovo jingle")

    def test_ready_replacement_is_promoted_without_conversion(self):
        from .models import RadioSettings

        jingle = self.ready(category="jingles", title="Existing", content=b"same")
        self.client.post(
            "/settings/",
            {
                "jingle_enabled": "on",
                "jingle_every": 4,
                "replacement": SimpleUploadedFile("renamed.mp3", b"same"),
            },
        )
        config = RadioSettings.objects.get(pk=1)
        self.assertEqual(config.current_jingle, jingle)
        self.assertIsNone(config.pending_jingle)
        self.assertEqual(Asset.objects.count(), 1)

    def test_interval_is_validated_and_metadata_cannot_inject_new_lines(self):
        from .playout import publish_snapshot
        from .models import RadioSettings

        self.client.post("/settings/", {"jingle_enabled": "on", "jingle_every": 0})
        self.assertEqual(RadioSettings.objects.get(pk=1).jingle_every, 3)
        a = self.ready(title='Title "quoted"\nnot-a-path')
        publish_snapshot()
        lines = data_path("playout/rotation.txt").read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('\\"quoted\\"', lines[2])

    def test_successful_worker_promotes_only_the_requested_replacement(self):
        from .models import RadioSettings

        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        source = Path(self.directory.name) / "new.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=3",
                str(source),
            ],
            check=True,
        )
        asset, _ = save_upload(
            SimpleUploadedFile("new.wav", source.read_bytes()), "jingles"
        )
        RadioSettings.objects.filter(pk=1).update(pending_jingle=asset)
        run_one()
        config = RadioSettings.objects.get(pk=1)
        self.assertEqual(config.current_jingle, asset)
        self.assertIsNone(config.pending_jingle)


class PlaylistTrashTests(TestCase):
    setUp = LibraryTests.setUp
    tearDown = LibraryTests.tearDown
    asset = LibraryTests.asset
    upload = LibraryTests.upload
    ready = RotationTests.ready

    def test_trash_preserves_original_and_playlist_but_excludes_everywhere(self):
        from .models import Playlist
        from .playout import publish_snapshot

        asset = self.ready()
        playlist = Playlist.objects.create(name="Classica")
        playlist.assets.add(asset)
        response = self.client.post(reverse("library:trash_asset", args=[asset.id]))
        self.assertEqual(response.status_code, 302)
        asset.refresh_from_db()
        self.assertIsNotNone(asset.trashed_at)
        self.assertFalse(asset.is_active)
        self.assertTrue(data_path(asset.original_path).exists())
        self.assertTrue(data_path(asset.prepared_path).exists())
        self.assertTrue(playlist.assets.filter(pk=asset.pk).exists())
        self.assertEqual(
            self.client.get("/api/library/status/").json()["counts"]["total"], 0
        )
        self.assertEqual(
            self.client.get(reverse("library:audio", args=[asset.id])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("library:detail", args=[asset.id])).status_code, 404
        )
        self.assertContains(self.client.get("/trash/"), "Brano")
        publish_snapshot()
        self.assertNotIn(str(asset.id), data_path("playout/rotation.txt").read_text())

    def test_restore_keeps_membership_and_requires_reactivation(self):
        from .models import Playlist

        asset = self.ready()
        playlist = Playlist.objects.create(name="Intervallo")
        playlist.assets.add(asset)
        self.client.post(reverse("library:trash_asset", args=[asset.id]))
        self.client.post(reverse("library:restore_asset", args=[asset.id]))
        asset.refresh_from_db()
        self.assertIsNone(asset.trashed_at)
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, "ready")
        self.assertTrue(playlist.assets.filter(pk=asset.pk).exists())
        self.assertEqual(
            self.client.get(reverse("library:audio", args=[asset.id])).status_code, 200
        )

    def test_trash_and_restore_require_csrf_and_post_and_reject_jingle(self):
        asset = self.ready()
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.user)
        url = reverse("library:trash_asset", args=[asset.id])
        self.assertEqual(protected.post(url).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.assertEqual(
            protected.post(
                reverse("library:restore_asset", args=[asset.id])
            ).status_code,
            403,
        )
        jingle = self.ready(category="jingles", content=b"jingle")
        self.assertEqual(
            self.client.post(
                reverse("library:trash_asset", args=[jingle.id])
            ).status_code,
            404,
        )

    def test_trashed_pending_job_is_not_claimed_and_duplicate_upload_guides_restore(
        self,
    ):
        asset = self.asset()
        self.client.post(reverse("library:trash_asset", args=[asset.id]))
        self.assertFalse(run_one())
        response = self.client.post(
            "/upload/",
            {
                "category": "music",
                "files": SimpleUploadedFile("again.mp3", b"fake audio"),
            },
            follow=True,
        )
        self.assertContains(response, "sono già nel cestino")
        self.assertEqual(Asset.objects.count(), 1)

    def test_selected_playlist_limits_snapshot_without_copying_files(self):
        from .models import Playlist, RadioSettings
        from .playout import publish_snapshot

        selected = self.ready(title="Inside", content=b"inside")
        outside = self.ready(title="Outside", content=b"outside")
        inactive = self.ready(title="Inactive", content=b"inactive", active=False)
        playlist = Playlist.objects.create(name="Mattina")
        playlist.assets.add(selected, inactive)
        self.client.post(reverse("library:playlist_activate", args=[playlist.id]))
        self.assertEqual(RadioSettings.objects.get(pk=1).selected_playlist, playlist)
        publish_snapshot()
        snapshot = data_path("playout/rotation.txt").read_text()
        self.assertIn(str(selected.id), snapshot)
        self.assertNotIn(str(outside.id), snapshot)
        self.assertNotIn(str(inactive.id), snapshot)
        self.assertEqual(len(list(data_path("originals").iterdir())), 3)
        self.client.post(
            "/playlists/",
            {"selected_playlist": ""},
        )
        self.assertIsNone(RadioSettings.objects.get(pk=1).selected_playlist)
        self.assertIn(str(outside.id), data_path("playout/rotation.txt").read_text())

    def test_empty_playlist_cannot_replace_current_rotation(self):
        from .models import Playlist, RadioSettings

        playlist = Playlist.objects.create(name="Vuota")
        response = self.client.post(
            reverse("library:playlist_activate", args=[playlist.id]), follow=True
        )
        self.assertContains(response, "non ha brani pronti e attivi")
        self.assertIsNone(RadioSettings.objects.get(pk=1).selected_playlist)
        response = self.client.post(
            "/playlists/",
            {"selected_playlist": playlist.id},
        )
        self.assertContains(response, "non ha ancora brani pronti e attivi")
        self.assertIsNone(RadioSettings.objects.get(pk=1).selected_playlist)

    def test_playlist_form_rejects_podcast_and_trashed_song(self):
        podcast = self.ready(category="podcasts", content=b"podcast")
        song = self.ready(content=b"song")
        self.client.post(reverse("library:trash_asset", args=[song.id]))
        for asset in [podcast, song]:
            response = self.client.post(
                "/playlists/new/", {"name": "Invalid", "assets": [asset.id]}
            )
            self.assertEqual(response.status_code, 200)
        from .models import Playlist

        self.assertEqual(Playlist.objects.count(), 0)

    def test_editing_playlist_preserves_trashed_membership(self):
        from .models import Playlist

        asset = self.ready()
        playlist = Playlist.objects.create(name="Prima")
        playlist.assets.add(asset)
        self.client.post(reverse("library:trash_asset", args=[asset.id]))
        response = self.client.post(
            reverse("library:playlist_edit", args=[playlist.id]),
            {"name": "Dopo", "description": "", "assets": []},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(playlist.assets.filter(pk=asset.id).exists())

    def test_trashing_during_conversion_does_not_publish_the_result(self):
        from .playout import publish_snapshot

        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        source = Path(self.directory.name) / "input.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=3",
                str(source),
            ],
            check=True,
        )
        asset, _ = save_upload(
            SimpleUploadedFile("input.wav", source.read_bytes()), "music"
        )
        original_prepare = audio.prepare

        def concurrent_trash(*args, **kwargs):
            result = original_prepare(*args, **kwargs)
            self.client.post(reverse("library:trash_asset", args=[asset.id]))
            return result

        with patch("library.worker.audio.prepare", side_effect=concurrent_trash):
            run_one()
        asset.refresh_from_db()
        self.assertEqual(asset.status, "ready")
        self.assertIsNotNone(asset.trashed_at)
        publish_snapshot()
        self.assertNotIn(str(asset.id), data_path("playout/rotation.txt").read_text())

    def test_playlist_selection_is_on_playlists_and_jingle_save_preserves_it(self):
        from .models import Playlist, RadioSettings

        song = self.ready(content=b"playlist-location")
        playlist = Playlist.objects.create(name="Playlist attiva")
        playlist.assets.add(song)
        response = self.client.post("/playlists/", {"selected_playlist": playlist.pk})
        self.assertEqual(response.status_code, 302)
        config = RadioSettings.objects.get(pk=1)
        self.assertEqual(config.selected_playlist_id, playlist.pk)
        self.assertContains(self.client.get("/playlists/"), 'name="selected_playlist"')
        self.assertNotContains(
            self.client.get("/settings/"), 'name="selected_playlist"'
        )
        self.client.post(
            "/settings/",
            {"jingle_enabled": "on", "jingle_every": 7, "selected_playlist": ""},
        )
        config.refresh_from_db()
        self.assertEqual(config.selected_playlist_id, playlist.pk)
        self.assertEqual(config.jingle_every, 7)
        self.client.post("/playlists/", {"selected_playlist": ""})
        config.refresh_from_db()
        self.assertIsNone(config.selected_playlist_id)
        self.assertEqual(config.jingle_every, 7)

    def test_playlist_selection_requires_staff_and_csrf(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(
            csrf.post("/playlists/", {"selected_playlist": ""}).status_code, 403
        )
        self.client.logout()
        self.assertEqual(
            self.client.post("/playlists/", {"selected_playlist": ""}).status_code, 302
        )
