# Using the station

The application interface is currently in Italian. Menu and button names below match the interface.

## Library and music

Upload one or more files from **Libreria**, choosing the correct category. Music uploads accept a title and artist; podcasts accept a title and description. Manually entered metadata is preserved during audio processing. Available formats depend on the installed ffmpeg decoders; use valid audio files and check that processing succeeds.

Julie keeps the original and prepares a copy for playout. Only ready, active music outside the trash can enter the rotation. Failed files remain available for inspection and retry.

Playlists organize tracks without duplicating files. Assign them during upload or on each track’s library row. On **Playlist**, choose which collection feeds the random rotation, or use all active music. Playlist scheduling by time of day is not implemented.

## Jingles

**Jingle** selects a jingle and the interval between insertions, measured in music tracks. This is part of the music rotation, not an exact clock schedule.

## Podcasts and calendar

Upload a podcast once and schedule it multiple times. On **Palinsesto**, select a date to create an appointment, or open an event to edit or cancel it. Weekly recurrences and single-occurrence exceptions are supported.

Scheduling checks prevent overlapping podcasts, accounting for duration and transition margin. Missed appointments are not automatically broadcast late. Past broadcasts are available through the calendar.

Visibility in the public calendar and availability in Replay are separate settings. A podcast can be made available on demand before it has ever aired. The option to publish after its first broadcast waits for an actual completed broadcast.

## Live broadcasts

Priority is **live input → scheduled podcast → music**. Live input uses Liquidsoap on internal port 8001, mount `live`, with the installation’s `JULIE_LIVE_PASSWORD`. In BUTT, select the Icecast server type and use the authorized network’s connection details. This internal connection does not automatically use the public website’s TLS certificate.

A live connection interrupts and discards the current podcast; it does not resume from the interrupted position. Music returns when the live connection ends. Automatic recording of live broadcasts is not implemented.

## On air, listeners and station status

**In onda** shows the latest observation and includes an audio player. Listener counts represent stream connections, not unique people. The dashboard player also counts as a connection when playing.

**Ascolti** provides history with a year filter and pagination in groups of twenty days. Missing observations remain unknown; they are not converted into zero listeners.

**Manutenzione → Stato della radio** brings together transmission status, available storage, backups and issues to review. Old observations are marked as outdated; a missing observation alone does not mean audio has stopped.

## Trash

**Cestino** excludes files while allowing recovery. Permanent deletion requires an explicit action and may also remove the original. Read the confirmation before freeing space. To clear the whole station, see [Backup and restore](backup.md).
