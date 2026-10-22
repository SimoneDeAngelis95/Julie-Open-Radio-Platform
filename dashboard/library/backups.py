"""Coordinated exports: pin immutable media, dump PostgreSQL, then release the gate.

No Docker socket is required. Archives contain private data and are restricted to administrators.
"""

import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .backup_guard import data_gate, reset_requested
from .models import Asset, BackupJob
from .storage import data_path


class BackupError(Exception):
    pass


class BackupBusy(BackupError):
    pass


def root():
    path = Path(settings.BACKUP_ROOT)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def secret_path(identifier):
    directory = root() / "secrets"
    directory.mkdir(exist_ok=True, mode=0o700)
    return directory / str(identifier)


def archive_path(job):
    suffix = {
        "database": ".zip",
        "audio": ".tar",
        "config": ".tar.gz",
        "full": ".zip",
    }[job.kind]
    path = root() / f"julie-{job.kind}-{job.id}{suffix}"
    # Previously generated encrypted archives retain their original download.
    legacy = path.with_name(path.name + ".gpg")
    if job.kind == "config" and not path.exists() and legacy.is_file():
        return legacy
    return path


def enqueue(kind):
    if reset_requested():
        raise BackupBusy(
            "Una richiesta di reset è in corso. Usa il backup completo collegato oppure annulla il reset."
        )
    if kind not in BackupJob.Kind.values:
        raise BackupError("Tipo di backup non valido.")
    job = BackupJob(kind=kind, progress="In attesa del servizio backup")
    try:
        with transaction.atomic():
            if BackupJob.objects.filter(status__in=["queued", "running"]).exists():
                raise BackupBusy(
                    "Un backup è già in preparazione. Attendi che termini."
                )
            job.save(force_insert=True)
    except IntegrityError as error:
        raise BackupBusy("Un backup è già in preparazione.") from error
    return job


def progress(job, message):
    BackupJob.objects.filter(pk=job.id).update(
        progress=message, updated_at=timezone.now()
    )


def run_command(arguments, *, env=None, timeout=3600):
    try:
        result = subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError(
            "Uno strumento di backup non è disponibile o ha superato il tempo previsto."
        ) from error
    if result.returncode:
        # Do not expose subprocess stderr: it can include connection details.
        raise BackupError(
            "La verifica del backup non è riuscita. Nessun archivio parziale è scaricabile."
        )
    return result.stdout


def postgres_env():
    database = connection.settings_dict
    if database["ENGINE"] != "django.db.backends.postgresql":
        raise BackupError("Il backup del database richiede PostgreSQL.")
    return {
        **os.environ,
        "PGHOST": database["HOST"],
        "PGPORT": str(database["PORT"] or 5432),
        "PGUSER": database["USER"],
        "PGPASSWORD": database["PASSWORD"],
        "PGDATABASE": database["NAME"],
    }


def dump_database(destination):
    run_command(
        [
            "pg_dump",
            "--no-password",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--file",
            str(destination),
        ],
        env=postgres_env(),
    )
    listing = run_command(["pg_restore", "--list", str(destination)])
    if b"library_asset" not in listing or not destination.is_file():
        raise BackupError("Il database esportato non contiene la libreria di Julie.")


def regular_files(directory):
    """Never follow symlinks, devices or unexpected entries in an export."""
    if directory.is_symlink():
        raise BackupError("Cartella non valida: i collegamenti non vengono seguiti.")
    if not directory.exists():
        return []
    files = []
    for parent, directories, names in os.walk(directory, followlinks=False):
        for name in directories + names:
            path = Path(parent) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise BackupError(
                    "La cartella contiene collegamenti o file speciali: backup escluso."
                )
            if stat.S_ISREG(mode):
                files.append(path)
    return sorted(files)


def pin_audio(directory):
    directory.mkdir(parents=True, mode=0o700)
    entries = []
    for category in ["originals", "ready"]:
        source_root = settings.MEDIA_ROOT.resolve() / category
        if source_root.is_symlink():
            raise BackupError("Cartella audio non valida.")
        for source in regular_files(source_root):
            relative = source.relative_to(settings.MEDIA_ROOT.resolve())
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.link(source, target, follow_symlinks=False)
            except OSError as error:
                raise BackupError(
                    "Non è stato possibile creare la fotografia audio. Nessun file originale è stato modificato."
                ) from error
            if not stat.S_ISREG(target.lstat().st_mode):
                raise BackupError("Fotografia audio non valida.")
            entries.append((str(relative), target))
    return entries


