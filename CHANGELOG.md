# Changelog

## Unreleased

- Add administrator-only temporary transmission suspension and resume controls; skip missed podcasts and automatically forget suspension after an audio-engine restart.

- Stop automatic upload batches on partial storage failures and preserve successful files for safe retry.

- Keep checked-out application sources readable during updates while preserving private backup permissions; normalize source readability inside the dashboard image.

- Read decoded duration from FFmpeg’s completed progress report, accepting unavailable startup timestamps without accepting an incomplete final report.

- Dismiss upload exit protection before the successful redirect to avoid a false unsaved-changes warning.

- Refresh the host resource overview with distinct metric tiles, CPU/RAM meters and explicit freshness indicators.

- Add a scoped uninstaller with data-preserving, purge and whole-directory removal modes, dry-run inspection and explicit confirmation.

- Editable audio normalization preferences in initial setup and Configuration, with an explicitly confirmed background library reprocessing queue that preserves available audio and Replay versions.

- Show the existing installation code and panel address at the end of first-run installation.

- Drag-and-drop audio selection and automatic upload batches, preserving per-file metadata and playlist choices.
- Read-only Linux host CPU, memory, uptime and temperature monitoring on the station status page.
- Audio preparation verifies decoded duration instead of trusting container duration estimates.
- Host monitoring changes the Compose configuration and requires a manual migration on existing installations.

## 0.1.0-beta.1 · initial release preparation

- Audio library, editorial metadata, playlists and jingles.
- Scheduled podcasts, recurrences and monthly calendar.
- Live input with priority over scheduled content.
- Public Replay and calendar APIs.
- Listener history and station status.
- Full backups, restore and reset with safety copies.
- Release updater with verified full backup, preserved data mounts and failure recovery.
- First-run setup and configurable public addresses.
- Docker Compose package and documentation for a new Linux installation.

This is the first beta. Clean installation, extended operation and recovery after interruptions still need complete-system validation. It does not include a public website, live recording, playlist time slots or a multilingual interface.
