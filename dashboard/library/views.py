import json
import re
import time
from datetime import timedelta
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    BroadcastForm,
    BulkPlaylistForm,
    MetadataForm,
    PlaylistForm,
    PlaylistSelectionForm,
    RadioSettingsForm,
    RemovalForm,
    UploadForm,
)
from .models import Asset, Broadcast, BroadcastSeries, Playlist, RadioSettings
from .recurrence import save_programming, station_lock
from .playout import eligible, music_candidates, safe_publish, station_settings
from .storage import data_path, save_upload

admin_required = user_passes_test(lambda user: user.is_authenticated and user.is_staff)


def library_queryset(request):
    assets = Asset.objects.exclude(category=Asset.Category.JINGLE).filter(
        trashed_at__isnull=True
    )
    category = request.GET.get("category", "")
    status = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()[:200]
    if category in Asset.Category.values:
        assets = assets.filter(category=category)
    if status in Asset.Status.values:
        assets = assets.filter(status=status)
    if query:
        assets = assets.filter(
            Q(title__icontains=query)
            | Q(artist__icontains=query)
            | Q(original_name__icontains=query)
        )
    playlist = request.GET.get("playlist", "")
    if playlist == "none":
        assets = assets.filter(category=Asset.Category.MUSIC, playlists__isnull=True)
    elif playlist:
        if playlist.isdigit() and len(playlist) <= 18:
            assets = assets.filter(playlists__pk=playlist)
        else:
            assets = assets.none()
    return assets.prefetch_related("playlists")


def counts():
    result = {
        row["status"]: row["total"]
        for row in Asset.objects.exclude(category=Asset.Category.JINGLE)
        .filter(trashed_at__isnull=True)
        .values("status")
        .annotate(total=Count("id"))
    }
    return {
        "total": sum(result.values()),
        "ready": result.get("ready", 0),
        "waiting": result.get("pending", 0) + result.get("processing", 0),
        "error": result.get("error", 0),
    }


@admin_required
@require_GET
def index(request):
    page = Paginator(library_queryset(request), 30).get_page(request.GET.get("page"))
    collections = list(Playlist.objects.all())
    for asset in page:
        assigned = {playlist.id for playlist in asset.playlists.all()}
        asset.playlist_options = [
            (playlist, playlist.id in assigned) for playlist in collections
        ]
        asset.playlist_count = len(assigned)
        names = [playlist.name for playlist in asset.playlists.all()]
        asset.playlist_caption = (
            (names[0] + (f" +{len(names) - 1}" if len(names) > 1 else ""))
            if names
            else "Seleziona playlist"
        )
    return render(
        request,
        "library/index.html",
        {
            "page": page,
            "counts": counts(),
            "playlists": collections,
            "playlist_filter": request.GET.get("playlist", ""),
            "categories": [
                (value, label)
                for value, label in Asset.Category.choices
                if value != Asset.Category.JINGLE
            ],
            "statuses": Asset.Status.choices,
            "category": request.GET.get("category", ""),
            "status": request.GET.get("status", ""),
            "q": request.GET.get("q", "")[:200],
        },
    )


