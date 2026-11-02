#!/bin/sh
# One-shot entrypoint for the data-volume-init service. Hands the extractor data volumes to the
# deploying user (TARGET_UID:TARGET_GID): the discogs-ingestion and musicbrainz-ingestion images
# create their data directories owned by 1000:1000, which a fresh named volume inherits, so an
# extractor running as any other host UID could not write its own volume.
#
# Runs as root with only CHOWN and DAC_READ_SEARCH. It only ever changes the owner of entries whose
# owner or group differs; it never deletes, chmods or rewrites contents, and it is a pure no-op
# when ownership already matches (a UID 1000 install changes nothing). It does not cross
# filesystems (-xdev). The extractor mounts the volumes with `nocopy`, otherwise Docker would
# re-apply the image directory's ownership to a still-empty volume when the extractor is created.
set -eu

U="${TARGET_UID:-}"
G="${TARGET_GID:-}"
case "$U" in '' | *[!0-9]*) echo "data-volume-init: TARGET_UID must be a non-negative integer, got '$U'" >&2; exit 2 ;; esac
case "$G" in '' | *[!0-9]*) echo "data-volume-init: TARGET_GID must be a non-negative integer, got '$G'" >&2; exit 2 ;; esac

for d in /volumes/*; do
  [ -d "$d" ] || { echo "data-volume-init: $d is not a mounted directory" >&2; exit 1; }
  changed="$(find "$d" -xdev \( ! -uid "$U" -o ! -gid "$G" \) -print | wc -l | tr -d ' ')"
  find "$d" -xdev \( ! -uid "$U" -o ! -gid "$G" \) -exec chown -h "$U:$G" {} +
  echo "data-volume-init: $d: re-owned $changed entries to $U:$G"
  stat -c 'data-volume-init: %n now %u:%g mode %a' "$d"
done
