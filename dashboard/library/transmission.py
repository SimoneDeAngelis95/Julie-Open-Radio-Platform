"""Temporary, engine-session-scoped suspension. Maintenance stops stay separate."""

import fcntl
import json
import os
import time
from contextlib import contextmanager

from django.utils import timezone

from . import engine
from .backup_guard import backup_requested, data_gate
from .models import Broadcast
from .storage import data_path


class TransmissionError(Exception):
    pass


@contextmanager
def control_gate(exclusive=False):
    path = data_path("locks/transmission-control.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(
                handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            yield None
        else:
            yield handle


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


def output_confirmed(expected):
    """Liquidsoap applies output start/stop asynchronously on its clock."""
    for attempt in range(31):
        status = engine.command("output.icecast.status")
        if status == expected:
            return True
        if status not in {"on", "off"} or attempt == 30:
            return False
        time.sleep(0.1)
    return False


def change(action):
    if action not in {"suspend", "resume"}:
        raise TransmissionError("Azione non valida.")
    with data_gate() as gate, control_gate(exclusive=True) as control:
        if (
            gate is None
            or backup_requested()
            or data_path("locks/radio-stopped").exists()
        ):
            raise TransmissionError("Completa prima la manutenzione della radio.")
        if control is None:
            raise TransmissionError(
                "Un comando della regia è in corso. Riprova fra un secondo."
            )
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
            if not output_confirmed("off"):
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
            if not output_confirmed("on"):
                raise TransmissionError(
                    "Ripartenza non confermata. La sospensione è conservata: riprova."
                )
            path.unlink(missing_ok=True)
