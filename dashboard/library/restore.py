"""A staged restore with a durable filesystem journal and an independent safety backup."""

import fcntl
import json
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import backups, reset, restore_data
from .backup_guard import data_gate, reset_requested
from .models import BackupJob, ResetJob, RestoreJob
from .restore_archive import RestoreError, inspect_archive
from .storage import data_path

ACTIVE = [
    "uploading",
    "checking",
    "ready",
    "preparing",
    "protected",
    "queued",
    "running",
    "applied",
    "failed",
]
PINNED = ["preparing", "protected", "queued", "running", "applied", "failed"]
logger = logging.getLogger(__name__)


def workspace(job):
    return backups.root() / "restore-inputs" / str(job.id)


def media(job):
    return data_path(f"restore-work/{job.id}")


def marker():
    return data_path("locks/restore-requested")


def mark(job):
    marker().parent.mkdir(parents=True, exist_ok=True)
    marker().write_text(str(job.id))


def validate_locations():
    root = settings.MEDIA_ROOT.resolve()
    for value in (
        settings.BACKUP_ROOT,
        settings.BACKUP_CONFIG_ROOT,
        settings.BACKUP_CODE_ROOT,
    ):
        protected = Path(value).resolve()
        if any(
            protected.is_relative_to(root / name)
            for name in ("originals", "ready", "work", "staging", "restore-work")
        ):
            raise RestoreError(
                "Backup o configurazione sono nelle cartelle audio. Correggi i percorsi prima del ripristino."
            )


def receive(upload):
    validate_locations()
    with data_gate(exclusive=True) as gate:
        if (
            gate is None
            or reset_requested()
            or ResetJob.objects.filter(status__in=reset.ACTIVE).exists()
            or RestoreJob.objects.filter(status__in=ACTIVE).exists()
        ):
            raise RestoreError(
                "Un’altra manutenzione o elaborazione è in corso. Attendi o annullala."
            )
        job = RestoreJob.objects.create(message="Ricevo il backup completo")
    directory = workspace(job)
    directory.mkdir(parents=True, mode=0o700)
    try:
        total = 0
        with (directory / "archive.zip").open("xb") as output:
            for chunk in upload.chunks(chunk_size=1024 * 1024):
                total += len(chunk)
                if (
                    total > settings.MAX_RESTORE_BYTES
                    or shutil.disk_usage(directory).free
                    < len(chunk) + 256 * 1024 * 1024
                ):
                    raise RestoreError("Archivio troppo grande o spazio insufficiente.")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        RestoreJob.objects.filter(pk=job.id).update(
            status="checking", message="Verifico il backup completo"
        )
    except Exception as error:
        RestoreJob.objects.filter(pk=job.id).update(
            status="invalid",
            message="Caricamento non riuscito. Riprova con un backup completo.",
            finished_at=timezone.now(),
        )
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(error, RestoreError):
            raise
        raise RestoreError(
            "Caricamento non riuscito. Verifica lo spazio libero e riprova."
        ) from error
    return job


def validate(job):
    directory = workspace(job)
    destination = media(job) / "new"
    shutil.rmtree(media(job), ignore_errors=True)
    destination.mkdir(parents=True, mode=0o700)
    for name in ("database.dump", "database.sql", "content.sqlite"):
        (directory / name).unlink(missing_ok=True)
    summary, files = inspect_archive(directory / "archive.zip", directory, destination)
    restore_data.export_copy(directory / "database.dump", directory / "database.sql")
    counts = restore_data.validate_copy(
        directory / "database.sql", directory / "content.sqlite", files
    )
    for name in ("originals", "ready"):
        (destination / name).mkdir(exist_ok=True)
    (directory / "audio-manifest.json").write_text(json.dumps(files))
    summary["counts"] = counts
    summary["audio_manifest_sha256"] = backups.sha256(directory / "audio-manifest.json")
    job.summary = summary
    job.archive_sha256 = backups.sha256(directory / "archive.zip")
    job.data_sha256 = backups.sha256(directory / "content.sqlite")
    # Publish readiness only after all staging operations have succeeded.
    (directory / "database.sql").unlink(missing_ok=True)
    (directory / "database.dump").unlink(missing_ok=True)
    job.status = "ready"
    job.message = "Backup verificato. Puoi preparare il ripristino."
    job.save()


def protect(identifier):
    with data_gate(exclusive=True) as gate:
        if gate is None or reset_requested():
            raise RestoreError("Un’operazione è in corso. Riprova fra poco.")
        with transaction.atomic():
            job = RestoreJob.objects.select_for_update().get(pk=identifier)
            if job.status != "ready":
                raise RestoreError("Verifica prima un backup completo.")
            backup = backups.enqueue("full")
            job.safety_backup = backup
            job.status = "preparing"
            job.message = "Preparo un backup completo della radio attuale"
            job.save()
        mark(job)


