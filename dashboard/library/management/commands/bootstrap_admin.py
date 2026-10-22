import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from library.onboarding import configured, prepare


class Command(BaseCommand):
    help = "Prepare first-run setup; retain existing administrators and legacy installations."

    def handle(self, *args, **options):
        if configured():
            self.stdout.write("Amministratore esistente conservato.")
            return
        # Explicit legacy credentials remain supported for unattended installations.
        password = os.environ.get("JULIE_ADMIN_PASSWORD", "")
        if password:
            get_user_model().objects.create_superuser(
                username=os.environ.get("JULIE_ADMIN_USERNAME", "admin"),
                password=password,
                email="",
            )
            self.stdout.write("Amministratore creato.")
            return
        prepare()
        self.stdout.write(
            "Apri /setup/ nel pannello. Codice nel file audio/library/installation-code.txt sul server."
        )
