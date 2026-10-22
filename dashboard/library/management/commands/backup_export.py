"""The four shell scripts enqueue the same jobs used by the browser."""

import time

from django.core.management.base import BaseCommand, CommandError

from library.backups import BackupError, archive_path, enqueue, process_one


class Command(BaseCommand):
    help = "Request database, audio, config or full backup and wait for the service."

    def add_arguments(self, parser):
        parser.add_argument("kind", choices=["database", "audio", "config", "full"])

    def handle(self, *args, **options):
        try:
            job = enqueue(options["kind"])
        except BackupError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"Backup richiesto: {job.id}. Puoi controllarlo anche nel pannello."
        )
        previous = ""
        while True:
            process_one()
            job.refresh_from_db()
            if job.progress != previous:
                self.stdout.write(job.progress)
                previous = job.progress
            if job.status == "ready":
                self.stdout.write(
                    f"Archivio: backups/exports/{archive_path(job).name}\nSHA-256: {job.sha256}"
                )
                return
            if job.status in ["error", "expired"]:
                raise CommandError(job.progress)
            time.sleep(2)
