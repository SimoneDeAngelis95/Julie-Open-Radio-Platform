"""Weekly schedules, stable exceptions and bounded, renewable materialization."""

import itertools
from datetime import UTC, datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Broadcast, BroadcastSeries, RadioSettings

ROME = ZoneInfo("Europe/Rome")
OPEN = ["scheduled", "dispatching", "playing"]
DAYS = [
    (str(i), day)
    for i, day in enumerate(
        ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    )
]


def station_lock():
    RadioSettings.objects.get_or_create(pk=1)
    RadioSettings.objects.select_for_update().get(pk=1)


def occurrences(starts_at, weekdays, until, first=None, last=None):
    """Fold 0 once on autumn transitions; nonexistent spring wall times are omitted."""
    local = starts_at.astimezone(ROME)
    day = max(local.date(), first or local.date())
    stop = min(
        until or last or day + timedelta(days=365),
        last or until or day + timedelta(days=365),
    )
    while day <= stop:
        if day.weekday() in weekdays:
            wall = datetime.combine(day, local.time().replace(tzinfo=None))
            instant = wall.replace(tzinfo=ROME, fold=0).astimezone(UTC)
            if (
                instant >= starts_at
                and instant.astimezone(ROME).replace(tzinfo=None) == wall
            ):
                yield day, instant
        day += timedelta(days=1)


def conflict_message(start, asset):
    local = start.astimezone(ROME)
    return f"Questo orario si sovrappone a «{asset.title}» il {local:%d/%m/%Y alle %H:%M}. Nessun appuntamento è stato salvato."


def validate_intervals(asset, planned, *, exclude_ids=(), exclude_series=None):
    """Check materialized appointments and future rules beyond the rolling horizon."""
    planned = list(planned)
    if not planned:
        raise ValidationError(
            "Nessuna data futura corrisponde ai giorni e al periodo scelti."
        )
    duration = asset.duration_seconds or 0
    spans = [
        (start.astimezone(UTC), start.astimezone(UTC) + timedelta(seconds=duration + 3))
        for _, start in planned
    ]
    for (_, end), (start, _) in itertools.pairwise(spans):
        if start < end:
            raise ValidationError(
                "Le puntate di questa serie si sovrappongono tra loro."
            )
    existing = (
        Broadcast.objects.filter(status__in=OPEN)
        .exclude(pk__in=exclude_ids)
        .select_related("asset")
    )
    if exclude_series:
        existing = existing.exclude(
            series_id=exclude_series, is_exception=False, status="scheduled"
        )
    existing = list(existing)
    for other in existing:
        end = other.scheduled_at + timedelta(
            seconds=(other.asset.duration_seconds or 0) + 3
        )
        if any(start < end and finish > other.scheduled_at for start, finish in spans):
            raise ValidationError(conflict_message(other.scheduled_at, other.asset))
    rules = BroadcastSeries.objects.filter(active=True).select_related("asset")
    if exclude_series:
        rules = rules.exclude(pk=exclude_series)
    # Existing occurrences/exceptions are authoritative, including cancellations.
    for rule in rules:
        padding = ceil((rule.asset.duration_seconds or 0) / 86400) + 1
        first = (spans[0][0] - timedelta(days=padding)).astimezone(ROME).date()
        last = spans[-1][1].astimezone(ROME).date() + timedelta(days=1)
        reserved_dates = set(
            rule.broadcasts.filter(occurrence_date__range=[first, last]).values_list(
                "occurrence_date", flat=True
            )
        )
        for day, start in occurrences(
            rule.starts_at, rule.weekdays, rule.until, first, last
        ):
            if day in reserved_dates:
                continue
            end = start + timedelta(seconds=(rule.asset.duration_seconds or 0) + 3)
            if any(begin < end and finish > start for begin, finish in spans):
                raise ValidationError(conflict_message(start, rule.asset))


def plan(starts_at, weekdays, until, now=None):
    now = now or timezone.now()
    today = now.astimezone(ROME).date()
    # Always at most one year per pass, even for a distant first appointment.
    first = max(today, starts_at.astimezone(ROME).date())
    horizon = max(today + timedelta(days=365), first + timedelta(days=365))
    return [
        (day, start)
        for day, start in occurrences(starts_at, weekdays, until, first, horizon)
        if start > now
    ], min(until or horizon, horizon)


