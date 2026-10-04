#!/bin/sh
# The half of the tests that needs a built image: that the binaries are there, that
# the service starts, answers /health, and refuses a non-YouTube host over real HTTP.
#
#     sh tests/test_image.sh                          # offline; no network needed
#     YT_TRANSCRIPT_LIVE=1 sh tests/test_image.sh     # and one real transcription
#
# The live test is opt-in because it is the one that can fail for reasons that are
# not this service's fault: yt-dlp is routinely rate-limited or outright blocked from
# datacenter and cloud IP ranges, and YouTube changes its extraction surface often
# enough that a pinned yt-dlp eventually stops working. A green offline run plus a
# red live run means "the service is fine, this host cannot reach YouTube", and those
# have to be distinguishable or the suite is not worth running.
#
# Build the image first:
#     docker buildx build --platform linux/arm64 -f .service/Dockerfile -t yt-transcript:test --load .

set -eu

IMAGE=${IMAGE:-yt-transcript:test}
PLATFORM=${PLATFORM:-linux/arm64}
NAME="yt-transcript-test-$$"
PORT=${PORT:-18080}

# A 55-second NASA clip: US government work, narrated, and short enough that the
# whole test is seconds rather than minutes. Overridable, because a URL in a test is
# a thing that rots -- the first URL tried while writing this was already dead.
LIVE_URL=${YT_TRANSCRIPT_LIVE_URL:-https://www.youtube.com/watch?v=ps3kWOQRQnY}

passed=0
failed=0

ok() {
    passed=$((passed + 1))
    printf '  ok    %s\n' "$1"
}

no() {
    failed=$((failed + 1))
    printf '  FAIL  %s\n' "$1"
}

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "# image:    $IMAGE"
echo "# platform: $PLATFORM"
echo

# ------------------------------------------------------------------ the filesystem
echo "the image holds what the service execs:"

check_in_image() {
    if docker run --rm --platform "$PLATFORM" --entrypoint /bin/sh "$IMAGE" -c "$2" >/dev/null 2>&1; then
        ok "$1"
    else
        no "$1"
    fi
}

check_in_image "yt-dlp runs and reports its pinned version" \
    "/usr/bin/python3 /opt/yt-dlp/bin/yt-dlp --version | grep -q 2026.08.19"
check_in_image "ffmpeg runs" \
    "/opt/ffmpeg/bin/ffmpeg -version"
# Asserted against ffmpeg's own protocol list rather than by watching a fetch fail:
# `-protocols` is what the binary was *compiled* with, so this states the property
# ("this decoder cannot open a URL") instead of observing one symptom of it. On a
# --disable-network build the list is `file` and nothing else.
check_in_image "ffmpeg supports only the file protocol (built --disable-network)" \
    "/opt/ffmpeg/bin/ffmpeg -hide_banner -protocols 2>/dev/null | grep -qx '  file'"
check_in_image "ffmpeg knows no http/https protocol at all" \
    "! /opt/ffmpeg/bin/ffmpeg -hide_banner -protocols 2>/dev/null | grep -qE '^  https?$'"
check_in_image "whisper-cli runs" \
    "/opt/whisper/bin/whisper-cli --version"
check_in_image "the model is present and is the pinned one" \
    "echo '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe  /opt/whisper/models/ggml-base.bin' | sha256sum -c -"
# A regression test with a story: `ADD` from a URL writes mode 0600, so the model
# was present, correct, and unreadable by the user the entrypoint drops to. The
# image built clean and every transcription failed at runtime. Checked as the
# service user, because as root it passes either way.
check_in_image "every artifact is readable by the unprivileged service user" \
    "setpriv --reuid 10001 --regid 10001 --clear-groups /bin/sh -c '
        head -c 4 /opt/whisper/models/ggml-base.bin > /dev/null \
        && /opt/whisper/bin/whisper-cli --version > /dev/null 2>&1 \
        && /opt/ffmpeg/bin/ffmpeg -version > /dev/null \
        && /usr/bin/python3 /opt/yt-dlp/bin/yt-dlp --version > /dev/null'"
check_in_image "python3 is the pinned 3.11" \
    "/usr/bin/python3 --version | grep -q 3.11"
check_in_image "setpriv is available for the entrypoint to drop privileges" \
    "command -v setpriv"
check_in_image "the unprivileged user exists" \
    "id ytt"
check_in_image "no shell interpreter is needed by the service modules" \
    "cd /service && /usr/bin/python3 -c 'import config, urls, whisper, pipeline, server, resolver'"
echo

# ------------------------------------------------------------------- it runs
echo "the service starts and answers:"

# The entrypoint is named explicitly, and that is not a workaround -- it is what
# nodo does. PACKING.md is explicit that a service's Dockerfile must define no
# ENTRYPOINT or CMD, because the packer exports the built image as a *filesystem*
# and the thing that gets exec'd comes from `init.entry_path` in service.json. So
# this image deliberately carries no Docker entrypoint metadata, and a harness that
# ran `docker run <image>` with no command would run the base image's inherited
# `bash` and exit immediately. Passing the path here runs what the node will run.
docker run -d --name "$NAME" --platform "$PLATFORM" \
    -p "127.0.0.1:${PORT}:8080" \
    --entrypoint /service/entrypoint.sh "$IMAGE" >/dev/null

waited=0
until curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; do
    waited=$((waited + 1))
    if [ "$waited" -gt 30 ]; then
        no "the service became reachable within 30s"
        echo "--- container log ---"
        docker logs "$NAME" 2>&1 | tail -20
        echo "passed=$passed failed=$failed"
        exit 1
    fi
    sleep 1
done
ok "/health answered within ${waited}s"

health=$(curl -fsS "http://127.0.0.1:${PORT}/health")

# `if`, not `A && B || C`: ok() and no() both increment a counter, so under `&&/||`
# a passing check that somehow made ok() return non-zero would record both.
if echo "$health" | grep -q '"status": *"ok"'; then
    ok "/health reports ok"
else
    no "/health reports ok"
fi

if echo "$health" | grep -q 'ggml-base.bin'; then
    ok "/health names the model"
else
    no "/health names the model"
fi

if docker logs "$NAME" 2>&1 | grep -q "dropping to ytt"; then
    ok "the entrypoint dropped privileges"
else
    no "the entrypoint dropped privileges"
fi

running_uid=$(docker exec "$NAME" sh -c 'id -u' 2>/dev/null || echo "?")
# The entrypoint execs setpriv, so PID 1 itself is the unprivileged process.
pid1_uid=$(docker exec "$NAME" sh -c 'awk "/^Uid:/ {print \$2}" /proc/1/status' 2>/dev/null || echo "?")
if [ "$pid1_uid" = "10001" ]; then
    ok "PID 1 runs as uid 10001, not root"
else
    no "PID 1 runs as uid 10001, not root (got '$pid1_uid', exec uid '$running_uid')"
fi
echo

# --------------------------------------------------------------- the boundary
echo "the host allow-list is enforced over real HTTP:"

check_status() {
    label=$1
    expected=$2
    body=$3
    actual=$(curl -s -o /dev/null -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' \
        -d "$body" "http://127.0.0.1:${PORT}/transcribe")
    if [ "$actual" = "$expected" ]; then
        ok "$label -> $expected"
    else
        no "$label -> expected $expected, got $actual"
    fi
}

check_status "a suffix-attack host"  400 '{"url":"https://youtube.com.attacker.example/watch?v=x"}'
check_status "a userinfo-attack host" 400 '{"url":"https://www.youtube.com@attacker.example/watch?v=x"}'
check_status "a file: URL"            400 '{"url":"file:///etc/passwd"}'
check_status "an unrelated host"      400 '{"url":"https://example.com/video"}'
check_status "no url at all"          400 '{}'
check_status "not JSON"               400 'not json'


not_found=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/nope")
if [ "$not_found" = "404" ]; then
    ok "GET /nope -> 404"
else
    no "GET /nope -> 404 (got $not_found)"
fi

# POSTed to an unknown path with a *valid* URL in the body, so what is being tested
# is the routing and not the allow-list.
unknown_post=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' \
    -d '{"url":"https://youtu.be/dQw4w9WgXcQ"}' \
    "http://127.0.0.1:${PORT}/not-an-endpoint")
if [ "$unknown_post" = "404" ]; then
    ok "POST /not-an-endpoint -> 404"
else
    no "POST /not-an-endpoint -> 404 (got $unknown_post)"
fi
echo

# ------------------------------------------------------------------- the live test
if [ "${YT_TRANSCRIPT_LIVE:-0}" = "1" ]; then
    echo "a real transcription (YT_TRANSCRIPT_LIVE=1):"
    echo "  url: $LIVE_URL"
    response=$(curl -s --max-time 900 \
        -X POST -H 'Content-Type: application/json' \
        -d "{\"url\":\"$LIVE_URL\"}" \
        "http://127.0.0.1:${PORT}/transcribe" || echo '{"error":"curl failed"}')

    if printf '%s' "$response" | /usr/bin/env python3 -c '
import json, sys
try:
    document = json.load(sys.stdin)
except ValueError:
    print("  response was not JSON"); sys.exit(1)
if "error" in document:
    print("  service returned an error: %s" % document.get("error"))
    print("  detail: %s" % document.get("detail", "")[:300])
    sys.exit(1)
text = document.get("text") or ""
if not text.strip():
    print("  transcription came back empty"); sys.exit(1)
print("  language:  %s" % document.get("language"))
print("  video_id:  %s" % document.get("video_id"))
print("  duration:  %ss" % document.get("duration_s"))
print("  elapsed:   %ss" % document.get("elapsed_s"))
print("  segments:  %d" % len(document.get("segments") or []))
print("  text[:200]: %s" % text[:200])
sys.exit(0)
'; then
        ok "a real video transcribed to non-empty text"
    else
        no "a real video transcribed to non-empty text"
        echo "  NOTE: yt-dlp is frequently rate-limited or blocked from datacenter"
        echo "        IPs. A failure here is not necessarily a fault in this service;"
        echo "        check the detail above and the container log."
        docker logs "$NAME" 2>&1 | tail -10 | sed 's/^/        /'
    fi
    echo
else
    echo "# live transcription skipped (set YT_TRANSCRIPT_LIVE=1 to run it)"
    echo
fi

echo "passed=$passed failed=$failed"
[ "$failed" -eq 0 ]