@admin_required
def upload(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["schedule"]:
            return upload_and_schedule(request, form)
        added, duplicates, failed, trashed_duplicates = 0, 0, 0, 0
        for index, item in enumerate(form.cleaned_data["files"]):
            try:
                asset, created = save_upload(
                    item,
                    form.cleaned_data["category"],
                    replay_after_broadcast=form.cleaned_data["replay_after_broadcast"],
                    **form.cleaned_data["file_metadata"][index],
                )
            except (OSError, ValueError):
                failed += 1
                continue
            if form.cleaned_data["playlists"]:
                with transaction.atomic():
                    current = Asset.objects.select_for_update().get(pk=asset.pk)
                    if not current.trashed_at and not current.purged_at:
                        current.playlists.add(*form.cleaned_data["playlists"])
            added += int(created)
            duplicates += int(not created and asset.trashed_at is None)
            trashed_duplicates += int(not created and asset.trashed_at is not None)
        safe_publish()
        if added:
            messages.success(
                request,
                f"{added} file caricati. La preparazione partirà automaticamente.",
            )
        if duplicates:
            messages.info(
                request, f"{duplicates} file erano già presenti nella stessa libreria."
            )
        if trashed_duplicates:
            messages.info(
                request,
                f"{trashed_duplicates} file sono già nel cestino. Puoi ripristinarli dalla sezione Cestino.",
            )
        if failed:
            messages.error(
                request,
                f"{failed} file non sono stati salvati. Verifica lo spazio disponibile e riprova.",
            )
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            if failed:
                return JsonResponse(
                    {"error": (
                        f"{failed} file del gruppo non sono stati salvati. "
                        "Gli altri sono conservati. Verifica lo spazio disponibile e riprova."
                    )},
                    status=503,
                )
            return JsonResponse({"redirect": reverse("library:index")})
        return redirect("library:index")
    return render_upload(request, form)


def render_upload(request, form):
    if (
        request.method == "POST"
        and request.headers.get("X-Requested-With") == "XMLHttpRequest"
    ):
        error = " ".join(
            str(message) for errors in form.errors.values() for message in errors
        )
        return JsonResponse({"error": error}, status=400)
    return render(request, "library/upload.html", {"form": form})


def upload_and_schedule(request, form):
    from .appointments import validate_asset, validate_slot

    # Save bytes before the station transaction: large uploads must not lock the calendar.
    try:
        asset, _ = save_upload(
            form.cleaned_data["files"][0],
            Asset.Category.PODCAST,
            replay_after_broadcast=form.cleaned_data["replay_after_broadcast"],
            **form.cleaned_data["file_metadata"][0],
        )
    except (OSError, ValueError):
        form.add_error(
            None, "File non salvato. Controlla lo spazio disponibile e riprova."
        )
        return render_upload(request, form)
    try:
        with transaction.atomic():
            RadioSettings.objects.get_or_create(pk=1)
            RadioSettings.objects.select_for_update().get(pk=1)
            asset.refresh_from_db()
            validate_asset(asset)
            validate_slot(form.cleaned_data["scheduled_at"], asset.duration_seconds)
            Broadcast.objects.create(
                asset=asset, scheduled_at=form.cleaned_data["scheduled_at"]
            )
    except ValidationError as error:
        form.add_error(
            None,
            "File conservato, ma trasmissione non programmata: "
            + " ".join(error.messages),
        )
        return render_upload(request, form)
    messages.success(
        request,
        "Podcast caricato e trasmissione programmata. La preparazione prosegue automaticamente.",
    )
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"redirect": reverse("library:calendar")})
    return redirect("library:calendar")


@admin_required
def detail(request, asset_id):
    asset = get_object_or_404(Asset, pk=asset_id, trashed_at__isnull=True)
    form = MetadataForm(request.POST or None, instance=asset)
    if request.method == "POST" and form.is_valid():
        # Lock/reload so a concurrent worker's status and measurements are kept.
        with transaction.atomic():
            current = get_object_or_404(
                Asset.objects.select_for_update(), pk=asset_id, trashed_at__isnull=True
            )
            metadata_fields = [field for field in form.fields if field != "playlists"]
            for field in metadata_fields:
                setattr(current, field, form.cleaned_data[field])
            current.metadata_edited = True
            current.save(
                update_fields=[*metadata_fields, "metadata_edited", "updated_at"]
            )
            if "playlists" in form.fields:
                current.playlists.set(form.cleaned_data["playlists"])
        safe_publish()
        messages.success(
            request,
            "Metadati salvati. Le modifiche alla rotazione valgono dai prossimi brani.",
        )
        return redirect("library:detail", asset_id=asset.id)
    return render(request, "library/detail.html", {"asset": asset, "form": form})


@admin_required
@require_POST
def retry(request, asset_id):
    with transaction.atomic():
        asset = get_object_or_404(
            Asset.objects.select_for_update(), pk=asset_id, trashed_at__isnull=True
        )
        if asset.status == Asset.Status.ERROR:
            asset.status = Asset.Status.PENDING
            asset.error_message = ""
            asset.finished_at = None
            asset.save(
                update_fields=["status", "error_message", "finished_at", "updated_at"]
            )
            messages.success(request, "File rimesso in preparazione.")
    if asset.category == Asset.Category.JINGLE:
        return redirect("library:settings")
    return redirect("library:detail", asset_id=asset_id)


