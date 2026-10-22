# Changelog

## Unreleased

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
