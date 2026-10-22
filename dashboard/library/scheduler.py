"""One bounded scheduler pass. Persist intent before touching the audio engine.

An uncertain send is never retried: a lost reply must not air an episode twice.
Liquidsoap keeps an event history; restarting it invalidates unfinished appointments.
"""

import logging
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.utils import timezone

from . import engine
from .models import Asset, Broadcast
from .playout import eligible

logger = logging.getLogger(__name__)
ACTIVE = [Broadcast.Status.DISPATCHING, Broadcast.Status.PLAYING]
GRACE = timedelta(seconds=30)
ACK_TIMEOUT = timedelta(seconds=30)


def finish(job, status, reason, at=None):
    matching = Broadcast.objects.filter(
        pk=job.pk, status__in=[Broadcast.Status.SCHEDULED, *ACTIVE]
    )
    if job.status == Broadcast.Status.SCHEDULED:
        matching = matching.filter(scheduled_at=job.scheduled_at, asset_id=job.asset_id)
    ended_at = at or timezone.now()
    if status == Broadcast.Status.COMPLETED:
        # Match deletion's asset -> appointment lock order; never publish an unconfirmed send.
        from .publication import note_completion

        with transaction.atomic():
            asset = Asset.objects.select_for_update().get(pk=job.asset_id)
            if matching.update(status=status, outcome=reason, ended_at=ended_at):
                note_completion(asset, job.started_at or job.scheduled_at, ended_at)
    else:
        matching.update(status=status, outcome=reason, ended_at=ended_at)


def event_time(value):
    return datetime.fromtimestamp(value, tz=UTC)


def reconcile(state, now):
    for job in Broadcast.objects.filter(status__in=ACTIVE).select_related("asset"):
        if job.engine_session and job.engine_session != state.session:
            finish(
                job, Broadcast.Status.INTERRUPTED, "La regia è stata riavviata.", now
            )
            continue
        events = state.events.get(str(job.id), {})
        if "started" in events and job.started_at is None:
            job.started_at = event_time(events["started"])
            job.status = Broadcast.Status.PLAYING
            job.save(update_fields=["started_at", "status"])
        if "interrupted" in events:
            finish(
                job,
                Broadcast.Status.INTERRUPTED,
                "Interrotto dalla diretta.",
                event_time(events["interrupted"]),
            )
        elif "completed" in events:
            finish(
                job,
                Broadcast.Status.COMPLETED,
                "Trasmissione terminata.",
                event_time(events["completed"]),
            )
        elif "live" in events or "busy" in events:
            reason = (
                "Diretta in corso."
                if "live" in events
                else "Un altro podcast è in onda."
            )
            finish(job, Broadcast.Status.SKIPPED, reason, now)
        elif (
            job.status == Broadcast.Status.DISPATCHING
            and now - job.dispatched_at > ACK_TIMEOUT
        ):
            # Accepted but undecodable requests cannot block the calendar forever.
            engine.command(f"julie.abort {job.id}")
            finish(
                job,
                Broadcast.Status.ERROR,
                "Avvio non confermato dalla regia; nessun nuovo tentativo automatico.",
                now,
            )
        elif (
            job.status == Broadcast.Status.PLAYING
            and now - job.started_at
            > timedelta(seconds=(job.asset.duration_seconds or 0) + 30)
        ):
            engine.command(f"julie.abort {job.id}")
            finish(
                job,
                Broadcast.Status.INTERRUPTED,
                "Fine non confermata entro la durata prevista.",
                now,
            )
        elif job.status == Broadcast.Status.PLAYING and state.current != str(job.id):
            finish(
                job,
                Broadcast.Status.INTERRUPTED,
                "La regia non conferma più questa trasmissione.",
                now,
            )


def _tick(now=None):
    now = now or timezone.now()
    # Past appointments are not replayed after downtime.
    Broadcast.objects.filter(
        status=Broadcast.Status.SCHEDULED, scheduled_at__lt=now - GRACE
    ).update(
        status=Broadcast.Status.SKIPPED,
        ended_at=now,
        outcome="Orario superato: trasmissione non recuperata automaticamente.",
    )
    try:
        state = engine.get_state()
    except engine.EngineError:
        logger.warning("Regia non raggiungibile; attendo il prossimo controllo")
        return False
    reconcile(state, now)
    for job in (
        Broadcast.objects.select_related("asset")
        .filter(status=Broadcast.Status.SCHEDULED, scheduled_at__lte=now)
        .order_by("scheduled_at", "id")
    ):
        if state.live:
            finish(job, Broadcast.Status.SKIPPED, "Diretta in corso.", now)
        elif state.current or Broadcast.objects.filter(status__in=ACTIVE).exists():
            finish(
                job,
                Broadcast.Status.SKIPPED,
                "Un altro podcast è in onda o in avvio.",
                now,
            )
        elif (
            job.asset.category != Asset.Category.PODCAST
            or not job.asset.is_active
            or not eligible(job.asset)
        ):
            finish(
                job,
                Broadcast.Status.SKIPPED,
                (
                    "Audio ancora in preparazione all’orario previsto."
                    if job.asset.status
                    in [Asset.Status.PENDING, Asset.Status.PROCESSING]
                    else "Preparazione audio fallita."
                    if job.asset.status == Asset.Status.ERROR
                    else "Podcast non disponibile: controlla Libreria e Cestino."
                ),
                now,
            )
        else:
            # Conditional claim also protects against cancellation racing this tick.
            claimed = Broadcast.objects.filter(
                pk=job.pk,
                status=Broadcast.Status.SCHEDULED,
                scheduled_at=job.scheduled_at,
                asset_id=job.asset_id,
            ).update(
                status=Broadcast.Status.DISPATCHING,
                dispatched_at=now,
                engine_session=state.session,
            )
            if not claimed:
                continue
            try:
                engine.dispatch(job)
            except engine.EngineError:
                logger.warning(
                    "Invio incerto per %s; riconciliazione senza reinvio", job.id
                )

    return True


# Serialize reset against whole passes, including engine reads and database writes.


def tick(*args, **kwargs):
    from .backup_guard import data_gate, reset_requested
    from .storage import data_path

    with data_gate() as gate:
        if (
            gate is None
            or reset_requested()
            or data_path("locks/radio-stopped").exists()
        ):
            return False
        return _tick(*args, **kwargs)
