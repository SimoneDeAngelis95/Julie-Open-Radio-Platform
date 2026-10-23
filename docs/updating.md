# Updating Julie

Run the updater from the installation directory as the regular installation owner, not with sudo. Docker may request sudo separately.

| Command | Target | Full backup |
| --- | --- | --- |
| `./update.sh` | Latest `main` commit | No |
| `./update.sh -b` | Latest `main` commit | Yes |
| `./update.sh -d` | Latest `dev` commit | No |
| `./update.sh -d -b` | Latest `dev` commit | Yes |
| `./update.sh -t TAG` | Existing release tag | No |
| `./update.sh -t TAG -b` | Existing release tag | Yes |

Use `./update.sh --help` for options. `-d` and `-t` cannot be combined. The previous positional syntax, `./update.sh TAG`, remains accepted and now also requires `-b` to request a full backup.

`main` is the normal installation channel; `dev` contains development work. Read the release notes before updating. For a station in use, `./update.sh -b` is recommended. Without `-b`, database and audio are **not backed up**, and a database migration cannot be undone using the update recovery files alone.

Do not run `git pull` or check out the target first: the updater fetches the selected remote branch or tags, verifies the destination and pins its commit for the whole operation. The working tree must be clean. Local secrets and audio are ignored by Git and remain in place. The resulting checkout is detached; run the updater again to follow the latest remote branch.

## What it does

1. Checks the requested branch or tag, version, history, running services and data mounts. At least 512 MiB must be free to begin; image builds and an optional backup may need substantially more. Downgrades and divergent histories are refused.
2. Refuses releases that change the engine, service topology or installation scripts. Those need the manual procedure in their release notes.
3. Saves the current source history, local configuration and a recovery reference to the existing dashboard image.
4. Stops the dashboard, audio worker, scheduler, listener collector and backup worker. Liquidsoap, Icecast and PostgreSQL remain running.
5. With `-b`, creates a full backup using the installed application, copies it into a separate recovery directory and verifies its size, SHA-256 and full-backup manifest.
6. Selects the target commit, verifies that local configuration and Compose settings are unchanged, builds the dashboard image and checks the application.
7. Applies database migrations, recreates only application services and checks dashboard health and service startup.

When `-b` is selected, the full backup must be successfully verified before any source checkout, image build or database migration. Without `-b`, the same service, data-mount and configuration checks still run, but the full-backup step is skipped. An existing backup, reset, restore or incomplete update blocks the operation. Concurrent updater invocations are locked out.

If the requested source commit is already selected, the script does nothing. This is not a repair command for a partly updated installation; review any incomplete-update record and do not use manual Git commands to bypass it.

## Data and service availability

The updater does not delete audio directories, replace local `.env` files, change database volumes, run a reset or use `docker compose down -v`. Local configuration changes made in tracked files are not silently overwritten: they must be reviewed before updating.

The dashboard and background jobs are unavailable during the operation. The running audio engine continues with its existing sources and rotation; new scheduled podcasts are not dispatched while the scheduler is stopped. Some appointments may be missed. Choose a maintenance window and avoid updating close to a scheduled broadcast.

Database migrations can change stored data as required by a release. When requested, the verified full backup provides a recovery point, but it does not replace an external copy or protect against disk failure. Download a full backup to another device before important upgrades.

## Recovery files

Each attempt uses a private directory under:

```text
backups/updates/TIMESTAMP-IDENTIFIER/
```

It contains:

| File | Purpose |
| --- | --- |
| `full-backup.zip` | Verified station backup, only with `-b` |
| `full-backup.sha256` | Checksum of the recovery archive, only with `-b` |
| `local-config.tar.gz` | Local configuration, including secrets |
| `source.bundle` | Previous source history |
| `state.json` | Target, previous commit, phase and recovery image reference |
| `update.log` | Command results for diagnosis |

Source history, local configuration, the recovery image reference and the journal are retained even without `-b`; they are small recovery aids, not a full station backup.

These copies are outside the expiring browser-download directory. **They are not deleted automatically after 24 hours.** Keep them private and monitor free space. The previous Docker image also retains a `julie-update-recovery:...` tag until deliberately removed.

The `backups/updates/INCOMPLETE` file points to an attempt that has not completed or been safely rolled back. It prevents another update from proceeding without review.

## If an update fails

Before migrations start, the updater attempts to restore the previous source selection and dashboard image, then restart application services. Audio files, configuration and database contents are not replaced. If that restart cannot be confirmed, the incomplete-update marker is retained.

Once migrations have started, the updater does **not** automatically restore the database or restart older code against a potentially newer schema. It attempts to stop application services, keeps the backup and recovery information, and requires manual review. This also applies if the new dashboard fails its health check. The audio engine is not deliberately stopped by the updater.

If a one-off backup or migration command fails or times out, its named container is checked and stopped before any automatic resumption is attempted. If its state cannot be established, automatic resumption is suspended. The journal records the container name for inspection after an interrupted process.

Do not repeatedly rerun the script, delete volumes or remove `INCOMPLETE` simply to dismiss the error. Inspect the journal and logs, verify whether migrations completed, and follow the release’s recovery instructions. Reconcile code, database and service state before clearing the marker. A full backup can be restored through the supported recovery process; it must not be unpacked over a running station.

## Limits

The updater is intended for forward dashboard releases that retain the current deployment layout. Engine changes, database engine upgrades, port or volume changes and installer changes require a separate migration procedure. A source rollback alone cannot reverse database migrations.

Automated updater tests use temporary Git repositories and simulated Docker operations, including backup corruption and migration failures. A complete update between release tags must also be validated on a separate running installation before a stable release.
