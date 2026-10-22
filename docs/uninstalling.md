# Uninstalling Julie

Run these commands from the installation directory as the user who installed Julie. The script requests sudo access only for Docker when necessary. It does not create an automatic backup.

## Remove services and keep the station

```sh
./uninstall.sh
```

Review the installation path and project name, then enter the requested confirmation. This stops the stream and removes this installation's containers and Compose networks. The database volume, audio, exported backups and local configuration remain available. Run `./install.sh` again to start the same station.

## Start again with an empty station

```sh
./uninstall.sh --purge
```

This also permanently removes the managed database volumes, audio directory, local backup directory, generated configuration and initial access file. The source code and Git history remain, so `./install.sh` can create a new station afterwards.

To remove the source checkout too, run the following from outside that directory:

```sh
cd ~
/path/to/julie/uninstall.sh --purge --remove-directory
```

`--remove-directory` deletes the entire installation folder, including any additional files saved inside it. Copies stored outside that folder remain untouched. A new installation then requires cloning the repository again.

## Inspect before removing

Add `--dry-run` to inspect the plan without stopping or deleting anything:

```sh
./uninstall.sh --purge --dry-run
```

For an intentionally unattended run, `--yes` confirms the selected mode without the typed prompt. Use it only when the path and options have already been checked.

The script verifies the Compose project, container working directory, volume labels and shared-resource usage before removal, and checks the inventory again immediately before executing it. It refuses redirected local paths and mounted filesystems in directories selected for deletion. Resources declared external in Compose are not deleted. An error interrupts the remaining steps; inspect the reported state before retrying.

Docker itself, cached images, the operating system, network settings, SSH access and other Docker projects are preserved. No Docker-wide prune or cleanup command is used.
