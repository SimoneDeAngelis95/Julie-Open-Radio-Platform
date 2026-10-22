"""Prepare new versions without withdrawing audio that is already available."""

import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from . import audio, publication
from .models import Asset, AudioReprocessItem, AudioReprocessJob
from .playout import safe_publish
from .storage import data_path
from .worker import lock_file

logger = logging.getLogger(__name__)


def finish_job(job_id):
    if not AudioReprocessItem.objects.filter(
        job_id=job_id, status__in=["pending", "working"]
    ).exists():
        AudioReprocessJob.objects.filter(pk=job_id, status="active").update(
            status="completed", finished_at=timezone.now()
        )


def recover():
    # A live preparation holds the same per-asset lock as ordinary uploads/removal.
    for item in AudioReprocessItem.objects.filter(status="working").iterator():
        if item.asset_id is None:
            AudioReprocessItem.objects.filter(pk=item.pk, status="working").update(
                status="skipped",
                finished_at=timezone.now(),
                error="Contenuto eliminato.",
            )
            continue
        handle = lock_file(item.asset_id)
        if handle is not None:
            try:
                AudioReprocessItem.objects.filter(pk=item.pk, status="working").update(
                    status="pending"
                )
            finally:
                handle.close()
    for job_id in AudioReprocessJob.objects.filter(status="active").values_list(
        "pk", flat=True
    ):
        finish_job(job_id)


def claim():
    with transaction.atomic():
        item = (
            AudioReprocessItem.objects.select_for_update(skip_locked=True)
            .filter(job__status="active", status="pending")
            .order_by("id")
            .first()
        )
        if item is None:
            return None
        if item.asset_id is None:
            item.status = "skipped"
            item.error = "Contenuto eliminato."
            item.finished_at = timezone.now()
            item.save()
            return item, None
        handle = lock_file(item.asset_id)
        if handle is None:
            return None
        try:
            item.status = "working"
            item.save(update_fields=["status"])
        except Exception:
            handle.close()
            raise
        return item, handle


