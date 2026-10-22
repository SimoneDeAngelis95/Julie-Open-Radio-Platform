"""Show initial access in the operator's terminal without changing setup state."""

import hashlib
import hmac
import re

from django.core.management.base import BaseCommand

from library.models import Installation
from library.onboarding import configured
from library.storage import data_path


class Command(BaseCommand):
    help = "Display the existing first-run code only while initial setup is open."

    def handle(self, *args, **options):
        if configured():
            self.stdout.write(
                "Configurazione già completata. Accedi con il tuo utente e la tua password."
            )
            return
        state = Installation.objects.filter(pk=1).first()
        try:
            with data_path("installation-code.txt").open() as source:
                code = source.read(101).strip()
        except OSError:
            code = ""
        if (
            not state
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", code)
            or not hmac.compare_digest(
                hashlib.sha256(code.encode()).hexdigest(), state.setup_digest
            )
        ):
            self.stdout.write(
                "Codice iniziale non disponibile. Consulta docs/installation.md."
            )
            return
        self.stdout.write("\nCodice di installazione: " + code)
        self.stdout.write(
            "Inseriscilo nella pagina di configurazione iniziale. Tienilo privato: serve solo al primo accesso."
        )
