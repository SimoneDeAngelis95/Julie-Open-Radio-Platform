#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Update dashboard releases without replacing station data or local configuration."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

APPS = [
    "dashboard",
    "dashboard-worker",
    "dashboard-scheduler",
    "dashboard-listening",
    "dashboard-backup",
]
RELEASE = re.compile(r"v?\d+\.\d+\.\d+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?\Z")
PROTECTED = {
    "compose.yaml",
    "compose.dashboard.yaml",
    "julie",
    "install.sh",
    "scripts/configure.py",
    "dashboard/deployment/compose.proxy.yaml",
    "backup-full.sh",
    "backup-audio.sh",
    "backup-config.sh",
    "backup-database.sh",
}
LOCAL = (".env", ".env.dashboard", ".env.proxy", "config/icecast.xml")
BUSY_CODE = """from library.models import BackupJob, ResetJob, RestoreJob
from library.reset import ACTIVE as reset_active
from library.restore import ACTIVE as restore_active
assert not BackupJob.objects.filter(status__in=['queued','running']).exists(), 'Backup in corso'
assert not ResetJob.objects.filter(status__in=reset_active).exists(), 'Reset in corso'
assert not RestoreJob.objects.filter(status__in=restore_active).exists(), 'Ripristino in corso'
print('JULIE_UPDATE_IDLE')"""
BACKUP_CODE = """import json, time
from library import backups
job = backups.enqueue('full')
while True:
    backups.process_one()
    job.refresh_from_db()
    if job.status == 'ready':
        path = backups.archive_path(job)
        print('JULIE_UPDATE_BACKUP ' + json.dumps({'id': str(job.id), 'name': path.name, 'sha256': job.sha256, 'size': job.size_bytes}), flush=True)
        break
    if job.status in ['error', 'expired']:
        raise RuntimeError('Backup completo non riuscito')
    time.sleep(2)"""
HEALTH_CODE = """import time, urllib.request
for attempt in range(45):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=3) as r:
            assert r.status == 200
        break
    except (OSError, AssertionError):
        if attempt == 44: raise
        time.sleep(2)"""


class UpdateError(Exception):
    pass


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def protected_change(path):
    return path in PROTECTED or path.startswith(("config/", "icecast/"))


def reserved_path(path):
    return (
        path in LOCAL
        or path in {"audio", "backups"}
        or path.startswith(("audio/", "backups/", "ACCESSO"))
        or (
            path.startswith(".env")
            and path not in {".env.example", ".env.dashboard.example"}
        )
    )


