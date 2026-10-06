#!/bin/sh
# What runs as PID 1, and the one thing it does that the Python cannot do for itself:
# drop to an unprivileged user.
#
# Under nodo's microVM virtualizer this process is execed by the initramfs `/init`
# straight out of `switch_root` (`bash/build_ch_initramfs.sh`), as root, with no init
# system underneath it. So privilege dropping has to happen here or not at all --
# there is no runtime that will honour a `USER` line, because the Dockerfile's
# metadata is not what starts this: `init.entry_path` in `.service/service.json` is,
# and nodo's packer exports the built image as a *filesystem* and drops everything
# else (PACKING.md, "the Dockerfile is not used to define a running container").
#
# `setpriv` is from util-linux, which is already in the base image, and
# `--no-new-privs` is the half that matters: after it, no execve in this process tree
# can gain privileges through a setuid bit, so nothing downstream -- yt-dlp, ffmpeg,
# whisper -- can climb back out even if it were tricked into running something.
#
# POSIX sh rather than bash: nothing here needs bash, and the image does not ship one.

set -eu

SERVICE_USER=ytt
SERVICE_UID=10001
SERVICE_GID=10001

log() {
    printf '[yt-transcript] %s\n' "$1" >&2
}

# The scratch directory every request's temp dir is made under. Created here, while
# still root, because the service user does not own /tmp's parent and a service that
# cannot write its scratch space fails on its first request rather than at start.
WORK_ROOT=${TMPDIR:-/tmp}
mkdir -p "$WORK_ROOT"
chmod 1777 "$WORK_ROOT"

if [ "$(id -u)" -eq 0 ]; then
    if ! command -v setpriv >/dev/null 2>&1; then
        # Stated rather than silently continuing as root: an operator reading the log
        # should be able to tell which of the two happened.
        log "FATAL: setpriv is not in this image, so privileges cannot be dropped"
        exit 2
    fi
    # A DNS resolver, while still root, because /etc is root's. Under nodo the guest
    # has none and every yt-dlp lookup would fail; service/resolver.py says why and
    # when it leaves the file alone. `-E -s`: no PYTHON* variable and no user site
    # directory can change what runs as root here.
    if ! /usr/bin/python3 -E -s /service/resolver.py; then
        log "FATAL: could not set up name resolution"
        exit 2
    fi

    log "dropping to ${SERVICE_USER} (uid ${SERVICE_UID}) with --no-new-privs"
    exec setpriv \
        --reuid "$SERVICE_UID" \
        --regid "$SERVICE_GID" \
        --clear-groups \
        --no-new-privs \
        /usr/bin/python3 -E -s /service/server.py
fi

# Already unprivileged -- which is how this runs under `docker run --user`, and how
# the tests run it. Nothing to drop, and /etc/resolv.conf cannot be written: the
# runtime's own resolver is used. Just start.
log "already running as uid $(id -u), starting without dropping privileges"
exec /usr/bin/python3 -E -s /service/server.py
