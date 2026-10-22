#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Remove only the verified Compose installation; destructive options are explicit."""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path


class UninstallError(Exception):
    pass


class Docker:
    def __init__(self):
        self.prefix = ["docker"]
        try:
            self.run("info")
        except UninstallError:
            if subprocess.run(["sudo", "-v"], check=False).returncode:
                raise UninstallError("Accesso a Docker non disponibile.") from None
            self.prefix = ["sudo", "docker"]
            self.run("info")

    def run(self, *arguments):
        try:
            result = subprocess.run(
                self.prefix + list(arguments),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UninstallError(
                "Docker non disponibile o tempo massimo superato. Nessuna ulteriore rimozione eseguita."
            ) from error
        if result.returncode:
            raise UninstallError(
                "Operazione Docker non riuscita. Controlla lo stato prima di riprovare."
            )
        return result.stdout


def validate_root(root):
    root = Path(root).resolve()
    if root in {
        Path("/"),
        Path.home(),
        Path("/opt"),
        Path("/home"),
        Path("/usr"),
        Path("/var"),
        Path("/tmp"),
    }:
        raise UninstallError(
            "Questa cartella non può essere rimossa come installazione Julie."
        )
    for relative in [
        "install.sh",
        "compose.yaml",
        "compose.dashboard.yaml",
        "dashboard/manage.py",
    ]:
        if not (root / relative).is_file() or (root / relative).is_symlink():
            raise UninstallError(
                "Cartella Julie non riconosciuta: nessun file rimosso."
            )
    if any(
        not (root / name).is_file() or (root / name).is_symlink()
        for name in (".env", ".env.dashboard")
    ):
        raise UninstallError(
            "Configurazione mancante: impossibile identificare con sicurezza questa installazione."
        )
    return root


def local_targets(root):
    return [
        root / relative
        for relative in [
            "audio",
            "backups",
            ".env",
            ".env.dashboard",
            ".env.proxy",
            "config/icecast.xml",
            "ACCESSO-LOCALE.txt",
        ]
    ]


def check_files(root, remove_directory):
    targets = [root] if remove_directory else local_targets(root)
    for target in targets:
        # A config-directory symlink must not redirect even an individual unlink.
        if target.parent != target.parent.resolve() or target.is_symlink():
            raise UninstallError(
                "Percorso locale reindirizzato da un collegamento: rimozione esclusa."
            )
        if target.exists() and target.is_dir():
            for parent, directories, _ in os.walk(target, followlinks=False):
                if Path(parent).is_mount() or any(
                    (Path(parent) / name).is_mount() for name in directories
                ):
                    raise UninstallError(
                        "Un disco è montato dentro la cartella da rimuovere: nessun dato cancellato."
                    )


def inspect_plan(root, docker, purge):
    files = ["compose.yaml", "compose.dashboard.yaml"]
    if "JULIE_PROXY_ENABLED=1" in (root / ".env").read_text().splitlines():
        files.append("dashboard/deployment/compose.proxy.yaml")
    arguments = [
        "compose",
        "--project-directory",
        str(root),
        "--env-file",
        str(root / ".env"),
    ]
    for filename in files:
        arguments += ["-f", str(root / filename)]
    config = json.loads(docker.run(*arguments, "config", "--format", "json"))
    project = config.get("name", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise UninstallError("Nome del progetto Docker non valido.")
    all_ids = docker.run("ps", "-aq").split()
    containers = json.loads(docker.run("inspect", *all_ids)) if all_ids else []
    owned = []
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == project:
            if (
                labels.get("com.docker.compose.project.working_dir") != str(root)
                or labels.get("com.docker.compose.service") not in config["services"]
            ):
                raise UninstallError(
                    "Il nome del progetto è usato anche da container non riconducibili a questa cartella. Operazione annullata."
                )
            owned.append(container)
        else:
            for mount in container.get("Mounts", []):
                if not mount.get("Source"):
                    continue
                source = Path(mount["Source"]).resolve()
                if source == root or root in source.parents:
                    raise UninstallError(
                        "Altri container utilizzano questa cartella. Operazione annullata."
                    )
    owned_ids = {item["Id"] for item in owned}
    volumes = []
    if purge:
        existing = set(docker.run("volume", "ls", "--format", "{{.Name}}").splitlines())
        for key, volume in config.get("volumes", {}).items():
            name = volume.get("name", f"{project}_{key}")
            if volume.get("external") or name not in existing:
                continue
            details = json.loads(docker.run("volume", "inspect", name))[0]
            labels = details.get("Labels") or {}
            if (
                labels.get("com.docker.compose.project") != project
                or labels.get("com.docker.compose.volume") != key
            ):
                raise UninstallError(
                    "Un volume dati non appartiene al progetto atteso. Operazione annullata."
                )
            if any(
                container["Id"] not in owned_ids
                and any(
                    mount.get("Name") == name for mount in container.get("Mounts", [])
                )
                for container in containers
            ):
                raise UninstallError(
                    "Il database o un volume è condiviso con altri container. Operazione annullata."
                )
            volumes.append(name)
    networks = []
    existing_networks = set(
        docker.run("network", "ls", "--format", "{{.Name}}").splitlines()
    )
    for key, network in config.get("networks", {}).items():
        name = network.get("name", f"{project}_{key}")
        if network.get("external") or name not in existing_networks:
            continue
        details = json.loads(docker.run("network", "inspect", name))[0]
        if (details.get("Labels") or {}).get("com.docker.compose.project") != project:
            raise UninstallError(
                "Rete Docker non riconducibile a Julie. Operazione annullata."
            )
        if set((details.get("Containers") or {}).keys()) - owned_ids:
            raise UninstallError(
                "La rete è condivisa con altri container. Operazione annullata."
            )
        networks.append(name)
    return {
        "project": project,
        "containers": sorted(owned_ids),
        "volumes": sorted(volumes),
        "networks": sorted(networks),
    }


def execute(root, docker, plan, *, purge=False, remove_directory=False):
    # Revalidate the inventory immediately before the first destructive command.
    if inspect_plan(root, docker, purge) != plan:
        raise UninstallError(
            "Le risorse Docker sono cambiate. Ripeti la verifica prima di rimuovere Julie."
        )
    if purge:
        check_files(root, remove_directory)
    if plan["containers"]:
        docker.run("rm", "--force", *plan["containers"])
    for name in plan["volumes"]:
        docker.run("volume", "rm", name)
    for name in plan["networks"]:
        docker.run("network", "rm", name)
    remaining = docker.run(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={plan['project']}"
    ).split()
    if remaining:
        raise UninstallError(
            "Sono presenti nuovi container del progetto. File locali conservati."
        )
    if purge:
        if remove_directory:
            shutil.rmtree(root)
        else:
            for path in local_targets(root):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        description="Disinstalla solo Julie. Per impostazione predefinita conserva database, audio e configurazione. Nessun backup automatico."
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Elimina anche database, audio, backup locali e configurazione.",
    )
    parser.add_argument(
        "--remove-directory",
        action="store_true",
        help="Con --purge, elimina anche tutta la cartella della repository.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra il piano senza fermare o eliminare nulla.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Conferma esplicitamente le opzioni indicate senza richiesta interattiva.",
    )
    options = parser.parse_args()
    if options.remove_directory and not options.purge:
        parser.error("--remove-directory richiede --purge")
    root = validate_root(Path(__file__).resolve().parent.parent)
    os.chdir(root)
    with ExitStack() as stack:
        if not options.dry_run:
            for name in [".install.lock", ".update.lock", ".uninstall.lock"]:
                handle = stack.enter_context((root / name).open("a"))
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise UninstallError(
                        "Installazione, aggiornamento o disinstallazione già in corso."
                    ) from error
        docker = Docker()
        plan = inspect_plan(root, docker, options.purge)
        if options.purge:
            check_files(root, options.remove_directory)
        print(
            f"Cartella Julie: {root}\nProgetto Docker: {plan['project']}\nContainer da rimuovere: {len(plan['containers'])}"
        )
        print("Volumi dati da eliminare: " + (", ".join(plan["volumes"]) or "nessuno"))
        print("La trasmissione si interromperà. Nessun backup automatico verrà creato.")
        print(
            "Audio, backup locali e configurazione: "
            + ("DA ELIMINARE" if options.purge else "conservati")
        )
        if options.remove_directory:
            print("Anche la cartella della repository verrà eliminata.")
        if options.dry_run:
            print("Verifica completata. Nessuna modifica eseguita.")
            return
        if not options.yes:
            phrase = ("ELIMINA " if options.purge else "DISINSTALLA ") + plan["project"]
            if input(f"Per confermare scrivi {phrase}: ").strip() != phrase:
                print("Operazione annullata. Nessuna risorsa rimossa.")
                return
        execute(
            root,
            docker,
            plan,
            purge=options.purge,
            remove_directory=options.remove_directory,
        )
        print(
            "Julie rimossa."
            if options.purge
            else "Servizi Julie rimossi; dati e configurazione conservati."
        )
        print("Sistema operativo, rete, SSH, Docker e immagini Docker conservati.")


if __name__ == "__main__":
    try:
        main()
    except (UninstallError, OSError, ValueError, EOFError) as error:
        print(f"Disinstallazione interrotta: {error}", file=sys.stderr)
        raise SystemExit(1) from error
