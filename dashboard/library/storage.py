"""Keep untrusted names out of filesystem paths and publish uploads atomically."""

import hashlib
import os
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction

from .models import Asset


def data_path(relative):
    root = settings.MEDIA_ROOT.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Percorso fuori dalla libreria.")
    return path


def save_upload(
    upload, category, *, replay_after_broadcast=False, title="", artist="", description=""
):
    staging = data_path("staging")
    staging.mkdir(parents=True, exist_ok=True)
    asset_id = uuid.uuid4()
    digest = hashlib.sha256()
    descriptor, temporary = tempfile.mkstemp(dir=staging, suffix=".part")
    temp_path = Path(temporary)
    original = None
    try:
        size = 0
        with os.fdopen(descriptor, "wb") as output:
            for chunk in upload.chunks():
                size += len(chunk)
                if size > settings.MAX_AUDIO_BYTES:
                    raise ValueError("File troppo grande.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        checksum = digest.hexdigest()
        existing = Asset.objects.filter(
            checksum=checksum, category=category, purged_at__isnull=True
        ).first()
        if existing:
            return existing, False
        suffix = Path(upload.name).suffix.lower()
        relative = f"originals/{asset_id}{suffix}"
        original = data_path(relative)
        original.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, original)
        try:
            with transaction.atomic():
                asset = Asset.objects.create(
                    id=asset_id,
                    category=category,
                    replay_after_broadcast=replay_after_broadcast,
                    title=title or Path(upload.name).stem.replace("_", " ")[:240],
                    title_edited=bool(title),
                    artist=artist if category == Asset.Category.MUSIC else "",
                    artist_edited=bool(artist) if category == Asset.Category.MUSIC else False,
                    description=description
                    if category == Asset.Category.PODCAST
                    else "",
                    original_name=Path(upload.name).name[:255],
                    original_path=relative,
                    checksum=checksum,
                    size_bytes=size,
                )
        except IntegrityError:
            existing = Asset.objects.filter(
                checksum=checksum, category=category, purged_at__isnull=True
            ).first()
            if not existing:
                raise
            original.unlink(missing_ok=True)
            return existing, False
        return asset, True
    except Exception:
        if original:
            original.unlink(missing_ok=True)
        raise
    finally:
        temp_path.unlink(missing_ok=True)
