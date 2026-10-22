"""Station audio preferences and explicitly confirmed background reprocessing."""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.db.models import Count
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Asset, AudioReprocessItem, AudioReprocessJob, RadioSettings


def effective(config=None):
    config = config or RadioSettings.objects.filter(pk=1).first()
    return (
        config.audio_target_lufs
        if config and config.audio_target_lufs is not None
        else settings.NORMALIZE_TARGET_LUFS,
        config.audio_ceiling_dbtp
        if config and config.audio_ceiling_dbtp is not None
        else settings.NORMALIZE_CEILING_DBTP,
    )


class AudioPreferencesForm(forms.Form):
    audio_target_lufs = forms.FloatField(
        label="Volume desiderato (LUFS)",
        min_value=-30,
        max_value=-10,
        initial=-16,
        widget=forms.NumberInput(attrs={"step": "0.5"}),
        help_text="Consigliato: −16 LUFS. Valori più vicini a zero aumentano il volume percepito.",
    )
    audio_ceiling_dbtp = forms.FloatField(
        label="Limite dei picchi (dBTP)",
        min_value=-6,
        max_value=-1,
        initial=-1.5,
        widget=forms.NumberInput(attrs={"step": "0.1"}),
        help_text="Consigliato: −1,5 dBTP. I file molto dinamici possono restare sotto il volume desiderato per rispettare questo limite.",
    )


def candidates():
    return Asset.objects.filter(
        status=Asset.Status.READY,
        trashed_at__isnull=True,
        purged_at__isnull=True,
        removal_pending=False,
    ).exclude(original_path="")


def enqueue(config):
    if AudioReprocessJob.objects.filter(status="active").exists():
        raise ValueError("Una rielaborazione è già in corso.")
    target, ceiling = effective(config)
    job = AudioReprocessJob.objects.create(target_lufs=target, ceiling_dbtp=ceiling)
    AudioReprocessItem.objects.bulk_create(
        [
            AudioReprocessItem(job=job, asset=asset, title=asset.title)
            for asset in candidates().only("id", "title").iterator()
        ],
        batch_size=250,
    )
    return job


@user_passes_test(lambda user: user.is_authenticated and user.is_superuser)
@require_http_methods(["GET", "POST"])
@never_cache
def index(request):
    target, ceiling = effective()
    form = AudioPreferencesForm(
        initial={"audio_target_lufs": target, "audio_ceiling_dbtp": ceiling}
    )
    if request.method == "POST":
        form = AudioPreferencesForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                config, _ = RadioSettings.objects.get_or_create(pk=1)
                config = RadioSettings.objects.select_for_update().get(pk=1)
                if AudioReprocessJob.objects.filter(status="active").exists():
                    form.add_error(
                        None,
                        "Attendi la fine della rielaborazione prima di modificare i valori o avviarne un’altra.",
                    )
                elif request.POST.get("action") == "save":
                    for name, value in form.cleaned_data.items():
                        setattr(config, name, value)
                    config.save(update_fields=list(form.cleaned_data))
                    messages.success(
                        request,
                        "Impostazioni audio salvate. Si applicano alle prossime preparazioni; la libreria esistente non è stata modificata.",
                    )
                    return redirect("library:audio_settings")
                elif request.POST.get("action") == "reprocess":
                    if request.POST.get("confirm") != "yes":
                        form.add_error(
                            None, "Conferma la rielaborazione della libreria."
                        )
                    elif tuple(form.cleaned_data.values()) != effective(config):
                        form.add_error(
                            None,
                            "Le impostazioni sono cambiate. Ricarica la pagina prima di confermare.",
                        )
                    else:
                        enqueue(config)
                        messages.success(
                            request,
                            "Rielaborazione avviata. La radio continua a usare le copie disponibili.",
                        )
                        return redirect("library:audio_settings")
                else:
                    form.add_error(None, "Azione non valida.")
    job = AudioReprocessJob.objects.order_by("-created_at").first()
    counts = (
        dict(
            job.items.values("status")
            .annotate(total=Count("id"))
            .values_list("status", "total")
        )
        if job
        else {}
    )
    total = sum(counts.values())
    done = sum(counts.get(key, 0) for key in ("done", "error", "skipped"))
    return render(
        request,
        "maintenance/audio_settings.html",
        {
            "form": form,
            "target": target,
            "ceiling": ceiling,
            "candidate_count": candidates().count(),
            "job": job,
            "active": bool(job and job.status == "active"),
            "counts": counts,
            "total": total,
            "done": done,
            "percent": round(100 * done / total) if total else 0,
            "errors": job.items.filter(status__in=["error", "skipped"])[:20]
            if job
            else [],
        },
    )
