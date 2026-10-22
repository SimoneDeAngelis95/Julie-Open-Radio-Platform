"""Small internal Liquidsoap protocol. No user supplied commands or file paths."""

import socket
from dataclasses import dataclass

from django.conf import settings


class EngineError(Exception):
    pass


@dataclass(frozen=True)
class EngineState:
    session: str
    live: bool
    current: str
    events: dict


def command(value):
    try:
        with socket.create_connection(
            (settings.PLAYOUT_HOST, settings.PLAYOUT_PORT), timeout=3
        ) as connection:
            connection.sendall((value + "\n").encode())
            result = b""
            while b"\nEND" not in result:
                block = connection.recv(8192)
                if not block:
                    raise EngineError("Risposta incompleta dalla regia")
                result += block
                if len(result) > 256_000:
                    raise EngineError("Risposta regia troppo grande")
        return result.decode().split("\nEND", 1)[0].strip()
    except (OSError, UnicodeError) as error:
        raise EngineError("Regia non raggiungibile") from error


def get_state():
    lines = command("julie.status").splitlines()
    if not lines or not lines[0].startswith("JULIE1|"):
        raise EngineError("Protocollo regia non disponibile")
    header = lines[0].split("|")
    if len(header) != 4:
        raise EngineError("Stato regia non valido")
    events = {}
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) == 3 and parts[1] in {
            "accepted",
            "started",
            "completed",
            "interrupted",
            "live",
            "busy",
            "invalid",
        }:
            try:
                timestamp = float(parts[2])
            except ValueError:
                continue
            events.setdefault(parts[0], {})[parts[1]] = timestamp
    return EngineState(header[1], header[2] == "true", header[3], events)


def dispatch(broadcast):
    from .playout import annotated_uri

    # UUID plus generated, trusted media URI; server rejects duplicate IDs.
    return command(f"julie.schedule {broadcast.id}|{annotated_uri(broadcast.asset)}")


def media_in_use():
    """Include pending and playing requests; fail closed if the inventory is uncertain."""
    if command("request.resolving").strip():
        raise EngineError(
            "La regia sta preparando una richiesta: riprova tra pochi secondi."
        )
    identifiers = command("request.all").split()
    if len(identifiers) > 1000 or any(not item.isdigit() for item in identifiers):
        raise EngineError("Inventario della regia non disponibile.")
    assets, filenames = set(), set()
    for identifier in identifiers:
        metadata = {}
        for line in command(f"request.metadata {identifier}").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"status", "filename", "julie_id"}:
                try:
                    import json

                    metadata[key] = json.loads(value)
                except (ValueError, TypeError) as error:
                    raise EngineError("Inventario della regia non valido.") from error
        if "status" not in metadata:
            raise EngineError("Inventario della regia cambiato: riprova.")
        if metadata.get("julie_id"):
            assets.add(metadata["julie_id"])
        if metadata.get("filename"):
            filenames.add(metadata["filename"])
    # Resolving can have started while requests were enumerated.
    if command("request.resolving").strip():
        raise EngineError(
            "La regia sta preparando una richiesta: riprova tra pochi secondi."
        )
    return assets, filenames
