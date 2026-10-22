"""Editable public addresses, separate from trusted server/network settings."""

from typing import ClassVar
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect, render
from django.utils.cache import patch_vary_headers
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Installation


def station():
    return Installation.objects.filter(pk=1).first()


def stream_url():
    state = station()
    return (state.stream_url if state else "") or settings.RADIO_STREAM_URL


def public_url(request, path):
    state = station()
    base = (state.api_url if state else "") or settings.PUBLIC_API_URL
    return base.rstrip("/") + path if base else request.build_absolute_uri(path)


def cors(response, request):
    state = station()
    origins = state.public_origins.split() if state else ["*"]
    origin = request.headers.get("Origin", "")
    if origins == ["*"]:
        response["Access-Control-Allow-Origin"] = "*"
    else:
        patch_vary_headers(response, ["Origin"])
        if origin and origin in origins:
            response["Access-Control-Allow-Origin"] = origin
    return response


def clean_address(value, *, origin=False):
    if not value:
        return value
    parts = urlsplit(value)
    try:
        _ = parts.port
    except ValueError:
        raise forms.ValidationError("La porta dell’indirizzo non è valida.") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.query
        or (origin and parts.path not in ("", "/"))
    ):
        raise forms.ValidationError(
            "Usa un indirizzo http:// o https:// senza credenziali, parametri o frammenti."
        )
    return value.rstrip("/") if origin else value


class PublicAddressesForm(forms.ModelForm):
    class Meta:
        model = Installation
        fields = (
            "radio_name",
            "stream_url",
            "api_url",
            "website_url",
            "public_origins",
        )
        labels: ClassVar = {
            "radio_name": "Nome della radio",
            "stream_url": "Indirizzo pubblico dello stream",
            "api_url": "Indirizzo pubblico delle API",
            "website_url": "Sito della radio",
            "public_origins": "Siti autorizzati a usare API e Replay",
        }
        widgets: ClassVar = {"public_origins": forms.Textarea(attrs={"rows": 3})}

    def clean_stream_url(self):
        return clean_address(self.cleaned_data["stream_url"])

    def clean_api_url(self):
        return clean_address(self.cleaned_data["api_url"], origin=True)

    def clean_website_url(self):
        return clean_address(self.cleaned_data["website_url"], origin=True)

    def clean_public_origins(self):
        values = self.cleaned_data["public_origins"].split()
        if "*" in values:
            if values != ["*"]:
                raise forms.ValidationError(
                    "Usa * da solo oppure elenca i siti autorizzati."
                )
            return "*"
        cleaned = []
        for value in values:
            forms.URLField().clean(value)
            cleaned.append(clean_address(value, origin=True))
        return "\n".join(dict.fromkeys(cleaned))


@user_passes_test(lambda user: user.is_authenticated and user.is_superuser)
@require_http_methods(["GET", "POST"])
@never_cache
def index(request):
    state = station() or Installation(pk=1)
    form = PublicAddressesForm(
        request.POST if request.method == "POST" else None, instance=state
    )
    if form.is_valid() and request.method == "POST":
        # Update only editorial fields: setup state must never be reopened or overwritten.
        Installation.objects.update_or_create(pk=1, defaults=form.cleaned_data)
        messages.success(request, "Configurazione salvata.")
        return redirect("library:configuration")
    return render(
        request,
        "maintenance/configuration.html",
        {
            "form": form,
            "effective_stream": stream_url(),
            "regia_url": settings.REGIA_URL
            or request.build_absolute_uri("/").rstrip("/"),
        },
    )
