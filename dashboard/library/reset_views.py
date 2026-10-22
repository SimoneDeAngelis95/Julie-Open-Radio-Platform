"""Explicit administrator-only steps. GET never starts a reset or erases content."""

from uuid import UUID

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import engine, reset
from .backup_views import administrator
from .backups import BackupError
from .models import Asset, Broadcast, ListeningSample, Playlist, ResetJob


@administrator
@require_http_methods(["GET", "POST"])
def index(request):
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "prepare":
                reset.prepare()
            elif action == "resume":
                reset.resume()
                messages.success(request, "Radio riattivata.")
            elif action in ["confirm", "cancel"]:
                try:
                    identifier = UUID(request.POST.get("job", ""))
                except (ValueError, TypeError, AttributeError):
                    raise reset.ResetError("Richiesta di reset non valida.") from None
                job = get_object_or_404(ResetJob, pk=identifier)
                if action == "cancel":
                    reset.cancel(job.id)
                else:
                    reset.confirm(
                        job.id,
                        request.POST.get("confirmation", ""),
                        request.POST.get("downloaded") == "on",
                    )
            else:
                raise reset.ResetError("Azione non valida.")
        except (reset.ResetError, BackupError, engine.EngineError) as failure:
            error = str(failure)
        else:
            return redirect("library:reset")
    job = ResetJob.objects.select_related("backup").first()
    return render(
        request,
        "maintenance/reset.html",
        {
            "job": job,
            "active": job is not None and job.status in reset.ACTIVE,
            "stopped": reset.stopped_marker().exists(),
            "error": error,
            "counts": {
                "audio": Asset.objects.count(),
                "playlists": Playlist.objects.count(),
                "broadcasts": Broadcast.objects.count(),
                "samples": ListeningSample.objects.count(),
            },
        },
    )
