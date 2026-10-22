"""Editorial metadata and the durable processing queue share one asset record."""

import uuid

from django.db import models


class Asset(models.Model):
    class Category(models.TextChoices):
        MUSIC = "music", "Musica"
        JINGLE = "jingles", "Jingle"
        PODCAST = "podcasts", "Podcast"

    class Status(models.TextChoices):
        PENDING = "pending", "In preparazione"
        PROCESSING = "processing", "In elaborazione"
        READY = "ready", "Pronto"
        ERROR = "error", "Errore"

    class ReplayStatus(models.TextChoices):
        PENDING = "pending", "Da preparare"
        PROCESSING = "processing", "In preparazione"
        READY = "ready", "Pronto"
        ERROR = "error", "Errore"

    description = models.TextField(blank=True, max_length=5000)
    auto_publish = models.BooleanField(default=True)
    replay_after_broadcast = models.BooleanField(default=False)
    first_broadcast_at = models.DateTimeField(null=True, blank=True)
    first_completed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    replay_status = models.CharField(
        max_length=12, choices=ReplayStatus.choices, default=ReplayStatus.PENDING
    )
    replay_error = models.CharField(max_length=250, blank=True)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=12, choices=Category.choices)
    title = models.CharField(max_length=240)
    artist = models.CharField(max_length=240, blank=True)
    composer = models.CharField(max_length=240, blank=True)
    album = models.CharField(max_length=240, blank=True)
    title_edited = models.BooleanField(default=False)
    artist_edited = models.BooleanField(default=False)
    metadata_edited = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    trashed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    copies_removed = models.BooleanField(default=False)
    removal_pending = models.BooleanField(default=False)
    purged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    original_name = models.CharField(max_length=255)
    original_path = models.CharField(max_length=350)
    prepared_path = models.CharField(max_length=350, blank=True)
    replay_path = models.CharField(max_length=350, blank=True)
    checksum = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    duration_seconds = models.FloatField(null=True, blank=True)
    loudness_lufs = models.FloatField(null=True, blank=True)
    true_peak_dbtp = models.FloatField(null=True, blank=True)
    peak_constrained = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    error_message = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["checksum", "category"],
                condition=models.Q(purged_at__isnull=True),
                name="asset_content_category_unique",
            )
        ]

    @property
    def duration_label(self):
        if self.duration_seconds is None:
            return "—"
        minutes, seconds = divmod(round(self.duration_seconds), 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def size_label(self):
        return f"{self.size_bytes / (1024 * 1024):.1f} MB"

    def __str__(self):
        return self.title


class RadioSettings(models.Model):
    """One station, one current jingle; a replacement can be prepared separately."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    selected_playlist = models.ForeignKey(
        "Playlist",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="selected_by_station",
    )
    jingle_enabled = models.BooleanField(default=True)
    jingle_every = models.PositiveSmallIntegerField(default=3)
    current_jingle = models.ForeignKey(
        Asset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_for_station",
    )
    pending_jingle = models.ForeignKey(
        Asset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pending_for_station",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="single_station_settings"
            ),
            models.CheckConstraint(
                condition=models.Q(jingle_every__gte=1, jingle_every__lte=100),
                name="valid_jingle_interval",
            ),
        ]


class Playlist(models.Model):
    name = models.CharField("Nome", max_length=100, unique=True)
    description = models.CharField("Descrizione", max_length=250, blank=True)
    assets = models.ManyToManyField(Asset, blank=True, related_name="playlists")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BroadcastSeries(models.Model):
    """Weekly rule in Rome civil time; broadcasts carry stable occurrence identities."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="series")
    starts_at = models.DateTimeField()
    weekdays = models.JSONField(default=list)
    until = models.DateField(null=True, blank=True)
    is_public = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    generated_until = models.DateField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def schedule_label(self):
        from django.utils import timezone

        names = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
        return (
            ", ".join(names[day] for day in sorted(self.weekdays))
            + " · "
            + timezone.localtime(self.starts_at).strftime("%H:%M")
        )

    class Meta:
        ordering = ["starts_at"]


class Broadcast(models.Model):
    """One appointment and its actual outcome; never replayed automatically."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Programmato"
        DISPATCHING = "dispatching", "In avvio"
        PLAYING = "playing", "In onda"
        COMPLETED = "completed", "Completato"
        INTERRUPTED = "interrupted", "Interrotto"
        SKIPPED = "skipped", "Saltato"
        ERROR = "error", "Errore"
        CANCELLED = "cancelled", "Annullato"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="broadcasts"
    )
    scheduled_at = models.DateTimeField("Data e ora", db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SCHEDULED, db_index=True
    )
    series = models.ForeignKey(
        BroadcastSeries,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="broadcasts",
    )
    occurrence_date = models.DateField(null=True, blank=True)
    is_exception = models.BooleanField(default=False)
    is_public = models.BooleanField("Mostra nel calendario pubblico", default=False)
    updated_at = models.DateTimeField(auto_now=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    engine_session = models.CharField(max_length=80, blank=True)
    outcome = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scheduled_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["series", "occurrence_date"], name="series_occurrence_unique"
            )
        ]

    @property
    def calendar_label(self):
        if self.status == self.Status.SCHEDULED:
            if self.asset.trashed_at or not self.asset.is_active:
                return "Podcast non disponibile"
            if self.asset.status in [Asset.Status.PENDING, Asset.Status.PROCESSING]:
                return "In preparazione"
            if self.asset.status == Asset.Status.ERROR:
                return "Preparazione fallita"
        return self.get_status_display()


class RemovalJob(models.Model):
    """Persist deletion intent before unlinking: a crash can safely resume the same files."""

    asset = models.OneToOneField(
        Asset, on_delete=models.CASCADE, related_name="removal_job"
    )
    paths = models.JSONField(default=list)
    delete_original = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=250, blank=True)


class RadioSnapshot(models.Model):
    """Latest observation; statistics collection never sits on the playout path."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    checked_at = models.DateTimeField()
    stream_online = models.BooleanField(null=True)
    listeners = models.PositiveIntegerField(null=True)
    title = models.CharField(max_length=500, blank=True)
    artist = models.CharField(max_length=240, blank=True)
    kind = models.CharField(max_length=30, default="Non disponibile")
    engine_online = models.BooleanField(default=False)


class ListeningSample(models.Model):
    """One aggregate observation per UTC minute. Never store listener identities."""

    minute = models.DateTimeField(unique=True)
    listeners = models.PositiveIntegerField(null=True)
    stream_online = models.BooleanField(null=True)

    class Meta:
        ordering = ["-minute"]


class BackupJob(models.Model):
    class Kind(models.TextChoices):
        DATABASE = "database", "Database"
        AUDIO = "audio", "Audio"
        CONFIG = "config", "Configurazione"
        FULL = "full", "Backup completo"

    class Status(models.TextChoices):
        QUEUED = "queued", "In attesa"
        RUNNING = "running", "In preparazione"
        READY = "ready", "Pronto"
        ERROR = "error", "Non riuscito"
        EXPIRED = "expired", "Scaduto"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.QUEUED
    )
    slot = models.PositiveSmallIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)
    progress = models.CharField(max_length=200, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["slot"],
                condition=models.Q(status__in=["queued", "running"]),
                name="one_active_backup",
            )
        ]


