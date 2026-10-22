"""Administrator-only restore wizard. Reading this page never changes station data."""

from uuid import UUID

from django import forms
from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from . import engine, reset, restore
from .backup_views import administrator
from .backups import BackupError
from .models import RestoreJob
from .restore_archive import RestoreError


class UploadForm(forms.Form):
    archive = forms.FileField(
        label="Backup completo di Julie",
        widget=forms.ClearableFileInput(attrs={"accept": ".zip"}),
    )


@administrator
@require_http_methods(["GET", "POST"])
def index(request):
    error = ""
    form = UploadForm()
    if request.method == "POST":
        try:
            action = request.POST.get("action")
            if getattr(request, "restore_upload_error", None):
                raise RestoreError(request.restore_upload_error)
            if action == "resume":
                reset.resume()
                messages.success(request, "Radio riattivata.")
            elif action == "upload":
                form = UploadForm(request.POST, request.FILES)
                if not form.is_valid():
                    raise RestoreError("Scegli un backup completo ZIP di Julie.")
                restore.receive(form.cleaned_data["archive"])
            else:
                try:
                    identifier = UUID(request.POST.get("job", ""))
                except (ValueError, TypeError, AttributeError):
                    raise RestoreError("Richiesta non valida.") from None
                job = get_object_or_404(RestoreJob, pk=identifier)
                if action == "protect":
                    restore.protect(job.id)
                elif action == "cancel":
                    restore.cancel(job.id)
                elif action == "confirm":
                    restore.confirm(
                        job.id,
                        request.POST.get("confirmation", ""),
                        request.POST.get("downloaded") == "on",
                    )
                else:
                    raise RestoreError("Azione non valida.")
        except (
            RestoreError,
            BackupError,
            reset.ResetError,
            engine.EngineError,
        ) as failure:
            error = str(failure)
        else:
            return redirect("library:restore")
    job = RestoreJob.objects.select_related("safety_backup").first()
    counts = job.summary.get("counts", {}) if job else {}
    return render(
        request,
        "maintenance/restore.html",
        {
            "form": form,
            "backup_date": parse_datetime(job.summary.get("captured_at", ""))
            if job
            else None,
            "max_restore_gb": settings.MAX_RESTORE_BYTES / 1024**3,
            "job": job,
            "active": job is not None and job.status in restore.ACTIVE,
            "error": error,
            "asset_count": counts.get("library_asset", 0),
            "playlist_count": counts.get("library_playlist", 0),
            "broadcast_count": counts.get("library_broadcast", 0),
            "sample_count": counts.get("library_listeningsample", 0),
        },
    )
