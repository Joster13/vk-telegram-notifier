#!/bin/sh
set -eu
mkdir -p /app/data /tmp/max2tg-logs
# Bothost persists /app/data across deployments; fix volume ownership
# before dropping root privileges.
chown -R notifier:notifier /app/data /tmp/max2tg-logs
exec gosu notifier "$@"
