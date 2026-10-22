import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from library import publication, reprocessing
from library.backup_guard import backup_requested, data_gate
from library.playout import safe_publish
from library.removal import recover_removals
from library.worker import recover_interrupted, run_one


class Command(BaseCommand):
    help = "Prepare queued audio assets, one at a time."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO)
        while True:
            close_old_connections()
            processed = False
            with data_gate() as handle:
                if handle is not None and not backup_requested():
                    recover_removals()
                    recover_interrupted()
                    publication.recovery()
                    reprocessing.recover()
                    reprocessing.cleanup_previous()
                    processed = publication.run_one()
                    if not processed:
                        processed = run_one()
                    if not processed:
                        processed = reprocessing.run_one()
                    safe_publish()
            if options["once"]:
                return
            if not processed:
                time.sleep(3)
