# Installation

## Requirements

- A 64-bit Linux system, the recommended platform; no desktop environment is needed.
- A regular user account with SSH access and permission to use sudo.
- Python 3.9 or later, Git, Docker Engine and the Docker Compose plugin.
- Storage for original files, processed audio and backups. Capacity depends on the library.

The services use Linux containers. All container images must support the host architecture. The setup scripts require a Linux environment: installing Docker Desktop on macOS or Windows does not automatically make the installation procedure compatible. No certified minimum RAM requirement has been established.

Install Docker Engine and the Compose plugin using the [official instructions for your Linux distribution](https://docs.docker.com/engine/install/). Julie’s installer does not install Docker or download and execute third-party setup scripts. The optional HTTPS configuration requires Compose **2.24.4 or later**.

## Get the project

Clone the repository and open the resulting directory:

```sh
git clone https://github.com/SimoneDeAngelis95/Julie-Open-Radio-Platform.git
cd Julie-Open-Radio-Platform
```

For a published release, check out the tag listed on its release page.

## First local startup

For a server accessible on a local network, replace the example address with the server’s assigned IPv4 address:

```sh
./install.sh --host 192.168.10.20 --bind 192.168.10.20
```

`--host` is the hostname or address used by browsers. `--bind` is the IPv4 address of a local server interface on which to publish ports, not the router’s public IP. Without options, the installation uses localhost and can be accessed on the server or through an SSH tunnel. Do not run the whole script with sudo; it requests elevated access only for Docker commands when needed.

The installer creates secrets, directories and the Icecast configuration, builds the images, checks Liquidsoap and starts the services. It does not create a default account. Once startup completes:

1. Open `http://SERVER-ADDRESS:8010`.
2. Enter the installation code displayed at the end of the installer on the setup page.
3. Create the administrator, name the station, choose the audio loudness and peak limits, and select an empty library or a full backup import. The proposed audio values are −16 LUFS and −1.5 dBTP; they can be changed later in Configuration. A backup import restores the audio preferences stored in that backup.

The installer displays the code only while initial setup is still open. Running it again after an account exists does not display a code or reopen setup. To display a valid existing code again without reinstalling, run `./julie exec -T dashboard python manage.py show_setup_code`.

The code is also stored on the server. To read it over SSH:

```sh
cat audio/library/installation-code.txt
```

Keep this code private: it authorizes creation of the first administrator. It is invalidated after setup. Local addresses are listed in `ACCESSO-LOCALE.txt`, which is excluded from Git. The interface and some installer messages are currently in Italian.

An empty library produces silence. Upload some tracks and wait until they are ready; they will enter the music rotation.

## Manage the services

```sh
./julie ps
./julie logs --tail=100 dashboard liquidsoap
./julie stop
./julie up -d
```

Persistent services have automatic restart policies. Make sure the Docker service starts at system boot. It is normal for `dashboard-init` to exit with code zero: it applies migrations and prepares initial access.

The `julie` file in the repository root is a command script, invoked as `./julie`. It forwards arguments to Docker Compose, selects the project’s configuration files and includes the proxy when enabled. It is not an additional service and does not keep running. For example, `./julie ps` lists services and `./julie logs` displays their logs. Use it to keep configuration selection consistent across commands.

Do not use `down -v`: it deletes the PostgreSQL volume. Do not use `--remove-orphans` while migrating an existing installation.

## Data locations

Julie runs from the directory where you clone the repository and run `./install.sh`. The installer does not copy the project into a fixed system directory such as `/opt`. For example, cloning into `/home/radio/Julie-Open-Radio-Platform` places the following paths inside that directory:

| Path | Contents |
| --- | --- |
| `audio/library/originals/` | Uploaded originals |
| `audio/library/ready/` | Processed copies for playout |
| `audio/library/ready/replay/` | MP3 copies for public Replay |
| `audio/library/work/` and `audio/library/staging/` | Temporary processing and upload files |
| `backups/exports/` | Backups prepared by the application |
| `.env`, `.env.dashboard`, `config/icecast.xml` | Local configuration and credentials |

Audio and exported backups are bind-mounted host directories: they are not stored in a container’s writable layer and survive container replacement. Deleting the project directory also deletes these files. Do not delete or move it as if it contained only source code.

PostgreSQL data is separate, in the Docker named volume `julie_dashboard_db` scoped to the Compose project (normally `julie_julie_dashboard_db`). Its physical location is managed by Docker. A copy of the project directory alone is therefore not a complete station backup; use the full-backup feature to include database and audio consistently.

The application mounts `audio/library/` as `/data`; Liquidsoap reads the same files as `/audio/library/`. These container paths refer to the host directories listed above. No audio files or credentials are tracked in Git.

Choose a location with enough storage before installation. An external disk can host the project and its audio directories, provided it is mounted consistently and has the required permissions. Moving only the audio to another disk requires an explicit change to the volume configuration; editing a public URL in the dashboard does not change storage paths.

## Files and permissions

The installation owner must be able to read and write the project directory. Application processes use the UID and GID recorded in `.env` during setup. Check ownership and permissions when moving data between users or filesystems. `.env`, `.env.dashboard` and `config/icecast.xml` contain passwords and must not be published.

Running setup again with a complete configuration preserves existing secrets; it does not update saved hostnames or ports. If initial setup was interrupted and files are missing, it stops rather than overwriting them. Complete or recover that configuration before continuing. Do not delete a working station’s files to force a new installation.

For future release upgrades, use [Updating Julie](updating.md). Before allowing Internet access, follow [Configuration](configuration.md).


## Uninstall

Use `./uninstall.sh` to remove services while retaining the station, or add `--purge` to start from an empty station. See [Uninstalling Julie](uninstalling.md) before deleting data or the installation folder.
