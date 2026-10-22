"""Persistent queue with short DB transactions and per-asset process locks."""

import fcntl
import logging
import os
import tempfile

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import audio
from .audio_settings import effective
from .models import Asset, RadioSettings
from .storage import data_path

logger = logging.getLogger(__name__)


def lock_file(asset_id):
    path = data_path(f"locks/{asset_id}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return None


def recover_interrupted():
    """A crashed process releases its file lock; a live worker keeps it."""
    for asset_id in Asset.objects.filter(status=Asset.Status.PROCESSING).values_list(
        "id", flat=True
    ):
        with transaction.atomic():
            asset = Asset.objects.select_for_update().get(pk=asset_id)
            if asset.status != Asset.Status.PROCESSING:
                continue
            handle = lock_file(asset.id)
            if handle is None:
                continue
            try:
                asset.status = Asset.Status.PENDING
                asset.error_message = ""
                asset.save(update_fields=["status", "error_message", "updated_at"])
            finally:
                handle.close()


def claim():
    with transaction.atomic():
        asset = (
            Asset.objects.select_for_update(skip_locked=True)
            .filter(status=Asset.Status.PENDING, trashed_at__isnull=True)
            .order_by("created_at")
            .first()
        )
        if asset is None:
            return None
        handle = lock_file(asset.id)
        if handle is None:
            return None
        try:
            asset.status = Asset.Status.PROCESSING
            asset.started_at = timezone.now()
            asset.error_message = ""
            asset.attempts = F("attempts") + 1
            asset.save(
                update_fields=[
                    "status",
                    "started_at",
                    "attempts",
                    "error_message",
                    "updated_at",
                ]
            )
        except Exception:
            handle.close()
            raise
    return asset.id, handle


def run_one():
    claimed = claim()
    if claimed is None:
        return False
    asset_id, handle = claimed
    try:
        asset = Asset.objects.get(pk=asset_id)
        workspace_root = data_path("work")
        workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=workspace_root) as temporary:
            workspace = data_path(temporary)
            target, ceiling = effective()
            result = audio.prepare(
                data_path(asset.original_path),
                workspace,
                target,
                ceiling,
            )
            # Lock only while merging current editorial values and publishing.
            # Metadata edits made during conversion always win over source tags.
            with transaction.atomic():
                asset = Asset.objects.select_for_update().get(pk=asset_id)
                if not asset.metadata_edited:
                    for field in ("title", "artist", "composer", "album"):
                        if (field == "title" and asset.title_edited) or (
                            field == "artist" and asset.artist_edited
                        ):
                            continue
                        value = result["tags"].get(field)
                        if value:
                            setattr(asset, field, value[:240])
                metadata = {
                    field: getattr(asset, field)
                    for field in ("title", "artist", "composer", "album")
                }
                tagged = workspace / "tagged.flac"
                audio.apply_tags(result["path"], tagged, metadata, result["deadline"])
                relative = f"ready/{asset.category}/{asset.id}.flac"
                destination = data_path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tagged, destination)
                asset.prepared_path = relative
                asset.duration_seconds = result["duration"]
                asset.loudness_lufs = result["after"]["input_i"]
                asset.true_peak_dbtp = result["after"]["input_tp"]
                asset.peak_constrained = result["peak_constrained"]
                asset.status = Asset.Status.READY
                asset.finished_at = timezone.now()
                asset.error_message = ""
                asset.save()
                # A newer replacement must never be overwritten by an older job.
                RadioSettings.objects.filter(pk=1, pending_jingle=asset).update(
                    current_jingle=asset, pending_jingle=None
                )
        logger.info("Asset %s ready", asset_id)
    except Exception as error:
        logger.exception("Preparation failed for %s", asset_id)
        message = (
            str(error)
            if isinstance(error, audio.AudioError)
            else "Preparazione non riuscita. Puoi riprovare; l’originale è conservato."
        )
        Asset.objects.filter(pk=asset_id).update(
            status=Asset.Status.ERROR,
            error_message=message,
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
    finally:
        handle.close()
    return True
