#!/usr/bin/env bash
# Runs every time the codespace starts: bring up the masked hospital DB and wait until llm_reader can log in.
set -euo pipefail
cd "$(dirname "$0")/.."

for _ in $(seq 1 30); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done

docker compose up -d

for _ in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready -U llm_reader -d medical >/dev/null 2>&1; then
    echo "medical DB ready on localhost:5432"
    exit 0
  fi
  sleep 2
done
echo "medical DB did not become ready; run 'docker compose logs' to inspect" >&2
exit 1
