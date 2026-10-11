#!/usr/bin/env bash
set -euo pipefail

root=${1:?Deployment directory is required}
release=${2:?Release directory is required}
image=${3:?Image digest is required}
[[ $image =~ ^ghcr.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || exit 1
[[ -s "$root/.secrets/app_password" ]] || exit 1
export APP_PASSWORD_PATH="$root/.secrets/app_password"

exec 9>"$root/deploy.lock"
flock -w 600 9
previous=$(readlink -f "$root/current" || true)
printf 'TTDOWNLOADER_IMAGE=%s\n' "$image" > "$release/image.env"

compose() {
  local directory=$1
  shift
  docker compose --project-name ttdownloader --env-file "$directory/image.env" \
    -f "$directory/compose.yaml" "$@"
}

compose "$release" config --quiet
compose "$release" pull
if compose "$release" up -d --wait --wait-timeout 180; then
  ln -sfn "$release" "$root/current"
  if [[ -f "$previous/compose.yaml" && $previous != "$release" ]]; then
    ln -sfn "$previous" "$root/previous"
  fi
  echo 'Deployment healthy.'
else
  compose "$release" logs --tail 80 || true
  if [[ -f "$previous/compose.yaml" ]]; then
    echo 'Deployment failed; restoring the previous release.' >&2
    compose "$previous" up -d --wait --wait-timeout 180
  else
    echo 'First deployment failed; no previous release exists.' >&2
    compose "$release" down
  fi
  exit 1
fi