def config_entries():
    source = Path(settings.BACKUP_CONFIG_ROOT)
    required = [".env", ".env.dashboard", "compose.yaml", "compose.dashboard.yaml"]
    entries = []
    for name in required:
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise BackupError(
                "Configurazione di backup incompleta: verifica i montaggi del servizio."
            )
        entries.append((name, path))
    for folder in ["config", "icecast", "processor", "scripts"]:
        for path in regular_files(source / folder):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = path.name if folder == "scripts" else str(path.relative_to(source))
            entries.append((name, path))
    for name in ["config/radio.liq", "config/icecast.xml", "icecast/Dockerfile"]:
        if name not in {entry[0] for entry in entries}:
            raise BackupError("Manca un file necessario al ripristino della radio.")
    code = Path(settings.BACKUP_CODE_ROOT)
    if code.is_symlink() or not code.is_dir() or not (code / "manage.py").is_file():
        raise BackupError(
            "Sorgenti del pannello non disponibili: verifica il montaggio della configurazione."
        )
    for path in regular_files(code):
        relative = path.relative_to(code)
        if any(
            part in {"__pycache__", "staticfiles", ".git", ".venv", "node_modules"}
            for part in relative.parts
        ) or path.suffix in {".pyc", ".sqlite3"}:
            continue
        entries.append((f"dashboard/{relative}", path))
    return entries


def manifest(job, files):
    return {
        "format": "julie-backup-v1",
        "id": str(job.id),
        "kind": job.kind,
        "captured_at": getattr(job, "captured_at", job.started_at).isoformat(),
        "coordinated": job.kind == "full",
        "files": files,
    }


class HashReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.source.read(size)
        self.digest.update(data)
        return data


class HashWriter:
    def __init__(self, destination):
        self.destination = destination
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data):
        self.digest.update(data)
        self.size += len(data)
        return self.destination.write(data)

    def flush(self):
        self.destination.flush()


def add_bytes(archive, name, content):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(content))


def write_tar(job, destination, entries, *, compressed=False):
    hashes = []
    with tarfile.open(
        fileobj=destination, mode="w|gz" if compressed else "w|"
    ) as archive:
        for name, path in entries:
            with path.open("rb") as source:
                info = tarfile.TarInfo(name)
                info.size = os.fstat(source.fileno()).st_size
                info.mode = os.fstat(source.fileno()).st_mode & 0o777
                reader = HashReader(source)
                archive.addfile(info, reader)
                hashes.append(
                    {
                        "path": name,
                        "size": info.size,
                        "sha256": reader.digest.hexdigest(),
                    }
                )
        add_bytes(
            archive,
            "manifest.json",
            json.dumps(manifest(job, hashes), ensure_ascii=False, indent=2).encode(),
        )


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_config(job, workspace):
    destination = workspace / "configuration.tar.gz"
    with destination.open("wb") as output:
        write_tar(job, output, config_entries(), compressed=True)
    return destination


def preflight(job):
    required = 64 * 1024 * 1024
    if job.kind in ["audio", "full"]:
        for category in ["originals", "ready"]:
            required += sum(
                path.stat().st_size + 2048
                for path in regular_files(data_path(category))
            )
    if job.kind in ["database", "full"]:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            required += 2 * cursor.fetchone()[0]
    if job.kind in ["config", "full"]:
        required += 3 * sum(path.stat().st_size for _, path in config_entries())
    if shutil.disk_usage(root()).free < required:
        raise BackupError(
            "Spazio insufficiente per preparare il backup. Scarica gli archivi pronti e attendi la loro scadenza, oppure libera spazio."
        )


@contextmanager
def consistent_snapshot(job):
    marker = data_path("locks/backup-requested")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(job.id))
    try:
        started = time.monotonic()
        while True:
            with data_gate(exclusive=True) as handle:
                if handle is not None:
                    yield
                    return
            progress(job, "Attendo il completamento dell’elaborazione audio in corso")
            if time.monotonic() - started > 3600:
                raise BackupError(
                    "Elaborazione audio ancora in corso: riprova più tardi."
                )
            time.sleep(1)
    finally:
        marker.unlink(missing_ok=True)


