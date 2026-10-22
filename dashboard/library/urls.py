from django.urls import path

from . import site_config, system_views, restore_views, reset_views, backup_views, podcast_views, public_api, schedule_api, studio_views, views

app_name = "library"
urlpatterns = [
    path("maintenance/configuration/", site_config.index, name="configuration"),
    path("maintenance/status/", system_views.index, name="system_status"),
    path("maintenance/restore/", restore_views.index, name="restore"),
    path("maintenance/reset/", reset_views.index, name="reset"),
    path("api/v1/schedule/", schedule_api.schedule, name="public_schedule"),
    path("calendar.ics", schedule_api.calendar, name="public_calendar"),
    path(
        "calendar/series/<uuid:series_id>/edit/", views.series_edit, name="series_edit"
    ),
    path(
        "calendar/series/<uuid:series_id>/cancel/",
        views.series_cancel,
        name="series_cancel",
    ),
    path("backups/", backup_views.index, name="backups"),
    path("backups/status/", backup_views.status, name="backup_status"),
    path(
        "backups/<uuid:job_id>/download/", backup_views.download, name="backup_download"
    ),
    path("on-air/", studio_views.on_air, name="on_air"),
    path("listening/", studio_views.listening, name="listening"),
    path("api/studio/status/", studio_views.status, name="studio_status"),
    path("podcasts/", podcast_views.index, name="podcasts"),
    path(
        "podcasts/<uuid:asset_id>/replay/",
        podcast_views.availability,
        name="podcast_availability",
    ),
    path("podcasts/<uuid:asset_id>/", podcast_views.edit, name="podcast_edit"),
    path(
        "podcasts/<uuid:asset_id>/private/",
        podcast_views.make_private,
        name="podcast_private",
    ),
    path("podcasts/<uuid:asset_id>/retry/", podcast_views.retry, name="podcast_retry"),
    path("api/v1/podcasts/", public_api.podcasts, name="public_podcasts"),
    path("api/v1/podcasts/<uuid:asset_id>/", public_api.podcast, name="public_podcast"),
    path(
        "api/v1/podcasts/<uuid:asset_id>/audio.mp3",
        public_api.audio,
        name="public_audio",
    ),
    path(
        "library/<uuid:asset_id>/playlists/", views.row_playlists, name="row_playlists"
    ),
    path("library/playlists/assign/", views.assign_playlists, name="assign_playlists"),
    path("trash/empty/", views.remove_files, name="empty_trash"),
    path("trash/<uuid:asset_id>/files/", views.remove_files, name="remove_files"),
    path("calendar/", views.calendar, name="calendar"),
    path(
        "calendar/<uuid:broadcast_id>/", views.broadcast_detail, name="broadcast_detail"
    ),
    path(
        "calendar/<uuid:broadcast_id>/edit/",
        views.broadcast_edit,
        name="broadcast_edit",
    ),
    path("calendar/new/", views.broadcast_create, name="broadcast_create"),
    path(
        "calendar/<uuid:broadcast_id>/cancel/",
        views.broadcast_cancel,
        name="broadcast_cancel",
    ),
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("settings/", views.radio_settings, name="settings"),
    path("trash/", views.trash, name="trash"),
    path("playlists/", views.playlists, name="playlists"),
    path("playlists/new/", views.playlist_edit, name="playlist_create"),
    path("playlists/<int:playlist_id>/", views.playlist_edit, name="playlist_edit"),
    path(
        "playlists/<int:playlist_id>/activate/",
        views.playlist_activate,
        name="playlist_activate",
    ),
    path("library/<uuid:asset_id>/trash/", views.trash_asset, name="trash_asset"),
    path("library/<uuid:asset_id>/restore/", views.restore_asset, name="restore_asset"),
    path("api/library/status/", views.status, name="status"),
    path("library/<uuid:asset_id>/", views.detail, name="detail"),
    path("library/<uuid:asset_id>/retry/", views.retry, name="retry"),
    path("library/<uuid:asset_id>/audio/", views.audio_file, name="audio"),
]
