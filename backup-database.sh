#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
set -eu
cd "$(dirname "$0")"
exec ./julie run --rm --no-deps dashboard-backup python manage.py backup_export database
