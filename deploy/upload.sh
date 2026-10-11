#!/usr/bin/env bash
# Remote command arguments are expanded locally after the allowlist checks below.
# shellcheck disable=SC2029
set -euo pipefail

: "${SSH_PRIVATE_KEY:?Set VPS_SSH_KEY}"
: "${SSH_KNOWN_HOSTS:?Set VPS_KNOWN_HOSTS}"
[[ $VPS_HOST =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || exit 1
[[ $VPS_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || exit 1
[[ $VPS_PORT =~ ^[0-9]+$ ]] || exit 1
[[ $DEPLOY_PATH =~ ^/[a-zA-Z0-9_/-]+$ && $DEPLOY_PATH != / ]] || exit 1
[[ $IMAGE =~ ^ghcr.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || exit 1
[[ $GITHUB_RUN_ID =~ ^[0-9]+$ && $GITHUB_RUN_ATTEMPT =~ ^[0-9]+$ ]] || exit 1

ssh_dir=$(mktemp -d)
trap 'rm -rf "$ssh_dir"' EXIT
printf '%s\n' "$SSH_PRIVATE_KEY" > "$ssh_dir/key"
printf '%s\n' "$SSH_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
chmod 600 "$ssh_dir/key" "$ssh_dir/known_hosts"
ssh_options=(-i "$ssh_dir/key" -p "$VPS_PORT" -o BatchMode=yes
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$ssh_dir/known_hosts" -o ConnectTimeout=15
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
target="$VPS_USER@$VPS_HOST"
release="$DEPLOY_PATH/releases/$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"

tar -C deploy -cf - compose.yaml deploy.sh | ssh "${ssh_options[@]}" "$target" \
  "umask 077; mkdir -p '$release' && tar -xf - -C '$release'"
ssh "${ssh_options[@]}" "$target" \
  "bash '$release/deploy.sh' '$DEPLOY_PATH' '$release' '$IMAGE'"
