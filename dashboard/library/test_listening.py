"""Counters, outage semantics, sampling and staff access; no listener identities."""

import io
import json
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from . import engine, listening, tests
from .models import Broadcast, ListeningSample, RadioSnapshot
from .studio_views import current_status


class ListeningTests(TestCase):
    setUp = tests.LibraryTests.setUp
    tearDown = tests.LibraryTests.tearDown

    def source(self, listeners=3, **extra):
        return {
            "listenurl": "http://icecast:8000/stream.mp3",
            "listeners": listeners,
            "title": "Titolo",
            **extra,
        }

    def read(self, document):
        with patch(
            "library.listening.urlopen",
            return_value=io.BytesIO(json.dumps(document).encode()),
        ):
            return listening.read_stats()

    def test_selects_only_our_mount_for_single_or_multiple_sources(self):
        for source in [
            self.source(),
            [self.source(999, listenurl="http://icecast:8000/another"), self.source()],
        ]:
            result = self.read({"icestats": {"source": source}})
            self.assertEqual(result["listeners"], 3)
            self.assertTrue(result["stream_online"])
        self.assertEqual(self.read({"icestats": {}})["listeners"], 0)
        self.assertFalse(self.read({"icestats": {}})["stream_online"])

    def test_missing_invalid_counters_stay_unknown(self):
        for value in [None, -1, True, "bad", 2.5, 2**40]:
            self.assertIsNone(
                self.read({"icestats": {"source": self.source(value)}})["listeners"]
            )
        self.assertEqual(
            self.read({"icestats": {"source": self.source("0")}})["listeners"], 0
        )
        for document in [{}, {"icestats": None}, {"icestats": {"source": "bad"}}]:
            with self.assertRaises(listening.StatsError):
                self.read(document)
        with patch("library.listening.urlopen", side_effect=OSError("timeout")):
            with self.assertRaises(listening.StatsError):
                listening.read_stats()

    @patch(
        "library.listening.engine.get_state",
        return_value=engine.EngineState("session", False, "", {}),
    )
    def test_collects_once_per_minute_but_refreshes_current_counter(self, state):
        now = timezone.now().replace(second=5)
        with (
            patch(
                "library.listening.read_stats",
                return_value={
                    "stream_online": True,
                    "listeners": 2,
                    "title": "Music",
                    "artist": "Voice",
                },
            ),
            patch("library.listening.timezone.now", return_value=now),
        ):
            listening.collect()
        with (
            patch(
                "library.listening.read_stats",
                return_value={
                    "stream_online": True,
                    "listeners": 5,
                    "title": "Next",
                    "artist": "",
                },
            ),
            patch(
                "library.listening.timezone.now",
                return_value=now + timedelta(seconds=15),
            ),
        ):
            listening.collect()
        self.assertEqual(ListeningSample.objects.count(), 1)
        self.assertEqual(ListeningSample.objects.get().listeners, 2)
        self.assertEqual(RadioSnapshot.objects.get().listeners, 5)
        self.assertEqual(RadioSnapshot.objects.get().title, "Next")

    def test_network_outage_is_not_a_zero_and_stale_snapshot_is_hidden(self):
        with (
            patch("library.listening.read_stats", side_effect=listening.StatsError()),
            patch(
                "library.listening.engine.get_state", side_effect=engine.EngineError()
            ),
        ):
            listening.collect()
        self.assertIsNone(ListeningSample.objects.get().listeners)
        self.assertIsNone(current_status()["stream_online"])
        RadioSnapshot.objects.update(
            listeners=9, title="Old", checked_at=timezone.now() - timedelta(minutes=2)
        )
        status = current_status()
        self.assertIsNone(status["listeners"])
        self.assertFalse(status["fresh"])
        self.assertEqual(status["title"], "")

    def test_live_and_actual_started_podcast_are_classified(self):
        tests.LibraryTests.upload(self, category="podcasts")
        from .models import Asset

        asset = Asset.objects.get(category="podcasts")
        job = Broadcast.objects.create(asset=asset, scheduled_at=timezone.now())
        values = {
            "stream_online": True,
            "listeners": 0,
            "title": "Source metadata",
            "artist": "",
        }
        for state, label in [
            (engine.EngineState("s", True, "", {}), "Diretta"),
            (engine.EngineState("s", False, str(job.id), {}), "Podcast in avvio"),
            (
                engine.EngineState(
                    "s", False, str(job.id), {str(job.id): {"started": 1}}
                ),
                "Podcast",
            ),
        ]:
            with (
                patch("library.listening.read_stats", return_value=values),
                patch("library.listening.engine.get_state", return_value=state),
            ):
                listening.collect()
            self.assertEqual(RadioSnapshot.objects.get().kind, label)
        self.assertEqual(RadioSnapshot.objects.get().title, asset.title)

    def test_staff_routes_and_no_hidden_autoplay(self):
        for path in ["/on-air/", "/listening/", "/api/studio/status/"]:
            self.assertEqual(Client().get(path).status_code, 302)
            self.assertEqual(self.client.get(path).status_code, 200)
        response = self.client.get("/on-air/")
        self.assertContains(response, 'preload="none"')
        self.assertNotContains(response, "autoplay")
        self.assertEqual(
            self.client.get("/api/studio/status/")["Cache-Control"], "no-store"
        )
        self.assertEqual(self.client.post("/api/studio/status/").status_code, 405)

    def test_history_ignores_unknowns_keeps_real_zero_and_uses_rome_dates(self):
        # UTC 23:30 belongs to the following day in Rome, including across DST.
        anchor = datetime(2026, 10, 25, 23, 30, tzinfo=datetime_timezone.utc)
        for offset, value in [(0, 0), (1, 10), (2, None)]:
            ListeningSample.objects.create(
                minute=anchor + timedelta(minutes=offset),
                listeners=value,
                stream_online=True,
            )
        with patch(
            "library.studio_views.timezone.localdate",
            return_value=timezone.localdate(anchor),
        ):
            response = self.client.get("/listening/?days=7")
        self.assertEqual(response.context["summary"]["peak"], 10)
        self.assertEqual(response.context["summary"]["average"], 5)
        row = next(
            row
            for row in response.context["rows"]
            if row["day"] == timezone.localdate(anchor)
        )
        self.assertEqual((row["known"], row["total"]), (2, 3))
        self.assertEqual(self.client.get("/listening/?days=bad").context["days"], 7)
        self.assertEqual(self.client.get("/listening/?days=999999").context["days"], 7)

    def sample_day(self, day, listeners):
        from .studio_views import day_start

        ListeningSample.objects.create(
            minute=day_start(day) + timedelta(hours=12),
            listeners=listeners,
            stream_online=True,
        )

    def test_twenty_days_per_page_and_summary_covers_entire_period(self):
        today = timezone.localdate()
        for offset in range(45):
            self.sample_day(today - timedelta(days=offset), 99 if offset == 44 else 1)
        first = self.client.get("/listening/?period=all")
        self.assertEqual(len(first.context["rows"]), 20)
        self.assertEqual(first.context["rows"][0]["day"], today)
        self.assertEqual(first.context["page"].paginator.count, 45)
        self.assertEqual(first.context["summary"]["peak"], 99)
        self.assertEqual(first.context["summary"]["total"], 45)
        self.assertEqual(max(row["peak"] for row in first.context["chart_rows"]), 99)
        second = self.client.get(first.context["next_url"])
        self.assertEqual(second.context["rows"][0]["day"], today - timedelta(days=20))
        self.assertEqual(second.context["summary"], first.context["summary"])
        third = self.client.get(second.context["next_url"])
        self.assertEqual(len(third.context["rows"]), 5)
        self.assertIsNone(third.context["next_url"])
        self.assertIn("period=all", third.context["previous_url"])

    def test_year_filter_uses_local_year_and_handles_leap_year(self):
        from datetime import date

        self.sample_day(date(2024, 2, 29), 8)
        self.sample_day(date(2024, 12, 31), 2)
        # This UTC instant belongs to 2025 in Rome.
        ListeningSample.objects.create(
            minute=datetime(2024, 12, 31, 23, 30, tzinfo=datetime_timezone.utc),
            listeners=100,
        )
        response = self.client.get("/listening/?period=year:2024")
        self.assertEqual(response.context["page"].paginator.count, 366)
        self.assertEqual(response.context["summary"]["peak"], 8)
        self.assertEqual(response.context["summary"]["total"], 2)
        self.assertIn("year%3A2024", response.context["next_url"])
        offset = (date(2024, 12, 31) - date(2024, 2, 29)).days
        leap_page = self.client.get(
            f"/listening/?period=year:2024&page={offset // 20 + 1}"
        )
        self.assertTrue(
            any(
                row["day"] == date(2024, 2, 29) and row["peak"] == 8
                for row in leap_page.context["rows"]
            )
        )
        response = self.client.get("/listening/?period=year:2025")
        self.assertEqual(response.context["summary"]["peak"], 100)

    def test_long_history_uses_monthly_chart_and_keeps_day_pagination(self):
        from datetime import date

        self.sample_day(date(2020, 1, 2), 7)
        self.sample_day(timezone.localdate(), 3)
        response = self.client.get("/listening/?period=all&page=2")
        self.assertTrue(response.context["chart_monthly"])
        self.assertGreater(response.context["page"].paginator.count, 365)
        self.assertEqual(len(response.context["rows"]), 20)
        self.assertLess(
            len(response.context["chart_rows"]),
            response.context["page"].paginator.count,
        )
        self.assertEqual(response.context["summary"]["peak"], 7)

    def test_empty_history_invalid_filters_and_invalid_pages(self):
        response = self.client.get("/listening/?period=all&page=bad")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].number, 1)
        self.assertIsNone(response.context["summary"]["peak"])
        self.assertEqual(
            self.client.get("/listening/?period=year:1900").context["selected_period"],
            "7",
        )
        response = self.client.get("/listening/?period=90&page=99999")
        self.assertEqual(response.context["page"].number, 5)
        self.assertEqual(len(response.context["rows"]), 10)
