"""Bounded monthly projection of the existing calendar; dates use Rome civil time."""

import calendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import Broadcast
from .recurrence import OPEN, ROME

MONTHS = [
    "",
    "Gennaio",
    "Febbraio",
    "Marzo",
    "Aprile",
    "Maggio",
    "Giugno",
    "Luglio",
    "Agosto",
    "Settembre",
    "Ottobre",
    "Novembre",
    "Dicembre",
]


def month_context(request):
    today = timezone.now().astimezone(ROME).date()
    try:
        month = date.fromisoformat(
            request.GET.get("month", today.strftime("%Y-%m")) + "-01"
        )
        if not 1900 <= month.year <= 2200:
            raise ValueError
        selected = parse_date(
            request.GET.get(
                "day",
                (
                    today
                    if (today.year, today.month) == (month.year, month.month)
                    else month
                ).isoformat(),
            )
        )
        if selected is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ValidationError(
            "Scegli un mese valido (AAAA-MM) e una data valida (AAAA-MM-GG)."
        ) from None
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(
        month.year, month.month
    )
    first, last = weeks[0][0], weeks[-1][-1] + timedelta(days=1)
    start, end = [datetime.combine(day, time(), tzinfo=ROME) for day in [first, last]]
    events = list(
        Broadcast.objects.filter(scheduled_at__gte=start, scheduled_at__lt=end)
        .select_related("asset", "series")
        .order_by("scheduled_at", "id")
    )
    # Include earlier long episodes when detecting overlaps at a day/month boundary.
    longest = (
        Broadcast.objects.filter(status__in=OPEN).aggregate(
            value=Max("asset__duration_seconds")
        )["value"]
        or 0
    )
    active = (
        Broadcast.objects.filter(
            status__in=OPEN,
            scheduled_at__gte=start - timedelta(seconds=longest + 3),
            scheduled_at__lt=end,
        )
        .select_related("asset")
        .order_by("scheduled_at", "id")
    )
    conflict_ids = set()
    furthest, previous = None, None
    for event in active:
        if furthest is not None and event.scheduled_at < furthest:
            conflict_ids.update([previous, event.id])
        finish = event.scheduled_at + timedelta(
            seconds=(event.asset.duration_seconds or 0) + 3
        )
        if furthest is None or finish > furthest:
            furthest, previous = finish, event.id
    days = defaultdict(list)
    for event in events:
        event.slot_conflict = event.id in conflict_ids
        days[event.scheduled_at.astimezone(ROME).date()].append(event)
    cells = [
        [
            {
                "date": day,
                "iso": day.isoformat(),
                "outside": day.month != month.month,
                "today": day == today,
                "past": day < today,
                "events": days[day][:3],
                "extra": max(0, len(days[day]) - 3),
                "count": len(days[day]),
            }
            for day in week
        ]
        for week in weeks
    ]
    previous = month - timedelta(days=1)
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {
        "has_conflicts": any(event.slot_conflict for event in events),
        "weeks": cells,
        "month_key": month.strftime("%Y-%m"),
        "month_label": f"{MONTHS[month.month]} {month.year}",
        "previous_month": previous.strftime("%Y-%m") if previous.year >= 1900 else None,
        "next_month": following.strftime("%Y-%m") if following.year <= 2200 else None,
        "selected_day": selected,
        "day_events": days[selected],
        "today": today,
        "weekday_labels": ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"],
    }
