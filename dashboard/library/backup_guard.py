"""One shared filesystem gate for uploads, mutations and the audio worker."""

import fcntl
from contextlib import contextmanager

from django.http import HttpResponse, JsonResponse

from .storage import data_path


@contextmanager
def data_gate(exclusive=False, blocking=False):
    path = data_path("locks/backup-data.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    acquired = False
    try:
        try:
            fcntl.flock(handle, operation | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except BlockingIOError:
            pass
        yield handle if acquired else None
    finally:
        handle.close()


def reset_requested():
    return (
        data_path("locks/reset-requested").exists()
        or data_path("locks/restore-requested").exists()
    )


def backup_requested():
    return data_path("locks/backup-requested").exists() or reset_requested()


class BackupGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        excluded = request.path.startswith(
            (
                "/login/",
                "/logout/",
                "/backups/",
                "/maintenance/reset/",
                "/maintenance/restore/",
            )
        )
        audio_read = request.method in ["GET", "HEAD"] and (
            (request.path.startswith("/library/") and request.path.endswith("/audio/"))
            or (
                request.path.startswith("/api/v1/podcasts/")
                and request.path.endswith("/audio.mp3")
            )
        )
        if (
            request.method in ["GET", "HEAD", "OPTIONS"] and not audio_read
        ) or excluded:
            return self.get_response(request)
        with data_gate() as handle:
            if handle is None or backup_requested():
                message = "Backup in corso: attendi la fotografia della libreria e riprova. La radio continua a trasmettere."
                if reset_requested():
                    message = "Manutenzione in corso: le modifiche sono in pausa. Apri Manutenzione per completare o annullare."
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    response = JsonResponse({"error": message}, status=503)
                else:
                    response = HttpResponse(
                        message, status=503, content_type="text/plain; charset=utf-8"
                    )
                response["Retry-After"] = "5"
                return response
            return self.get_response(request)