@admin_required
@require_GET
def status(request):
    assets = Paginator(library_queryset(request), 30).get_page(request.GET.get("page"))
    payload = [
        {
            "id": str(asset.id),
            "status": asset.status,
            "label": asset.get_status_display(),
            "title": asset.title,
            "artist": asset.original_name
            if asset.category == Asset.Category.PODCAST
            else asset.artist,
            "duration": asset.duration_label,
            "error": asset.error_message,
            "audio_url": reverse("library:audio", args=[asset.id])
            if asset.status == Asset.Status.READY
            else None,
        }
        for asset in assets
    ]
    return JsonResponse({"assets": payload, "counts": counts()})


def byte_chunks(path, start, length):
    with path.open("rb") as source:
        source.seek(start)
        while length:
            data = source.read(min(length, 64 * 1024))
            if not data:
                break
            length -= len(data)
            yield data


@admin_required
@require_GET
def audio_file(request, asset_id):
    asset = get_object_or_404(
        Asset, pk=asset_id, status=Asset.Status.READY, trashed_at__isnull=True
    )
    path = data_path(asset.prepared_path)
    if not path.is_file():
        raise Http404
    size = path.stat().st_size
    header = request.headers.get("Range")
    if header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
        start, end = 0, size - 1
        try:
            if not match or not any(match.groups()):
                raise ValueError
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                count = int(last)
                if count <= 0:
                    raise ValueError
                start = max(0, size - count)
            if start >= size or end < start:
                raise ValueError
        except ValueError:
            return HttpResponse(
                status=416, headers={"Content-Range": f"bytes */{size}"}
            )
        response = StreamingHttpResponse(
            byte_chunks(path, start, end - start + 1),
            status=206,
            content_type="audio/flac",
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = end - start + 1
    else:
        response = FileResponse(path.open("rb"), content_type="audio/flac")
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, no-store"
    return response


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


@admin_required
def radio_settings(request):
    config = station_settings()
    form = RadioSettingsForm(
        request.POST or None, request.FILES or None, instance=config
    )
    if request.method == "POST" and form.is_valid():
        replacement = form.cleaned_data.get("replacement")
        asset = None
        if replacement:
            try:
                asset, _ = save_upload(replacement, Asset.Category.JINGLE)
            except (OSError, ValueError):
                form.add_error(
                    "replacement",
                    "Il file non è stato salvato. Controlla lo spazio e riprova.",
                )
        if not form.errors:
            with transaction.atomic():
                current = RadioSettings.objects.select_for_update().get(pk=1)
                current.jingle_enabled = form.cleaned_data["jingle_enabled"]
                current.jingle_every = form.cleaned_data["jingle_every"]
                if asset is not None:
                    asset.refresh_from_db()
                    if asset.status == Asset.Status.READY:
                        current.current_jingle = asset
                        current.pending_jingle = None
                    else:
                        current.pending_jingle = asset
                current.save()
            safe_publish()
            messages.success(
                request,
                "Impostazioni salvate. Il nuovo jingle sarà usato solo quando sarà pronto.",
            )
            return redirect("library:settings")
    config.refresh_from_db()
    return render(
        request,
        "library/settings.html",
        {
            "form": form,
            "config": config,
            "active_count": sum(
                1
                for asset in music_candidates(config.selected_playlist)
                if eligible(asset)
            ),
        },
    )


@admin_required
@require_POST
def trash_asset(request, asset_id):
    with transaction.atomic():
        asset = get_object_or_404(
            Asset.objects.select_for_update().exclude(category=Asset.Category.JINGLE),
            pk=asset_id,
            trashed_at__isnull=True,
        )
        asset.trashed_at = timezone.now()
        asset.is_active = False
        asset.auto_publish = False
        asset.published_at = None
        asset.save(
            update_fields=[
                "trashed_at",
                "is_active",
                "auto_publish",
                "published_at",
                "updated_at",
            ]
        )
    safe_publish()
    messages.success(
        request, "File spostato nel cestino. L’originale e le playlist sono conservati."
    )
    return redirect("library:index")


@admin_required
@require_POST
def restore_asset(request, asset_id):
    with transaction.atomic():
        asset = get_object_or_404(
            Asset.objects.select_for_update().exclude(category=Asset.Category.JINGLE),
            pk=asset_id,
            trashed_at__isnull=False,
            purged_at__isnull=True,
        )
        if asset.removal_pending:
            messages.error(
                request,
                "Eliminazione in corso: attendi il completamento prima di ripristinare.",
            )
            return redirect("library:trash")
        if asset.copies_removed:
            asset.status = Asset.Status.PENDING
            asset.copies_removed = False
            asset.error_message = ""
        asset.trashed_at = None
        # Restoration must not unexpectedly put a song back on air.
        asset.is_active = False
        asset.save(
            update_fields=[
                "trashed_at",
                "is_active",
                "status",
                "copies_removed",
                "error_message",
                "updated_at",
            ]
        )
    safe_publish()
    messages.success(
        request,
        "File ripristinato. Riattiva la disponibilità nella scheda prima di usarlo in radio; se necessario verrà preparato di nuovo.",
    )
    return redirect("library:detail", asset_id=asset_id)


@admin_required
@require_GET
def trash(request):
    assets = (
        Asset.objects.exclude(category=Asset.Category.JINGLE)
        .filter(trashed_at__isnull=False, purged_at__isnull=True)
        .order_by("-trashed_at")
    )
    return render(
        request,
        "library/trash.html",
        {"page": Paginator(assets, 30).get_page(request.GET.get("page"))},
    )


@admin_required
def playlists(request):
    config = station_settings()
    form = PlaylistSelectionForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = RadioSettings.objects.select_for_update().get(pk=1)
            current.selected_playlist = form.cleaned_data["selected_playlist"]
            current.save(update_fields=["selected_playlist"])
        safe_publish()
        messages.success(
            request, "Selezione salvata. La radio la userà dalle prossime selezioni."
        )
        return redirect("library:playlists")
    items = Playlist.objects.annotate(
        visible_count=Count("assets", filter=Q(assets__trashed_at__isnull=True)),
        ready_count=Count(
            "assets",
            filter=Q(
                assets__trashed_at__isnull=True,
                assets__status=Asset.Status.READY,
                assets__is_active=True,
                assets__category=Asset.Category.MUSIC,
            ),
        ),
    )
    return render(
        request,
        "library/playlists.html",
        {
            "playlists": items,
            "config": station_settings(),
            "selection_form": form,
            "active_count": sum(
                1
                for asset in music_candidates(station_settings().selected_playlist)
                if eligible(asset)
            ),
        },
    )


@admin_required
def playlist_edit(request, playlist_id=None):
    playlist = (
        get_object_or_404(Playlist, pk=playlist_id) if playlist_id else Playlist()
    )
    form = PlaylistForm(request.POST or None, instance=playlist)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # Keep memberships of trashed songs so restoring does not lose organization.
            trashed = (
                list(playlist.assets.filter(trashed_at__isnull=False))
                if playlist.pk
                else []
            )
            playlist = form.save()
            if trashed:
                playlist.assets.add(*trashed)
        safe_publish()
        messages.success(request, "Playlist salvata.")
        return redirect("library:playlist_edit", playlist_id=playlist.id)
    return render(
        request,
        "library/playlist_edit.html",
        {"form": form, "playlist": playlist, "config": station_settings()},
    )


@admin_required
@require_POST
def playlist_activate(request, playlist_id):
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    if not any(eligible(asset) for asset in music_candidates(playlist)):
        messages.error(
            request,
            "La playlist non ha brani pronti e attivi. Non è stata cambiata la rotazione.",
        )
    else:
        with transaction.atomic():
            config = RadioSettings.objects.select_for_update().get(pk=1)
            config.selected_playlist = playlist
            config.save(update_fields=["selected_playlist"])
        safe_publish()
        messages.success(
            request, "La radio userà questa playlist dalle prossime selezioni."
        )
    return redirect("library:playlists")


@admin_required
@require_GET
def calendar(request):

    from .calendar_display import month_context

    try:
        display = month_context(request)
    except ValidationError as error:
        return HttpResponse(" ".join(error.messages), status=400)
    try:
        heartbeat = json.loads(data_path("playout/scheduler-status.json").read_text())
        scheduler_ok = (
            time.time() - heartbeat["checked"] < 15 and heartbeat["connected"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        scheduler_ok = False
    return render(
        request,
        "calendar.html",
        {
            **display,
            "series": BroadcastSeries.objects.filter(active=True).select_related(
                "asset"
            ),
            "scheduler_ok": scheduler_ok,
        },
    )


def calendar_redirect(moment):
    from .recurrence import ROME

    local = moment.astimezone(ROME)
    return redirect(
        reverse("library:calendar") + local.strftime("?month=%Y-%m&day=%Y-%m-%d")
    )


@admin_required
def broadcast_create(request):

    initial = {}
    if request.method == "GET" and request.GET.get("asset"):
        try:
            asset_id = UUID(request.GET["asset"])
        except ValueError:
            raise Http404("Podcast non valido") from None
        asset = get_object_or_404(
            Asset, pk=asset_id, category=Asset.Category.PODCAST, trashed_at__isnull=True
        )
        initial["asset"] = asset
    if request.method == "GET" and request.GET.get("date"):
        from datetime import datetime, time as day_time
        from django.utils.dateparse import parse_date
        from .recurrence import ROME

        try:
            day = parse_date(request.GET["date"])
            if day is None:
                raise ValueError
        except ValueError:
            return HttpResponse("Data non valida.", status=400)
        now = timezone.now().astimezone(ROME)
        if day >= now.date():
            initial["scheduled_at"] = (
                datetime.combine(day, day_time(18), tzinfo=ROME)
                if day > now.date()
                else (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
            )
    form = BroadcastForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        # Serialize creation through the station row and revalidate overlaps under lock.
        with transaction.atomic():
            RadioSettings.objects.get_or_create(pk=1)
            RadioSettings.objects.select_for_update().get(pk=1)
            form = BroadcastForm(request.POST)
            if form.is_valid():
                save_programming(form)
                messages.success(request, "Programmazione salvata.")
                return calendar_redirect(form.cleaned_data["scheduled_at"])
    return render(request, "broadcast_edit.html", {"form": form})


@admin_required
def broadcast_edit(request, broadcast_id):
    job = get_object_or_404(Broadcast.objects.select_related("asset"), pk=broadcast_id)
    if job.status != Broadcast.Status.SCHEDULED:
        messages.error(
            request, "Puoi modificare soltanto un appuntamento ancora programmato."
        )
        return redirect("library:calendar")
    form = BroadcastForm(request.POST or None, instance=job)
    if request.method == "POST":
        with transaction.atomic():
            RadioSettings.objects.get_or_create(pk=1)
            RadioSettings.objects.select_for_update().get(pk=1)
            current = Broadcast.objects.select_for_update().get(pk=broadcast_id)
            if current.status != Broadcast.Status.SCHEDULED:
                messages.error(
                    request,
                    "La trasmissione è già in avvio o conclusa: l'orario non è stato modificato.",
                )
                return redirect("library:calendar")
            form = BroadcastForm(request.POST, instance=current)
            if form.is_valid():
                save_programming(form)
                messages.success(request, "Appuntamento aggiornato.")
                return calendar_redirect(form.cleaned_data["scheduled_at"])
    return render(request, "broadcast_edit.html", {"form": form, "editing": True})


@admin_required
@require_POST
def broadcast_cancel(request, broadcast_id):
    job = get_object_or_404(Broadcast, pk=broadcast_id)
    with transaction.atomic():
        station_lock()
        changed = Broadcast.objects.filter(
            pk=broadcast_id, status=Broadcast.Status.SCHEDULED
        ).update(
            status=Broadcast.Status.CANCELLED,
            ended_at=timezone.now(),
            outcome="Annullato dal pannello.",
            is_exception=True,
            updated_at=timezone.now(),
        )
    if changed:
        messages.success(request, "Programmazione annullata.")
    else:
        messages.error(
            request, "Puoi annullare soltanto un podcast ancora programmato."
        )
    return calendar_redirect(job.scheduled_at)


@admin_required
@require_POST
def assign_playlists(request):
    form = BulkPlaylistForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Selezione non valida: scegli brani musicali e playlist disponibili.",
        )
        return redirect("library:index")
    with transaction.atomic():
        assets = list(
            Asset.objects.select_for_update().filter(
                pk__in=form.cleaned_data["assets"],
                category=Asset.Category.MUSIC,
                trashed_at__isnull=True,
            )
        )
        for asset in assets:
            memberships = asset.playlists
            operation = form.cleaned_data["operation"]
            if operation == "replace":
                memberships.set(form.cleaned_data["playlists"])
            elif operation == "remove":
                memberships.remove(*form.cleaned_data["playlists"])
            else:
                memberships.add(*form.cleaned_data["playlists"])
    safe_publish()
    messages.success(request, f"Playlist aggiornate per {len(assets)} brani.")
    return redirect("library:index")


@admin_required
def remove_files(request, asset_id=None):
    from django.core import signing

    from . import engine
    from .playout import publish_snapshot
    from .removal import RemovalBlocked, begin_removal

    candidates = Asset.objects.filter(
        trashed_at__isnull=False, purged_at__isnull=True
    ).exclude(category=Asset.Category.JINGLE)
    if asset_id:
        candidates = candidates.filter(pk=asset_id)
        if not candidates.exists():
            raise Http404
    if request.method == "POST":
        form = RemovalForm(request.POST)
        if form.is_valid():
            try:
                selection = signing.loads(
                    form.cleaned_data["selection"], salt="trash-files", max_age=600
                )
                ids = selection["ids"]
                if selection["scope"] != (
                    str(asset_id) if asset_id else "all"
                ) or not isinstance(ids, list):
                    raise signing.BadSignature
            except (signing.BadSignature, KeyError, TypeError):
                messages.error(
                    request,
                    "Conferma scaduta o non valida: riapri la schermata di eliminazione.",
                )
                return redirect("library:trash")
            selected = list(candidates.filter(pk__in=ids))
            try:
                publish_snapshot()
                usage = engine.media_in_use()
            except engine.EngineError:
                messages.error(
                    request,
                    "Impossibile verificare i file in uso nella regia. Nessun file è stato eliminato: riprova quando sarà raggiungibile.",
                )
                return redirect("library:trash")
            completed, pending, excluded = 0, 0, []
            for asset in selected:
                try:
                    if begin_removal(
                        asset.id, form.cleaned_data["delete_original"], usage
                    ):
                        completed += 1
                    else:
                        pending += 1
                except RemovalBlocked as error:
                    excluded.append(f"{asset.title}: {error}")
            if completed:
                messages.success(
                    request,
                    f"Eliminazione completata per {completed} contenuti. "
                    + (
                        "Anche gli originali sono stati eliminati."
                        if form.cleaned_data["delete_original"]
                        else "Originali conservati nel Cestino."
                    ),
                )
            if pending:
                messages.info(
                    request,
                    f"{pending} eliminazioni in attesa di completamento; il servizio riproverà automaticamente.",
                )
            for reason in excluded[:10]:
                messages.warning(request, reason)
            if len(excluded) > 10:
                messages.warning(
                    request,
                    f"Altri {len(excluded) - 10} contenuti esclusi perché occupati o già elaborati.",
                )
            if not selected:
                messages.info(
                    request, "Nessuno degli elementi confermati è ancora nel Cestino."
                )
            safe_publish()
            return redirect("library:trash")
        # Re-render only the signed selection the user actually reviewed.
        try:
            selection = signing.loads(
                request.POST.get("selection", ""), salt="trash-files", max_age=600
            )
            if selection["scope"] != (str(asset_id) if asset_id else "all"):
                raise signing.BadSignature
            candidates = candidates.filter(pk__in=selection["ids"])
        except (signing.BadSignature, KeyError, TypeError):
            return redirect("library:trash")
    else:
        ids = [str(value) for value in candidates.values_list("id", flat=True)]
        candidates = candidates.filter(pk__in=ids)
        form = RemovalForm(
            initial={
                "selection": signing.dumps(
                    {"ids": ids, "scope": str(asset_id) if asset_id else "all"},
                    salt="trash-files",
                )
            }
        )
    return render(
        request,
        "library/remove_files.html",
        {"form": form, "assets": candidates, "emptying": asset_id is None},
    )


@admin_required
@require_POST
def row_playlists(request, asset_id):
    from .forms import RowPlaylistForm

    form = RowPlaylistForm(request.POST)
    ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not form.is_valid():
        if ajax:
            return JsonResponse(
                {
                    "error": "Playlist non più disponibile. Ricarica la pagina e riprova."
                },
                status=400,
            )
        messages.error(
            request, "Playlist non disponibile: ricarica la Libreria e riprova."
        )
        return redirect("library:index")
    with transaction.atomic():
        asset = get_object_or_404(
            Asset.objects.select_for_update(),
            pk=asset_id,
            category=Asset.Category.MUSIC,
            trashed_at__isnull=True,
            purged_at__isnull=True,
        )
        asset.playlists.set(form.cleaned_data["playlists"])
        memberships = list(asset.playlists.values("id", "name"))
    safe_publish()
    if ajax:
        return JsonResponse({"playlists": memberships})
    messages.success(request, f"Playlist salvate per {asset.title}.")
    # Keep filtering/pagination for the non-JavaScript form, without accepting redirects.
    from django.http import QueryDict

    query = QueryDict(request.POST.get("library_query", ""))
    allowed = QueryDict(mutable=True)
    for key in ["category", "status", "q", "playlist", "page"]:
        if key in query:
            allowed[key] = query[key][:200]
    target = reverse("library:index")
    return redirect(target + ("?" + allowed.urlencode() if allowed else ""))


@admin_required
def series_edit(request, series_id):
    series = get_object_or_404(BroadcastSeries, pk=series_id, active=True)
    # Anchor the change in the future, while retaining the rule's wall-clock time.
    from datetime import datetime
    from .recurrence import ROME

    local = series.starts_at.astimezone(ROME)
    tomorrow = timezone.now().astimezone(ROME).date() + timedelta(days=1)
    start = max(local, datetime.combine(tomorrow, local.time(), tzinfo=ROME))
    initial = {
        "repeat": "weekly",
        "weekdays": [str(day) for day in series.weekdays],
        "until": series.until,
    }
    instance = Broadcast(
        asset=series.asset, scheduled_at=start, is_public=series.is_public
    )
    form = BroadcastForm(
        request.POST or None, instance=instance, initial=initial, series=series
    )
    if request.method == "POST":
        with transaction.atomic():
            station_lock()
            series = get_object_or_404(
                BroadcastSeries.objects.select_for_update(), pk=series_id, active=True
            )
            list(series.broadcasts.select_for_update().values_list("id", flat=True))
            form = BroadcastForm(request.POST, instance=instance, series=series)
            if form.is_valid():
                save_programming(form, series)
                messages.success(
                    request,
                    "Serie aggiornata. Puntate già avviate e modifiche singole conservate.",
                )
                return redirect("library:calendar")
    return render(
        request, "broadcast_edit.html", {"form": form, "editing_series": True}
    )


@admin_required
@require_POST
def series_cancel(request, series_id):
    with transaction.atomic():
        station_lock()
        series = get_object_or_404(
            BroadcastSeries.objects.select_for_update(), pk=series_id
        )
        series.active = False
        series.save(update_fields=["active", "updated_at"])
        series.broadcasts.filter(status="scheduled").update(
            status="cancelled",
            ended_at=timezone.now(),
            updated_at=timezone.now(),
            outcome="Serie annullata dal pannello.",
        )
    messages.success(
        request,
        "Serie annullata. Le trasmissioni già avviate mantengono il proprio esito.",
    )
    return redirect("library:calendar")


@admin_required
@require_GET
def broadcast_detail(request, broadcast_id):
    job = get_object_or_404(
        Broadcast.objects.select_related("asset", "series"), pk=broadcast_id
    )
    from .recurrence import ROME

    return render(
        request,
        "broadcast_detail.html",
        {"job": job, "month_key": job.scheduled_at.astimezone(ROME).strftime("%Y-%m")},
    )
