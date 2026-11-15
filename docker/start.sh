#!/usr/bin/env bash
set -euo pipefail
umask 077

# /entrypoint.sh has already initialized and chowned /app-data. Running the
# native CLI as www-data avoids root-owned database files and Git commits.
/usr/sbin/runuser -u www-data -- /opt/venv/bin/python /opt/railway/bootstrap.py

# Credentials are only bootstrap inputs; the web processes do not need them.
unset BOOTSTRAP_ADMIN_PASSWORD BOOTSTRAP_ADMIN_EMAIL
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
