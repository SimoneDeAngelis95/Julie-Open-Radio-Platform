# Backup and restore

## Create a backup

**Manutenzione → Backup** is reserved for administrators. Available types are database, audio, configuration and **Backup completo** (full backup). Archives are prepared in the background and downloaded through the browser.

A full backup coordinates database and audio in one snapshot. Three separate archives taken at different times are not necessarily consistent. Use a full backup for migration or restoration.

Archives contain private data and are not encrypted. Store them securely, including a copy outside the server. Downloads normally expire after 24 hours; copies needed by an active reset or restore are retained longer. A temporary copy on the same server is not a substitute for an external backup.

Command-line alternatives:

```sh
./backup-database.sh
./backup-audio.sh
./backup-config.sh
./backup-full.sh
```

These commands use the same backup services as the dashboard. Creating a backup does not restart the station. Content changes may pause briefly while the snapshot is taken. Audio and full backups require enough free space on the destination filesystem.

## Restore

**Manutenzione → Ripristina backup** accepts a **Julie full-backup ZIP**:

1. Upload the archive and wait for validation.
2. Review its contents summary.
3. Prepare and download the current station’s safety backup.
4. Confirm using the phrase shown on the page.
5. Wait for completion, inspect the library and manually reactivate the station.

Restoring replaces station content and interrupts transmission. It preserves the destination installation’s accounts, passwords, server settings and public address configuration. Application code is not replaced by code contained in the archive. Dashboard restore is therefore neither a software update nor a full operating-system recovery.

Podcasts that were starting or playing in the saved state are not automatically resent. Playlists, schedules, editorial metadata, Replay availability and listener history are restored.

The upload limit defaults to 20 GiB and can be changed with `JULIE_MAX_RESTORE_BYTES`. Internal safety limits also apply: database and SQL 512 MiB, decompressed configuration 128 MiB, 100,000 audio files and two million records. Larger installations need a dedicated administrative recovery procedure rather than repeated browser attempts.

If a confirmed restore fails, keep its files and safety backup and use the recovery action provided by the page. Do not manually remove maintenance markers or delete directories to force the station to resume.

## Reset

**Manutenzione → Riparti da zero** first prepares a full safety backup. Reset requires explicit confirmation and clears content, playlists, schedules and listener history while retaining accounts and installation settings. The station remains stopped until manually reactivated.

## Recover the server

For migration to another server, prepare a fresh installation, create its administrator and import the full backup. Keep network credentials, `.env.proxy`, certificates and configuration outside the project in a separate server recovery plan. The PostgreSQL volume holds live data; it is not a disposable cache.
