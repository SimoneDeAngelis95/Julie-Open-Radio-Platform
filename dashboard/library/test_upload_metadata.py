"""Editorial upload metadata must survive preparation without suppressing music tags."""

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from . import audio
from .forms import MetadataForm, PodcastPublicationForm
from .models import Asset
from .storage import data_path, save_upload
from .tests import LibraryTests
from .worker import run_one


class UploadMetadataTests(LibraryTests):
    def test_batch_titles_and_descriptions_are_independent(self):
        response = self.client.post(
            "/upload/",
            {
                "category": "podcasts",
                "files": [
                    SimpleUploadedFile("one.mp3", b"one"),
                    SimpleUploadedFile("two.mp3", b"two"),
                ],
                "file_title_0": "Prima puntata",
                "file_description_0": "Descrizione uno",
                "file_title_1": "Seconda puntata",
                "file_description_1": "Descrizione due",
            },
        )
        self.assertEqual(response.status_code, 302)
        first = Asset.objects.get(title="Prima puntata")
        second = Asset.objects.get(title="Seconda puntata")
        self.assertEqual(first.description, "Descrizione uno")
        self.assertEqual(second.description, "Descrizione due")
        self.assertTrue(first.title_edited)

    def test_partial_ajax_failure_preserves_saved_files_and_can_be_retried(self):
        def submit():
            return self.client.post(
                "/upload/",
                {"category": "music", "files": [
                    SimpleUploadedFile("one.mp3", b"one"),
                    SimpleUploadedFile("two.mp3", b"two"),
                ]},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        def fail_second(upload, *args, **kwargs):
            if upload.name == "two.mp3":
                raise OSError("No space left on device")
            return save_upload(upload, *args, **kwargs)

        with patch("library.views.save_upload", side_effect=fail_second):
            response = submit()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("redirect", response.json())
        self.assertIn("1 file", response.json()["error"])
        self.assertEqual(Asset.objects.count(), 1)
        response = submit()
        self.assertEqual(response.status_code, 200)
        self.assertIn("redirect", response.json())
        self.assertEqual(Asset.objects.count(), 2)

    def test_single_title_fallback_and_scheduling(self):
        start = timezone.localtime(timezone.now() + timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        response = self.client.post(
            "/upload/",
            {
                "category": "podcasts",
                "files": SimpleUploadedFile("episode.mp3", b"episode"),
                "title": "Titolo scelto",
                "description": "Descrizione scelta",
                "schedule": "on",
                "scheduled_at": start,
            },
        )
        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get()
        self.assertEqual(asset.title, "Titolo scelto")
        self.assertEqual(asset.description, "Descrizione scelta")
        self.assertEqual(asset.broadcasts.count(), 1)

    def test_duplicate_upload_does_not_overwrite_existing_metadata(self):
        first, _ = save_upload(
            SimpleUploadedFile("same.mp3", b"same"),
            "podcasts",
            title="Originale",
            description="Conserva",
        )
        self.client.post(
            "/upload/",
            {
                "category": "podcasts",
                "files": SimpleUploadedFile("same.mp3", b"same"),
                "file_title_0": "Diverso",
                "file_description_0": "Sostituisci",
            },
        )
        first.refresh_from_db()
        self.assertEqual(first.title, "Originale")
        self.assertEqual(first.description, "Conserva")
        self.assertEqual(Asset.objects.count(), 1)

    def test_automatic_title_and_music_description_rules(self):
        self.client.post(
            "/upload/",
            {
                "category": "music",
                "files": SimpleUploadedFile("nome_file.mp3", b"music"),
                "file_description_0": "Non applicabile",
            },
        )
        asset = Asset.objects.get()
        self.assertEqual(asset.title, "nome file")
        self.assertFalse(asset.title_edited)
        self.assertEqual(asset.description, "")

    def test_oversized_per_file_fields_reject_before_saving(self):
        for name, value in [
            ("file_title_0", "x" * 241),
            ("file_description_0", "x" * 5001),
            ("file_artist_0", "x" * 241),
        ]:
            response = self.client.post(
                "/upload/",
                {
                    "category": "podcasts",
                    "files": SimpleUploadedFile("one.mp3", b"one"),
                    name: value,
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(Asset.objects.exists())

    def test_podcast_forms_remove_author_but_music_keeps_artist(self):
        podcast, _ = save_upload(
            SimpleUploadedFile("episode.mp3", b"episode"), "podcasts"
        )
        self.assertEqual(
            list(PodcastPublicationForm(instance=podcast).fields),
            ["title", "description"],
        )
        form = MetadataForm(instance=podcast)
        self.assertNotIn("artist", form.fields)
        self.assertNotIn("album", form.fields)
        self.assertIn("description", form.fields)
        music = self.asset()
        self.assertIn("artist", MetadataForm(instance=music).fields)
        self.assertContains(self.client.get("/"), "LA TUA RADIO")
        self.assertNotContains(
            self.client.get("/listening/"), "RILEVAMENTI DISPONIBILI"
        )

    def test_custom_music_title_survives_encoding_while_artist_imports(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        source = Path(self.directory.name) / "source.wav"
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
            SimpleUploadedFile("source.wav", source.read_bytes()),
            "music",
            title="Titolo scelto",
        )
        self.assertTrue(run_one())
        asset.refresh_from_db()
        self.assertEqual(asset.status, "ready")
        self.assertEqual(asset.title, "Titolo scelto")
        self.assertEqual(asset.artist, "Artista sorgente")
        import time

        tags = audio.probe(data_path(asset.prepared_path), time.monotonic() + 30)[
            "format"
        ]["tags"]
        tags = {key.lower(): value for key, value in tags.items()}
        self.assertEqual(tags["title"], "Titolo scelto")
        self.assertEqual(tags["artist"], "Artista sorgente")
        source_bytes = source.read_bytes() + b"different-checksum"
        custom, _ = save_upload(SimpleUploadedFile("custom.wav", source_bytes),
                                "music", title="Altro titolo", artist="Artista scelto")
        self.assertTrue(run_one())
        custom.refresh_from_db()
        self.assertEqual(custom.status, "ready")
        self.assertEqual(custom.artist, "Artista scelto")
        custom_tags = audio.probe(data_path(custom.prepared_path), time.monotonic() + 30)["format"]["tags"]
        self.assertEqual({k.lower(): v for k, v in custom_tags.items()}["artist"], "Artista scelto")


    def test_music_artist_is_per_file_and_podcasts_ignore_artist(self):
        self.client.post("/upload/", {
            "category": "music",
            "files": [SimpleUploadedFile("a.mp3", b"a"), SimpleUploadedFile("b.mp3", b"b")],
            "file_title_0": "Brano A", "file_artist_0": "Artista A",
            "file_title_1": "Brano B", "file_artist_1": "Artista B",
        })
        first = Asset.objects.get(title="Brano A")
        self.assertEqual(first.artist, "Artista A")
        self.assertTrue(first.artist_edited)
        self.assertEqual(Asset.objects.get(title="Brano B").artist, "Artista B")
        self.client.post("/upload/", {
            "category": "podcasts", "files": SimpleUploadedFile("pod.mp3", b"pod"),
            "artist": "Ignorato",
        })
        self.assertEqual(Asset.objects.get(category="podcasts").artist, "")

    def test_sidebar_icon_and_page_straplines(self):
        for url in ("/", "/upload/", "/podcasts/", "/calendar/", "/listening/"):
            response = self.client.get(url)
            self.assertContains(response, 'class="brand-icon"')
            self.assertNotContains(response, "TUTTO PARTE DA QUI")
            self.assertNotContains(response, "AGGIUNGI UNA NUOVA VOCE")



def load_tests(loader, suite, pattern):
    import unittest

    return unittest.TestSuite(
        UploadMetadataTests(name)
        for name in UploadMetadataTests.__dict__
        if name.startswith("test_")
    )
