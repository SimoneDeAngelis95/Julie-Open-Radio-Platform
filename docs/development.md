# Development, checks and updates

## Local environment

Python 3.13 and ffmpeg are required to run audio tests outside Docker. Create a virtual environment and install `dashboard/requirements.txt`.

```sh
python3 scripts/test_installation.py
python3 scripts/test_update.py
JULIE_TESTING=1 python3 dashboard/manage.py collectstatic --noinput
JULIE_TESTING=1 python3 dashboard/manage.py test library --noinput
JULIE_TESTING=1 python3 dashboard/manage.py makemigrations --check --dry-run
```

`JULIE_TESTING=1` uses SQLite and fast password hashing. Never enable it on a real station. Two tests require PostgreSQL and are skipped in the SQLite path. The GitHub workflow runs the suite with PostgreSQL 17, its matching client tools and ffmpeg. Check the workflow result after pushing; including the workflow is not evidence of a completed GitHub run.

Use a separate database and working directories for PostgreSQL tests. Never run tests against a production database. Restore tests verify the database name, use temporary directories and simulate engine commands.

## Validation scope

The application has automated checks for audio processing, scheduling, public access, maintenance and recovery. Installation-generator tests cover secret generation, preserving existing configuration and rejecting invalid inputs. Compose configuration can be validated in local and HTTPS modes without starting the services.

Before a stable release, complete:

- Installation from a clean clone on Linux, including volume permissions.
- Startup and full service restart.
- Extended listening, music/podcast/live transitions and return to rotation.
- Full backup and restore into a separate installation.
- Login, uploads, Replay Range/HEAD and calendar access through real HTTPS.
- Recovery after unexpected shutdowns.

Syntax checks and unit tests do not replace this complete-system validation. Package changes, including UID handling, need integrated checks on the destination system. The beta is not presented as universally validated on every computer.

## Update a station

Use `./update.sh` for main, `./update.sh -d` for dev, or `./update.sh -t RELEASE_TAG` for a specific release. Add `-b` to create and verify a full backup. The updater preserves local configuration and data mounts. It does not support downgrades or engine/topology changes. Read [Updating Julie](updating.md) for the complete process and failure recovery.

A previous source tag may not support a newer database schema. Checking out old code is not a database rollback.

## Releases

VERSION identifies the intended version: `0.1.0-beta.1`. Create the tag and GitHub release after completing the checks listed in the release notes. The main branch may include later development; install an identified version on a station server.

Docker base images are referenced by tags, so future rebuilds may include system updates. Deployments requiring exact reproducibility should also record and verify the image digests used.

The uninstaller safety checks run independently of Docker with `python scripts/test_uninstall.py`. They use only disposable directories and a simulated Docker inventory.
