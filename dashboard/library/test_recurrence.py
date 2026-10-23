"""Rules, exceptions, real calendar intervals and public integration boundaries."""

from datetime import date, datetime, timedelta

from django.test import Client
from django.utils import timezone

from .models import Broadcast, BroadcastSeries
from .recurrence import ROME, extend_series, occurrences
from .test_scheduler import CalendarTests


class RecurrenceTests(CalendarTests):
    # Reuse fixtures, not inherited tests (see load_tests below).
    def fields(self, start=None, **kwargs):
        start = start or (timezone.now().astimezone(ROME) + timedelta(days=2)).replace(
            hour=18, minute=0, second=0, microsecond=0
        )
        return {
            "asset": str(self.asset.id),
            "scheduled_at": start.strftime("%Y-%m-%dT%H:%M"),
            "repeat": "weekly",
            "weekdays": [str(start.weekday())],
            "until": (start + timedelta(days=21)).date().isoformat(),
            "is_public": "on",
            **kwargs,
        }

    def create(self, **kwargs):
        data = self.fields(**kwargs)
        response = self.client.post("/calendar/new/", data)
        self.assertEqual(response.status_code, 302, response.content.decode())
        return BroadcastSeries.objects.latest("updated_at"), data

    def test_new_schedule_is_public_by_default_but_can_be_private(self):
        from .forms import BroadcastForm

        self.assertTrue(BroadcastForm()["is_public"].value())
        fields = self.fields()
        fields.pop("is_public")
        form = BroadcastForm(data=fields)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.cleaned_data["is_public"])
        private = Broadcast(asset=self.asset, scheduled_at=timezone.now(), is_public=False)
        private._state.adding = False
        self.assertFalse(BroadcastForm(instance=private)["is_public"].value())
        self.assertFalse(BroadcastForm(initial={"is_public": False})["is_public"].value())

    def test_weekly_dates_share_audio_and_single_occurrence_is_editable(self):
        series, _ = self.create()
        jobs = list(series.broadcasts.order_by("scheduled_at"))
        self.assertEqual(len(jobs), 4)
        self.assertEqual({job.asset_id for job in jobs}, {self.asset.id})
        self.assertTrue(all(job.is_public for job in jobs))
        first = jobs[0]
        moved = first.scheduled_at.astimezone(ROME) + timedelta(hours=1)
        response = self.client.post(
            f"/calendar/{first.id}/edit/",
            {"scheduled_at": moved.strftime("%Y-%m-%dT%H:%M"), "is_public": ""},
        )
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertTrue(first.is_exception)
        self.assertFalse(first.is_public)
        self.assertEqual(first.scheduled_at, moved)
        unchanged = jobs[1].scheduled_at
        jobs[1].refresh_from_db()
        self.assertEqual(jobs[1].scheduled_at, unchanged)

    def test_any_occurrence_conflict_rejects_whole_series(self):
        data = self.fields()
        start = datetime.fromisoformat(data["scheduled_at"]).replace(tzinfo=ROME)
        existing = Broadcast.objects.create(
            asset=self.asset, scheduled_at=start + timedelta(days=14, seconds=30)
        )
        response = self.client.post("/calendar/new/", data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sovrappone")
        self.assertContains(response, self.asset.title)
        self.assertFalse(BroadcastSeries.objects.exists())
        self.assertEqual(
            list(Broadcast.objects.values_list("id", flat=True)), [existing.id]
        )

    def test_weekdays_until_and_known_duration_are_required_for_series(self):
        for data in [self.fields(weekdays=[]), self.fields(until="2020-01-01")]:
            self.assertEqual(self.client.post("/calendar/new/", data).status_code, 200)
        self.asset.status = "processing"
        self.asset.save()
        self.assertContains(
            self.client.post("/calendar/new/", self.fields()), "già pronto"
        )
        self.assertFalse(BroadcastSeries.objects.exists())

    def test_series_edit_retains_ids_exceptions_and_old_results(self):
        series, data = self.create()
        jobs = list(series.broadcasts.order_by("scheduled_at"))
        cancelled, completed, remaining = jobs[:3]
        self.client.post(f"/calendar/{cancelled.id}/cancel/")
        Broadcast.objects.filter(pk=completed.pk).update(
            status="completed", ended_at=timezone.now()
        )
        start = datetime.fromisoformat(data["scheduled_at"]) + timedelta(minutes=10)
        data["scheduled_at"] = start.strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(f"/calendar/series/{series.id}/edit/", data)
        self.assertEqual(response.status_code, 302, response.content.decode())
        cancelled.refresh_from_db()
        completed.refresh_from_db()
        remaining.refresh_from_db()
        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(cancelled.is_exception)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(remaining.scheduled_at.astimezone(ROME).minute, 10)
        self.assertEqual(series.broadcasts.count(), 4)

    def test_series_cancel_stops_future_generation_but_not_playing(self):
        series, _ = self.create()
        playing = series.broadcasts.first()
        Broadcast.objects.filter(pk=playing.pk).update(status="playing")
        self.client.post(f"/calendar/series/{series.id}/cancel/")
        series.refresh_from_db()
        playing.refresh_from_db()
        self.assertFalse(series.active)
        self.assertEqual(playing.status, "playing")
        self.assertEqual(series.broadcasts.filter(status="cancelled").count(), 3)
        extend_series(timezone.now() + timedelta(days=400))
        self.assertEqual(series.broadcasts.count(), 4)

    def test_indefinite_series_extends_idempotently_and_keeps_cancelled_exception(self):
        series, _ = self.create(until="")
        self.assertGreaterEqual(series.broadcasts.count(), 52)
        cancelled = series.broadcasts.last()
        self.client.post(f"/calendar/{cancelled.id}/cancel/")
        previous = series.broadcasts.count()
        later = timezone.now() + timedelta(days=30)
        extend_series(later)
        count = series.broadcasts.count()
        self.assertGreater(count, previous)
        extend_series(later)
        self.assertEqual(count, series.broadcasts.count())
        cancelled.refresh_from_db()
        self.assertEqual(cancelled.status, "cancelled")

    def test_future_unmaterialized_weekly_slot_blocks_one_off(self):
        series, _ = self.create(until="")
        later = series.starts_at.astimezone(ROME) + timedelta(days=7 * 70)
        response = self.client.post(
            "/calendar/new/", self.fields(start=later.astimezone(ROME), repeat="once")
        )
        self.assertContains(response, "sovrappone")
        self.assertFalse(Broadcast.objects.filter(scheduled_at=later).exists())

    def test_distant_appointments_and_later_rules_reserve_future_slots(self):
        data = self.fields(until="")
        start = datetime.fromisoformat(data["scheduled_at"]).replace(tzinfo=ROME)
        distant = start + timedelta(weeks=80)
        job = Broadcast.objects.create(asset=self.asset, scheduled_at=distant)
        self.assertContains(self.client.post("/calendar/new/", data), "sovrappone")
        job.delete()
        BroadcastSeries.objects.create(
            asset=self.asset, starts_at=distant, weekdays=[distant.weekday()]
        )
        self.assertContains(self.client.post("/calendar/new/", data), "sovrappone")
        self.assertEqual(BroadcastSeries.objects.count(), 1)

    def test_rejected_series_change_keeps_all_dates_and_identifiers(self):
        series, data = self.create()
        original = list(series.broadcasts.values_list("id", "scheduled_at"))
        start = datetime.fromisoformat(data["scheduled_at"]).replace(tzinfo=ROME)
        Broadcast.objects.create(
            asset=self.asset, scheduled_at=start + timedelta(days=7, hours=1)
        )
        data["scheduled_at"] = (start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(f"/calendar/series/{series.id}/edit/", data)
        self.assertContains(response, "sovrappone")
        self.assertEqual(
            original, list(series.broadcasts.values_list("id", "scheduled_at"))
        )

    def test_extension_conflict_remains_visible_without_partial_creation(self):
        series, _ = self.create(until="")
        future = series.starts_at.astimezone(ROME) + timedelta(weeks=56)
        Broadcast.objects.create(asset=self.asset, scheduled_at=future)
        previous = series.broadcasts.count()
        extend_series(timezone.now() + timedelta(days=60))
        series.refresh_from_db()
        self.assertIn("sovrappone", series.last_error)
        self.assertEqual(series.broadcasts.count(), previous)

    def test_dst_keeps_rome_clock_and_omits_nonexistent_time(self):
        start = datetime(2027, 3, 21, 18, tzinfo=ROME)
        dates = list(occurrences(start, [6], date(2027, 4, 4)))
        self.assertEqual(
            [instant.astimezone(ROME).hour for _, instant in dates], [18, 18, 18]
        )
        self.assertEqual((dates[1][1] - dates[0][1]).total_seconds(), 7 * 86400 - 3600)
        missing = list(
            occurrences(
                datetime(2027, 3, 21, 2, 30, tzinfo=ROME), [6], date(2027, 4, 4)
            )
        )
        self.assertEqual(
            [day for day, _ in missing], [date(2027, 3, 21), date(2027, 4, 4)]
        )
        repeated = list(
            occurrences(
                datetime(2027, 10, 31, 2, 30, tzinfo=ROME), [6], date(2027, 10, 31)
            )
        )
        self.assertEqual(len(repeated), 1)

    def test_public_calendar_independent_of_replay_with_privacy_and_cors(self):
        series, _ = self.create()
        self.asset.auto_publish = False
        self.asset.description = "Descrizione pubblica"
        self.asset.save()
        first = series.broadcasts.last()
        Broadcast.objects.create(
            asset=self.asset,
            scheduled_at=first.scheduled_at + timedelta(hours=2),
            is_public=False,
        )
        public = Client()
        response = public.get("/api/v1/schedule/")
        payload = response.json()
        self.assertEqual(payload["count"], 4)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertNotIn("original_path", str(payload))
        self.assertNotIn("outcome", str(payload))
        self.assertNotIn("audio_url", str(payload))
        self.assertEqual(public.get("/api/v1/podcasts/").json()["count"], 0)
        self.assertEqual(public.post("/api/v1/schedule/").status_code, 405)
        self.assertEqual(public.options("/calendar.ics").status_code, 204)
        self.assertEqual(public.head("/calendar.ics").content, b"")
        self.asset.trashed_at = timezone.now()
        self.asset.save()
        self.assertEqual(public.get("/api/v1/schedule/").json()["count"], 0)

    def test_public_date_pagination_validation_and_revocation(self):
        series, data = self.create()
        start = datetime.fromisoformat(data["scheduled_at"])
        query = {
            "from": start.date().isoformat(),
            "to": (start + timedelta(days=8)).date().isoformat(),
            "page_size": 1,
        }
        response = Client().get("/api/v1/schedule/", query)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual(len(response.json()["results"]), 1)
        for query in [
            {"from": "bad"},
            {"from": "2026-01-01", "to": "2028-01-01"},
            {"page_size": "999"},
            {"from": "2026-01-01T10:00:00"},
        ]:
            self.assertEqual(Client().get("/api/v1/schedule/", query).status_code, 400)
        series.broadcasts.update(is_public=False)
        self.assertEqual(Client().get("/api/v1/schedule/").json()["count"], 0)

    def test_ics_uid_survives_edit_cancel_and_utf8_lines_are_valid(self):
        self.asset.title = "Caffè, musica; " * 15 + "\nBEGIN:VEVENT"
        self.asset.full_clean()
        self.asset.save()
        series, _ = self.create()
        job = series.broadcasts.last()
        self.client.post(f"/calendar/{job.id}/cancel/")
        result = Client().get("/calendar.ics").content
        self.assertIn(f"UID:{job.id}@julie-radio".encode(), result)
        self.assertIn(b"STATUS:CANCELLED", result)
        self.assertEqual(result.split(b"\r\n").count(b"BEGIN:VEVENT"), 4)
        for line in result.split(b"\r\n"):
            self.assertLessEqual(len(line), 75)
            line.decode("utf-8")
        unfolded = result.replace(b"\r\n ", b"").decode()
        self.assertIn("Caffè\\, musica\\;", unfolded)
        self.assertIn("\\nBEGIN:VEVENT", unfolded)

    def test_series_staff_only_and_csrf(self):
        series, _ = self.create()
        public = Client()
        self.assertEqual(
            public.get(f"/calendar/series/{series.id}/edit/").status_code, 302
        )
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(
            csrf.post(f"/calendar/series/{series.id}/cancel/").status_code, 403
        )


# Inherit the established fixture without running its existing test methods twice.
def load_tests(loader, suite, pattern):
    import unittest

    return unittest.TestSuite(
        RecurrenceTests(name)
        for name in RecurrenceTests.__dict__
        if name.startswith("test_")
    )