def cancel(identifier):
    with data_gate(exclusive=True) as gate:
        if gate is None:
            raise RestoreError(
                "La fotografia della radio è in corso. Riprova fra poco."
            )
        with transaction.atomic():
            job = RestoreJob.objects.select_for_update().get(pk=identifier)
            if job.status not in ("ready", "preparing", "protected"):
                raise RestoreError("Questo ripristino non può più essere annullato.")
            job.status = "cancelled"
            job.finished_at = timezone.now()
            job.message = "Ripristino annullato. I contenuti attuali sono conservati."
            job.save()
        if marker().exists() and marker().read_text().strip() == str(job.id):
            marker().unlink()
    cleanup_job(job)


def confirm(identifier, phrase, downloaded):
    if phrase != "RIPRISTINA JULIE" or not downloaded:
        raise RestoreError(
            "Scrivi RIPRISTINA JULIE e conferma di avere conservato il backup della radio attuale."
        )
    with data_gate(exclusive=True) as gate:
        if gate is None:
            raise RestoreError("Un’operazione è in corso.")
        with transaction.atomic():
            job = (
                RestoreJob.objects.select_for_update(of=("self",))
                .select_related("safety_backup")
                .get(pk=identifier)
            )
            backup = job.safety_backup
            if (
                job.status not in ("protected", "failed")
                or not backup
                or backup.status != "ready"
                or not backups.archive_path(backup).is_file()
            ):
                raise RestoreError("Il backup della radio attuale non è pronto.")
            if not job.confirmed_at and (
                not backup.expires_at or backup.expires_at <= timezone.now()
            ):
                raise RestoreError(
                    "Il backup di sicurezza è scaduto. Annulla e ripeti la preparazione."
                )
            job.confirmed_at = job.confirmed_at or timezone.now()
            job.status = "queued"
            job.message = "Ripristino confermato. Attendo il servizio."
            mark(job)
            job.save()


def verify_staging(job):
    validate_locations()
    directory = workspace(job)
    if (
        backups.sha256(directory / "content.sqlite") != job.data_sha256
        or backups.sha256(directory / "archive.zip") != job.archive_sha256
    ):
        raise RestoreError("I file verificati sono cambiati. Ripristino bloccato.")
    if backups.sha256(directory / "audio-manifest.json") != job.summary.get(
        "audio_manifest_sha256"
    ):
        raise RestoreError("Il piano dei file è cambiato. Ripristino bloccato.")
    backup = job.safety_backup
    if (
        not backup
        or backup.status != "ready"
        or backups.sha256(backups.archive_path(backup)) != backup.sha256
    ):
        raise RestoreError("Il backup di sicurezza non supera la verifica.")
    files = json.loads((directory / "audio-manifest.json").read_text())
    # A restart can find the replacement already in the live tree: the journal tells us where.
    for name, (size, digest) in files.items():
        root = name.split("/")[0]
        staged = media(job) / "new" / root
        path = (
            (media(job) / "new" / name)
            if staged.exists()
            else settings.MEDIA_ROOT.resolve() / name
        )
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != size
            or backups.sha256(path) != digest
        ):
            raise RestoreError("Un audio preparato per il ripristino non è integro.")


def sync_directories(*paths):
    for path in paths:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def prepare_permissions(job):
    original_root = media(job) / "new" / "originals"
    for root, directories, files in os.walk(media(job) / "new"):
        private = Path(root).is_relative_to(original_root)
        os.chmod(root, 0o700 if private else 0o755)
        for name in files:
            os.chmod(os.path.join(root, name), 0o600 if private else 0o644)
        sync_directories(root)


def swap_forward(job):
    previous = media(job) / "previous"
    previous.mkdir(exist_ok=True)
    root = settings.MEDIA_ROOT.resolve()
    for name in ("originals", "ready"):
        new = media(job) / "new" / name
        old = previous / name
        current = root / name
        if current.is_symlink():
            raise RestoreError(
                "La cartella audio attuale è un collegamento: ripristino bloccato."
            )
        if new.exists():
            if old.exists() and current.exists():
                raise RestoreError("Stato della sostituzione audio non coerente.")
            if not old.exists():
                current.mkdir(exist_ok=True)
                if current.stat().st_dev != new.stat().st_dev:
                    raise RestoreError(
                        "Le cartelle audio devono essere sullo stesso filesystem per un ripristino atomico."
                    )
                os.replace(current, old)
            os.replace(new, current)
            sync_directories(root, previous, media(job) / "new")
        elif not current.is_dir() or not old.is_dir():
            raise RestoreError("Non posso ricostruire lo stato del ripristino audio.")