def build(job, workspace, audio_snapshot):
    database = workspace / "database.dump"
    config = None
    audio = []
    with consistent_snapshot(job):
        job.captured_at = timezone.now()
        progress(job, "Creo la fotografia coerente della libreria")
        preflight(job)
        # These inodes are immutable in Julie: writers publish using os.replace.
        if job.kind in ["audio", "full"]:
            audio = pin_audio(audio_snapshot)
        if job.kind == "full":
            present = {name for name, _ in audio}
            for asset in (
                Asset.objects.filter(purged_at__isnull=True)
                .values("original_path", "prepared_path", "replay_path")
                .iterator()
            ):
                if any(path and path not in present for path in asset.values()):
                    raise BackupError(
                        "Alcuni file indicati nella libreria non sono presenti: verifica gli audio prima del backup completo."
                    )
        if job.kind in ["database", "full"]:
            dump_database(database)
        if job.kind in ["config", "full"]:
            config = prepare_config(job, workspace)
    progress(job, "Creo l’archivio scaricabile; la libreria è di nuovo disponibile")
    output = workspace / "archive.part"
    if job.kind == "audio":
        with output.open("wb") as destination:
            write_tar(job, destination, audio)
    elif job.kind == "config":
        os.replace(config, output)
    else:
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            files = []
            for name, path in [
                ("database.dump", database),
                ("configuration.tar.gz", config),
            ]:
                if path is not None and path.exists():
                    archive.write(path, name)
                    files.append(
                        {
                            "path": name,
                            "size": path.stat().st_size,
                            "sha256": sha256(path),
                        }
                    )
            if job.kind == "full":
                with archive.open("audio.tar", "w", force_zip64=True) as destination:
                    writer = HashWriter(destination)
                    write_tar(job, writer, audio)
                    files.append(
                        {
                            "path": "audio.tar",
                            "size": writer.size,
                            "sha256": writer.digest.hexdigest(),
                        }
                    )
            archive.writestr(
                "manifest.json",
                json.dumps(manifest(job, files), ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "README.txt",
                "Julie backup v1. Database: pg_restore. Audio: originals/ e ready/ sotto audio/library/. Configurazione: estrarre configuration.tar.gz. Gli archivi contengono credenziali: conservarli in un luogo riservato. I componenti dello stesso backup completo condividono l’ID nel manifest. Non ripristinare sopra una radio attiva.\n",
            )
    with output.open("rb") as source:
        os.fsync(source.fileno())
    return output


def fail(job, message):
    archive_path(job).unlink(missing_ok=True)
    secret_path(job.id).unlink(missing_ok=True)
    BackupJob.objects.filter(pk=job.id).update(
        status="error",
        progress=message,
        finished_at=timezone.now(),
        updated_at=timezone.now(),
    )


def process_one():
    """Run at most one export. A process lock also protects restart recovery."""
    descriptor = os.open(root() / "worker.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        cleanup()
        for interrupted in BackupJob.objects.filter(status="running"):
            fail(interrupted, "Preparazione interrotta. Crea un nuovo backup.")
            shutil.rmtree(
                data_path(f"backup-snapshots/{interrupted.id}"), ignore_errors=True
            )
        data_path("locks/backup-requested").unlink(missing_ok=True)
        snapshot_root = data_path("backup-snapshots")
        if snapshot_root.exists():
            for directory in snapshot_root.iterdir():
                try:
                    identifier = UUID(directory.name)
                except ValueError:
                    continue
                if (
                    directory.is_dir()
                    and not directory.is_symlink()
                    and BackupJob.objects.filter(pk=identifier).exists()
                ):
                    shutil.rmtree(directory)
        job = BackupJob.objects.filter(status="queued").first()
        if job is None:
            return False
        job.status = "running"
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])
        snapshot = data_path(f"backup-snapshots/{job.id}")
        try:
            # Remove credentials left by a queued request from the previous version.
            secret_path(job.id).unlink(missing_ok=True)
            with tempfile.TemporaryDirectory(
                dir=root(), prefix=f"work-{job.id}-"
            ) as temporary:
                output = build(job, Path(temporary), snapshot)
                digest = sha256(output)
                size = output.stat().st_size
                os.chmod(output, 0o600)
                os.replace(output, archive_path(job))
                finished = timezone.now()
                BackupJob.objects.filter(pk=job.id).update(
                    status="ready",
                    progress="Pronto da scaricare",
                    size_bytes=size,
                    sha256=digest,
                    finished_at=finished,
                    expires_at=finished + timedelta(hours=24),
                    updated_at=finished,
                )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, BackupError)
                else "Preparazione non riuscita. Nessun archivio parziale è scaricabile; puoi riprovare."
            )
            fail(job, message)
        finally:
            secret_path(job.id).unlink(missing_ok=True)
            shutil.rmtree(snapshot, ignore_errors=True)
        return True


def cleanup():
    for job in BackupJob.objects.filter(
        status="queued", created_at__lt=timezone.now() - timedelta(hours=24)
    ):
        fail(job, "Richiesta scaduta senza essere preparata. Crea un nuovo backup.")
    for job in (
        BackupJob.objects.filter(status="ready", expires_at__lte=timezone.now())
        .exclude(
            radio_reset__status__in=[
                "preparing",
                "ready",
                "queued",
                "running",
                "failed",
            ]
        )
        .exclude(
            restores__status__in=[
                "preparing",
                "protected",
                "queued",
                "running",
                "applied",
                "failed",
            ]
        )
    ):
        archive_path(job).unlink(missing_ok=True)
        BackupJob.objects.filter(pk=job.id).update(
            status="expired",
            progress="Archivio temporaneo scaduto",
            updated_at=timezone.now(),
        )
    # Remove only our own interrupted workspaces, while holding the worker lock.
    for directory in root().glob("work-*"):
        if directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)
