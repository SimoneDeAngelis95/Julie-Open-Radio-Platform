"""Per-song playlist forms save independently, with and without JavaScript."""

from html.parser import HTMLParser

from django.test import Client, TestCase

from . import tests
from .models import Playlist


class RowPlaylistTests(TestCase):
    setUp = tests.LibraryTests.setUp
    tearDown = tests.LibraryTests.tearDown
    asset = tests.LibraryTests.asset

    def test_confirm_changes_only_one_song_and_can_clear_all(self):
        song = self.asset(b"one")
        other = self.asset(b"two")
        first = Playlist.objects.create(name="Mattina")
        second = Playlist.objects.create(name="Relax")
        song.playlists.add(first)
        other.playlists.add(first)
        response = self.client.post(
            f"/library/{song.id}/playlists/",
            {"playlists": [second.id]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(
            response.json(), {"playlists": [{"id": second.id, "name": "Relax"}]}
        )
        self.assertEqual(list(other.playlists.all()), [first])
        self.assertEqual(list(song.playlists.all()), [second])
        song.refresh_from_db()
        self.assertFalse(song.metadata_edited)
        self.assertTrue(song.is_active)
        self.client.post(
            f"/library/{song.id}/playlists/", {}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(song.playlists.count(), 0)

    def test_unknown_playlist_does_not_change_membership(self):
        song = self.asset()
        playlist = Playlist.objects.create(name="Keep")
        song.playlists.add(playlist)
        response = self.client.post(
            f"/library/{song.id}/playlists/",
            {"playlists": ["99999"]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(song.playlists.all()), [playlist])

    def test_trashed_and_podcast_rows_are_not_editable(self):
        song = self.asset()
        self.client.post(f"/library/{song.id}/trash/")
        self.assertEqual(
            self.client.post(f"/library/{song.id}/playlists/", {}).status_code, 404
        )
        song.category = "podcasts"
        song.trashed_at = None
        song.save()
        self.assertEqual(
            self.client.post(f"/library/{song.id}/playlists/", {}).status_code, 404
        )

    def test_fallback_preserves_filters_and_never_redirects_outside_library(self):
        song = self.asset()
        playlist = Playlist.objects.create(name="Test")
        response = self.client.post(
            f"/library/{song.id}/playlists/",
            {
                "playlists": [playlist.id],
                "library_query": "category=music&playlist=1&page=2&next=https://example.com",
            },
        )
        self.assertEqual(response["Location"], "/?category=music&playlist=1&page=2")
        self.assertEqual(list(song.playlists.all()), [playlist])

    def test_auth_csrf_and_get_do_not_mutate(self):
        song = self.asset()
        path = f"/library/{song.id}/playlists/"
        self.assertEqual(self.client.get(path).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(path, {}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(path, {}).status_code, 302)

    def test_row_forms_show_assigned_choices_without_bulk_controls(self):
        song = self.asset()
        playlist = Playlist.objects.create(name="Assigned")
        song.playlists.add(playlist)
        body = self.client.get("/").content.decode()

        class Forms(HTMLParser):
            def __init__(self):
                self.depth = 0
                self.nested = False
                self.selected = False
                super().__init__()

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "form":
                    self.nested |= self.depth > 0
                    self.depth += 1
                if (
                    tag == "input"
                    and values.get("name") == "playlists"
                    and values.get("value") == str(playlist.id)
                    and "checked" in values
                ):
                    self.selected = True

            def handle_endtag(self, tag):
                if tag == "form":
                    self.depth -= 1

        parser = Forms()
        parser.feed(body)
        self.assertFalse(parser.nested)
        self.assertTrue(parser.selected)
        self.assertNotIn("Assegna playlist ai brani selezionati", body)
        self.assertNotIn('name="assets"', body)
        self.assertIn('class="playlist-column"', body)
        self.assertIn("popovertarget=", body)
        self.assertNotIn("Impostazioni radio</a>", body)
        self.assertIn(">Conferma</button>", body)
