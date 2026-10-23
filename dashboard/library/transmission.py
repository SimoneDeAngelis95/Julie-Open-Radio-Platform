"""Temporary, engine-session-scoped suspension. Maintenance stops stay separate."""

import json
import os

from django.utils import timezone

from . import engine
from .backup_guard import data_gate, reset_requested
from .models import Broadcast
from .storage import data_path


class TransmissionError(Exception):
    pass


def marker():
    return data_path("locks/transmission-suspended.json")


def skip_due(now):
    Broadcast.objects.filter(
        status=Broadcast.Status.SCHEDULED, scheduled_at__lte=now
    ).update(
        status=Broadcast.Status.SKIPPED,
        ended_at=now,
        outcome="Radio sospesa: appuntamento non recuperato automaticamente.",
    )


def suspended(state, now):
    """Called under the shared data gate before scheduler reconciliation/dispatch."""
    path = marker()
    if not path.exists():
        return False
    try:
        session = json.loads(path.read_text())["session"]
    except (ValueError, KeyError):
        # A corrupt intent must not silently resume dispatch.
        skip_due(now)
        return True
    skip_due(now)
    if session != state.session:
        path.unlink(missing_ok=True)
        return False
    return True


def clear_podcasts(state, now):
    from .scheduler import ACTIVE, finish

    if state.current:
        engine.command(f"julie.abort {state.current}")
    engine.command("podcasts.flush_and_skip")
    for job in Broadcast.objects.filter(status__in=ACTIVE):
        finish(
            job,
            Broadcast.Status.INTERRUPTED,
            "Trasmissione sospesa dall’amministratore.",
            now,
        )


def change(action):
    if action not in {"suspend", "resume"}:
        raise TransmissionError("Azione non valida.")
    with data_gate(exclusive=True) as gate:
        if (
            gate is None
            or reset_requested()
            or data_path("locks/radio-stopped").exists()
        ):
            raise TransmissionError("Completa prima la manutenzione della radio.")
        state = engine.get_state()
        path = marker()
        now = timezone.now()
        if action == "suspend":
            from .scheduler import reconcile

            reconcile(state, now)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".part")
            temporary.write_text(json.dumps({"session": state.session}))
            os.replace(temporary, path)
            # Persist intent before commands: an uncertain reply must still block dispatch.
            engine.command("output.icecast.stop")
            if engine.command("output.icecast.status") != "off":
                raise TransmissionError(
                    "Arresto non confermato. La programmazione resta sospesa: riprova."
                )
            clear_podcasts(state, now)
            skip_due(now)
        elif path.exists():
            # Clear expired slots before output starts, including those inside scheduler grace.
            clear_podcasts(state, now)
            skip_due(now)
            engine.command("output.icecast.start")
            if engine.command("output.icecast.status") != "on":
                raise TransmissionError(
                    "Ripartenza non confermata. La sospensione è conservata: riprova."
                )
            path.unlink(missing_ok=True)
