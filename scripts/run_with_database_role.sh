#!/bin/sh
# Select exactly one least-privilege database URL and remove every other DB
# credential before application code starts. This also overwrites a legacy
# JOBNKILL_DATABASE_URL that an existing Render Blueprint might preserve.
set -eu

case "${JOBNKILL_DATABASE_ROLE:-}" in
  web)
    selected_database_url=${JOBNKILL_WEB_DATABASE_URL:-}
    required_name=JOBNKILL_WEB_DATABASE_URL
    ;;
  collector)
    selected_database_url=${JOBNKILL_COLLECTOR_DATABASE_URL:-}
    required_name=JOBNKILL_COLLECTOR_DATABASE_URL
    ;;
  cleanup)
    selected_database_url=${JOBNKILL_CLEANUP_DATABASE_URL:-}
    required_name=JOBNKILL_CLEANUP_DATABASE_URL
    ;;
  *)
    echo "JOBNKILL_DATABASE_ROLE must be web, collector, or cleanup." >&2
    exit 2
    ;;
esac

if [ -z "$selected_database_url" ]; then
  echo "$required_name is required." >&2
  exit 2
fi
case "$selected_database_url" in
  postgresql://*|postgres://*) ;;
  *)
    echo "$required_name must be a PostgreSQL URL." >&2
    exit 2
    ;;
esac

JOBNKILL_DATABASE_URL=$selected_database_url
export JOBNKILL_DATABASE_URL
unset DATABASE_URL JOBNKILL_WEB_DATABASE_URL JOBNKILL_COLLECTOR_DATABASE_URL \
  JOBNKILL_CLEANUP_DATABASE_URL JOBNKILL_ADMIN_DATABASE_URL selected_database_url

exec "$@"
