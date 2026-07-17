#!/usr/bin/env bash
# One-command re-runnable seed loader.
#
# Usage:
#   scripts/seed/seed.sh                  # against the local dev stack
#   COMPOSE_PROJECT=deploy \
#     COMPOSE_FILE=docker-compose.deploy.yml \
#     MYSQL_SERVICE=mysql \
#     MYSQL_ROOT_PASSWORD=... scripts/seed/seed.sh   # against a deployment
#
# Env vars (defaults tuned for docker/development-easy):
#   COMPOSE_DIR             directory containing the compose file
#   COMPOSE_FILE            compose file to use (relative to COMPOSE_DIR)
#   MYSQL_SERVICE           compose service name for the DB
#   MYSQL_ROOT_PASSWORD     root password for the DB
#   MYSQL_DATABASE          target database
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")"/../.. && pwd)"
SEED_SQL="${REPO_ROOT}/scripts/seed/seed.sql"

COMPOSE_DIR="${COMPOSE_DIR:-${REPO_ROOT}/docker/development-easy}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
MYSQL_SERVICE="${MYSQL_SERVICE:-mysql}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-root}"
MYSQL_DATABASE="${MYSQL_DATABASE:-openemr}"

if [[ ! -f "${SEED_SQL}" ]]; then
  echo "seed.sql not found at ${SEED_SQL}" >&2
  exit 1
fi

echo "Loading seed into ${MYSQL_SERVICE}/${MYSQL_DATABASE} via compose file ${COMPOSE_DIR}/${COMPOSE_FILE}"

(
  cd "${COMPOSE_DIR}"
  docker compose -f "${COMPOSE_FILE}" exec -T -e MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" \
    "${MYSQL_SERVICE}" mariadb -uroot "${MYSQL_DATABASE}"
) < "${SEED_SQL}"

echo ""
echo "Seed complete."