class ResetJob(models.Model):
    """A durable, explicitly confirmed reset; its recovery backup is never deleted here."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup = models.OneToOneField(
        BackupJob, on_delete=models.PROTECT, related_name="radio_reset"
    )
    status = models.CharField(max_length=16, default="preparing")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    message = models.CharField(max_length=500, blank=True)
    slot = models.PositiveSmallIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["slot"],
                condition=models.Q(
                    status__in=["preparing", "ready", "queued", "running", "failed"]
                ),
                name="one_active_reset",
            )
        ]


class RestoreJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=16, default="uploading")
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    message = models.CharField(max_length=500, blank=True)
    summary = models.JSONField(default=dict)
    archive_sha256 = models.CharField(max_length=64, blank=True)
    data_sha256 = models.CharField(max_length=64, blank=True)
    safety_backup = models.ForeignKey(
        BackupJob, null=True, on_delete=models.PROTECT, related_name="restores"
    )
    slot = models.PositiveSmallIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["slot"],
                condition=models.Q(
                    status__in=[
                        "uploading",
                        "checking",
                        "ready",
                        "preparing",
                        "protected",
                        "queued",
                        "running",
                        "applied",
                        "failed",
                    ]
                ),
                name="one_active_restore",
            )
        ]


class Installation(models.Model):
    """Server identity is retained when restoring station content."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    radio_name = models.CharField(max_length=100, default="Julie")
    stream_url = models.URLField(max_length=500, blank=True)
    api_url = models.URLField(max_length=500, blank=True)
    website_url = models.URLField(max_length=500, blank=True)
    public_origins = models.TextField(default="*", blank=True, max_length=4000)
    setup_digest = models.CharField(max_length=64, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
