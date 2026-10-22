#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Create installation secrets once. Never reset an existing station."""

import argparse
import fcntl
import ipaddress
import os
import re
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def configure(root, host, bind):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ValueError("Specifica un nome host o un IPv4, senza schema o porta.")
    ipaddress.IPv4Address(bind)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        targets = [root / ".env", root / ".env.dashboard", root / "config/icecast.xml"]
        if any(path.exists() for path in targets):
            if not all(path.is_file() and not path.is_symlink() for path in targets):
                raise ValueError(
                    "Configurazione incompleta: conserva i file esistenti e consulta docs/installazione.md. Nessun file sovrascritto."
                )
            print("Configurazione esistente conservata; nessuna password modificata.")
            return
        config = root / "config"
        config.mkdir(mode=0o700, exist_ok=True)
        config.chmod(0o700)
        for relative in [
            "audio/library",
            "audio/library/originals",
            "audio/library/ready",
            "audio/library/work",
            "audio/library/staging",
            "audio/library/locks",
            "audio/library/playout",
            "backups/exports",
        ]:
            (root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        rotation = root / "audio/library/playout/rotation.txt"
        if not rotation.exists():
            write_private(rotation, "0\n\n")
        source = secrets.token_urlsafe(32)
        live = secrets.token_urlsafe(32)
        tree = ET.parse(config / "icecast.xml.example")
        tree.find("hostname").text = host
        for field, value in [
            ("source-password", source),
            ("relay-password", secrets.token_urlsafe(32)),
            ("admin-password", secrets.token_urlsafe(32)),
        ]:
            tree.find(f"authentication/{field}").text = value
        env = {
            "COMPOSE_PROJECT_NAME": "julie",
            "JULIE_UID": str(os.getuid()),
            "JULIE_GID": str(os.getgid()),
            "JULIE_BIND_ADDRESS": bind,
            "JULIE_DASHBOARD_PORT": "8010",
            "JULIE_STREAM_PORT": "8000",
            "JULIE_LIVE_PORT": "8001",
            "JULIE_PROXY_ENABLED": "0",
            "ICECAST_SOURCE_PASSWORD": source,
            "JULIE_LIVE_PASSWORD": live,
        }
        dashboard = {
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "DJANGO_ALLOWED_HOSTS": f"{host},localhost,127.0.0.1",
            "POSTGRES_HOST": "dashboard-db",
            "POSTGRES_DB": "julie",
            "POSTGRES_USER": "julie",
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "JULIE_STREAM_URL": f"http://{host}:8000/stream.mp3",
            "JULIE_TARGET_LUFS": "-20",
            "JULIE_CEILING_DBTP": "-1.5",
        }
        write_private(
            targets[0], "".join(f"{key}={value}\n" for key, value in env.items())
        )
        write_private(
            targets[1], "".join(f"{key}={value}\n" for key, value in dashboard.items())
        )
        write_private(
            targets[2], ET.tostring(tree.getroot(), encoding="unicode") + "\n"
        )
        access = root / "ACCESSO-LOCALE.txt"
        if not access.exists():
            write_private(
                access,
                f"Pannello: http://{host}:8010/setup/\nStream: http://{host}:8000/stream.mp3\n"
                "Codice iniziale: audio/library/installation-code.txt (dopo il primo avvio).\n"
                "Ingresso diretta: porta 8001, mount live, password JULIE_LIVE_PASSWORD in .env.\n",
            )
        print(
            "Configurazione creata. Indirizzi in ACCESSO-LOCALE.txt; segreti conservati solo sul server."
        )


def main():
    parser = argparse.ArgumentParser(
        description="Prepara una nuova installazione Julie senza sovrascrivere i segreti."
    )
    parser.add_argument(
        "--host", default="localhost", help="Nome host o IPv4 usato dai browser."
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="IPv4 locale su cui pubblicare le porte (default: solo localhost).",
    )
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Esegui come utente normale; install.sh usa sudo solo per Docker.")
    try:
        configure(Path(__file__).resolve().parent.parent, args.host, args.bind)
    except (OSError, ValueError, ET.ParseError) as error:
        parser.exit(1, f"Configurazione non completata: {error}\n")


if __name__ == "__main__":
    main()
