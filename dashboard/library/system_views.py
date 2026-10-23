"""Read-only operational overview, using observations collected off the audio path."""

import shutil
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import engine, transmission
from .host_metrics import read_metrics
from .models import Asset, BackupJob, Broadcast, BroadcastSeries, RadioSnapshot
from .storage import data_path


def storage_status(label, path):
    try:
        usage = shutil.disk_usage(path)
        return {
            "label": label,
            "known": True,
            "free": usage.free,
            "total": usage.total,
            "used_percent": round(100 * usage.used / usage.total) if usage.total else 0,
            "low": usage.free < max(1024**3, usage.total * 0.1),
        }
    except OSError:
        return {"label": label, "known": False}


@user_passes_test(lambda user: user.is_authenticated and user.is_superuser)
@require_GET
@never_cache
def index(request):
    now = timezone.now()
    snapshot = RadioSnapshot.objects.filter(pk=1).first()
    fresh = bool(
        snapshot and timedelta(0) <= now - snapshot.checked_at < timedelta(seconds=50)
    )
    failed_audio = Asset.objects.filter(
        purged_at__isnull=True,
        trashed_at__isnull=True,
    ).filter(status=Asset.Status.ERROR)
    failed_replay = Asset.objects.filter(
        purged_at__isnull=True,
        trashed_at__isnull=True,
        category=Asset.Category.PODCAST,
        replay_status=Asset.ReplayStatus.ERROR,
    )
    broadcasts = (
        Broadcast.objects.select_related("asset")
        .filter(
            scheduled_at__gte=now - timedelta(days=30),
            status__in=[
                Broadcast.Status.ERROR,
                Broadcast.Status.SKIPPED,
                Broadcast.Status.INTERRUPTED,
            ],
        )
        .order_by("-scheduled_at")
    )
    series = BroadcastSeries.objects.filter(active=True).exclude(last_error="")
    backups = BackupJob.objects.order_by("-created_at")
    latest_success = (
        backups.filter(
            status__in=[BackupJob.Status.READY, BackupJob.Status.EXPIRED],
            finished_at__isnull=False,
        )
        .order_by("-finished_at")
        .first()
    )
    return render(
        request,
        "maintenance/status.html",
        {
            "checked_at": now,
            "snapshot": snapshot,
            "fresh": fresh,
            "paused": data_path("locks/radio-stopped").exists(),
            "temporarily_suspended": transmission.marker().exists(),
            "storage": [
                storage_status("Audio", settings.MEDIA_ROOT),
                storage_status("Backup", settings.BACKUP_ROOT),
            ],
            "latest_backup": backups.first(),
            "successful_backup": latest_success,
            "audio_count": failed_audio.count(),
            "audio_errors": failed_audio.order_by("-updated_at")[:10],
            "replay_count": failed_replay.count(),
            "replay_errors": failed_replay.order_by("-updated_at")[:10],
            "broadcast_count": broadcasts.count(),
            "broadcast_errors": broadcasts[:10],
            "series_count": series.count(),
            "series_errors": series.order_by("id")[:10],
        },
    )


@user_passes_test(lambda user: user.is_authenticated and user.is_superuser)
@require_GET
@never_cache
def host_status(request):
    return JsonResponse(read_metrics())


@user_passes_test(lambda user: user.is_authenticated and user.is_superuser)
@require_POST
@never_cache
def transmission_control(request):
    if request.POST.get("confirm") != "yes":
        messages.error(request, "Conferma il comando della trasmissione.")
    else:
        try:
            action = request.POST.get("action")
            transmission.change(action)
        except (transmission.TransmissionError, engine.EngineError, OSError) as error:
            message = str(error) if not isinstance(error, OSError) else "Comando non completato. Controlla lo stato e riprova."
            messages.error(request, message)
        else:
            messages.success(request, "Trasmissione sospesa temporaneamente." if action == "suspend" else "Trasmissione riattivata.")
    return redirect("library:system_status")
