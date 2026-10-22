import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from .models import Installation
from .onboarding import prepare


class OnboardingTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(MEDIA_ROOT=Path(self.directory.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        prepare()
        self.code = (
            (Path(self.directory.name) / "installation-code.txt").read_text().strip()
        )

    def values(self, **changes):
        values = {
            "code": self.code,
            "radio_name": "Radio Scuola",
            "username": "direzione",
            "password": "Una-radio-scuola-2026!",
            "password_repeat": "Una-radio-scuola-2026!",
            "start": "empty",
            "audio_target_lufs": -16,
            "audio_ceiling_dbtp": -1.5,
        }
        values.update(changes)
        return values

    def test_empty_installation_redirects_and_health_is_available(self):
        self.assertRedirects(
            self.client.get("/login/"), "/setup/", fetch_redirect_response=False
        )
        self.assertEqual(self.client.get("/health/").status_code, 200)
        self.assertContains(self.client.get("/setup/"), "Europe/Rome")

    def test_wrong_code_and_password_cannot_create_account(self):
        self.assertContains(
            self.client.post("/setup/", self.values(code="wrong")), "non valido"
        )
        self.assertFalse(get_user_model().objects.exists())
        self.client.post("/setup/", self.values(password="123", password_repeat="123"))
        self.assertFalse(get_user_model().objects.exists())

    def test_success_consumes_code_logs_in_and_preserves_identity(self):
        self.assertRedirects(
            self.client.post("/setup/", self.values()),
            "/",
            fetch_redirect_response=False,
        )
        user = get_user_model().objects.get()
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(self.values()["password"]))
        state = Installation.objects.get()
        self.assertEqual(state.radio_name, "Radio Scuola")
        self.assertEqual(state.setup_digest, "")
        self.assertIsNotNone(state.completed_at)
        self.assertFalse((Path(self.directory.name) / "installation-code.txt").exists())
        self.assertContains(self.client.get("/"), "Radio Scuola")
        self.assertRedirects(
            self.client.post("/setup/", self.values(username="intruso")),
            "/login/",
            fetch_redirect_response=False,
        )
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_restore_choice_enters_existing_restore_flow_without_starting_it(self):
        self.assertRedirects(
            self.client.post("/setup/", self.values(start="restore")),
            "/maintenance/restore/",
            fetch_redirect_response=False,
        )
        from .models import RestoreJob

        self.assertFalse(RestoreJob.objects.exists())

    def test_existing_account_disables_setup_even_without_installation_record(self):
        get_user_model().objects.create_user(username="existing", password="old")
        Installation.objects.all().delete()
        self.assertRedirects(
            self.client.get("/setup/"), "/login/", fetch_redirect_response=False
        )
        self.assertEqual(self.client.get("/login/").status_code, 200)
        with override_settings():
            call_command("bootstrap_admin", verbosity=0)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_code_is_private_stable_and_never_rendered(self):
        prepare()
        path = Path(self.directory.name) / "installation-code.txt"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_text().strip(), self.code)
        self.assertNotContains(self.client.get("/setup/"), self.code)
        self.assertIn("no-store", self.client.get("/setup/")["Cache-Control"])

    def test_csrf_is_required(self):
        self.assertEqual(
            Client(enforce_csrf_checks=True).post("/setup/", self.values()).status_code,
            403,
        )
