"""First administrator creation requires a secret held by the server operator."""

import hashlib
import os
import secrets
from typing import ClassVar

from django import forms
from django.contrib.auth import get_user_model, login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .audio_settings import AudioPreferencesForm
from .models import Installation, RadioSettings
from .site_config import PublicAddressesForm, station
from .storage import data_path


def configured():
    return (
        Installation.objects.filter(pk=1, completed_at__isnull=False).exists()
        or get_user_model().objects.exists()
    )


def prepare():
    with transaction.atomic():
        state, _ = Installation.objects.get_or_create(pk=1)
        state = Installation.objects.select_for_update().get(pk=1)
        if configured():
            return None
        if state.setup_digest and data_path("installation-code.txt").is_file():
            return data_path("installation-code.txt")
        token = secrets.token_urlsafe(32)
        path = data_path("installation-code.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replacing an interrupted preparation is safe while the singleton is locked.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(token + "\n")
            output.flush()
            os.fsync(output.fileno())
        state.setup_digest = hashlib.sha256(token.encode()).hexdigest()
        state.save(update_fields=["setup_digest"])
        return path


class SetupForm(PublicAddressesForm, AudioPreferencesForm):
    field_order: ClassVar = [
        "code",
        "radio_name",
        "username",
        "password",
        "password_repeat",
        "start",
        "audio_target_lufs",
        "audio_ceiling_dbtp",
        "stream_url",
        "api_url",
        "website_url",
        "public_origins",
    ]
    code = forms.CharField(
        label="Codice di installazione", widget=forms.PasswordInput, max_length=100
    )
    radio_name = forms.CharField(
        label="Nome della radio", max_length=100, initial="Julie"
    )
    username = forms.CharField(label="Nome utente", max_length=150)
    password = forms.CharField(
        label="Password", widget=forms.PasswordInput, strip=False
    )
    password_repeat = forms.CharField(
        label="Ripeti la password", widget=forms.PasswordInput, strip=False
    )
    start = forms.ChoiceField(
        label="Come vuoi iniziare?",
        choices=[("empty", "Parti da zero"), ("restore", "Importa un backup completo")],
    )

    def clean_username(self):
        name = self.cleaned_data["username"]
        field = get_user_model()._meta.get_field("username")
        field.run_validators(name)
        if get_user_model().objects.filter(username=name).exists():
            raise ValidationError("Questo nome utente è già utilizzato.")
        return name

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            validate_password(
                password, get_user_model()(username=cleaned.get("username", ""))
            )
        if password != cleaned.get("password_repeat"):
            self.add_error("password_repeat", "Le password non coincidono.")
        return cleaned


@never_cache
@sensitive_post_parameters("code", "password", "password_repeat")
@require_http_methods(["GET", "POST"])
def setup(request):
    if configured():
        return redirect("login")
    form = SetupForm(request.POST or None, instance=station() or Installation(pk=1))
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            state, _ = Installation.objects.get_or_create(pk=1)
            state = Installation.objects.select_for_update().get(pk=1)
            if configured():
                return redirect("login")
            digest = hashlib.sha256(form.cleaned_data["code"].encode()).hexdigest()
            if not state.setup_digest or not secrets.compare_digest(
                digest, state.setup_digest
            ):
                form.add_error("code", "Codice di installazione non valido.")
            else:
                user = get_user_model().objects.create_superuser(
                    username=form.cleaned_data["username"],
                    password=form.cleaned_data["password"],
                )
                for field in PublicAddressesForm.Meta.fields:
                    setattr(state, field, form.cleaned_data[field])
                RadioSettings.objects.update_or_create(
                    pk=1,
                    defaults={
                        name: form.cleaned_data[name]
                        for name in ("audio_target_lufs", "audio_ceiling_dbtp")
                    },
                )
                state.completed_at = timezone.now()
                state.setup_digest = ""
                state.save()
        if not form.errors:
            login(request, user)
            # The consumed code has no authority even if filesystem cleanup fails.
            try:
                data_path("installation-code.txt").unlink(missing_ok=True)
            except OSError:
                pass
            return redirect(
                "library:restore"
                if form.cleaned_data["start"] == "restore"
                else "library:index"
            )
    return render(request, "registration/setup.html", {"form": form})


class InitialSetupMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in ("/", "/login/") and not configured():
            return redirect("setup")
        return self.get_response(request)


def identity(request):
    state = Installation.objects.filter(pk=1).first()
    return {"radio_name": state.radio_name if state else "Julie"}
