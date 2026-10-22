"""Replay availability depends on the editorial choice and a verified MP3."""

import logging
import os
import tempfile
import time
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from . import audio
from .models import Asset
from .storage import data_path
from .worker import lock_file

logger = logging.getLogger(__name__)


def public_candidates():
    return Asset.objects.filter(
        category=Asset.Category.PODCAST,
        status=Asset.Status.READY,
        auto_publish=True,
        replay_after_broadcast=False,
        published_at__isnull=False,
        replay_status=Asset.ReplayStatus.READY,
        trashed_at__isnull=True,
        purged_at__isnull=True,
        removal_pending=False,
    ).exclude(replay_path="")


def replay_exists(asset):
    try:
        return bool(asset.replay_path) and data_path(asset.replay_path).is_file()
    except (OSError, ValueError):
        return False


def publicly_available(asset):
    return bool(
        asset.category == Asset.Category.PODCAST
        and asset.status == Asset.Status.READY
        and asset.auto_publish
        and not asset.replay_after_broadcast
        and asset.published_at
        and asset.replay_status == Asset.ReplayStatus.READY
        and not asset.trashed_at
        and not asset.purged_at
        and not asset.removal_pending
        and replay_exists(asset)
    )


def publish_if_ready(asset):
    """Caller holds the asset row lock. Publication is reversible independently of radio."""
    if (
        asset.auto_publish
        and not asset.replay_after_broadcast
        and not asset.published_at
        and asset.category == Asset.Category.PODCAST
        and asset.status == Asset.Status.READY
        and not asset.trashed_at
        and not asset.purged_at
        and not asset.removal_pending
        and asset.replay_status == Asset.ReplayStatus.READY
        and replay_exists(asset)
    ):
        asset.published_at = timezone.now()
        asset.save(update_fields=["published_at", "updated_at"])


def note_completion(asset, started_at, ended_at):
    if asset.category != Asset.Category.PODCAST:
        return
    if asset.first_completed_at is None or ended_at < asset.first_completed_at:
        asset.first_completed_at = ended_at
        asset.first_broadcast_at = started_at
        asset.save(
            update_fields=["first_completed_at", "first_broadcast_at", "updated_at"]
        )
    if asset.auto_publish and asset.replay_after_broadcast:
        asset.replay_after_broadcast = False
        asset.save(update_fields=["replay_after_broadcast", "updated_at"])
    publish_if_ready(asset)


def recovery():
    for identifier in Asset.objects.filter(
        replay_status=Asset.ReplayStatus.PROCESSING
    ).values_list("id", flat=True):
        with transaction.atomic():
            asset = Asset.objects.select_for_update().get(pk=identifier)
            handle = lock_file(identifier)
            if handle is None:
                continue
            try:
                if asset.replay_status == Asset.ReplayStatus.PROCESSING:
                    asset.replay_status = Asset.ReplayStatus.PENDING
                    asset.save(update_fields=["replay_status", "updated_at"])
            finally:
                handle.close()


def claim():
    with transaction.atomic():
        asset = (
            Asset.objects.select_for_update(skip_locked=True)
            .filter(
                category=Asset.Category.PODCAST,
                status=Asset.Status.READY,
                auto_publish=True,
                replay_after_broadcast=False,
                trashed_at__isnull=True,
                purged_at__isnull=True,
                removal_pending=False,
                replay_status=Asset.ReplayStatus.PENDING,
            )
            .order_by("created_at", "id")
            .first()
        )
        if asset is None:
            return None
        handle = lock_file(asset.id)
        if handle is None:
            return None
        try:
            asset.replay_status = Asset.ReplayStatus.PROCESSING
            asset.replay_error = ""
            asset.save(update_fields=["replay_status", "replay_error", "updated_at"])
        except Exception:
            handle.close()
            raise
        return asset.id, handle


def encode_mp3(source, workspace):
    deadline = time.monotonic() + 3600
    destination = workspace / "replay.mp3"
    audio.command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-threads",
            "1",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-map_metadata",
            "-1",
            "-vn",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-threads",
            "1",
            str(destination),
        ],
        deadline,
    )
    info = audio.probe(destination, deadline)
    stream = info["streams"][0]
    if (
        stream.get("codec_name") != "mp3"
        or stream.get("sample_rate") != "44100"
        or stream.get("channels") != 2
    ):
        raise audio.AudioError("La copia MP3 non supera la verifica del formato.")
    decoded = audio.command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(destination),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        deadline,
    )
    if decoded.stderr.strip():
        raise audio.AudioError("La copia MP3 non è completamente decodificabile.")
    return destination, float(info["format"]["duration"]), deadline


def run_one():
    claimed = claim()
    if claimed is None:
        return False
    identifier, handle = claimed
    try:
        asset = Asset.objects.get(pk=identifier)
        directory = data_path("work")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            workspace = Path(temporary)
            mp3, duration, deadline = encode_mp3(
                data_path(asset.prepared_path), workspace
            )
            if (
                asset.duration_seconds is None
                or abs(duration - asset.duration_seconds) > 0.25
            ):
                raise audio.AudioError(
                    "La durata della copia MP3 non corrisponde al podcast."
                )
            with transaction.atomic():
                current = Asset.objects.select_for_update().get(pk=identifier)
                # Private/trashed changes during encoding win; no output is published.
                if (
                    not current.auto_publish
                    or current.replay_after_broadcast
                    or current.trashed_at
                    or current.removal_pending
                    or current.purged_at
                ):
                    current.replay_status = Asset.ReplayStatus.PENDING
                    current.save(update_fields=["replay_status", "updated_at"])
                    return True
                tagged = workspace / "tagged.mp3"
                audio.apply_tags(
                    mp3,
                    tagged,
                    {
                        field: getattr(current, field)
                        for field in ["title", "artist", "album", "composer"]
                    },
                    deadline,
                )
                relative = f"ready/replay/{identifier}.mp3"
                destination = data_path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tagged, destination)
                current.replay_path = relative
                current.replay_status = Asset.ReplayStatus.READY
                current.replay_error = ""
                current.save(
                    update_fields=[
                        "replay_path",
                        "replay_status",
                        "replay_error",
                        "updated_at",
                    ]
                )
                publish_if_ready(current)
    except Exception:
        logger.exception("MP3 preparation failed for %s", identifier)
        Asset.objects.filter(pk=identifier).update(
            replay_status=Asset.ReplayStatus.ERROR,
            replay_error="Preparazione MP3 non riuscita. La radio non è stata modificata; puoi riprovare.",
        )
    finally:
        handle.close()
    return True
