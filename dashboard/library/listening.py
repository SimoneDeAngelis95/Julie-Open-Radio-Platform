"""Aggregate public Icecast counters, with unknown values kept distinct from zero."""

import json
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import engine
from .models import Broadcast, ListeningSample, RadioSnapshot


class StatsError(Exception):
    pass


def read_stats():
    try:
        with urlopen(settings.ICECAST_STATS_URL, timeout=3) as response:
            body = response.read(262145)
        if len(body) > 262144:
            raise ValueError("oversized response")
        document = json.loads(body)
        stats = document["icestats"]
        if not isinstance(stats, dict):
            raise ValueError("invalid stats")
        sources = stats.get("source", [])
        if isinstance(sources, dict):
            sources = [sources]
        if not isinstance(sources, list):
            raise ValueError("invalid sources")
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("invalid source")
            if urlsplit(source.get("listenurl", "")).path == settings.RADIO_MOUNT:
                value = source.get("listeners")
                # A missing or invalid counter must not become a false zero.
                listeners = None
                if not isinstance(value, bool) and str(value).isdigit():
                    listeners = int(value)
                    if listeners > 2_147_483_647:
                        listeners = None
                return {
                    "stream_online": True,
                    "listeners": listeners,
                    "title": str(source.get("title") or "")[:500],
                    "artist": str(source.get("artist") or "")[:240],
                }
        return {"stream_online": False, "listeners": 0, "title": "", "artist": ""}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StatsError("Statistiche Icecast non disponibili") from error


def _collect():
    observed = timezone.now()
    values = {"stream_online": None, "listeners": None, "title": "", "artist": ""}
    try:
        values.update(read_stats())
    except StatsError:
        pass
    values.update(engine_online=False, kind="Non disponibile")
    try:
        state = engine.get_state()
        values["engine_online"] = True
        if state.live:
            values["kind"] = "Diretta"
        elif state.current:
            job = (
                Broadcast.objects.select_related("asset")
                .filter(pk=UUID(state.current))
                .first()
            )
            if job and "started" in state.events.get(state.current, {}):
                values.update(
                    kind="Podcast", title=job.asset.title, artist=job.asset.artist
                )
            else:
                values["kind"] = "Podcast in avvio"
        else:
            values["kind"] = "Musica / jingle"
    except (engine.EngineError, ValueError):
        pass
    if values["stream_online"] is False:
        values["kind"] = "Stream non disponibile"
    with transaction.atomic():
        RadioSnapshot.objects.update_or_create(
            pk=1, defaults={"checked_at": observed, **values}
        )
        ListeningSample.objects.get_or_create(
            minute=observed.replace(second=0, microsecond=0),
            defaults={key: values[key] for key in ["listeners", "stream_online"]},
        )
    return values


# Serialize reset against whole passes, including engine reads and database writes.


def collect(*args, **kwargs):
    from .backup_guard import data_gate, reset_requested

    with data_gate() as gate:
        if gate is None or reset_requested():
            return False
        return _collect(*args, **kwargs)
