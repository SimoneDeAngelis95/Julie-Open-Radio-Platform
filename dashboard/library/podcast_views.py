"""Staff management is separate from anonymous archive access."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .forms import PodcastPublicationForm, ReplayAvailabilityForm
from .models import Asset
from .publication import publicly_available, publish_if_ready, replay_exists
from .views import admin_required


def candidates():
    return Asset.objects.filter(
        category=Asset.Category.PODCAST, trashed_at__isnull=True, purged_at__isnull=True
    )


def decorate(request, asset):
    asset.public_available = publicly_available(asset)
    asset.needs_replay_retry = asset.replay_status == Asset.ReplayStatus.ERROR or (
        asset.replay_status == Asset.ReplayStatus.READY and not replay_exists(asset)
    )
    asset.public_url = (
        request.build_absolute_uri(reverse("library:public_audio", args=[asset.id]))
        if asset.public_available
        else ""
    )
    if asset.public_available:
        asset.publication_label = "Disponibile"
    elif not asset.auto_publish:
        asset.publication_label = "Non disponibile"
    elif asset.replay_after_broadcast:
        asset.publication_label = "Disponibile dopo la prima messa in onda completata"
    elif asset.status != Asset.Status.READY:
        asset.publication_label = "In attesa della preparazione audio"
    elif asset.needs_replay_retry:
        asset.publication_label = "Errore nella preparazione MP3"
    else:
        asset.publication_label = "MP3 in preparazione"
    return asset


@admin_required
@require_GET
def index(request):
    queryset = candidates()
    query = request.GET.get("q", "").strip()[:200]
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query) | Q(artist__icontains=query)
        )
    items = [decorate(request, asset) for asset in queryset]
    visibility = request.GET.get("visibility", "")
    if visibility in ["public", "private"]:
        items = [
            asset
            for asset in items
            if (asset.auto_publish and not asset.replay_after_broadcast)
            == (visibility == "public")
        ]
    return render(
        request,
        "podcasts/index.html",
        {
            "page": Paginator(items, 30).get_page(request.GET.get("page")),
            "q": query,
            "visibility": visibility,
        },
    )


@admin_required
def edit(request, asset_id):
    asset = get_object_or_404(candidates(), pk=asset_id)
    form = PodcastPublicationForm(request.POST or None, instance=asset)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = get_object_or_404(candidates().select_for_update(), pk=asset_id)
            for field in form.fields:
                setattr(current, field, form.cleaned_data[field])
            current.metadata_edited = True
            if not current.auto_publish:
                current.published_at = None
            elif current.replay_status == Asset.ReplayStatus.READY:
                if not replay_exists(current):
                    current.replay_status = Asset.ReplayStatus.PENDING
                    current.replay_error = ""
            current.save(
                update_fields=[
                    *form.fields,
                    "metadata_edited",
                    "published_at",
                    "replay_status",
                    "replay_error",
                    "updated_at",
                ]
            )
            publish_if_ready(current)
        messages.success(
            request,
            "Pubblicazione aggiornata. La programmazione radio resta indipendente.",
        )
        return redirect("library:podcast_edit", asset_id=asset_id)
    return render(
        request, "podcasts/edit.html", {"asset": decorate(request, asset), "form": form}
    )


@admin_required
@require_POST
def make_private(request, asset_id):
    with transaction.atomic():
        asset = get_object_or_404(candidates().select_for_update(), pk=asset_id)
        asset.auto_publish = False
        asset.replay_after_broadcast = False
        asset.published_at = None
        asset.save(
            update_fields=[
                "auto_publish",
                "replay_after_broadcast",
                "published_at",
                "updated_at",
            ]
        )
    messages.success(
        request,
        "Podcast reso privato: API e link MP3 non sono più accessibili per nuove richieste.",
    )
    return redirect("library:podcasts")


@admin_required
@require_POST
def retry(request, asset_id):
    with transaction.atomic():
        asset = get_object_or_404(candidates().select_for_update(), pk=asset_id)
        if asset.replay_status == Asset.ReplayStatus.ERROR or (
            asset.replay_status == Asset.ReplayStatus.READY and not replay_exists(asset)
        ):
            asset.replay_status = Asset.ReplayStatus.PENDING
            asset.replay_error = ""
            asset.published_at = None
            asset.save(
                update_fields=[
                    "replay_status",
                    "replay_error",
                    "published_at",
                    "updated_at",
                ]
            )
            messages.success(request, "Preparazione MP3 rimessa in coda.")
    return redirect("library:podcast_edit", asset_id=asset_id)


@admin_required
@require_POST
def availability(request, asset_id):
    form = ReplayAvailabilityForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("Scegli Disponibile oppure Non disponibile.")
    with transaction.atomic():
        asset = get_object_or_404(candidates().select_for_update(), pk=asset_id)
        asset.auto_publish = form.cleaned_data["availability"] == "available"
        asset.replay_after_broadcast = False
        if not asset.auto_publish:
            asset.published_at = None
        elif asset.replay_status == Asset.ReplayStatus.READY and not replay_exists(
            asset
        ):
            asset.replay_status = Asset.ReplayStatus.PENDING
            asset.replay_error = ""
        asset.save(
            update_fields=[
                "auto_publish",
                "replay_after_broadcast",
                "published_at",
                "replay_status",
                "replay_error",
                "updated_at",
            ]
        )
        publish_if_ready(asset)
    messages.success(request, "Disponibilità nel Replay aggiornata.")
    return redirect("library:podcasts")
