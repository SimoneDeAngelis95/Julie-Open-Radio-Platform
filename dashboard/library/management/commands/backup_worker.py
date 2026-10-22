import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from library.backups import process_one
from library import reset, restore


class Command(BaseCommand):
    help = "Prepare queued administrator backups and expire temporary downloads."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            try:
                processed = restore.process_one()
                if not processed:
                    processed = reset.process_one()
                if not processed:
                    processed = process_one()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Backup service pass failed; retrying"
                )
                processed = False
            if options["once"]:
                return
            if not processed:
                time.sleep(5)
