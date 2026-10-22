"""Superusers only: credentials must never be exposed to ordinary staff."""

import hashlib
import json
import os
import re
import stat
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_safe

from .backups import BackupError, archive_path, enqueue
from .forms import BackupForm
from .models import BackupJob
from .public_api import FileSlice


def administrator(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponse(
                "Solo l’amministratore può gestire i backup.", status=403
            )
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    return wrapped


def version():
    values = list(
        BackupJob.objects.values("id", "status", "progress", "size_bytes")[:20]
    )
    return hashlib.sha256(
        json.dumps(values, default=str, sort_keys=True).encode()
    ).hexdigest()


@administrator
def index(request):
    form = BackupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            enqueue(form.cleaned_data["kind"])
        except BackupError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request, "Backup richiesto. La preparazione procede in background."
            )
            return redirect("library:backups")
    page = Paginator(BackupJob.objects.all(), 20).get_page(request.GET.get("page"))
    for job in page:
        job.available = (
            job.status == "ready"
            and job.expires_at
            and job.expires_at > timezone.now()
            and archive_path(job).is_file()
            and not archive_path(job).is_symlink()
        )
        job.size_mb = round(job.size_bytes / (1024 * 1024), 1)
    return render(
        request,
        "backups/index.html",
        {
            "form": form,
            "page": page,
            "active": BackupJob.objects.filter(
                status__in=["queued", "running"]
            ).first(),
            "version": version(),
        },
    )


@administrator
@require_GET
def status(request):
    return JsonResponse({"version": version()})


@administrator
@require_safe
def download(request, job_id):
    job = get_object_or_404(
        BackupJob, pk=job_id, status="ready", expires_at__gt=timezone.now()
    )
    path = archive_path(job)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        source = os.fdopen(descriptor, "rb")
    except OSError:
        raise Http404 from None
    info = os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode):
        source.close()
        raise Http404
    size = info.st_size
    header = request.headers.get("Range") if request.method == "GET" else None
    if header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
        try:
            if not match or not any(match.groups()):
                raise ValueError
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            else:
                if int(last) <= 0:
                    raise ValueError
                start, end = max(0, size - int(last)), size - 1
            if start >= size or end < start:
                raise ValueError
        except ValueError:
            source.close()
            return HttpResponse(
                status=416, headers={"Content-Range": f"bytes */{size}"}
            )
        source.seek(start)
        response = StreamingHttpResponse(
            FileSlice(source, end - start + 1),
            status=206,
            content_type="application/octet-stream",
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = end - start + 1
    elif request.method == "HEAD":
        source.close()
        response = HttpResponse(
            headers={"Content-Length": str(size)},
            content_type="application/octet-stream",
        )
    else:
        response = FileResponse(
            source,
            as_attachment=True,
            filename=path.name,
            content_type="application/octet-stream",
        )
    response["Content-Disposition"] = f'attachment; filename="{path.name}"'
    response["Accept-Ranges"] = "bytes"
    return response
