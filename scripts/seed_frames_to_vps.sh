#!/usr/bin/env bash
# Copy rendered frames for chosen FIELDS from the desktop archive to the VPS.
#
# The desktop is the raw archive of record and renders whatever it likes; the
# VPS renders only what its own pipeline fetches. A field the VPS does not
# fetch (today: temp and rain) therefore has to be seeded across rather than
# produced there - which is the whole point of doing raw-dependent work on the
# desktop and shipping the product.
#
# Frames only. No raw, no code, no manifests:
#   - raw is exactly what the VPS's small disk cannot hold, and seeding it
#     would defeat the fetch->render->reclaim design.
#   - the manifest is derived state the VPS regenerates every 60 s, so a copied
#     one would be overwritten within the minute anyway. Note the consequence:
#     until the VPS's own code knows about a field, seeded frames sit on disk
#     unreferenced. That is fine and reversible - they simply appear once it
#     does - but do not read "seeded" as "visible".
#
# Dry-run by default, --apply to transfer, matching the pipeline's convention.
#
#   scripts/seed_frames_to_vps.sh                    # show what would move
#   scripts/seed_frames_to_vps.sh --apply
#   scripts/seed_frames_to_vps.sh --field temp --apply

set -euo pipefail

SRC="${SEED_SRC:-/mnt/e/data/eclipse-weather/viz/tool1_frames}"
DEST_HOST="${SEED_DEST_HOST:-root@203.0.113.10}"
DEST_DIR="${SEED_DEST_DIR:-/opt/eclipse-weather/data/viz/tool1_frames}"

FIELDS=()
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --field) shift; FIELDS+=("$1") ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ ${#FIELDS[@]} -eq 0 ] && FIELDS=(temp rain)

[ -d "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }

# Include every directory so rsync can descend, then the chosen field trees,
# then exclude everything else. Without the bare '*/' rule rsync never enters
# a model directory and the field rules match nothing.
FILTER=(--include='*/')
for f in "${FIELDS[@]}"; do FILTER+=(--include="${f}/***"); done
FILTER+=(--exclude='*')

echo "  source : $SRC"
echo "  dest   : $DEST_HOST:$DEST_DIR"
echo "  fields : ${FIELDS[*]}"

for f in "${FIELDS[@]}"; do
  n=$(find "$SRC" -type d -name "$f" -exec find {} -name '*.png' \; 2>/dev/null | wc -l)
  sz=$(find "$SRC" -type d -name "$f" -exec du -sh {} \; 2>/dev/null | awk '{s+=$1} END {print s}')
  echo "  local $f: $n frame(s)"
done

if [ "$APPLY" = "0" ]; then
  echo "  --- DRY RUN (rsync -n), nothing transferred ---"
  rsync -an --stats --prune-empty-dirs "${FILTER[@]}" "$SRC/" "$DEST_HOST:$DEST_DIR/"
  echo "  rerun with --apply to transfer"
  exit 0
fi

# --partial so an interrupted transfer resumes instead of restarting; frames
# are many small files, but temp/ across ten models is not small in aggregate.
rsync -a --partial --prune-empty-dirs --info=stats2 \
  "${FILTER[@]}" "$SRC/" "$DEST_HOST:$DEST_DIR/"

echo "  --- verifying counts on the VPS ---"
for f in "${FIELDS[@]}"; do
  local_n=$(find "$SRC" -type d -name "$f" -exec find {} -name '*.png' \; 2>/dev/null | wc -l)
  remote_n=$(ssh "$DEST_HOST" "find $DEST_DIR -path '*/$f/*.png' | wc -l")
  echo "  $f: local $local_n, remote $remote_n"
done