class Updater:
    def __init__(self, root, release=None, *, branch=None, backup=False):
        self.root = Path(root).resolve()
        self.branch = branch if branch is not None else (None if release else "main")
        self.release = release or self.branch
        self.backup_enabled = backup
        self.checkpoint = None
        self.stopped = False
        self.code_changed = False
        self.migration_started = False
        self.old = None
        self.old_branch = None
        self.recovery_image = None
        self.original_image_name = None
        self.log = None
        self.oneoff_container = None
        self.command_uncertain = False
        self.command_counter = 0

    def run(self, args, *, timeout=600, private=False):
        try:
            result = subprocess.run(
                args,
                cwd=self.root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                # Git must create readable application sources for non-root containers.
                # Keep the private parent umask for backups, configuration and all other commands.
                umask=0o022 if args[:2] == ["git", "checkout"] else -1,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError(f"Comando non completato: {args[0]}.") from error
        if self.log and not private:
            self.log.write(result.stdout)
            self.log.flush()
        if result.returncode:
            raise UpdateError(
                f"Comando non riuscito: {args[0]}. Consulta il rapporto se disponibile."
            )
        return result.stdout.strip()

    def git(self, *args):
        return self.run(["git", *args])

    def compose(self, *args, **options):
        return self.run([str(self.root / "julie"), *args], **options)

    def docker(self, *args):
        return self.run([*self.docker_prefix, *args])

    def local_hashes(self):
        return {
            name: digest(self.root / name)
            for name in LOCAL
            if (self.root / name).is_file()
        }

    def preflight(self):
        if (self.root / "backups/updates/INCOMPLETE").exists():
            raise UpdateError(
                "Un aggiornamento precedente richiede verifica. Consulta backups/updates/INCOMPLETE."
            )
        if self.branch not in (None, "main", "dev"):
            raise UpdateError("Sono supportati soltanto i rami main e dev.")
        if self.branch is None and not RELEASE.fullmatch(self.release):
            raise UpdateError(
                "Indica un tag di release, per esempio v0.1.0-beta.2; non un ramo."
            )
        if self.git("rev-parse", "--show-toplevel") != str(self.root):
            raise UpdateError("Esegui lo script dalla repository di installazione.")
        if self.git("status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError(
                "Ci sono modifiche locali non registrate. Nessun file verrà sovrascritto."
            )
        for name in (".env", ".env.dashboard", "config/icecast.xml"):
            path = self.root / name
            if not path.is_file() or path.is_symlink():
                raise UpdateError(
                    "Installazione non configurata o configurazione non regolare."
                )
        self.current_version = self.git("show", "HEAD:VERSION")
        self.old = self.git("rev-parse", "HEAD")
        self.old_branch = self.git("branch", "--show-current")
        if self.branch:
            ref = f"refs/remotes/origin/{self.branch}"
            self.git("fetch", "origin", "--no-tags", "--no-recurse-submodules",
                     f"+refs/heads/{self.branch}:{ref}")
        else:
            self.git("fetch", "origin", "--tags", "--no-recurse-submodules")
            ref = f"refs/tags/{self.release}"
        self.target = self.git("rev-parse", "--verify", f"{ref}^{{commit}}")
        if self.target == self.old:
            return False
        try:
            self.git("merge-base", "--is-ancestor", self.old, self.target)
        except UpdateError:
            raise UpdateError(
                "Downgrade o cronologia divergente: serve una procedura manuale."
            ) from None
        version = self.git("show", f"{self.target}:VERSION")
        if self.branch is None and version != self.release.removeprefix("v"):
            raise UpdateError("Il tag e il file VERSION non corrispondono.")
        if shutil.disk_usage(self.root).free < 512 * 1024 * 1024:
            raise UpdateError("Spazio libero insufficiente: servono almeno 512 MiB per avviare l’aggiornamento.")
        changes = self.git("diff", "--name-only", self.old, self.target).splitlines()
        if any(protected_change(path) for path in changes):
            raise UpdateError(
                "Questa release cambia motore o installazione: segui la migrazione nelle note di rilascio."
            )
        paths = self.git("ls-tree", "-r", "--name-only", self.target).splitlines()
        if any(reserved_path(path) for path in paths):
            raise UpdateError(
                "La release contiene percorsi riservati ai dati locali: aggiornamento rifiutato."
            )
        self.config_hashes = self.local_hashes()
        self.compose_config = self.compose("config", "--format", "json", private=True)
        config = json.loads(self.compose_config)
        for service, destination, expected in [
            ("dashboard", "/data", self.root / "audio/library"),
            ("dashboard-backup", "/backups", self.root / "backups/exports"),
        ]:
            volumes = config["services"][service].get("volumes", [])
            if not any(
                v.get("type") == "bind"
                and v.get("target") == destination
                and Path(v["source"]).resolve() == expected.resolve()
                for v in volumes
            ):
                raise UpdateError(
                    "I percorsi dati Compose sono personalizzati: serve una verifica manuale."
                )
        running = set(
            self.compose("ps", "--status", "running", "--services").splitlines()
        )
        if not set(APPS + ["dashboard-db", "liquidsoap", "icecast"]).issubset(running):
            raise UpdateError(
                "Alcuni servizi non sono avviati. Controlla la radio prima di aggiornare."
            )
        try:
            self.run(["docker", "info"], private=True)
            self.docker_prefix = ["docker"]
        except UpdateError:
            self.run(["sudo", "-v"], private=True)
            self.docker_prefix = ["sudo", "docker"]
        self.original_image_name = config["services"]["dashboard"]["image"]
        listed_image = self.compose("images", "-q", "dashboard")
        if not listed_image or "\n" in listed_image:
            raise UpdateError("Immagine attuale del pannello non identificabile.")
        self.old_image = self.docker(
            "image", "inspect", "--format", "{{.Id}}", listed_image
        )
        tagged = self.docker(
            "image", "inspect", "--format", "{{.Id}}", self.original_image_name
        )
        if tagged.removeprefix("sha256:") != self.old_image.removeprefix("sha256:"):
            raise UpdateError("Immagine locale e pannello avviato non corrispondono.")
        self.check_idle(running=True)
        return True

    def oneoff(self, service, *args, timeout=600):
        self.command_counter += 1
        name = f"julie-update-{self.checkpoint.name}-{self.command_counter}"
        self.oneoff_container = name
        self.save_state("running_" + service)
        try:
            result = self.compose(
                "run",
                "--rm",
                "--no-deps",
                "--name",
                name,
                service,
                *args,
                timeout=timeout,
            )
        except BaseException:
            # Killing a docker client does not reliably stop its container.
            try:
                remaining = self.docker("ps", "-aq", "--filter", "name=^/" + name + "$")
                if remaining:
                    self.docker("stop", name)
            except (UpdateError, OSError, ValueError):
                self.command_uncertain = True
            raise
        self.oneoff_container = None
        return result

    def check_idle(self, *, running):
        if running:
            output = self.compose(
                "exec",
                "-T",
                "dashboard-backup",
                "python",
                "manage.py",
                "shell",
                "-c",
                BUSY_CODE,
            )
        else:
            output = self.oneoff(
                "dashboard-backup", "python", "manage.py", "shell", "-c", BUSY_CODE
            )
        if "JULIE_UPDATE_IDLE" not in output.splitlines():
            raise UpdateError("Impossibile verificare che Manutenzione sia libera.")

    def save_state(self, phase):
        state = {
            "phase": phase,
            "from_commit": self.old,
            "to_commit": self.target,
            "release": self.release,
            "backup_requested": self.backup_enabled,
            "branch": self.branch,
            "migration_started": self.migration_started,
            "old_branch": self.old_branch,
            "recovery_image": self.recovery_image,
            "oneoff_container": self.oneoff_container,
            "command_uncertain": self.command_uncertain,
        }
        path = self.checkpoint / "state.json"
        temporary = path.with_suffix(".part")
        with temporary.open("w") as output:
            json.dump(state, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(self.checkpoint)

    def prepare_checkpoint(self):
        parent = self.root / "backups/updates"
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.checkpoint = parent / (
            time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        self.checkpoint.mkdir(mode=0o700)
        sync_directory(parent)
        sync_directory(parent.parent)
        self.log = (self.checkpoint / "update.log").open("w")
        self.git("bundle", "create", str(self.checkpoint / "source.bundle"), "HEAD")
        with tarfile.open(self.checkpoint / "local-config.tar.gz", "w:gz") as archive:
            for name in self.config_hashes:
                archive.add(self.root / name, arcname=name, recursive=False)
        self.recovery_image = "julie-update-recovery:" + self.checkpoint.name.lower()
        self.docker("image", "tag", self.old_image, self.recovery_image)
        for name in ["source.bundle", "local-config.tar.gz"]:
            with (self.checkpoint / name).open("rb") as saved:
                os.fsync(saved.fileno())
        self.save_state("prepared")
        with (parent / "INCOMPLETE").open("x") as marker:
            marker.write(str(self.checkpoint) + "\n")
            marker.flush()
            os.fsync(marker.fileno())
        sync_directory(parent)

    def safety_backup(self):
        output = self.oneoff(
            "dashboard-backup",
            "python",
            "manage.py",
            "shell",
            "-c",
            BACKUP_CODE,
            timeout=24 * 3600,
        )
        rows = [
            line.removeprefix("JULIE_UPDATE_BACKUP ")
            for line in output.splitlines()
            if line.startswith("JULIE_UPDATE_BACKUP ")
        ]
        if len(rows) != 1:
            raise UpdateError(
                "Il backup completo non ha restituito un esito verificabile."
            )
        info = json.loads(rows[0])
        name = info["name"]
        if Path(name).name != name or not name.endswith(".zip"):
            raise UpdateError("Percorso del backup non valido.")
        source = self.root / "backups/exports" / name
        destination = self.checkpoint / "full-backup.zip"
        if source.is_symlink() or not source.is_file():
            raise UpdateError("Backup completo non trovato.")
        if (
            shutil.disk_usage(self.checkpoint).free
            < source.stat().st_size + 256 * 1024**2
        ):
            raise UpdateError(
                "Spazio insufficiente per conservare la copia di sicurezza."
            )
        with source.open("rb") as original, destination.open("xb") as output_file:
            shutil.copyfileobj(original, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        if (
            destination.stat().st_size != info["size"]
            or digest(destination) != info["sha256"]
        ):
            raise UpdateError("Verifica della copia di sicurezza non riuscita.")
        with zipfile.ZipFile(destination) as archive:
            required = {
                "database.dump",
                "audio.tar",
                "configuration.tar.gz",
                "manifest.json",
                "README.txt",
            }
            if (
                len(archive.namelist()) != len(required)
                or set(archive.namelist()) != required
                or archive.getinfo("manifest.json").file_size > 8 * 1024**2
            ):
                raise UpdateError("Archivio di sicurezza non completo.")
            manifest = json.loads(archive.read("manifest.json"))
            if (
                manifest.get("format") != "julie-backup-v1"
                or manifest.get("kind") != "full"
                or manifest.get("coordinated") is not True
                or manifest.get("id") != info["id"]
            ):
                raise UpdateError("Manifest del backup non valido.")
        (self.checkpoint / "full-backup.sha256").write_text(
            info["sha256"] + "  full-backup.zip\n"
        )
        self.save_state("backup_verified")

    def restore_before_migration(self):
        if self.code_changed:
            self.git("checkout", "--detach", self.old)
            if self.old_branch:
                self.git("checkout", self.old_branch)
        if self.recovery_image:
            self.docker("image", "tag", self.recovery_image, self.original_image_name)
        if self.local_hashes() != self.config_hashes:
            raise UpdateError(
                "La configurazione locale è cambiata: ripartenza automatica sospesa."
            )
        if json.loads(
            self.compose("config", "--format", "json", private=True)
        ) != json.loads(self.compose_config):
            raise UpdateError(
                "La configurazione dei servizi è cambiata: ripartenza automatica sospesa."
            )
        self.compose("up", "-d", "--no-deps", *APPS)
        self.stopped = False
        self.save_state("old_version_resumed")
        (self.root / "backups/updates/INCOMPLETE").unlink(missing_ok=True)
        sync_directory(self.root / "backups/updates")

    def apply(self):
        if self.git("status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError(
                "La repository è cambiata durante i controlli. Nessun servizio fermato."
            )
        self.prepare_checkpoint()
        print(
            "Il pannello e la programmazione saranno temporaneamente in pausa.",
            flush=True,
        )
        self.save_state("stopping_apps")
        self.stopped = True  # A partly successful stop must also be recovered.
        self.compose("stop", *APPS)
        self.save_state("apps_stopped")
        self.check_idle(running=False)
        if self.backup_enabled:
            self.safety_backup()
            print(f"Copia verificata: {self.checkpoint / 'full-backup.zip'}", flush=True)
        else:
            self.save_state("backup_skipped")
            print("Backup completo non richiesto: nessuna copia di database e audio.", flush=True)
        if self.git("status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError(
                "Modifiche locali durante la preparazione: aggiornamento sospeso."
            )
        self.code_changed = True
        self.git("checkout", "--detach", self.target)
        if self.local_hashes() != self.config_hashes:
            raise UpdateError(
                "La configurazione locale è cambiata. Nessuna migrazione avviata."
            )
        after = self.compose("config", "--format", "json", private=True)
        if json.loads(after) != json.loads(self.compose_config):
            raise UpdateError(
                "La release cambia la configurazione dei servizi. Nessuna migrazione avviata."
            )
        self.save_state("building")
        print("Preparo la nuova versione del pannello.", flush=True)
        self.compose("build", "dashboard", timeout=3600)
        self.oneoff(
            "dashboard", "python", "manage.py", "check", "--database", "default"
        )
        print("Aggiorno lo schema del database.", flush=True)
        self.migration_started = True
        self.save_state("migrating")
        self.oneoff("dashboard", "python", "manage.py", "migrate", "--noinput")
        if self.local_hashes() != self.config_hashes or json.loads(
            self.compose("config", "--format", "json", private=True)
        ) != json.loads(self.compose_config):
            raise UpdateError(
                "Configurazione cambiata durante l’aggiornamento: serve una verifica manuale."
            )
        self.save_state("starting_apps")
        self.compose("up", "-d", "--no-deps", *APPS)
        self.compose(
            "exec", "-T", "dashboard", "python", "-c", HEALTH_CODE, timeout=180
        )
        running = set(
            self.compose("ps", "--status", "running", "--services").splitlines()
        )
        if not set(APPS).issubset(running):
            raise UpdateError("Alcuni servizi applicativi non sono ripartiti.")
        self.save_state("completed")
        (self.root / "backups/updates/INCOMPLETE").unlink(missing_ok=True)
        sync_directory(self.root / "backups/updates")
        self.stopped = False
        print(
            f"Aggiornamento a {self.release} completato. Rapporto e file di recupero in {self.checkpoint}",
            flush=True,
        )

    def execute(self):
        if not self.preflight():
            print("Questa versione è già installata. Nessuna modifica.")
            return
        print(
            f"Aggiornamento richiesto: {self.current_version} ({self.old[:12]}) → {self.release} ({self.target[:12]})",
            flush=True,
        )
        print("Backup completo: " + ("sì" if self.backup_enabled else "no"), flush=True)
        if not self.backup_enabled:
            print("Senza backup non è disponibile il ripristino del database dopo le migrazioni.", flush=True)
        try:
            self.apply()
        except BaseException:
            if self.stopped:
                if not self.migration_started and not self.command_uncertain:
                    try:
                        self.restore_before_migration()
                        print(
                            "Aggiornamento interrotto prima delle migrazioni: versione precedente riavviata.",
                            flush=True,
                        )
                    except (UpdateError, OSError, ValueError):
                        print(
                            "Ripartenza automatica non riuscita. Consulta il rapporto di aggiornamento.",
                            flush=True,
                        )
                else:
                    try:
                        self.compose("stop", *APPS)
                        self.save_state("manual_recovery_required")
                    except (UpdateError, OSError, ValueError):
                        print(
                            "Controllo dei servizi non completato: verifica anche i container ancora attivi.",
                            flush=True,
                        )
                    print(
                        "Aggiornamento da verificare: pannello e programmazione in pausa. Nessun ripristino automatico del database.",
                        flush=True,
                    )
            if self.checkpoint:
                print(f"Rapporto e file di recupero: {self.checkpoint}", flush=True)
            raise
        finally:
            if self.log:
                self.log.close()


def argument_parser():
    parser = argparse.ArgumentParser(
        description="Aggiorna Julie da main, dev o un tag. Backup completo facoltativo."
    )
    parser.add_argument("release", nargs="?", help="Tag (sintassi precedente, equivalente a -t TAG)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("-d", "--dev", action="store_true", help="Aggiorna dal ramo dev")
    target.add_argument("-t", "--tag", help="Tag esistente da installare")
    parser.add_argument("-b", "--backup", action="store_true", help="Crea e verifica un backup completo prima di aggiornare")
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    if args.release and (args.dev or args.tag):
        parser.error("Il tag posizionale non è combinabile con -d o -t.")
    release = args.tag or args.release
    branch = None if release else ("dev" if args.dev else "main")
    if os.geteuid() == 0:
        parser.error(
            "Esegui come utente normale; sudo viene richiesto soltanto per Docker."
        )
    root = Path(__file__).resolve().parent.parent
    os.umask(0o077)
    try:
        with (root / ".update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpdateError("Un altro aggiornamento è già in corso.") from None
            Updater(root, release, branch=branch, backup=args.backup).execute()
    except (UpdateError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Aggiornamento non completato: {error}\n")
    except KeyboardInterrupt:
        parser.exit(
            130, "Aggiornamento interrotto. Controlla il rapporto prima di riprovare.\n"
        )


if __name__ == "__main__":
    main()
