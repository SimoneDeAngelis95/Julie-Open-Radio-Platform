"""Publish an atomic, local snapshot. The radio can keep playing without the web app."""

import fcntl
import json
import logging
import os
import tempfile
from django.conf import settings
from .models import Asset, RadioSettings
from .storage import data_path

logger = logging.getLogger(__name__)


def station_settings():
    return RadioSettings.objects.get_or_create(pk=1)[0]


def annotated_uri(asset):
    """Export only trusted local paths; metadata cannot inject playlist entries."""
    metadata = {
        "title": asset.title,
        "artist": asset.artist,
        "julie_kind": {
            Asset.Category.JINGLE: "jingle",
            Asset.Category.PODCAST: "podcast",
        }.get(asset.category, "music"),
        "julie_id": str(asset.id),
    }
    fields = ",".join(
        key
        + "="
        + json.dumps(value.replace("\n", " ").replace("\r", " "), ensure_ascii=False)
        for key, value in metadata.items()
    )
    return f"annotate:{fields}:{settings.PLAYOUT_MEDIA_ROOT}/{asset.prepared_path}"


def eligible(asset):
    return (
        asset is not None
        and asset.trashed_at is None
        and asset.status == Asset.Status.READY
        and bool(asset.prepared_path)
        and data_path(asset.prepared_path).is_file()
    )


def publish_snapshot():
    directory = data_path("playout")
    directory.mkdir(parents=True, exist_ok=True)
    # Serialize exports before querying, so a late stale snapshot cannot replace a newer one.
    with (directory / "export.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = station_settings()
        jingle = config.current_jingle
        jingle_uri = (
            annotated_uri(jingle)
            if config.jingle_enabled
            and eligible(jingle)
            and jingle.category == Asset.Category.JINGLE
            else ""
        )
        music = [
            annotated_uri(asset)
            for asset in music_candidates(config.selected_playlist).order_by("id")
            if eligible(asset)
        ]
        # Line 1: interval (0 = disabled); line 2: sole jingle; remaining lines: music.
        content = (
            "\n".join(
                [str(config.jingle_every if jingle_uri else 0), jingle_uri, *music]
            )
            + "\n"
        )
        destination = directory / "rotation.txt"
        if destination.exists() and destination.read_text() == content:
            return
        descriptor, name = tempfile.mkstemp(dir=directory, suffix=".part")
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(name, 0o644)
            os.replace(name, destination)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def safe_publish():
    try:
        publish_snapshot()
    except Exception:
        # The durable DB is authoritative; the worker retries export on its next pass.
        logger.exception("Could not export radio snapshot; keeping the previous one")


def music_candidates(playlist=None):
    assets = Asset.objects.filter(
        category=Asset.Category.MUSIC,
        status=Asset.Status.READY,
        is_active=True,
        trashed_at__isnull=True,
    )
    if playlist is not None:
        assets = assets.filter(playlists=playlist)
    return assets
