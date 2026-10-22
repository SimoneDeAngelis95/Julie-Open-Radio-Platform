"""Shared validation for uploads and calendar forms, including unknown durations."""

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Asset, Broadcast
from .playout import eligible

OPEN_STATUSES = [
    Broadcast.Status.SCHEDULED,
    Broadcast.Status.DISPATCHING,
    Broadcast.Status.PLAYING,
]


def validate_time(start):
    if start <= timezone.now():
        raise ValidationError("Scegli un orario futuro.")


def validate_asset(asset):
    if (
        asset.category != Asset.Category.PODCAST
        or asset.trashed_at
        or not asset.is_active
    ):
        raise ValidationError(
            "Il podcast non è disponibile. Controlla la Libreria e il Cestino."
        )
    if asset.status == Asset.Status.ERROR:
        raise ValidationError(
            "La preparazione è fallita. Riprovala dalla Libreria prima di programmare il podcast."
        )
    if asset.status == Asset.Status.READY and (
        not eligible(asset) or not asset.duration_seconds
    ):
        raise ValidationError(
            "Il podcast non è disponibile o non ha una durata valida."
        )


def validate_slot(start, duration=None, exclude=None):
    """Unknown durations reserve the start; runtime priority still prevents overlap."""
    validate_time(start)
    from .recurrence import validate_intervals

    # Reuse the same validation for one-off appointments and weekly rules.
    class Slot:
        duration_seconds = duration

    validate_intervals(
        Slot(), [(None, start)], exclude_ids=[exclude] if exclude else []
    )
