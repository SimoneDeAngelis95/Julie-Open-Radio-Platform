# Architecture

Julie separates audio preparation from the transmission path.

| Service | Role |
| --- | --- |
| `icecast` | Distributes the stream to listeners |
| `liquidsoap` | Handles music, jingles, scheduled podcasts and live input |
| `dashboard-db` | PostgreSQL operational database |
| `dashboard-init` | Applies migrations and prepares first-run access |
| `dashboard` | Django/Gunicorn dashboard and APIs |
| `dashboard-worker` | Processes audio and file operations |
| `dashboard-scheduler` | Dispatches scheduled podcasts and maintains recurrences |
| `dashboard-listening` | Collects station status and listener observations |
| `dashboard-backup` | Handles backups, reset and restore |
| `proxy` | Optional Caddy HTTPS entry point |

The engine reads an atomically published rotation snapshot from disk. Continuing the current track does not require a web request or database query. Dashboard downtime does not mean every other feature remains available: new processing, scheduling and status updates still depend on their respective services.

Original audio is kept in `audio/library/originals`; playout and Replay copies are in `ready`. Processing uses ffmpeg and FLAC for playout, with default normalization at -20 LUFS and a -1.5 dBTP ceiling. The final stream is MP3 at 128 kbit/s. Audio processing settings are currently part of the technical configuration.

The internal scheduling protocol uses identifiers and event reconciliation to avoid resending a broadcast after an uncertain dispatch. Recurrences preserve Italian civil time and explicitly handle daylight saving transitions. This does not guarantee transmission under every failure condition; review outcomes in the calendar.

The database uses a dedicated Docker volume. Audio and backups are persistent host directories. Maintenance locks coordinate writes and destructive operations; do not delete them manually. Restore validates the archive, prepares a safety copy and imports only the expected application data. It does not execute arbitrary archive SQL against the live database.

Liquidsoap control is not published on host interfaces. Live input is separate from the APIs and dashboard. The HTTPS overlay restricts direct Django and Icecast ports to localhost.
