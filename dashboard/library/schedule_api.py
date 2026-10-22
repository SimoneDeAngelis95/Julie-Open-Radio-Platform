"""Public, read-only calendar projections. No media paths or operational errors."""

from datetime import UTC, datetime, time, timedelta

from django.core.paginator import EmptyPage, Paginator
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import Broadcast
from .public_api import public_endpoint
from .recurrence import ROME


def boundary(value):
    day = parse_date(value)
    if day:
        return datetime.combine(day, time(), tzinfo=ROME)
    instant = parse_datetime(value)
    if instant is None or timezone.is_naive(instant):
        raise ValueError
    return instant


def window(request, calendar=False):
    today = timezone.now().astimezone(ROME).date()
    first = (
        boundary(request.GET["from"])
        if "from" in request.GET
        else datetime.combine(
            today - timedelta(days=30 if calendar else 0), time(), tzinfo=ROME
        )
    )
    last = (
        boundary(request.GET["to"])
        if "to" in request.GET
        else first + timedelta(days=366 if calendar else 31)
    )
    if last <= first or last - first > timedelta(days=366):
        raise ValueError
    return first, last


def candidates(first, last):
    return (
        Broadcast.objects.filter(
            is_public=True,
            asset__trashed_at__isnull=True,
            asset__purged_at__isnull=True,
            asset__is_active=True,
            scheduled_at__gte=first,
            scheduled_at__lt=last,
        )
        .select_related("asset")
        .order_by("scheduled_at", "id")
    )


def projected(job):
    end = (
        job.scheduled_at + timedelta(seconds=job.asset.duration_seconds)
        if job.asset.duration_seconds
        else None
    )
    modified = max(
        value
        for value in [
            job.updated_at,
            job.asset.updated_at,
            job.dispatched_at,
            job.started_at,
            job.ended_at,
        ]
        if value
    )
    return {
        "id": str(job.id),
        "podcast_id": str(job.asset_id),
        "title": job.asset.title,
        "description": job.asset.description,
        "starts_at": job.scheduled_at.astimezone(ROME).isoformat(),
        "ends_at": end.astimezone(ROME).isoformat() if end else None,
        "status": job.status,
        "series_id": str(job.series_id) if job.series_id else None,
        "updated_at": modified.isoformat(),
    }


@public_endpoint
def schedule(request):
    try:
        first, last = window(request)
        number = int(request.GET.get("page", "1"))
        size = int(request.GET.get("page_size", "100"))
        if number < 1 or not 1 <= size <= 200:
            raise ValueError
        page = Paginator(candidates(first, last), size).page(number)
    except (ValueError, TypeError, OverflowError):
        return JsonResponse(
            {
                "error": "Usa from e to come date ISO o istanti con fuso orario, intervallo massimo 366 giorni; page positivo e page_size da 1 a 200."
            },
            status=400,
        )
    except EmptyPage:
        return JsonResponse({"error": "Pagina non disponibile."}, status=404)
    return JsonResponse(
        {
            "timezone": "Europe/Rome",
            "from": first.isoformat(),
            "to": last.isoformat(),
            "count": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "results": [projected(job) for job in page],
        }
    )


def escape(value):
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold(line):
    """Fold at 75 UTF-8 octets, never inside a multibyte character (RFC 5545)."""
    parts, current = [], ""
    for character in line:
        if len((current + character).encode()) > 75:
            parts.append(current)
            current = " "
        current += character
    parts.append(current)
    return "\r\n".join(parts)


def stamp(value):
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


@public_endpoint
def calendar(request):
    try:
        first, last = window(request, calendar=True)
    except (ValueError, TypeError, OverflowError):
        return JsonResponse(
            {"error": "Intervallo calendario non valido (massimo 366 giorni)."},
            status=400,
        )
    jobs = candidates(first, last)
    if jobs.count() > 10000:
        return JsonResponse(
            {"error": "Troppi appuntamenti: scegli un periodo più breve."}, status=400
        )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Julie//Palinsesto//IT",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Julie - Palinsesto",
    ]
    for job in jobs.iterator():
        item = projected(job)
        modified = datetime.fromisoformat(item["updated_at"])
        cancelled = job.status in ["cancelled", "skipped", "error", "interrupted"]
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{job.id}@julie-radio",
                f"DTSTAMP:{stamp(modified)}",
                f"LAST-MODIFIED:{stamp(modified)}",
                f"SEQUENCE:{int(modified.timestamp())}",
                f"DTSTART:{stamp(job.scheduled_at)}",
            ]
        )
        if item["ends_at"]:
            lines.append("DTEND:" + stamp(datetime.fromisoformat(item["ends_at"])))
        lines.extend(
            [
                "SUMMARY:" + escape(item["title"]),
                "DESCRIPTION:" + escape(item["description"]),
                "STATUS:" + ("CANCELLED" if cancelled else "CONFIRMED"),
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return HttpResponse(
        "\r\n".join(fold(line) for line in lines) + "\r\n",
        content_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="julie-palinsesto.ics"'},
    )