def validate_weekly(
    asset, starts_at, weekdays, until, *, exclude_series=None, exceptions=()
):
    """Also reserve distant singles and later-starting rules, beyond materialized dates."""
    planned, horizon = plan(starts_at, weekdays, until)
    anchors = list(
        Broadcast.objects.filter(
            status__in=OPEN, scheduled_at__date__gt=horizon
        ).values_list("scheduled_at", flat=True)
    )
    anchors += list(
        BroadcastSeries.objects.filter(active=True)
        .exclude(pk=exclude_series)
        .values_list("starts_at", flat=True)
    )
    padding = ceil((asset.duration_seconds or 0) / 86400) + 1
    for anchor in anchors:
        day = max(anchor, starts_at).astimezone(ROME).date()
        planned += list(
            occurrences(
                starts_at,
                weekdays,
                until,
                day - timedelta(days=padding),
                day + timedelta(days=366),
            )
        )
    planned = sorted(
        {
            (day, instant)
            for day, instant in planned
            if day not in exceptions and instant > timezone.now()
        },
        key=lambda item: item[1],
    )
    validate_intervals(asset, planned, exclude_series=exclude_series)


def save_programming(form, series=None):
    """Caller holds the station lock; validate everything before any mutation."""
    if series is not None:
        # Lock occurrence rows as well: the scheduler claims with a conditional UPDATE.
        list(series.broadcasts.select_for_update().values_list("id", flat=True))
    data = form.cleaned_data
    if data.get("repeat") != "weekly":
        job = form.save(commit=False)
        if job.series_id:
            job.is_exception = True
        job.save()
        return job
    planned, horizon = plan(
        data["scheduled_at"], [int(day) for day in data["weekdays"]], data.get("until")
    )
    # A manually edited or cancelled occurrence remains an exception.
    exceptions = (
        {}
        if series is None
        else dict(
            series.broadcasts.filter(is_exception=True).values_list(
                "occurrence_date", "id"
            )
        )
    )
    planned = [(day, start) for day, start in planned if day not in exceptions]
    validate_weekly(
        data["asset"],
        data["scheduled_at"],
        [int(day) for day in data["weekdays"]],
        data.get("until"),
        exclude_series=series.pk if series else None,
        exceptions=exceptions,
    )
    if series is None:
        series = BroadcastSeries(asset=data["asset"])
    series.starts_at = data["scheduled_at"]
    series.weekdays = [int(day) for day in data["weekdays"]]
    series.until = data.get("until")
    series.is_public = data["is_public"]
    series.generated_until = horizon
    series.last_error = ""
    series.save()
    now = timezone.now()
    wanted = {day for day, _ in planned}
    future = series.broadcasts.filter(
        status="scheduled", is_exception=False, scheduled_at__gt=now
    )
    future.exclude(occurrence_date__in=wanted).update(
        status="cancelled", ended_at=now, outcome="Rimosso dalla serie.", updated_at=now
    )
    for day, start in planned:
        existing = series.broadcasts.filter(occurrence_date=day).first()
        if existing and (
            existing.is_exception
            or existing.status
            in [
                "dispatching",
                "playing",
                "completed",
                "interrupted",
                "skipped",
                "error",
            ]
        ):
            continue
        Broadcast.objects.update_or_create(
            series=series,
            occurrence_date=day,
            defaults={
                "asset": series.asset,
                "scheduled_at": start,
                "is_public": series.is_public,
                "status": "scheduled",
                "ended_at": None,
                "outcome": "",
            },
        )
    return series


def extend_series(now=None):
    """Daily maintenance; conflicts remain visible and are retried, never overwritten."""
    now = now or timezone.now()
    today = now.astimezone(ROME).date()
    for identifier in (
        BroadcastSeries.objects.filter(active=True)
        .filter(Q(until__isnull=True) | Q(until__gte=today))
        .filter(
            Q(generated_until__isnull=True)
            | Q(generated_until__lt=today + timedelta(days=364))
        )
        .values_list("pk", flat=True)
    ):
        with transaction.atomic():
            station_lock()
            series = (
                BroadcastSeries.objects.select_for_update()
                .select_related("asset")
                .get(pk=identifier)
            )
            if not series.active:
                continue
            planned, horizon = plan(
                series.starts_at, series.weekdays, series.until, now
            )
            existing_dates = set(
                series.broadcasts.values_list("occurrence_date", flat=True)
            )
            planned = [
                (day, start) for day, start in planned if day not in existing_dates
            ]
            try:
                from .appointments import validate_asset

                validate_asset(series.asset)
                if planned:
                    validate_intervals(series.asset, planned, exclude_series=series.pk)
                    Broadcast.objects.bulk_create(
                        [
                            Broadcast(
                                series=series,
                                occurrence_date=day,
                                asset=series.asset,
                                scheduled_at=start,
                                is_public=series.is_public,
                            )
                            for day, start in planned
                        ]
                    )
                series.generated_until = horizon
                series.last_error = ""
            except ValidationError as error:
                series.last_error = " ".join(error.messages)[:500]
            series.save(update_fields=["generated_until", "last_error", "updated_at"])
