"""Reset only station content, behind a frozen library and a verified full backup.

No Docker access, shell commands, or user supplied paths. A failed destructive pass
keeps the gate closed and the output stopped; the same job can be retried safely.
"""

import fcntl
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import backups, engine
from .backup_guard import data_gate, reset_requested
from .models import (
    Asset,
    AudioReprocessJob,
    BackupJob,
    Broadcast,
    BroadcastSeries,
    ListeningSample,
    Playlist,
    RadioSettings,
    RadioSnapshot,
    ResetJob,
    RestoreJob,
)
from .storage import data_path

ACTIVE = ["preparing", "ready", "queued", "running", "failed"]
logger = logging.getLogger(__name__)


class ResetError(Exception):
    pass


def marker():
    return data_path("locks/reset-requested")


def stopped_marker():
    return data_path("locks/radio-stopped")


def set_marker(job):
    path = marker()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(job.id))


def prepare():
    with data_gate(exclusive=True) as gate:
        if gate is None:
            raise ResetError(
                "Un’elaborazione è in corso. Attendi che termini e riprova."
            )
        if (
            reset_requested()
            or ResetJob.objects.filter(status__in=ACTIVE).exists()
            or RestoreJob.objects.filter(
                status__in=[
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
            ).exists()
        ):
            raise ResetError("È già presente una richiesta di reset.")
        try:
            with transaction.atomic():
                backup = backups.enqueue("full")
                job = ResetJob.objects.create(
                    backup=backup, message="Preparo il backup completo"
                )
                set_marker(job)
        except Exception:
            # No other reset can enter while the exclusive gate is held.
            marker().unlink(missing_ok=True)
            raise
    return job


def cancel(job_id):
    with data_gate(exclusive=True) as gate:
        if gate is None:
            raise ResetError("Il backup sta acquisendo la libreria. Riprova fra poco.")
        with transaction.atomic():
            job = ResetJob.objects.select_for_update().get(pk=job_id)
            if job.status not in ["preparing", "ready"]:
                raise ResetError("Questa richiesta non può più essere annullata.")
            job.status = "cancelled"
            job.message = "Reset annullato. Nessun contenuto cancellato."
            job.finished_at = timezone.now()
            job.save()
        marker().unlink(missing_ok=True)


def confirm(job_id, phrase, acknowledged):
    if phrase != "RESET JULIE" or not acknowledged:
        raise ResetError(
            "Scrivi RESET JULIE e conferma di avere scaricato il backup completo."
        )
    with data_gate(exclusive=True) as gate:
        if gate is None:
            raise ResetError("Un’operazione è in corso. Riprova fra poco.")
        with transaction.atomic():
            job = (
                ResetJob.objects.select_for_update()
                .select_related("backup")
                .get(pk=job_id)
            )
            if job.status not in ["ready", "failed"]:
                raise ResetError("Il reset non è pronto per la conferma.")
            backup = job.backup
            if backup.status != "ready" or not backups.archive_path(backup).is_file():
                raise ResetError(
                    "Il backup completo non è disponibile. Nessuna cancellazione avviata."
                )
            if not job.confirmed_at and (
                not backup.expires_at or backup.expires_at <= timezone.now()
            ):
                raise ResetError(
                    "Il backup è scaduto. Annulla la richiesta e prepara un nuovo backup."
                )
            set_marker(job)
            job.status = "queued"
            job.confirmed_at = job.confirmed_at or timezone.now()
            job.message = "Confermato: attendo il servizio di manutenzione"
            job.save()


def stop_output():
    engine.command("output.icecast.stop")
    if engine.command("output.icecast.status") != "off":
        raise ResetError(
            "Non posso confermare l’arresto della radio. Nessun file cancellato."
        )
    # Mark before any deletion. The operator explicitly restarts after repopulating.
    stopped_marker().parent.mkdir(parents=True, exist_ok=True)
    stopped_marker().touch()
    # Prevent dynamic prefetch from queuing an old track immediately after flush.
    empty_rotation()
    engine.command("live.stop")
    state = engine.get_state()
    if state.current:
        engine.command(f"julie.abort {state.current}")
    engine.command("podcasts.flush_and_skip")
    engine.command("music.flush_and_skip")


def clear_audio():
    """Fixed owned subtrees only. Never follow a symlink or touch config/backups/locks."""
    root = settings.MEDIA_ROOT.resolve()
    for name in ("originals", "ready", "work", "staging"):
        path = root / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            if not path.is_dir():
                raise ResetError(f"La cartella audio {name} non è valida.")
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


def empty_rotation():
    directory = data_path("playout")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "reset-rotation.part"
    temporary.write_text("0\n\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, directory / "rotation.txt")


def erase(job):
    backup = job.backup
    path = backups.archive_path(backup)
    if backup.kind != "full" or path.is_symlink() or not path.is_file():
        raise ResetError(
            "Il backup completo non è valido. Nessun contenuto cancellato."
        )
    media = settings.MEDIA_ROOT.resolve()
    protected = (
        path.resolve(),
        Path(settings.BACKUP_CONFIG_ROOT).resolve(),
        Path(settings.BACKUP_CODE_ROOT).resolve(),
    )
    if any(
        item.is_relative_to(media / name)
        for item in protected
        for name in ("originals", "ready", "work", "staging")
    ):
        raise ResetError(
            "Backup o configurazione si trovano nelle cartelle audio. Correggi la configurazione prima del reset."
        )
    if (
        backup.status != "ready"
        or not backup.sha256
        or backups.sha256(path) != backup.sha256
    ):
        raise ResetError(
            "La verifica del backup completo non è riuscita. Nessun contenuto cancellato."
        )
    with data_gate(exclusive=True) as gate:
        if gate is None:
            return False
        stop_output()
        clear_audio()
        with transaction.atomic():
            # PROTECT dependencies must be removed in this order.
            AudioReprocessJob.objects.all().delete()
            preferences = RadioSettings.objects.filter(pk=1).values("audio_target_lufs", "audio_ceiling_dbtp").first() or {}
            Broadcast.objects.all().delete()
            BroadcastSeries.objects.all().delete()
            RadioSettings.objects.all().delete()
            Playlist.objects.all().delete()
            Asset.objects.all().delete()
            ListeningSample.objects.all().delete()
            RadioSnapshot.objects.all().delete()
            RadioSettings.objects.create(pk=1, jingle_enabled=False, **preferences)
            job.status = "completed"
            job.finished_at = timezone.now()
            job.message = (
                "Radio ripulita. Carica i nuovi contenuti e riattiva la trasmissione."
            )
            job.save()
            BackupJob.objects.filter(pk=backup.pk).update(
                expires_at=timezone.now() + timedelta(hours=24)
            )
        marker().unlink(missing_ok=True)
    return True


def process_one():
    """Run in the existing backup service. A crash resumes the same confirmed job."""
    path = data_path("locks/reset-worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        job = (
            ResetJob.objects.select_related("backup").filter(status__in=ACTIVE).first()
        )
        if job is None:
            # Recovery if the process died after recording completion but before unlink.
            if (
                marker().exists()
                and ResetJob.objects.filter(
                    pk=marker().read_text().strip(),
                    status__in=["completed", "cancelled"],
                ).exists()
            ):
                marker().unlink(missing_ok=True)
            return False
        set_marker(job)
        if job.status in ["preparing", "ready"]:
            if job.backup.status in [
                "error",
                "expired",
            ] or job.created_at < timezone.now() - timedelta(hours=24):
                cancel(job.id)
                ResetJob.objects.filter(pk=job.id).update(
                    message=(
                        "Reset annullato senza cancellazioni: "
                        + (
                            job.backup.progress
                            if job.backup.status in ["error", "expired"]
                            else "conferma non ricevuta entro 24 ore."
                        )
                    )[:500]
                )
                return True
            if job.status == "preparing" and job.backup.status == "ready":
                # A browser cancellation can happen after this worker read the job.
                # Never resurrect that cancelled request with a stale model save.
                ResetJob.objects.filter(pk=job.id, status="preparing").update(
                    status="ready",
                    message="Scarica il backup completo, poi conferma il reset",
                )
            return False
        if job.status in ["queued", "running", "failed"]:
            BackupJob.objects.filter(pk=job.backup_id).update(
                expires_at=timezone.now() + timedelta(hours=24)
            )
        if job.status == "failed":
            return False  # Explicit retry is required; gate remains closed.
        job.status = "running"
        job.message = "Fermo la radio e cancello i contenuti"
        job.save()
        try:
            return erase(job)
        except Exception:
            logger.exception("Radio reset failed; content gate remains closed")
            job.status = "failed"
            job.message = "Reset sospeso. La libreria resta bloccata. Verifica regia, spazio e permessi, poi riprova dalla stessa richiesta."
            job.save()
            return False


def resume():
    with data_gate(exclusive=True) as gate:
        if gate is None or reset_requested():
            raise ResetError("Completa o annulla prima la richiesta di reset.")
        from .playout import publish_snapshot

        publish_snapshot()
        engine.command("output.icecast.start")
        if engine.command("output.icecast.status") != "on":
            raise ResetError("La radio non conferma la ripartenza. Riprova.")
        stopped_marker().unlink(missing_ok=True)