def rollback_files(job):
    root = settings.MEDIA_ROOT.resolve()
    for name in ("ready", "originals"):
        old = media(job) / "previous" / name
        new = media(job) / "new" / name
        current = root / name
        if old.exists():
            if current.exists():
                if new.exists():
                    raise RestoreError(
                        "Ripristino audio ambiguo: le copie vengono conservate."
                    )
                os.replace(current, new)
            os.replace(old, current)
            sync_directories(root, media(job) / "previous", media(job) / "new")


def finalize(job):
    from .playout import publish_snapshot

    restore_data.reset_sequences()
    publish_snapshot()
    with transaction.atomic():
        job.status = "completed"
        job.finished_at = timezone.now()
        job.message = "Ripristino completato. I contenuti del backup sono disponibili."
        job.save()
        BackupJob.objects.filter(pk=job.safety_backup_id).update(
            expires_at=timezone.now() + timedelta(hours=24)
        )
    marker().unlink(missing_ok=True)
    cleanup_job(job)


def apply(job):
    verify_staging(job)
    prepare_permissions(job)
    with data_gate(exclusive=True) as gate:
        if gate is None:
            return False
        reset.stop_output()
        try:
            swap_forward(job)
            with transaction.atomic():
                restore_data.clear_content()
                restore_data.import_content(workspace(job) / "content.sqlite")
                job.status = "applied"
                job.save()
        except Exception:
            # DB rollback precedes file rollback. No committed restored DB may point at old audio.
            job.refresh_from_db()
            if job.status != "applied":
                rollback_files(job)
            raise
        finalize(job)
    return True


def cleanup_job(job):
    shutil.rmtree(media(job), ignore_errors=True)
    shutil.rmtree(workspace(job), ignore_errors=True)


def process_one():
    path = data_path("locks/restore-worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        job = (
            RestoreJob.objects.select_related("safety_backup")
            .filter(status__in=ACTIVE)
            .first()
        )
        if job is None:
            if marker().exists():
                terminal = RestoreJob.objects.filter(
                    pk=marker().read_text().strip(),
                    status__in=["completed", "cancelled"],
                ).first()
                if terminal and terminal.status == "completed":
                    with data_gate(exclusive=True) as gate:
                        if gate is not None:
                            finalize(terminal)
                elif terminal:
                    marker().unlink(missing_ok=True)
            # A crash during non-critical cleanup must not turn success into a new import.
            for terminal in RestoreJob.objects.filter(
                status__in=["completed", "cancelled", "invalid"]
            )[:20]:
                if media(terminal).exists() or workspace(terminal).exists():
                    cleanup_job(terminal)
            return False
        try:
            if job.status == "uploading":
                if job.created_at < timezone.now() - timedelta(hours=2):
                    job.status = "invalid"
                    job.message = "Caricamento interrotto. Riprova."
                    job.save()
                    cleanup_job(job)
                return False
            if job.status == "checking":
                validate(job)
                return True
            if job.status == "ready":
                if job.created_at < timezone.now() - timedelta(hours=24):
                    cancel(job.id)
                return False
            mark(job)
            if job.status in ("preparing", "protected"):
                backup = job.safety_backup
                if backup.status in (
                    "error",
                    "expired",
                ) or job.created_at < timezone.now() - timedelta(hours=24):
                    cancel(job.id)
                    return True
                if job.status == "preparing" and backup.status == "ready":
                    RestoreJob.objects.filter(pk=job.id, status="preparing").update(
                        status="protected",
                        message="Scarica il backup della radio attuale, poi conferma il ripristino",
                    )
                return False
            BackupJob.objects.filter(pk=job.safety_backup_id).update(
                expires_at=timezone.now() + timedelta(hours=24)
            )
            if job.status == "failed":
                return False
            if job.status == "applied":
                with data_gate(exclusive=True) as gate:
                    if gate is None:
                        return False
                    finalize(job)
                return True
            job.status = "running"
            job.message = "Ripristino in corso"
            job.save()
            return apply(job)
        except Exception as error:
            logger.exception("Restore pass failed; keeping current and staged copies")
            job.refresh_from_db()
            if job.status in ("applied", "completed"):
                return (
                    False  # Database commit succeeded: only finalize on the next pass.
                )
            if job.confirmed_at:
                job.status = "failed"
                job.message = "Ripristino sospeso. Copie e backup conservati; controlla lo stato del servizio e riprova dalla stessa richiesta."
                mark(job)
            else:
                job.status = "invalid"
                job.finished_at = timezone.now()
                job.message = (
                    str(error)[:500]
                    if isinstance(error, RestoreError)
                    else "Archivio non valido, incompleto o non compatibile. Nessun contenuto attuale modificato."
                )
                marker().unlink(missing_ok=True)
                cleanup_job(job)
            job.save()
            return False
