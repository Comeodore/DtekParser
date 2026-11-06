#!/usr/bin/env bash
# Deploys this repository's pushed main to the home server, and nothing else.
#
#   tools/deploy.sh [--dry-run] [service ...]
#
# Refuses unless the checkout is on main, has no uncommitted changes to tracked files and equals
# origin/main. Ships `git archive HEAD`, never the working tree, so the server always runs a commit
# that is on main on GitHub, and records that commit in /opt/dtek-service/REVISION. Without
# services it rebuilds every service of docker-compose.yml (compose recreates only those whose
# image changed). --dry-run lists what would change on the server and stops there.
set -euo pipefail

HOST=${DTEK_DEPLOY_HOST:-ubuntu}
# What the images and compose need. The server keeps .env, data/, reserve-data/ and rollback/.
PATHS=(app deye_reserve Dockerfile Dockerfile.reserve .dockerignore docker-compose.yml
       requirements.txt requirements-reserve.txt .env.example)

die() { echo "deploy: $*" >&2; exit 1; }

dry=false
if [ "${1:-}" = --dry-run ]; then dry=true; shift; fi
for s in "$@"; do [[ $s =~ ^[a-z0-9_-]+$ ]] || die "bad service name '$s'"; done

cd "$(git rev-parse --show-toplevel)"
branch=$(git symbolic-ref --short -q HEAD || true)
[ "$branch" = main ] || die "the checkout is on '${branch:-a detached HEAD}', not main. Only main is deployed."
git diff --quiet HEAD -- || die "there are uncommitted changes. Commit them to main and push first."
git fetch -q origin main
head=$(git rev-parse HEAD)
[ "$head" = "$(git rev-parse origin/main)" ] ||
    die "HEAD $(git rev-parse --short HEAD) is not origin/main $(git rev-parse --short origin/main). Push (or pull) first."

echo "deploy: $(git log -1 --format='%h %s') -> $HOST"
git -c tar.umask=022 archive --format=tar --add-virtual-file="REVISION:$(git log -1 --format='%H %s')"$'\n' HEAD "${PATHS[@]}" |
    ssh "$HOST" 'rm -rf /tmp/dtek-deploy && mkdir -p /tmp/dtek-deploy && tar -x -C /tmp/dtek-deploy'

# shellcheck disable=SC2029  # $dry and the services are checked above
ssh "$HOST" "bash -s -- $dry $*" <<'REMOTE'
set -euo pipefail
dir=/opt/dtek-service stage=/tmp/dtek-deploy dry=$1
shift
trap 'rm -rf "$stage"' EXIT
n=()
[ "$dry" = true ] && n=(-n)

echo "deploy: running before: $(head -1 "$dir/REVISION" 2>/dev/null || echo 'unknown (no REVISION)')"
echo "deploy: changes on the server:"
for d in app deye_reserve; do
    rsync -rlpic "${n[@]}" --delete --exclude __pycache__ "$stage/$d/" "$dir/$d/" | sed "s|^|  $d/: |"
done
cd "$stage"
mapfile -t files < <(find . -maxdepth 1 -type f ! -name REVISION -printf '%P\n')
rsync -lpic "${n[@]}" -- "${files[@]}" "$dir/" | sed 's|^|  |'
[ "$dry" = true ] && { echo "deploy: dry run, nothing changed"; exit 0; }

cd "$dir"
docker compose up -d --build "$@"
cp "$stage/REVISION" "$dir/REVISION"
docker image prune -f >/dev/null
docker builder prune -f >/dev/null

# dtek-service answers once its startup is done (it may wait up to 2 minutes for the instance lock).
for _ in $(seq 60); do
    health=$(curl -s -m 3 http://127.0.0.1:9998/api/v1/health) && break
    sleep 3
done
docker compose ps --format '  {{.Name}}: {{.Status}}'
echo "deploy: health: ${health:-no answer}" | cut -c1-300
echo "deploy: running now: $(head -1 "$dir/REVISION")"
REMOTE
