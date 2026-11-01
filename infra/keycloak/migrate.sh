#!/bin/bash
# moves the realms from the h2 file keycloak's development mode used into postgresql, once: learning, whose
# users keep the ids the services store as the token's subject, and master, with its admin. the h2 file is
# left in place.
set -euo pipefail

data=/opt/keycloak/data
marker="$data/postgres-migrated"
export_dir="$data/h2-export"

if [ -f "$marker" ]; then
  exit 0
fi
if [ -f "$data/h2/keycloakdb.mv.db" ]; then
  rm -rf "$export_dir"
  env -u KC_DB_URL -u KC_DB_USERNAME -u KC_DB_PASSWORD KC_DB=dev-file \
    /opt/keycloak/bin/kc.sh export --dir "$export_dir" --users realm_file
  # replaces a realm that an earlier start may have imported from the seed file, with new ids.
  /opt/keycloak/bin/kc.sh import --dir "$export_dir" --override true
  # the export holds password hashes and client secrets.
  rm -rf "$export_dir"
fi
touch "$marker"
