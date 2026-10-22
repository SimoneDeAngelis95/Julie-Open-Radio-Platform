"""Playlist workflows and removal safeguards exercise real files in disposable dirs."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from . import engine, tests
from .models import Asset, Broadcast, Playlist, RemovalJob
from .removal import RemovalBlocked, begin_removal, recover_removals
from .storage import data_path, save_upload
from .worker import lock_file


class OrganizationTests(TestCase):
    setUp = tests.LibraryTests.setUp
    tearDown = tests.LibraryTests.tearDown

    def song(self, content=b"song", category="music"):
        return save_upload(SimpleUploadedFile("song.mp3", content), category)[0]

    def trashed(self, content=b"trashed", category="music"):
        asset = self.song(content, category)
        asset.status = "ready"
        asset.prepared_path = f"ready/{asset.id}.flac"
        asset.replay_path = f"ready/{asset.id}.mp3"
        asset.trashed_at = timezone.now() - timedelta(seconds=10)
        asset.is_active = False
        asset.duration_seconds = 20
        for relative in [asset.prepared_path, asset.replay_path]:
            path = data_path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"prepared")
        asset.save()
        return asset

    def selection(self, path):
        return self.client.get(path).context["form"].initial["selection"]

    def remove(self, asset=None, **extra):
        path = f"/trash/{asset.id}/files/" if asset else "/trash/empty/"
        values = {"selection": self.selection(path), "confirm": "on"}
        values.update(extra)
        with patch("library.engine.media_in_use", return_value=(set(), set())):
            return self.client.post(path, values, follow=True)

    def test_upload_assigns_multiple_playlists_to_every_song_and_duplicates_add(self):
        first = Playlist.objects.create(name="Mattina")
        second = Playlist.objects.create(name="Sera")

        def upload(playlists, files):
            return self.client.post(
                "/upload/",
                {"category": "music", "playlists": playlists, "files": files},
            )

        upload(
            [first.id, second.id],
            [SimpleUploadedFile("a.mp3", b"a"), SimpleUploadedFile("b.mp3", b"b")],
        )
        self.assertEqual(Asset.objects.count(), 2)
        for asset in Asset.objects.all():
            self.assertEqual(asset.playlists.count(), 2)
        third = Playlist.objects.create(name="Notte")
        upload([third.id], [SimpleUploadedFile("duplicate.mp3", b"a")])
        self.assertEqual(Asset.objects.count(), 2)
        self.assertEqual(
            Asset.objects.get(
                checksum=__import__("hashlib").sha256(b"a").hexdigest()
            ).playlists.count(),
            3,
        )
        self.assertContains(self.client.get("/"), "Mattina")
        self.assertEqual(
            self.client.get(f"/?playlist={third.id}").context["page"].paginator.count, 1
        )
        self.assertEqual(
            self.client.get("/?playlist=none").context["page"].paginator.count, 0
        )

    def test_podcast_upload_cannot_join_music_playlists(self):
        playlist = Playlist.objects.create(name="Music")
        response = self.client.post(
            "/upload/",
            {
                "category": "podcasts",
                "playlists": [playlist.id],
                "files": SimpleUploadedFile("p.mp3", b"p"),
            },
        )
        self.assertContains(response, "soltanto per la musica")
        self.assertFalse(Asset.objects.exists())

    def test_detail_edits_memberships_and_podcast_can_be_reenabled(self):
        asset = self.song()
        playlist = Playlist.objects.create(name="Mix")
        response = self.client.post(
            f"/library/{asset.id}/",
            {
                "title": "Nuovo",
                "artist": "Artista",
                "is_active": "on",
                "playlists": [playlist.id],
            },
        )
        self.assertEqual(response.status_code, 302)
        asset.refresh_from_db()
        self.assertEqual(asset.title, "Nuovo")
        self.assertEqual(list(asset.playlists.all()), [playlist])
        podcast = self.song(b"podcast", "podcasts")
        podcast.is_active = False
        podcast.save()
        self.assertContains(
            self.client.get(f"/library/{podcast.id}/"),
            "Disponibile per la programmazione",
        )
        self.client.post(
            f"/library/{podcast.id}/", {"title": "Podcast", "is_active": "on"}
        )
        podcast.refresh_from_db()
        self.assertTrue(podcast.is_active)

    def test_bulk_add_replace_remove_and_reject_podcast_or_trashed_selection(self):
        a = self.song(b"a")
        b = self.song(b"b")
        p = Playlist.objects.create(name="One")
        q = Playlist.objects.create(name="Two")
        for operation, chosen, expected in [
            ("add", [p.id], {p.id}),
            ("add", [q.id], {p.id, q.id}),
            ("remove", [p.id], {q.id}),
            ("replace", [], set()),
        ]:
            response = self.client.post(
                "/library/playlists/assign/",
                {"assets": [a.id, b.id], "playlists": chosen, "operation": operation},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(set(a.playlists.values_list("id", flat=True)), expected)
            self.assertEqual(set(b.playlists.values_list("id", flat=True)), expected)
        invalid = self.song(b"p", "podcasts")
        trashed = self.trashed()
        for item in [invalid, trashed]:
            self.client.post(
                "/library/playlists/assign/",
                {"assets": [a.id, item.id], "playlists": [p.id], "operation": "add"},
            )
            self.assertEqual(a.playlists.count(), 0)

    def test_partial_removal_keeps_original_memberships_and_restore_requeues(self):
        asset = self.trashed()
        original = data_path(asset.original_path)
        prepared = data_path(asset.prepared_path)
        replay = data_path(asset.replay_path)
        playlist = Playlist.objects.create(name="Keep")
        asset.playlists.add(playlist)
        response = self.remove(asset)
        self.assertContains(response, "Solo originale conservato")
        self.assertTrue(original.exists())
        self.assertFalse(prepared.exists())
        self.assertFalse(replay.exists())
        asset.refresh_from_db()
        self.assertTrue(asset.copies_removed)
        self.assertFalse(asset.removal_pending)
        self.assertEqual(asset.playlists.count(), 1)
        self.client.post(f"/library/{asset.id}/restore/")
        asset.refresh_from_db()
        self.assertEqual(asset.status, "pending")
        self.assertIsNone(asset.trashed_at)
        self.assertFalse(asset.is_active)
        self.assertFalse(asset.copies_removed)

    def test_full_removal_preserves_history_cancels_future_and_allows_reupload(self):
        asset = self.trashed(category="podcasts")
        old = Broadcast.objects.create(
            asset=asset,
            scheduled_at=timezone.now(),
            status="completed",
            outcome="Finito",
        )
        future = Broadcast.objects.create(
            asset=asset, scheduled_at=timezone.now() + timedelta(days=1)
        )
        original = data_path(asset.original_path)
        prepared = data_path(asset.prepared_path)
        response = self.remove(asset, delete_original="on")
        asset.refresh_from_db()
        old.refresh_from_db()
        future.refresh_from_db()
        self.assertIsNotNone(asset.purged_at)
        self.assertFalse(original.exists())
        self.assertFalse(prepared.exists())
        self.assertNotContains(response, asset.original_name)
        self.assertEqual(old.outcome, "Finito")
        self.assertEqual(old.status, "completed")
        self.assertEqual(future.status, "cancelled")
        self.assertEqual(
            self.client.post(f"/library/{asset.id}/restore/").status_code, 404
        )
        reuploaded, created = save_upload(
            SimpleUploadedFile("again.mp3", b"trashed"), "podcasts"
        )
        self.assertTrue(created)
        self.assertNotEqual(asset.id, reuploaded.id)

    def test_confirmation_required_default_original_unchecked_and_get_is_read_only(
        self,
    ):
        asset = self.trashed()
        path = f"/trash/{asset.id}/files/"
        page = self.client.get(path)
        self.assertFalse(page.context["form"].initial.get("delete_original", False))
        self.client.post(
            path, {"selection": self.selection(path), "delete_original": "on"}
        )
        self.assertTrue(data_path(asset.original_path).exists())
        self.assertEqual(RemovalJob.objects.count(), 0)
        self.client.post(
            path, {"selection": "forged", "delete_original": "on", "confirm": "on"}
        )
        self.assertTrue(data_path(asset.original_path).exists())

    def test_empty_trash_uses_reviewed_snapshot_and_excludes_busy_assets(self):
        first = self.trashed(b"one")
        busy = self.trashed(b"two")
        busy.status = "processing"
        busy.save()
        token = self.selection("/trash/empty/")
        later = self.trashed(b"new")
        with patch("library.engine.media_in_use", return_value=(set(), set())):
            self.client.post(
                "/trash/empty/",
                {"selection": token, "delete_original": "on", "confirm": "on"},
            )
        first.refresh_from_db()
        busy.refresh_from_db()
        later.refresh_from_db()
        self.assertIsNotNone(first.purged_at)
        self.assertIsNone(busy.purged_at)
        self.assertIsNone(later.purged_at)
        self.assertTrue(data_path(busy.original_path).exists())
        self.assertTrue(data_path(later.original_path).exists())

    def test_in_use_request_original_and_worker_locks_block_removal(self):
        asset = self.trashed()
        path = asset.prepared_path
        for usage in [({str(asset.id)}, set()), (set(), {"/audio/library/" + path})]:
            with self.assertRaises(RemovalBlocked):
                begin_removal(asset.id, True, usage)
        handle = lock_file(asset.id)
        try:
            with self.assertRaises(RemovalBlocked):
                begin_removal(asset.id, True, (set(), set()))
        finally:
            handle.close()
        self.assertTrue(data_path(asset.original_path).exists())
        self.assertFalse(RemovalJob.objects.exists())

    def test_broadcast_in_startup_blocks_file_deletion(self):
        asset = self.trashed(category="podcasts")
        Broadcast.objects.create(
            asset=asset, scheduled_at=timezone.now(), status="dispatching"
        )
        with self.assertRaises(RemovalBlocked):
            begin_removal(asset.id, True, (set(), set()))
        self.assertTrue(data_path(asset.original_path).exists())

    def test_unreachable_engine_leaves_files_and_appointments_untouched(self):
        asset = self.trashed(category="podcasts")
        job = Broadcast.objects.create(
            asset=asset, scheduled_at=timezone.now() + timedelta(days=1)
        )
        path = f"/trash/{asset.id}/files/"
        token = self.selection(path)
        with patch(
            "library.engine.media_in_use", side_effect=engine.EngineError("offline")
        ):
            self.client.post(
                path, {"selection": token, "confirm": "on", "delete_original": "on"}
            )
        self.assertTrue(data_path(asset.original_path).exists())
        job.refresh_from_db()
        self.assertEqual(job.status, "scheduled")

    def test_disk_error_persists_intent_blocks_restore_and_retries(self):
        asset = self.trashed()
        original = data_path(asset.original_path)
        real_unlink = Path.unlink

        def fail_copy(path, *args, **kwargs):
            if path.suffix == ".flac":
                raise OSError("disk error")
            return real_unlink(path, *args, **kwargs)

        with (
            patch("library.engine.media_in_use", return_value=(set(), set())),
            patch("pathlib.Path.unlink", fail_copy),
        ):
            self.assertFalse(begin_removal(asset.id, True, (set(), set())))
        asset.refresh_from_db()
        self.assertTrue(asset.removal_pending)
        self.client.post(f"/library/{asset.id}/restore/")
        asset.refresh_from_db()
        self.assertIsNotNone(asset.trashed_at)
        with patch("library.engine.media_in_use", return_value=(set(), set())):
            recover_removals()
        asset.refresh_from_db()
        self.assertFalse(asset.removal_pending)
        self.assertIsNotNone(asset.purged_at)
        self.assertFalse(original.exists())

    def test_symlinks_directories_and_shared_paths_are_not_deleted(self):
        asset = self.trashed()
        target = data_path("unrelated.flac")
        target.write_bytes(b"safe")
        path = data_path(asset.prepared_path)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(RemovalBlocked):
            begin_removal(asset.id, True, (set(), set()))
        self.assertTrue(target.exists())
        path.unlink()
        path.mkdir()
        with self.assertRaises(RemovalBlocked):
            begin_removal(asset.id, True, (set(), set()))
        path.rmdir()
        path.write_bytes(b"file")
        other = self.song(b"other")
        other.prepared_path = asset.prepared_path
        other.save()
        with self.assertRaises(RemovalBlocked):
            begin_removal(asset.id, True, (set(), set()))
        self.assertTrue(path.exists())

    def test_auth_and_csrf_for_bulk_and_removal(self):
        asset = self.trashed()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(
            csrf.post(f"/trash/{asset.id}/files/", {"confirm": "on"}).status_code, 403
        )
        self.assertEqual(csrf.post("/library/playlists/assign/", {}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get("/trash/empty/").status_code, 302)
        self.assertEqual(
            self.client.post("/library/playlists/assign/", {}).status_code, 302
        )

    def test_engine_inventory_includes_queued_requests_and_fails_closed(self):
        replies = {
            "request.resolving": "",
            "request.all": "1 2",
            "request.metadata 1": 'status="ready"\njulie_id="abc"\nfilename="/audio/one"',
            "request.metadata 2": 'status="ready"\nfilename="/audio/two"',
        }
        with patch("library.engine.command", side_effect=lambda value: replies[value]):
            identifiers, paths = engine.media_in_use()
        self.assertEqual(identifiers, {"abc"})
        self.assertEqual(paths, {"/audio/one", "/audio/two"})
        with patch("library.engine.command", return_value="3"):
            with self.assertRaises(engine.EngineError):
                engine.media_in_use()
