import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from .models import Installation
from .site_config import PublicAddressesForm, public_url, stream_url


class SiteConfigurationTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = override_settings(MEDIA_ROOT=Path(temporary.name))
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_superuser("admin", password="test")
        self.client.force_login(self.user)

    def values(self, **changes):
        values = {
            "radio_name": "Radio Nuova",
            "stream_url": "https://audio.example.org/live.mp3",
            "api_url": "https://programmi.example.net",
            "website_url": "https://www.example.com",
            "public_origins": "https://www.example.com",
        }
        values.update(changes)
        return values

    def test_save_custom_domains_preserves_completed_setup_and_updates_stream(self):
        completed = timezone.now()
        Installation.objects.create(pk=1, completed_at=completed)
        self.assertRedirects(
            self.client.post("/maintenance/configuration/", self.values()),
            "/maintenance/configuration/",
        )
        state = Installation.objects.get()
        self.assertEqual(state.completed_at, completed)
        self.assertEqual(stream_url(), "https://audio.example.org/live.mp3")
        self.assertContains(
            self.client.get("/on-air/"), "https://audio.example.org/live.mp3"
        )
        self.assertContains(
            self.client.get("/maintenance/configuration/"), "Radio Nuova"
        )

    def test_get_does_not_create_configuration_and_staff_cannot_write(self):
        self.assertEqual(
            self.client.get("/maintenance/configuration/").status_code, 200
        )
        self.assertFalse(Installation.objects.exists())
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(
            self.client.post("/maintenance/configuration/", self.values()).status_code,
            302,
        )
        self.assertFalse(Installation.objects.exists())

    def test_validation_rejects_credentials_paths_and_unsafe_schemes(self):
        for changes in [
            {"api_url": "https://api.example.com/api/v1/"},
            {"stream_url": "https://user:secret@example.com/a.mp3"},
            {"stream_url": "ftp://example.com/a.mp3"},
            {"public_origins": "https://example.com/private"},
            {"public_origins": "* https://example.com"},
            {"website_url": "https://example.com/?secret=1"},
        ]:
            with self.subTest(changes=changes):
                self.assertFalse(PublicAddressesForm(self.values(**changes)).is_valid())

    def test_local_addresses_and_empty_fields_are_supported(self):
        form = PublicAddressesForm(
            self.values(
                stream_url="http://192.168.2.5:8000/stream.mp3",
                api_url="",
                website_url="",
                public_origins="",
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        with override_settings(RADIO_STREAM_URL="http://localhost:8000/stream.mp3"):
            self.assertEqual(stream_url(), "http://localhost:8000/stream.mp3")

    def test_canonical_links_and_cors_apply_to_public_calendar_and_replay(self):
        Installation.objects.create(
            api_url="https://programmi.example.net",
            public_origins="https://www.example.com",
        )
        self.assertEqual(
            public_url(RequestFactory().get("/"), "/calendar.ics"),
            "https://programmi.example.net/calendar.ics",
        )
        for path in ["/api/v1/schedule/", "/api/v1/podcasts/", "/calendar.ics"]:
            with self.subTest(path=path):
                allowed = self.client.options(
                    path, HTTP_ORIGIN="https://www.example.com"
                )
                self.assertEqual(
                    allowed["Access-Control-Allow-Origin"], "https://www.example.com"
                )
                self.assertIn("Origin", allowed["Vary"])
                denied = self.client.options(
                    path, HTTP_ORIGIN="https://intruso.example.com"
                )
                self.assertNotIn("Access-Control-Allow-Origin", denied)
                self.assertNotIn("Access-Control-Allow-Credentials", allowed)
        self.assertEqual(self.client.get("/api/v1/podcasts/").status_code, 200)

    def test_csrf_required_and_cors_never_grants_panel_access(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(
            client.post("/maintenance/configuration/", self.values()).status_code, 403
        )
        self.assertNotIn(
            "Access-Control-Allow-Origin",
            self.client.get("/maintenance/configuration/"),
        )

    @override_settings(
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
    )
    def test_proxy_https_is_recognized_and_login_uses_secure_cookies(self):
        response = self.client.get("/login/", HTTP_X_FORWARDED_PROTO="https")
        self.assertTrue(response.wsgi_request.is_secure())
        self.assertTrue(response.cookies["csrftoken"]["secure"])
        client = Client()
        response = client.post(
            "/login/",
            {"username": "admin", "password": "test"},
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertTrue(response.cookies["sessionid"]["secure"])

    def test_configured_address_does_not_allow_untrusted_host(self):
        Installation.objects.create(api_url="https://intruso.example.com")
        with override_settings(ALLOWED_HOSTS=["testserver"]):
            self.assertEqual(
                self.client.get("/login/", HTTP_HOST="intruso.example.com").status_code,
                400,
            )

    def test_first_setup_accepts_station_addresses_without_changing_server_trust(self):
        from .onboarding import prepare
        from .storage import data_path

        get_user_model().objects.all().delete()
        prepare()
        values = self.values()
        values.update(
            code=data_path("installation-code.txt").read_text().strip(),
            username="nuovaradio",
            password="Una-radio-scuola-2026!",
            password_repeat="Una-radio-scuola-2026!",
            audio_target_lufs=-16,
            audio_ceiling_dbtp=-1.5,
            start="empty",
        )
        self.assertEqual(self.client.post("/setup/", values).status_code, 302)
        self.assertEqual(
            Installation.objects.get().api_url, "https://programmi.example.net"
        )
        self.assertTrue(get_user_model().objects.get().is_superuser)

    @override_settings(SECURE_PROXY_SSL_HEADER=None)
    def test_forwarded_https_is_not_trusted_without_server_configuration(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https")
        self.assertFalse(request.is_secure())
