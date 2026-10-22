"""Keep audience polling independent from scheduling and audio processing."""

import fcntl
import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from library.listening import collect
from library.storage import data_path


class Command(BaseCommand):
    help = "Collect aggregate listening statistics every 15 seconds."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        directory = data_path("locks")
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "listening.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                close_old_connections()
                try:
                    collect()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Listening collection failed; will retry"
                    )
                if options["once"]:
                    return
                time.sleep(15)
