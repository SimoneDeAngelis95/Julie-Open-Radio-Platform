import io
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from .models import Installation
from .onboarding import prepare


class SetupCodeTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        override = override_settings(MEDIA_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)

    def output(self):
        output = io.StringIO()
        call_command("show_setup_code", stdout=output)
        return output.getvalue()

    def test_displays_valid_code_without_rotating_it(self):
        path = prepare()
        code = path.read_text().strip()
        digest = Installation.objects.get(pk=1).setup_digest
        self.assertIn(code, self.output())
        self.assertEqual(path.read_text().strip(), code)
        self.assertEqual(Installation.objects.get(pk=1).setup_digest, digest)

    def test_existing_account_hides_even_a_leftover_code(self):
        path = prepare()
        code = path.read_text().strip()
        get_user_model().objects.create_user("existing")
        result = self.output()
        self.assertNotIn(code, result)
        self.assertIn("già completata", result)

    def test_missing_or_invalid_code_is_not_displayed_or_regenerated(self):
        self.assertIn("non disponibile", self.output())
        self.assertFalse(Installation.objects.exists())
        path = prepare()
        path.write_text("not-the-valid-token-but-still-long-enough")
        result = self.output()
        self.assertIn("non disponibile", result)
        self.assertNotIn("not-the-valid-token", result)
        path.unlink()
        self.assertIn("non disponibile", self.output())
        self.assertFalse(path.exists())
