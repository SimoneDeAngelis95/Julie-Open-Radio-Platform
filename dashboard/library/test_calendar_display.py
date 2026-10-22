"""Monthly navigation, date shortcuts and bounded day agendas."""

from datetime import datetime, timedelta

from django.test import Client

from .models import Broadcast
from .recurrence import ROME
from .test_scheduler import CalendarTests


class CalendarDisplayTests(CalendarTests):
    def test_month_navigation_and_selected_date_shortcut(self):
        response = self.client.get("/calendar/", {"month": "2027-01"})
        self.assertContains(response, "Gennaio 2027")
        self.assertContains(response, "?month=2026-12")
        self.assertContains(response, "?month=2027-02")
        self.assertContains(response, "/calendar/new/?date=2027-01-12")
        form = self.client.get("/calendar/new/", {"date": "2027-01-12"})
        self.assertEqual(
            form.context["form"].initial["scheduled_at"].date().isoformat(),
            "2027-01-12",
        )
        self.assertEqual(form.context["form"].initial["scheduled_at"].hour, 18)

    def test_saving_keeps_the_appointment_month_visible(self):
        response = self.client.post(
            "/calendar/new/", self.payload(datetime(2027, 2, 12, 18, tzinfo=ROME))
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("month=2027-02", response["Location"])
        self.assertIn("day=2027-02-12", response["Location"])

    def test_invalid_dates_and_months_are_rejected(self):
        for values in [
            {"month": "invalid"},
            {"month": "2027-13"},
            {"month": "9999-01"},
            {"day": "2027-02-30"},
        ]:
            self.assertEqual(self.client.get("/calendar/", values).status_code, 400)
        self.assertEqual(
            self.client.get("/calendar/new/", {"date": "invalid"}).status_code, 400
        )

    def test_month_and_rome_day_boundaries(self):
        before = datetime(2027, 2, 1, 0, 30, tzinfo=ROME)
        first = Broadcast.objects.create(asset=self.asset, scheduled_at=before)
        second = Broadcast.objects.create(
            asset=self.asset, scheduled_at=before + timedelta(hours=24)
        )
        response = self.client.get(
            "/calendar/", {"month": "2027-02", "day": "2027-02-01"}
        )
        self.assertEqual([job.id for job in response.context["day_events"]], [first.id])
        self.assertNotIn(second.id, [job.id for job in response.context["day_events"]])

    def test_busy_day_limits_grid_and_opens_full_agenda(self):
        start = datetime(2027, 2, 2, 10, tzinfo=ROME)
        jobs = [
            Broadcast.objects.create(
                asset=self.asset, scheduled_at=start + timedelta(hours=i)
            )
            for i in range(5)
        ]
        response = self.client.get(
            "/calendar/", {"month": "2027-02", "day": "2027-02-02"}
        )
        cell = next(
            cell
            for week in response.context["weeks"]
            for cell in week
            if cell["iso"] == "2027-02-02"
        )
        self.assertEqual(len(cell["events"]), 3)
        self.assertEqual(cell["extra"], 2)
        self.assertContains(response, "Altri 2")
        self.assertEqual(len(response.context["day_events"]), 5)
        self.assertContains(response, f"/calendar/{jobs[-1].id}/")

    def test_overlapping_previous_day_is_flagged(self):
        self.asset.duration_seconds = 3600
        self.asset.save()
        start = datetime(2027, 2, 1, 23, 30, tzinfo=ROME)
        Broadcast.objects.create(asset=self.asset, scheduled_at=start)
        other = Broadcast.objects.create(
            asset=self.asset, scheduled_at=start + timedelta(minutes=45)
        )
        response = self.client.get(
            "/calendar/", {"month": "2027-02", "day": "2027-02-02"}
        )
        self.assertEqual(response.context["day_events"][0].id, other.id)
        self.assertTrue(response.context["day_events"][0].slot_conflict)

    def test_details_show_controls_and_history_remains_in_calendar(self):
        job = Broadcast.objects.create(
            asset=self.asset, scheduled_at=self.now + timedelta(days=1)
        )
        self.assertContains(
            self.client.get(f"/calendar/{job.id}/"), "Modifica appuntamento"
        )
        self.assertEqual(Client().get(f"/calendar/{job.id}/").status_code, 302)
        self.client.post(f"/calendar/{job.id}/cancel/")
        response = self.client.get(f"/calendar/{job.id}/")
        self.assertContains(response, "Annullato")
        self.assertNotContains(response, "Modifica appuntamento")
        self.assertNotContains(self.client.get("/calendar/"), "Storico delle trasmissioni")


def load_tests(loader, suite, pattern):
    import unittest

    return unittest.TestSuite(
        CalendarDisplayTests(name)
        for name in CalendarDisplayTests.__dict__
        if name.startswith("test_")
    )
