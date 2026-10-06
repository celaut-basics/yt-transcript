#!/bin/sh
# Every offline test, with the service's own modules importable.
#
#     sh tests/run.sh
#
# Python 3 and nothing else -- no pytest, no venv, no network, no model, no Docker.
# That is deliberate: these are the tests that should run on a workstation before a
# build, so their dependency list is the same as the service's own.
#
# The container-level tests are separate and need an image:
#     sh tests/test_image.sh            # smoke: binaries, model, health, a bad host
#     YT_TRANSCRIPT_LIVE=1 sh tests/test_image.sh   # and one real transcription

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

# `service/` on the path, because that is where the modules live in the image too --
# `/service/server.py` imports `config`, not `service.config`, and a test layout that
# needed a package would be testing a different import graph than the one that ships.
PYTHONPATH="$ROOT/service" export PYTHONPATH

PYTHON=${PYTHON:-python3}

echo "# python:  $($PYTHON --version 2>&1)"
echo "# modules: $ROOT/service"
echo

cd "$ROOT/tests"
exec "$PYTHON" -m unittest discover -s "$ROOT/tests" -p 'test_*.py' -v
