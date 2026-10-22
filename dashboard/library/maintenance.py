"""Calendar expansion is background work, never part of the on-air dispatch loop."""

import logging
import threading

from django.db import close_old_connections, connections

from .backup_guard import backup_requested, data_gate
from .recurrence import extend_series

logger = logging.getLogger(__name__)


def maintenance_pass():
    """Share the backup gate so an export sees either side of a complete expansion."""
    try:
        close_old_connections()
        with data_gate() as handle:
            if handle is None or backup_requested():
                return False
            extend_series()
        return True
    except Exception:
        logger.exception(
            "Recurring schedule expansion failed; dispatch remains independent"
        )
        return False
    finally:
        # Django connections belong to this maintenance thread, not to dispatch.
        connections.close_all()


def start_maintenance(stop):
    def loop():
        while not stop.is_set():
            completed = maintenance_pass()
            if stop.wait(3600 if completed else 60):
                break

    thread = threading.Thread(
        target=loop, name="julie-calendar-maintenance", daemon=True
    )
    thread.start()
    return thread
