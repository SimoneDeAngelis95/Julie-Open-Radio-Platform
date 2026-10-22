import fcntl
import json
import logging
import os
import threading
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from library.maintenance import start_maintenance
from library.scheduler import tick
from library.storage import data_path


class Command(BaseCommand):
    help = "Run the calendar independently of audio conversion, once per second."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO)
        directory = data_path("locks")
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "scheduler.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop = threading.Event()
            maintenance = start_maintenance(stop)
            try:
                while True:
                    close_old_connections()
                    try:
                        connected = tick()
                        status = data_path("playout/scheduler-status.json")
                        status.parent.mkdir(parents=True, exist_ok=True)
                        temporary = status.with_suffix(".part")
                        temporary.write_text(
                            json.dumps({"checked": time.time(), "connected": connected})
                        )
                        os.replace(temporary, status)
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "Calendar pass failed; retrying next second"
                        )
                    time.sleep(1)
            finally:
                stop.set()
                maintenance.join(timeout=2)