def run_one():
    claimed = claim()
    if claimed is None:
        return False
    item, handle = claimed
    if handle is None:
        finish_job(item.job_id)
        return True
    published_paths = []
    try:
        asset = Asset.objects.get(pk=item.asset_id)
        if (
            asset.trashed_at
            or asset.purged_at
            or asset.removal_pending
            or asset.status != Asset.Status.READY
        ):
            AudioReprocessItem.objects.filter(pk=item.pk).update(
                status="skipped",
                error="Contenuto non più disponibile per la rielaborazione.",
                finished_at=timezone.now(),
            )
            return True
        directory = data_path("work")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            workspace = Path(temporary)
            job = item.job
            result = audio.prepare(
                data_path(asset.original_path),
                workspace,
                job.target_lufs,
                job.ceiling_dbtp,
            )
            # Prepare a matching Replay first, but keep the existing public MP3 online.
            replay = None
            if (
                asset.category == Asset.Category.PODCAST
                and asset.replay_status == Asset.ReplayStatus.READY
                and asset.replay_path
            ):
                replay, duration, replay_deadline = publication.encode_mp3(
                    result["path"], workspace
                )
                if abs(duration - result["duration"]) > 0.25:
                    raise audio.AudioError(
                        "La durata della copia MP3 non corrisponde al podcast."
                    )
            with transaction.atomic():
                current = Asset.objects.select_for_update().get(pk=asset.pk)
                if (
                    current.trashed_at
                    or current.purged_at
                    or current.removal_pending
                    or current.status != Asset.Status.READY
                ):
                    AudioReprocessItem.objects.filter(pk=item.pk).update(
                        status="skipped",
                        error="Contenuto modificato durante la preparazione.",
                        finished_at=timezone.now(),
                    )
                    return True
                # Concurrent editorial edits win. Versioned paths leave all old readers valid.
                metadata = {
                    key: getattr(current, key)
                    for key in ("title", "artist", "composer", "album")
                }
                tagged = workspace / "tagged.flac"
                audio.apply_tags(result["path"], tagged, metadata, result["deadline"])
                version = f"{item.job_id.hex}-{item.pk}"
                relative = f"ready/{current.category}/{current.id}-{version}.flac"
                destination = data_path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tagged, destination)
                published_paths.append(relative)
                previous = [current.prepared_path] if current.prepared_path else []
                current.prepared_path = relative
                # Preserve the duration used by existing calendar slots, rejecting meaningful drift.
                if (
                    current.duration_seconds
                    and abs(current.duration_seconds - result["duration"]) > 0.25
                ):
                    raise audio.AudioError(
                        "La durata non corrisponde alla copia già programmabile. La versione precedente è conservata."
                    )
                current.duration_seconds = result["duration"]
                current.loudness_lufs = result["after"]["input_i"]
                current.true_peak_dbtp = result["after"]["input_tp"]
                current.peak_constrained = result["peak_constrained"]
                if (
                    replay is not None
                    and current.replay_status == Asset.ReplayStatus.READY
                ):
                    tagged_mp3 = workspace / "tagged-replay.mp3"
                    audio.apply_tags(replay, tagged_mp3, metadata, replay_deadline)
                    replay_relative = f"ready/replay/{current.id}-{version}.mp3"
                    replay_destination = data_path(replay_relative)
                    replay_destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tagged_mp3, replay_destination)
                    published_paths.append(replay_relative)
                    previous.append(current.replay_path)
                    current.replay_path = replay_relative
                current.save(
                    update_fields=[
                        "prepared_path",
                        "duration_seconds",
                        "loudness_lufs",
                        "true_peak_dbtp",
                        "peak_constrained",
                        "replay_path",
                        "updated_at",
                    ]
                )
                AudioReprocessItem.objects.filter(pk=item.pk).update(
                    status="done",
                    previous_paths=previous,
                    finished_at=timezone.now(),
                    error="",
                )
        safe_publish()
    except Exception as error:
        logger.exception(
            "Audio reprocessing failed for item %s; previous audio retained", item.pk
        )
        # Files written before a rolled-back commit have no DB references and can be removed.
        for relative in published_paths:
            if (
                not Asset.objects.filter(prepared_path=relative).exists()
                and not Asset.objects.filter(replay_path=relative).exists()
            ):
                data_path(relative).unlink(missing_ok=True)
        message = (
            str(error)
            if isinstance(error, audio.AudioError)
            else "Rielaborazione non riuscita. La versione precedente è conservata."
        )
        AudioReprocessItem.objects.filter(pk=item.pk).exclude(status="done").update(
            status="error", error=message[:250], finished_at=timezone.now()
        )
    finally:
        handle.close()
        finish_job(item.job_id)
    return True


def cleanup_previous():
    """Retain old versions at least a day, and fail closed when engine usage is unknown."""
    from django.conf import settings
    from django.db.models import Q

    from . import engine
    from .removal import RemovalBlocked, removable_path

    candidates = AudioReprocessItem.objects.filter(
        status="done", cleaned=False, finished_at__lt=timezone.now() - timedelta(days=1)
    ).order_by("id")[:10]
    if not candidates:
        return
    try:
        identifiers, filenames = engine.media_in_use()
    except engine.EngineError:
        return
    for item in candidates:
        if str(item.asset_id) in identifiers:
            continue
        if any(
            f"{settings.PLAYOUT_MEDIA_ROOT}/{path}" in filenames
            for path in item.previous_paths
        ):
            continue
        try:
            for relative in item.previous_paths:
                if not relative.startswith("ready/"):
                    raise ValueError("Unexpected previous audio path")
                if Asset.objects.filter(
                    Q(prepared_path=relative)
                    | Q(replay_path=relative)
                    | Q(original_path=relative)
                ).exists():
                    continue
                removable_path(relative).unlink(missing_ok=True)
            AudioReprocessItem.objects.filter(pk=item.pk).update(cleaned=True)
        except (OSError, ValueError, RemovalBlocked):
            logger.exception("Previous audio cleanup deferred for item %s", item.pk)
