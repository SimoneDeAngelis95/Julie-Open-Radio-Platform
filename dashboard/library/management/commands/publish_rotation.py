from django.core.management.base import BaseCommand
from library.playout import publish_snapshot


class Command(BaseCommand):
    help = "Publish the verified local rotation snapshot for Liquidsoap."

    def handle(self, *args, **options):
        publish_snapshot()
        self.stdout.write("Rotazione esportata.")
