from django import forms
from django.conf import settings

from .appointments import validate_asset, validate_slot, validate_time
from .models import Asset, BackupJob, Broadcast, Playlist, RadioSettings

EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data]
        return [super(MultipleFileField, self).clean(item, initial) for item in files]


class UploadForm(forms.Form):
    playlists = forms.ModelMultipleChoiceField(
        label="Playlist per i brani",
        queryset=Playlist.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    category = forms.ChoiceField(
        label="Destinazione",
        choices=[
            (value, label)
            for value, label in Asset.Category.choices
            if value != Asset.Category.JINGLE
        ],
    )
    files = MultipleFileField(
        label="File audio",
        widget=MultipleFileInput(
            attrs={"accept": ".mp3,.wav,.flac,.m4a,.aac,.ogg,.opus"}
        ),
    )

    title = forms.CharField(
        label="Titolo (facoltativo)", required=False, max_length=240
    )
    artist = forms.CharField(label="Artista (facoltativo)", required=False, max_length=240)
    description = forms.CharField(
        label="Descrizione del podcast (facoltativa)",
        required=False,
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    replay_after_broadcast = forms.BooleanField(
        label="Rendi disponibile nel Replay dopo la prima messa in onda", required=False
    )

    schedule = forms.BooleanField(label="Programma la trasmissione", required=False)
    scheduled_at = forms.DateTimeField(
        label="Data e ora della trasmissione",
        required=False,
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
        ),
    )

    def clean(self):
        data = super().clean()
        if data.get("category") != Asset.Category.PODCAST:
            data["replay_after_broadcast"] = False
        if data.get("playlists") and data.get("category") != Asset.Category.MUSIC:
            self.add_error(
                "playlists", "Le playlist sono disponibili soltanto per la musica."
            )
        files = data.get("files") or []
        if len(files) > 1 and (data.get("title") or data.get("artist") or data.get("description")):
            raise forms.ValidationError(
                "Per più file usa i campi accanto a ciascun file, oppure caricali singolarmente."
            )
        edits = []
        for index, item in enumerate(files):
            title = self.data.get(
                f"file_title_{index}", data.get("title", "") if len(files) == 1 else ""
            ).strip()
            artist = self.data.get(
                f"file_artist_{index}", data.get("artist", "") if len(files) == 1 else ""
            ).strip()
            description = self.data.get(
                f"file_description_{index}",
                data.get("description", "") if len(files) == 1 else "",
            ).strip()
            if len(title) > 240 or len(artist) > 240 or len(description) > 5000:
                raise forms.ValidationError(
                    "Titolo e artista massimo 240 caratteri; descrizione massimo 5000 caratteri."
                )
            edits.append(
                {
                    "title": title,
                    "artist": artist if data.get("category") == Asset.Category.MUSIC else "",
                    "description": description
                    if data.get("category") == Asset.Category.PODCAST
                    else "",
                }
            )
        data["file_metadata"] = edits
        if not data.get("schedule"):
            return data
        if data.get("category") != Asset.Category.PODCAST:
            raise forms.ValidationError(
                "La programmazione è disponibile soltanto per i podcast."
            )
        files = data.get("files")
        if files is not None and len(files) != 1:
            raise forms.ValidationError(
                "Per caricare e programmare scegli un podcast alla volta."
            )
        start = data.get("scheduled_at")
        if start is None:
            if "scheduled_at" not in self.errors:
                self.add_error(
                    "scheduled_at", "Scegli giorno e ora della trasmissione."
                )
        else:
            validate_slot(start)
        return data

    def clean_files(self):
        from pathlib import Path

        files = self.cleaned_data["files"]
        if len(files) > 20:
            raise forms.ValidationError("Carica al massimo 20 file alla volta.")
        for item in files:
            if not item.size:
                raise forms.ValidationError(f"{item.name}: il file è vuoto.")
            if item.size > settings.MAX_AUDIO_BYTES:
                raise forms.ValidationError(f"{item.name}: il limite è 1 GB per file.")
            if Path(item.name).suffix.lower() not in EXTENSIONS:
                raise forms.ValidationError(f"{item.name}: formato non supportato.")
        return files


class MetadataForm(forms.ModelForm):
    playlists = forms.ModelMultipleChoiceField(
        label="Playlist",
        queryset=Playlist.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Asset
        fields = ["title", "artist", "composer", "album", "description", "is_active"]
        labels = {
            "title": "Titolo",
            "artist": "Artista / esecutore",
            "composer": "Compositore",
            "album": "Album",
            "is_active": "Attivo in radio",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.category != Asset.Category.MUSIC:
            if self.instance.category == Asset.Category.PODCAST:
                self.fields["is_active"].label = "Disponibile per la programmazione"
                for name in ["artist", "composer", "album"]:
                    self.fields.pop(name, None)
            else:
                self.fields.pop("is_active", None)
            self.fields.pop("playlists", None)
        else:
            self.initial["playlists"] = self.instance.playlists.all()
        if self.instance.category != Asset.Category.PODCAST:
            self.fields.pop("description", None)
        elif "description" in self.fields:
            self.fields["description"].label = "Descrizione"
            self.fields["description"].widget = forms.Textarea(attrs={"rows": 4})


class RadioSettingsForm(forms.ModelForm):
    replacement = forms.FileField(
        label="Sostituisci il jingle",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={"accept": ".mp3,.wav,.flac,.m4a,.aac,.ogg,.opus"}
        ),
    )

    class Meta:
        model = RadioSettings
        fields = ["jingle_enabled", "jingle_every"]
        labels = {
            "jingle_enabled": "Jingle attivo",
            "jingle_every": "Riproduci ogni quanti brani?",
        }
        widgets = {"jingle_every": forms.NumberInput(attrs={"min": 1, "max": 100})}

    def clean_replacement(self):
        from pathlib import Path

        item = self.cleaned_data.get("replacement")
        if item and (
            item.size <= 0
            or item.size > settings.MAX_AUDIO_BYTES
            or Path(item.name).suffix.lower() not in EXTENSIONS
        ):
            raise forms.ValidationError("Scegli un file audio valido, massimo 1 GB.")
        return item


class PlaylistSelectionForm(forms.ModelForm):
    class Meta:
        model = RadioSettings
        fields = ["selected_playlist"]
        labels = {"selected_playlist": "Playlist da mandare in onda"}

    def clean_selected_playlist(self):
        from .playout import eligible, music_candidates

        playlist = self.cleaned_data.get("selected_playlist")
        if playlist is not None and not any(
            eligible(asset) for asset in music_candidates(playlist)
        ):
            raise forms.ValidationError(
                "Questa playlist non ha ancora brani pronti e attivi. Aggiungili o attendi la preparazione prima di usarla in radio."
            )
        return playlist

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["selected_playlist"].empty_label = "Tutta la musica attiva"


class PlaylistForm(forms.ModelForm):
    class Meta:
        model = Playlist
        fields = ["name", "description", "assets"]
        labels = {"assets": "Brani della playlist"}
        widgets = {"assets": forms.CheckboxSelectMultiple()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assets"].queryset = Asset.objects.filter(
            category=Asset.Category.MUSIC, trashed_at__isnull=True
        ).order_by("title")


class BroadcastForm(forms.ModelForm):
    scheduled_at = forms.DateTimeField(
        label="Data e ora",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
        ),
    )

    repeat = forms.ChoiceField(
        label="Ripetizione",
        choices=[("once", "Una volta"), ("weekly", "Ogni settimana")],
        required=False,
        initial="once",
    )
    weekdays = forms.MultipleChoiceField(
        label="Giorni della settimana",
        choices=[
            (str(i), day)
            for i, day in enumerate(
                [
                    "Lunedì",
                    "Martedì",
                    "Mercoledì",
                    "Giovedì",
                    "Venerdì",
                    "Sabato",
                    "Domenica",
                ]
            )
        ],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    until = forms.DateField(
        label="Fino al giorno (facoltativo)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    class Meta:
        model = Broadcast
        fields = ["asset", "scheduled_at", "is_public"]
        labels = {"asset": "Podcast"}

    def __init__(self, *args, **kwargs):
        self.editing_series = kwargs.pop("series", None)
        super().__init__(*args, **kwargs)
        candidates = Asset.objects.filter(
            category=Asset.Category.PODCAST,
            status__in=[
                Asset.Status.READY,
                Asset.Status.PENDING,
                Asset.Status.PROCESSING,
            ],
            is_active=True,
            trashed_at__isnull=True,
        )
        if not self.instance._state.adding and self.instance.asset_id:
            # Editing changes the appointment time, never its episode or past outcomes.
            candidates = Asset.objects.filter(pk=self.instance.asset_id)
            self.fields["asset"].disabled = True
        self.fields["asset"].queryset = candidates.order_by("title")
        self.fields["asset"].empty_label = "Scegli un podcast"
        if not self.instance._state.adding and self.editing_series is None:
            for name in ["repeat", "weekdays", "until"]:
                self.fields.pop(name)
        if self.editing_series:
            self.fields["asset"].disabled = True
            self.fields["repeat"].choices = [("weekly", "Ogni settimana")]

    def clean_scheduled_at(self):
        value = self.cleaned_data["scheduled_at"]
        validate_time(value)
        return value

    def clean(self):
        data = super().clean()
        asset, start = data.get("asset"), data.get("scheduled_at")
        if asset and start:
            # An existing failed preparation can be rescheduled while the user retries it.
            if not (
                not self.instance._state.adding
                and asset.status == Asset.Status.ERROR
                and not asset.trashed_at
                and asset.is_active
            ):
                validate_asset(asset)
            if data.get("repeat") == "weekly":
                from .recurrence import validate_weekly

                if asset.status != Asset.Status.READY:
                    raise forms.ValidationError(
                        "Per una serie scegli un podcast già pronto, con durata nota."
                    )
                days = [int(day) for day in data.get("weekdays", [])]
                if not days:
                    self.add_error("weekdays", "Scegli almeno un giorno.")
                elif data.get("until") and data["until"] < start.date():
                    self.add_error("until", "La fine non può precedere l’inizio.")
                else:
                    exceptions = set()
                    if self.editing_series:
                        exceptions = set(
                            self.editing_series.broadcasts.filter(
                                is_exception=True
                            ).values_list("occurrence_date", flat=True)
                        )
                    validate_weekly(
                        asset,
                        start,
                        days,
                        data.get("until"),
                        exclude_series=self.editing_series.pk
                        if self.editing_series
                        else None,
                        exceptions=exceptions,
                    )
            else:
                validate_slot(start, asset.duration_seconds, exclude=self.instance.pk)
        return data


class BulkPlaylistForm(forms.Form):
    assets = forms.ModelMultipleChoiceField(
        queryset=Asset.objects.filter(
            category=Asset.Category.MUSIC,
            trashed_at__isnull=True,
            purged_at__isnull=True,
        )
    )
    playlists = forms.ModelMultipleChoiceField(
        label="Playlist",
        queryset=Playlist.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    operation = forms.ChoiceField(
        label="Operazione",
        choices=[
            ("add", "Aggiungi alle playlist"),
            ("replace", "Sostituisci le assegnazioni"),
            ("remove", "Rimuovi dalle playlist"),
        ],
    )

    def clean_assets(self):
        assets = self.cleaned_data["assets"]
        if len(assets) > 200:
            raise forms.ValidationError("Seleziona al massimo 200 brani alla volta.")
        return assets

    def clean(self):
        data = super().clean()
        if data.get("operation") in ["add", "remove"] and not data.get("playlists"):
            self.add_error("playlists", "Scegli almeno una playlist.")
        return data


class RemovalForm(forms.Form):
    selection = forms.CharField(widget=forms.HiddenInput)
    delete_original = forms.BooleanField(
        label="Cancella anche gli originali", required=False
    )
    confirm = forms.BooleanField(
        label="Confermo l’eliminazione dei file indicati (non annullabile)"
    )


class RowPlaylistForm(forms.Form):
    playlists = forms.ModelMultipleChoiceField(
        queryset=Playlist.objects.all(), required=False
    )


class PodcastPublicationForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = ["title", "description"]
        labels = {
            "title": "Titolo",
            "description": "Descrizione",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class ReplayAvailabilityForm(forms.Form):
    availability = forms.ChoiceField(
        label="Replay",
        choices=[("available", "Disponibile"), ("unavailable", "Non disponibile")],
    )


class BackupForm(forms.Form):
    kind = forms.ChoiceField(
        label="Cosa vuoi salvare?", choices=BackupJob.Kind.choices, initial="full"
    )
