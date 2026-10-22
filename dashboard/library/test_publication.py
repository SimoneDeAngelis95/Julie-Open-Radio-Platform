"""Publication policy, direct-link privacy, HTTP seeking and real MP3 creation."""

import hashlib
import shutil
import subprocess
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from . import publication, tests, worker
from .audio import AudioError
from .models import Asset, Broadcast
from .scheduler import finish
from .storage import data_path, save_upload


class PublicationTests(TestCase):
    setUp = tests.LibraryTests.setUp
    tearDown = tests.LibraryTests.tearDown

    def asset(self, content=b"podcast", **overrides):
        asset = save_upload(SimpleUploadedFile("episode.mp3", content), "podcasts")[0]
        asset.status = "ready"
        asset.duration_seconds = 1
        asset.prepared_path = f"ready/podcasts/{asset.id}.flac"
        path = data_path(asset.prepared_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"flac")
        overrides.setdefault("auto_publish", False)
        for key, value in overrides.items():
            setattr(asset, key, value)
        asset.save()
        return asset

    def public(self, content=b"public", **overrides):
        now = timezone.now()
        asset = self.asset(
            content,
            auto_publish=True,
            first_broadcast_at=now - timedelta(seconds=1),
            first_completed_at=now,
            published_at=now,
            replay_status="ready",
            **overrides,
        )
        asset.replay_path = f"ready/replay/{asset.id}.mp3"
        path = data_path(asset.replay_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"0123456789abcdefghijklmnopqrstuvwxyz")
        asset.save()
        return asset

    def urls(self, asset):
        prefix = f"/api/v1/podcasts/{asset.id}/"
        return prefix, prefix + "audio.mp3"

    def test_unavailable_episode_is_private_even_with_a_guessed_audio_url(self):
        asset = self.asset()
        anonymous = Client()
        self.assertFalse(asset.auto_publish)
        self.assertEqual(anonymous.get("/api/v1/podcasts/").json()["count"], 0)
        for url in self.urls(asset):
            self.assertEqual(anonymous.get(url).status_code, 404)

    def test_only_confirmed_completion_qualifies_and_repeats_do_not_duplicate(self):
        asset = self.asset(auto_publish=True)
        now = timezone.now()
        interrupted = Broadcast.objects.create(
            asset=asset, scheduled_at=now, status="playing", started_at=now
        )
        finish(interrupted, "interrupted", "Diretta", now + timedelta(seconds=1))
        asset.refresh_from_db()
        self.assertIsNone(asset.first_completed_at)
        for number in [1, 2]:
            start = now + timedelta(minutes=number)
            job = Broadcast.objects.create(
                asset=asset, scheduled_at=start, status="playing", started_at=start
            )
            finish(job, "completed", "Finito", start + timedelta(seconds=1))
        asset.refresh_from_db()
        self.assertEqual(asset.first_broadcast_at, now + timedelta(minutes=1))
        self.assertEqual(
            asset.first_completed_at, now + timedelta(minutes=1, seconds=1)
        )
        self.assertIsNone(asset.published_at)  # MP3 not yet prepared.
        self.assertEqual(Client().get("/api/v1/podcasts/").json()["count"], 0)

    def test_public_list_contains_only_editorial_fields_and_stable_links(self):
        asset = self.public(description="Un episodio di prova")
        response = Client().get("/api/v1/podcasts/")
        item = response.json()["results"][0]
        self.assertEqual(item["id"], str(asset.id))
        self.assertEqual(item["description"], asset.description)
        self.assertTrue(item["audio_url"].endswith(self.urls(asset)[1]))
        self.assertEqual(
            set(item),
            {
                "id",
                "title",
                "artist",
                "description",
                "duration_seconds",
                "first_broadcast_at",
                "published_at",
                "audio_url",
                "url",
            },
        )
        self.assertNotIn(asset.original_path, response.content.decode())
        self.assertEqual(Client().get(self.urls(asset)[0]).json(), item)

    def test_radio_availability_is_independent_of_publication(self):
        asset = self.public(is_active=False)
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 200)
        job = Broadcast.objects.create(
            asset=asset, scheduled_at=timezone.now() + timedelta(days=1)
        )
        self.client.post(f"/podcasts/{asset.id}/private/")
        asset.refresh_from_db()
        job.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(job.status, "scheduled")

    def test_private_revokes_catalog_and_known_links_and_does_not_republish(self):
        asset = self.public()
        anonymous = Client()
        urls = self.urls(asset)
        self.assertEqual(anonymous.get(urls[1]).status_code, 200)
        self.client.post(f"/podcasts/{asset.id}/private/")
        asset.refresh_from_db()
        self.assertFalse(asset.auto_publish)
        self.assertIsNone(asset.published_at)
        for url in urls:
            self.assertEqual(anonymous.get(url).status_code, 404)
        self.assertEqual(anonymous.get("/api/v1/podcasts/").json()["count"], 0)
        with patch("library.publication.encode_mp3") as encode:
            self.assertFalse(publication.run_one())
            encode.assert_not_called()
        start = timezone.now()
        job = Broadcast.objects.create(
            asset=asset, scheduled_at=start, status="playing", started_at=start
        )
        finish(job, "completed", "Finito", start + timedelta(seconds=1))
        asset.refresh_from_db()
        self.assertIsNone(asset.published_at)

    def test_api_filters_all_unavailable_states_and_missing_file(self):
        asset = self.public()
        anonymous = Client()
        changes = [
            ("auto_publish", False),
            ("published_at", None),
            ("status", "processing"),
            ("replay_status", "error"),
            ("trashed_at", timezone.now()),
            ("purged_at", timezone.now()),
            ("removal_pending", True),
            ("category", "music"),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                original = getattr(asset, field)
                setattr(asset, field, value)
                asset.save()
                self.assertEqual(anonymous.get("/api/v1/podcasts/").json()["count"], 0)
                self.assertEqual(anonymous.get(self.urls(asset)[1]).status_code, 404)
                setattr(asset, field, original)
                asset.save()
        data_path(asset.replay_path).unlink()
        self.assertEqual(anonymous.get("/api/v1/podcasts/").json()["count"], 0)
        self.assertEqual(anonymous.get(self.urls(asset)[1]).status_code, 404)

    def test_trash_and_restore_do_not_restore_public_visibility(self):
        asset = self.public()
        self.client.post(f"/library/{asset.id}/trash/")
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 404)
        self.client.post(f"/library/{asset.id}/restore/")
        asset.refresh_from_db()
        self.assertFalse(asset.auto_publish)
        self.assertIsNone(asset.published_at)
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 404)

    def test_seek_head_cors_and_cache_policy(self):
        asset = self.public()
        url = self.urls(asset)[1]
        client = Client()
        for value, expected in [
            ("bytes=0-3", b"0123"),
            ("bytes=-3", b"xyz"),
            ("bytes=33-", b"xyz"),
        ]:
            response = client.get(
                url, HTTP_RANGE=value, HTTP_ORIGIN="https://radio.example"
            )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b"".join(response.streaming_content), expected)
            self.assertEqual(response["Access-Control-Allow-Origin"], "*")
            self.assertIn("no-store", response["Cache-Control"])
        for value in ["bytes=999-", "bytes=4-1", "bytes=0-1,4-5", "bytes=-0", "bad"]:
            self.assertEqual(client.get(url, HTTP_RANGE=value).status_code, 416)
        head = client.head(url, HTTP_RANGE="bytes=0-3")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(head["Content-Length"], "36")
        self.assertEqual(
            client.options(url, HTTP_ACCESS_CONTROL_REQUEST_HEADERS="Range")[
                "Access-Control-Allow-Headers"
            ],
            "Range",
        )
        self.assertEqual(client.post(url).status_code, 405)
        self.assertEqual(client.head("/api/v1/podcasts/").content, b"")
        self.assertIn("no-store", client.get("/api/v1/podcasts/")["Cache-Control"])

    def test_pagination_and_invalid_values(self):
        self.public(b"one")
        self.public(b"two")
        page = Client().get("/api/v1/podcasts/?page_size=1").json()
        self.assertEqual(page["count"], 2)
        self.assertEqual(len(page["results"]), 1)
        self.assertIsNotNone(page["next"])
        self.assertEqual(
            Client().get("/api/v1/podcasts/?page_size=1&page=2").json()["next"], None
        )
        for query in ["page=0", "page=x", "page_size=101", "page_size=0"]:
            self.assertEqual(
                Client().get("/api/v1/podcasts/?" + query).status_code, 400
            )

    def test_staff_controls_metadata_publication_filters_and_auth(self):
        asset = self.public()
        other = self.asset(b"private")
        self.assertContains(
            self.client.get("/podcasts/?visibility=public"), asset.title
        )
        self.assertEqual(
            self.client.get("/podcasts/?visibility=public")
            .context["page"]
            .paginator.count,
            1,
        )
        self.assertEqual(
            self.client.get("/podcasts/?visibility=private")
            .context["page"]
            .paginator.count,
            1,
        )
        self.client.post(
            f"/podcasts/{asset.id}/",
            {
                "title": "Episodio aggiornato",
                "artist": "Voce",
                "description": "Testo",
                "auto_publish": "on",
            },
        )
        asset.refresh_from_db()
        self.assertEqual(asset.description, "Testo")
        self.assertTrue(asset.auto_publish)
        self.assertContains(
            self.client.get(f"/podcasts/{asset.id}/"), "Link MP3 pubblico"
        )
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(f"/podcasts/{asset.id}/private/").status_code, 403)
        self.assertEqual(
            self.client.get(f"/podcasts/{asset.id}/private/").status_code, 405
        )
        self.client.logout()
        self.assertEqual(self.client.get("/podcasts/").status_code, 302)
        self.assertEqual(
            self.client.post(f"/podcasts/{other.id}/", {}).status_code, 302
        )

    def test_existing_mp3_can_be_republished_only_by_explicit_opt_in(self):
        asset = self.public()
        self.client.post(f"/podcasts/{asset.id}/private/")
        self.client.post(f"/podcasts/{asset.id}/replay/", {"availability": "available"})
        asset.refresh_from_db()
        self.assertIsNotNone(asset.published_at)
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 200)

    def test_worker_error_does_not_break_radio_and_retry_is_explicit(self):
        asset = self.asset(auto_publish=True, first_completed_at=timezone.now())
        with patch("library.publication.encode_mp3", side_effect=AudioError("broken")):
            with self.assertLogs("library.publication", level="ERROR"):
                self.assertTrue(publication.run_one())
        asset.refresh_from_db()
        self.assertEqual(asset.status, "ready")
        self.assertEqual(asset.replay_status, "error")
        self.assertTrue(data_path(asset.prepared_path).exists())
        self.assertIsNone(asset.published_at)
        self.client.post(f"/podcasts/{asset.id}/retry/")
        asset.refresh_from_db()
        self.assertEqual(asset.replay_status, "pending")

    def test_crashed_replay_job_recovers_but_a_live_worker_is_not_stolen(self):
        asset = self.asset(
            auto_publish=True,
            first_completed_at=timezone.now(),
            replay_status="processing",
        )
        handle = worker.lock_file(asset.id)
        try:
            publication.recovery()
            asset.refresh_from_db()
            self.assertEqual(asset.replay_status, "processing")
        finally:
            handle.close()
        publication.recovery()
        asset.refresh_from_db()
        self.assertEqual(asset.replay_status, "pending")

    def test_private_change_during_encoding_wins(self):
        asset = self.asset(auto_publish=True, first_completed_at=timezone.now())

        def encode(source, workspace):
            Asset.objects.filter(pk=asset.id).update(
                auto_publish=False, published_at=None
            )
            path = workspace / "encoded.mp3"
            path.write_bytes(b"encoded")
            return path, 1, 0

        with patch("library.publication.encode_mp3", side_effect=encode):
            publication.run_one()
        asset.refresh_from_db()
        self.assertFalse(asset.auto_publish)
        self.assertIsNone(asset.published_at)
        self.assertEqual(asset.replay_path, "")
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 404)

    def test_real_flac_to_mp3_and_first_publication(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg required")
        source = data_path("source.wav")
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=520:duration=1",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(source),
            ],
            check=True,
        )
        asset = save_upload(
            SimpleUploadedFile("real.wav", source.read_bytes()), "podcasts"
        )[0]
        self.assertTrue(worker.run_one())
        asset.refresh_from_db()
        asset.auto_publish = True
        asset.save()
        self.assertTrue(publication.run_one())
        asset.refresh_from_db()
        self.assertEqual(asset.replay_status, "ready")
        self.assertIsNotNone(asset.published_at)
        self.assertTrue(data_path(asset.replay_path).is_file())
        response = Client().get(self.urls(asset)[1])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b"".join(response.streaming_content).startswith(b"ID3"))

    def test_new_upload_defaults_to_replay_without_a_broadcast(self):
        asset = save_upload(SimpleUploadedFile("new.mp3", b"new-upload"), "podcasts")[0]
        self.assertTrue(asset.auto_publish)
        self.assertIsNone(asset.first_completed_at)
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 404)

    def test_row_choice_can_publish_unbroadcast_audio_and_revoke_it(self):
        asset = self.public()
        asset.auto_publish = False
        asset.published_at = None
        asset.first_completed_at = None
        asset.first_broadcast_at = None
        asset.save()
        url = f"/podcasts/{asset.id}/replay/"
        self.assertEqual(
            self.client.post(url, {"availability": "available"}).status_code, 302
        )
        asset.refresh_from_db()
        self.assertIsNotNone(asset.published_at)
        self.assertEqual(Broadcast.objects.filter(asset=asset).count(), 0)
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 200)
        self.assertIsNone(
            Client().get(self.urls(asset)[0]).json()["first_broadcast_at"]
        )
        self.client.post(url, {"availability": "unavailable"})
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 404)
        asset.refresh_from_db()
        self.assertFalse(asset.auto_publish)

    def test_row_choice_validates_auth_csrf_and_never_changes_metadata(self):
        asset = self.asset()
        url = f"/podcasts/{asset.id}/replay/"
        self.assertEqual(
            Client().post(url, {"availability": "available"}).status_code, 302
        )
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(url, {"availability": "available"}).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(
            self.client.post(url, {"availability": "invalid"}).status_code, 400
        )
        self.client.post(url, {"availability": "available", "title": "Ignore"})
        asset.refresh_from_db()
        self.assertNotEqual(asset.title, "Ignore")
        self.assertTrue(asset.auto_publish)
        self.assertIsNone(asset.first_completed_at)

    def test_pending_replay_filter_uses_choice_and_metadata_save_preserves_it(self):
        asset = self.asset(auto_publish=True)
        self.assertEqual(
            self.client.get("/podcasts/?visibility=public")
            .context["page"]
            .paginator.count,
            1,
        )
        self.client.post(
            f"/podcasts/{asset.id}/",
            {"title": "Edited", "artist": "", "description": ""},
        )
        asset.refresh_from_db()
        self.assertTrue(asset.auto_publish)
        self.assertEqual(asset.title, "Edited")

    def test_deferred_replay_waits_for_completion_and_ignores_interruption(self):
        asset = self.public()
        asset.replay_after_broadcast = True
        asset.first_completed_at = None
        asset.first_broadcast_at = None
        asset.published_at = None
        asset.save()
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 404)
        self.assertIsNone(publication.claim())
        start = timezone.now()
        interrupted = Broadcast.objects.create(
            asset=asset, scheduled_at=start, started_at=start, status="playing"
        )
        finish(interrupted, "interrupted", "Diretta", start + timedelta(seconds=1))
        asset.refresh_from_db()
        self.assertTrue(asset.replay_after_broadcast)
        completed = Broadcast.objects.create(
            asset=asset, scheduled_at=start, started_at=start, status="playing"
        )
        finish(completed, "completed", "Finito", start + timedelta(seconds=1))
        asset.refresh_from_db()
        self.assertFalse(asset.replay_after_broadcast)
        self.assertIsNotNone(asset.published_at)
        self.assertEqual(Client().get(self.urls(asset)[1]).status_code, 200)

    def test_manual_choices_override_deferred_replay(self):
        for choice in ["available", "unavailable"]:
            asset = self.public(choice.encode())
            asset.replay_after_broadcast = True
            asset.save()
            self.client.post(f"/podcasts/{asset.id}/replay/", {"availability": choice})
            asset.refresh_from_db()
            self.assertFalse(asset.replay_after_broadcast)
            self.assertEqual(asset.auto_publish, choice == "available")
            self.assertEqual(
                Client().get(self.urls(asset)[1]).status_code,
                200 if choice == "available" else 404,
            )

    def test_upload_saves_waiting_choice_atomically_and_duplicates_keep_their_choice(
        self,
    ):
        for scheduled in [False, True]:
            content = b"scheduled" if scheduled else b"unscheduled"
            fields = {
                "category": "podcasts",
                "replay_after_broadcast": "on",
                "files": SimpleUploadedFile("waiting.mp3", content),
            }
            if scheduled:
                fields.update(
                    schedule="on",
                    scheduled_at=(timezone.now() + timedelta(days=2)).strftime(
                        "%Y-%m-%dT%H:%M"
                    ),
                )
            self.assertEqual(self.client.post("/upload/", fields).status_code, 302)
            asset = Asset.objects.get(
                checksum=hashlib.sha256(content).hexdigest()
            )
            self.assertTrue(asset.auto_publish)
            self.assertTrue(asset.replay_after_broadcast)
            self.assertEqual(
                Broadcast.objects.filter(asset=asset).count(),
                int(scheduled),
            )
            existing, created = save_upload(
                SimpleUploadedFile("same.mp3", content),
                "podcasts",
                replay_after_broadcast=False,
            )
            self.assertFalse(created)
            self.assertTrue(existing.replay_after_broadcast)

    def test_music_ignores_podcast_replay_checkbox(self):
        self.client.post(
            "/upload/",
            {
                "category": "music",
                "replay_after_broadcast": "on",
                "files": SimpleUploadedFile("music.mp3", b"music-upload"),
            },
        )
        asset = Asset.objects.get(category="music")
        self.assertFalse(asset.replay_after_broadcast)
