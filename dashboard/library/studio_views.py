"""Staff-only current playout and sampled connection history."""

from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Avg, Count, Max, Min
from django.db.models.functions import TruncDate, TruncMonth
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import Broadcast, ListeningSample, RadioSnapshot
from .playout import station_settings
from .site_config import stream_url
from .views import admin_required


def current_status():
    snapshot = RadioSnapshot.objects.filter(pk=1).first()
    fresh = bool(
        snapshot
        and timedelta(0) <= timezone.now() - snapshot.checked_at < timedelta(seconds=50)
    )
    upcoming = (
        Broadcast.objects.select_related("asset")
        .filter(
            status=Broadcast.Status.SCHEDULED,
            scheduled_at__gte=timezone.now(),
            asset__trashed_at__isnull=True,
            asset__purged_at__isnull=True,
            asset__is_active=True,
        )
        .order_by("scheduled_at", "id")
        .first()
    )
    config = station_settings()
    return {
        "fresh": fresh,
        "checked_at": snapshot.checked_at.isoformat() if snapshot else None,
        "stream_online": snapshot.stream_online if fresh else None,
        "listeners": snapshot.listeners if fresh else None,
        "kind": snapshot.kind if fresh else "Dati non aggiornati",
        "title": snapshot.title if fresh else "",
        "artist": snapshot.artist if fresh else "",
        "engine_online": snapshot.engine_online if fresh else False,
        "playlist": config.selected_playlist.name
        if config.selected_playlist
        else "Tutta la musica attiva",
        "stream_url": stream_url(),
        "upcoming": {
            "title": upcoming.asset.title,
            "at": upcoming.scheduled_at.isoformat(),
        }
        if upcoming
        else None,
    }


@admin_required
@require_GET
def on_air(request):
    return render(request, "studio/on_air.html", {"status": current_status()})


@admin_required
@require_GET
def status(request):
    response = JsonResponse(current_status())
    response["Cache-Control"] = "no-store"
    return response


def day_start(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def summarize_days(samples, first_day, last_day):
    """Query just the visible twenty calendar days, including explicit unknown gaps."""
    daily = {
        item["day"]: item
        for item in samples.filter(
            minute__gte=day_start(first_day),
            minute__lt=day_start(last_day + timedelta(days=1)),
        )
        .annotate(day=TruncDate("minute"))
        .values("day")
        .annotate(
            peak=Max("listeners"),
            average=Avg("listeners"),
            known=Count("listeners"),
            total=Count("id"),
        )
    }
    return daily


def history_chart(samples, first_day, last_day):
    """Use monthly bars beyond a year; never squeeze thousands of daily bars on screen."""
    monthly = (last_day - first_day).days + 1 > 366
    grouping = TruncMonth("minute") if monthly else TruncDate("minute")
    points = {}
    for item in (
        samples.annotate(bucket=grouping)
        .values("bucket")
        .annotate(peak=Max("listeners"))
    ):
        key = item["bucket"].date() if monthly else item["bucket"]
        points[key] = item["peak"]
    maximum = (
        max((value for value in points.values() if value is not None), default=0) or 1
    )
    rows = []
    cursor = first_day.replace(day=1) if monthly else first_day
    while cursor <= last_day:
        peak = points.get(cursor)
        rows.append(
            {
                "day": cursor,
                "peak": peak,
                "height": round(100 * peak / maximum, 2) if peak is not None else 0,
            }
        )
        if monthly:
            cursor = (
                cursor.replace(year=cursor.year + 1, month=1)
                if cursor.month == 12
                else cursor.replace(month=cursor.month + 1)
            )
        else:
            cursor += timedelta(days=1)
    return rows, monthly


@admin_required
@require_GET
def listening(request):
    today = timezone.localdate()
    first = ListeningSample.objects.aggregate(first=Min("minute"))["first"]
    years = [
        value.year
        for value in ListeningSample.objects.filter(
            minute__lt=day_start(today + timedelta(days=1))
        ).datetimes("minute", "year", order="DESC")
    ]
    selected = request.GET.get("period", request.GET.get("days", "7"))
    periods = [(str(days), f"Ultimi {days} giorni") for days in [7, 30, 90, 365]]
    periods += [(f"year:{year}", f"Anno {year}") for year in years]
    periods.append(("all", "Tutto lo storico"))
    if selected not in dict(periods):
        selected = "7"
    days = int(selected) if selected.isdigit() else None
    if selected == "all":
        first_day = min(timezone.localdate(first), today) if first else today
        last_day = today
    elif selected.startswith("year:"):
        year = int(selected.split(":")[1])
        first_day = today.replace(year=year, month=1, day=1)
        last_day = min(today.replace(year=year, month=12, day=31), today)
    else:
        first_day = today - timedelta(days=days - 1)
        last_day = today
    samples = ListeningSample.objects.filter(
        minute__gte=day_start(first_day),
        minute__lt=day_start(last_day + timedelta(days=1)),
    )
    summary = samples.aggregate(
        peak=Max("listeners"),
        average=Avg("listeners"),
        known=Count("listeners"),
        total=Count("id"),
    )
    # range is lazy: even a long archive allocates only the visible twenty dates.
    page = Paginator(range((last_day - first_day).days + 1), 20).get_page(
        request.GET.get("page")
    )
    dates = [last_day - timedelta(days=offset) for offset in page.object_list]
    daily = summarize_days(samples, min(dates), max(dates))
    rows = [
        daily.get(
            day, {"day": day, "peak": None, "average": None, "known": 0, "total": 0}
        )
        for day in dates
    ]
    chart_rows, monthly = history_chart(samples, first_day, last_day)

    def page_url(number):
        return (
            reverse("library:listening")
            + "?"
            + urlencode({"period": selected, "page": number})
        )

    return render(
        request,
        "studio/listening.html",
        {
            "days": days,
            "selected_period": selected,
            "summary": summary,
            "rows": rows,
            "chart_rows": chart_rows,
            "chart_monthly": monthly,
            "first": first,
            "current": current_status(),
            "periods": periods,
            "page": page,
            "previous_url": page_url(page.previous_page_number())
            if page.has_previous()
            else None,
            "next_url": page_url(page.next_page_number()) if page.has_next() else None,
        },
    )
