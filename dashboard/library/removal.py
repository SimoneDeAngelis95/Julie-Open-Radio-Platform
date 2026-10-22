"""Recoverable deletion intent, idempotent unlinking and conservative usage checks."""

import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import engine
from .models import Asset, Broadcast, RemovalJob
from .storage import data_path
from .worker import lock_file

logger = logging.getLogger(__name__)


class RemovalBlocked(Exception):
    pass


def removable_path(relative):
    """Only exact regular-file paths: never follow symlinks or recursively remove dirs."""
    raw = settings.MEDIA_ROOT.resolve() / relative
    resolved = data_path(relative)
    if (
        not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or raw != resolved
    ):
        raise RemovalBlocked("Percorso audio non valido; nessun file cancellato.")
    if resolved.exists() and not resolved.is_file():
        raise RemovalBlocked("Il percorso non è un file audio regolare.")
    return resolved


def check_engine(asset, usage):
    identifiers, filenames = usage
    paths = {
        f"{settings.PLAYOUT_MEDIA_ROOT}/{path}"
        for path in [asset.original_path, asset.prepared_path, asset.replay_path]
        if path
    }
    if str(asset.id) in identifiers or paths.intersection(filenames):
        raise RemovalBlocked(
            "Audio ancora in uso nella regia: attendi la fine della riproduzione."
        )


def begin_removal(asset_id, delete_original, usage):
    """Commit the exact path list before touching files; only trashed assets qualify."""
    with transaction.atomic():
        asset = Asset.objects.select_for_update().get(pk=asset_id)
        if (
            not asset.trashed_at
            or asset.purged_at
            or asset.category == Asset.Category.JINGLE
        ):
            raise RemovalBlocked("Il contenuto non è più nel Cestino.")
        if asset.removal_pending:
            raise RemovalBlocked(
                "Eliminazione già in corso; il servizio riproverà automaticamente."
            )
        if asset.status == Asset.Status.PROCESSING:
            raise RemovalBlocked("Audio in elaborazione: attendi il completamento.")
        if timezone.now() - asset.trashed_at < timedelta(seconds=3):
            raise RemovalBlocked(
                "Attendi pochi secondi perché la regia recepisca il Cestino."
            )
        check_engine(asset, usage)
        handle = lock_file(asset.id)
        if handle is None:
            raise RemovalBlocked("Audio in elaborazione: attendi il completamento.")
        try:
            appointments = list(
                Broadcast.objects.select_for_update().filter(asset=asset)
            )
            if any(
                job.status in [Broadcast.Status.DISPATCHING, Broadcast.Status.PLAYING]
                for job in appointments
            ):
                raise RemovalBlocked("Podcast in avvio o in onda: attendi la fine.")
            paths = {
                path
                for path in [asset.prepared_path, asset.replay_path]
                if path and (delete_original or path != asset.original_path)
            }
            if delete_original and asset.original_path:
                paths.add(asset.original_path)
            for path in paths:
                removable_path(path)
            if (
                paths
                and Asset.objects.exclude(pk=asset.id)
                .filter(
                    Q(original_path__in=paths)
                    | Q(prepared_path__in=paths)
                    | Q(replay_path__in=paths)
                )
                .exists()
            ):
                raise RemovalBlocked(
                    "Un file è condiviso con un altro contenuto: eliminazione esclusa."
                )
            if not delete_original and asset.copies_removed:
                raise RemovalBlocked(
                    "Le copie elaborate sono già state eliminate; resta soltanto l’originale."
                )
            RemovalJob.objects.update_or_create(
                asset=asset,
                defaults={
                    "paths": sorted(paths),
                    "delete_original": delete_original,
                    "completed_at": None,
                    "last_error": "",
                },
            )
            asset.auto_publish = False
            asset.published_at = None
            asset.removal_pending = True
            asset.save(update_fields=["removal_pending", "auto_publish", "published_at", "updated_at"])
            Broadcast.objects.filter(
                asset=asset, status=Broadcast.Status.SCHEDULED
            ).update(
                status=Broadcast.Status.CANCELLED,
                ended_at=timezone.now(),
                outcome="Annullato per eliminazione dei file audio.",
            )
        finally:
            handle.close()
    return process_removal(asset_id)


def process_removal(asset_id):
    """Failures keep their durable intent and block restore until the retry succeeds."""
    handle = lock_file(asset_id)
    if handle is None:
        return False
    try:
        job = RemovalJob.objects.select_related("asset").get(asset_id=asset_id)
        if job.completed_at:
            return True
        check_engine(job.asset, engine.media_in_use())
        for relative in job.paths:
            removable_path(relative).unlink(missing_ok=True)
        with transaction.atomic():
            asset = Asset.objects.select_for_update().get(pk=asset_id)
            asset.prepared_path = ""
            asset.replay_path = ""
            asset.replay_status = Asset.ReplayStatus.PENDING
            asset.replay_error = ""
            asset.status = Asset.Status.PENDING
            asset.removal_pending = False
            asset.copies_removed = True
            if job.delete_original:
                asset.original_path = ""
                asset.purged_at = timezone.now()
                asset.playlists.clear()
            asset.save(
                update_fields=[
                    "prepared_path",
                    "replay_path",
                    "replay_status",
                    "replay_error",
                    "status",
                    "removal_pending",
                    "copies_removed",
                    "original_path",
                    "purged_at",
                    "updated_at",
                ]
            )
            RemovalJob.objects.filter(pk=job.pk).update(
                completed_at=timezone.now(), last_error=""
            )
        return True
    except (OSError, ValueError, engine.EngineError, RemovalBlocked):
        logger.exception("File removal will retry for %s", asset_id)
        RemovalJob.objects.filter(asset_id=asset_id).update(
            last_error="Eliminazione non conclusa. Il servizio riproverà automaticamente; ripristino temporaneamente sospeso."
        )
        return False
    finally:
        handle.close()


def recover_removals():
    # Bounded work per pass, independent intents survive process or server restarts.
    for asset_id in RemovalJob.objects.filter(completed_at__isnull=True).values_list(
        "asset_id", flat=True
    )[:10]:
        process_removal(asset_id)
