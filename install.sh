#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo 'Installa Python 3 prima di continuare.' >&2; exit 1; }
case "${1:-}" in
    -h|--help) python3 scripts/configure.py --help; exit 0 ;;
esac
command -v docker >/dev/null || { echo 'Installa Docker Engine e il plugin Compose: docs/installazione.md' >&2; exit 1; }
if ! docker info >/dev/null 2>&1; then sudo -v; fi
python3 scripts/configure.py "$@"
./julie config --quiet
./julie build dashboard icecast
./julie run --rm --no-deps liquidsoap --check /app/radio.liq
./julie up -d
./julie ps
printf '\nApri il pannello all’indirizzo indicato in ACCESSO-LOCALE.txt.\nIl codice iniziale è in audio/library/installation-code.txt.\n'
