# yt-transcript

A YouTube video, downloaded and transcribed, packaged as a
[Celaut](https://github.com/celaut-project/nodo) service. Input is a video
URL; output is its transcription.

## Why

Downloading and parsing video from an arbitrary URL is exactly the kind of
thing worth confining to a microVM instead of running on your own machine or
node -- extractors and audio/video decoders are both frequent sources of
exploits, and a transcript is all the caller actually needs back. Sealing the
downloader and the transcriber behind a content-addressed spec gives the
caller text without either one ever touching anything else, the same
reasoning [file-as-service](https://github.com/celaut-basics/file-as-service)
gives for sealing a file with its interpreter.

## What it does

Given a YouTube URL:

1. Download the video's audio track.
2. Transcribe it to text.
3. Return the transcription as the service's output.

Concretely, one HTTP slot on `:8080`, and four programs behind it: `yt-dlp` to fetch,
`ffmpeg` to decode, `whisper.cpp` to transcribe, and ~400 lines of Python standard
library to hold them together.

## The API

### `POST /transcribe`

```sh
curl -s -X POST http://<instance>:8080/transcribe \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=ps3kWOQRQnY"}'
```

```json
{
  "text": "Earth. Home. Of all the planets NASA has explored, none have matched the dynamic complexity of our own. ...",
  "segments": [
    {"start_ms": 0, "end_ms": 30000, "text": "Earth. Home. Of all the planets NASA has explored, ..."},
    {"start_ms": 30000, "end_ms": 55000, "text": "... we study our planet not only to learn about it, but also to protect it."}
  ],
  "language": "en",
  "video_id": "ps3kWOQRQnY",
  "duration_s": 55.0,
  "elapsed_s": 6.81
}
```

That is a real response, copied from a run — see [What was verified by running
it](#what-was-verified-by-running-it).

| field | |
|---|---|
| `text` | the whole transcript, segments joined with single spaces |
| `segments` | `start_ms` / `end_ms` / `text` per segment. The bounds are whisper's own integer millisecond offsets, and are `null` if it emitted none |
| `language` | what the model **detected**, which is the useful answer when `YT_LANGUAGE=auto`. `null` if whisper reported none |
| `video_id` | reported for correlation, `null` if the URL carries none (a `/playlist?` URL, say). Never used to build the request — the URL is |
| `duration_s` | from the metadata probe, so it is the video's length and not the audio file's |
| `elapsed_s` | wall-clock for the whole request |

Failures are JSON too, with an `error` and often a `detail` carrying the last lines of
whichever tool failed:

| status | when |
|---|---|
| `400` | the URL is not on the host allow-list, or the body is not `{"url": "..."}` |
| `404` | not `/transcribe` or `/health` |
| `413` | the body is over 8 kB, **or** the video is longer than `YT_MAX_DURATION_S` |
| `422` | a live/upcoming stream, or a video reporting no duration — neither can be bounded |
| `500` | transcription failed, or something unexpected did |
| `502` | yt-dlp or ffmpeg failed. `detail` says what they said |
| `503` | already transcribing. One at a time; `Retry-After: 30` |
| `504` | the request exceeded `YT_REQUEST_TIMEOUT_S` |

### `GET /health`

```json
{"status": "ok", "model": "ggml-base.bin", "max_duration_s": 3600,
 "language": "auto", "threads": 4, "busy": false}
```

Answers while a transcription is running — the semaphore serialises the *work*, not
the server, because a health check that queued behind an hour of audio would read to
a node as an instance that had died.

## The environment it reads

| variable | | what it is |
|---|---|---|
| `YT_PORT` | `8080` | The port the HTTP slot listens on. Must match `api[0].port` in `.service/service.json`. |
| `YT_MAX_DURATION_S` | `3600` | Longest video accepted, checked **before** downloading. |
| `YT_WHISPER_THREADS` | *(the instance's CPUs)* | whisper's `-t`. `0` means "as many as this instance was given"; whisper's own default of 4 would oversubscribe a 2-vCPU instance and idle a large one. |
| `YT_LANGUAGE` | `auto` | Language code (`en`, `es`, `zh-tw`) or `auto` to detect. |
| `YT_REQUEST_TIMEOUT_S` | `4 × YT_MAX_DURATION_S` | Whole-request budget, shared across probe, download, decode and transcribe. |

Every one is refused loudly rather than clamped: `YT_MAX_DURATION_S=abc` stops the
service at start instead of silently becoming 3600. A limit that reverts to its
default when mistyped is not the limit the spec declares — and this one is what stands
between a clip and an eight-hour livestream on a 4 GB disk.

## The network it asks for

**`["*"]` — open egress.** Stated plainly rather than dressed up as something
narrower, and the reason is worth reading because two of the three parts are facts
about nodo rather than about YouTube.

**YouTube's media hosts cannot be enumerated.** A video's audio comes from a
per-session host of the form `rN---sn-XXXXXXXX.googlevideo.com`, where the label is
assigned per request. No fixed list covers it. This is the same position
[`celaut-basics/bitcoin-node`](https://github.com/celaut-basics/bitcoin-node) is in
with DNS seeds, and it asks for `["*"]` for the same reason.

**A hostname tag would not work even if the list existed**, and this is the part that
surprised me. Two findings from this checkout of nodo, both verified rather than
assumed:

1. **A hostname tag opens TCP 80 and 443 to resolved A records — and nothing on UDP
   53.** The node resolves the tag itself (`resolve_domain`, `src/manager/networks.py`)
   and writes firewall allows for those addresses. `src/virtualizers/microvm/network.py`
   states outright that no port-53 rule is written and that nodo does not serve DNS.
   So a guest under a hostname tag is granted addresses for hosts **it cannot look
   up** — and every program here resolves names for itself.
2. **Only the first tag that resolves is used.** `resolve_network` walks
   `network.tags` and `break`s at the first that resolves. A demo-service-style entry
   listing five YouTube hosts would grant the first one's addresses and silently drop
   the other four.

And a wildcard tag is not merely unsupported but fatal: `resolve_domain` raises
`ValueError` on `*.googlevideo.com`, which propagates out of `build_network_resolution`
and aborts the launch. There is no wildcard-hostname syntax to use. Both of these are
in [`NODE-REQUIREMENTS.md`](NODE-REQUIREMENTS.md) in the form Josemi would want them.

**What bounds the egress is the service, not the node.** Since the declaration cannot
be narrow, the narrowing is done where it can be:

- the only program in this image that opens a socket is **yt-dlp**;
- `ffmpeg` is compiled `--disable-network` and its protocol list is literally `file` —
  it cannot open a URL at all, which the image tests assert;
- the URL reaching yt-dlp has already been checked against **five exact hostnames**
  (`service/urls.py`), after parsing, against `urlsplit().hostname`;
- nothing ever reaches a shell, and no cookie or credential exists to leak.

An operator who wants that boundary enforced at the node instead of taken on trust has
`service_networks` in `config.yaml`; `NODE-REQUIREMENTS.md` says what happens under
each policy.

## Building it

```sh
nodo pack .        # produces the service and prints its id (content hash)
```

Locally, without a node:

```sh
docker buildx build --platform linux/arm64 -f .service/Dockerfile -t yt-transcript:test --load .

# The entrypoint is named explicitly: the Dockerfile deliberately sets no ENTRYPOINT
# or CMD, because nodo reads it from `init.entry_path` in service.json instead.
docker run -d -p 8080:8080 --entrypoint /service/entrypoint.sh yt-transcript:test
```

**Everything is pinned**, by digest or SHA-256, with no `latest` anywhere:

| | pinned by |
|---|---|
| `debian:bookworm-slim` | index digest `sha256:88200866…a4171` (the same one `celaut-basics/ergo-node` pins) |
| `yt-dlp` 2026.08.19 | the SHA-256 from yt-dlp's published `SHA2-256SUMS` — an upstream document |
| `ffmpeg` 7.1.5 | SHA-256 computed from the published tarball |
| `whisper.cpp` v1.9.4 | SHA-256 of the release tarball |
| `ggml-base.bin` | `60ed5bc3…2efe`, which is also the file's Git-LFS object id on Hugging Face — so the digest is the one the hosting itself addresses it by |
| Debian packages | exact patch versions |

Two caveats stated rather than glossed over. **FFmpeg publishes a detached GPG
signature but no `SHA256SUMS`**, so like Ergo's jar in `ergo-node`, what is pinned is
*an* artifact in a reviewed file, not a checksum compared against an upstream
document; verifying the signature would be the improvement. And **pinning Debian
packages to the patch version** means the build stops when one leaves the mirror after
a security update, until this file is edited — the same trade `ergo-node` and
`bitcoin-node` already make.

### Architecture

`linux/arm64`, matching `ergo-node` and `demo-service` (Josemi's nodo runs on ARM —
`docs/FEDORA_ARM.md`). The Dockerfile takes `TARGETARCH` from BuildKit, and ffmpeg,
whisper.cpp and the model are architecture-independent inputs — the two compiled from
source build for whatever platform is requested, and the model is data. **Building for
amd64 is two lines**: `architecture` in `.service/service.json`, and the yt-dlp
checksum, since that one artifact is fetched rather than built. (The yt-dlp artifact
pinned here is the *zipapp*, which is Python and portable, so in practice even that
may not need to change — but the checksum belongs to a specific file and this README
will not claim a build it has not run.)

### What it costs to run

**Image 506 MB; exported filesystem 290 MB** — the latter is what nodo actually packs,
measured with `docker buildx build -o type=tar`. Where it goes:

| | |
|---|---|
| the whisper model | 142 MB |
| Python 3.11 stdlib | 29 MB |
| yt-dlp | 3.0 MB |
| whisper-cli | 2.6 MB |
| ffmpeg | **2.2 MB** |
| this service's own code | 120 kB |

That ffmpeg figure is the one worth noting: Debian's `ffmpeg` package is ~70-90 MB of
every demuxer, decoder and protocol it ships, each of them a parser. Configured down
to the codecs YouTube actually serves (`opus`, `vorbis`, `aac`, `mp3`, `flac`), one
output format and the `file` protocol, it is **2.2 MB** — a 30x difference, in the one
component whose whole job is reading untrusted bytes.

**Memory: 600 MB at init, 1.5 GB at most.** Measured: **12 MiB idle, 297 MiB peak**
during a transcription (the model is mmap'd). 1.5 GB is headroom for a longer video
and for `small` if someone swaps the model.

**Disk: 3 GB at init, 4 GB at most.** The filesystem is 290 MB; the rest is scratch for
one request — an hour of Opus at YouTube's ~64 kbit/s is ~29 MB, and its 16 kHz mono
PCM intermediate is ~115 MB. `--max-filesize 512m` is the backstop if format selection
picks something unexpected.

**On model size:** `base` (148 MB) rather than `small` (488 MB) or `tiny` (77 MB).
base is the size at which this still fits resources a node will grant it and stays at
roughly real time on the declared CPU — measured at **8x faster than real time** (55 s
of audio in 6.8 s) on this machine. Moving to `small` is a one-line change in the
Dockerfile plus the resources in `service.json`, and costs ~2.5x the CPU time.

## Tests

```sh
sh tests/run.sh                                  # 114 offline tests, ~11 s
sh tests/test_image.sh                           # 24 against a built image
YT_TRANSCRIPT_LIVE=1 sh tests/test_image.sh      # + one real transcription
```

The offline suite needs **Python 3 and nothing else** — no pytest, no venv, no
network, no model, no Docker. That is deliberate: its dependency list is the same as
the service's own, which is what makes it a suite that gets run.

What they cover:

**The host allow-list** (`test_urls.py`), weighted towards the strings that *look*
like YouTube URLs and are not — `youtube.com.attacker.example`,
`https://www.youtube.com@attacker.example/`, the allowed host in the path, in the
query, in the fragment, a homograph, `file:///etc/passwd`. Each one is a way a
substring or suffix check gets this wrong.

**whisper's JSON** (`test_whisper.py`), against three **committed captures** of what
`whisper-cli -oj` really wrote — including a two-segment one, which is what proves
segment bounds are read per segment rather than from the first. See
`tests/fixtures/README.md`.

**The environment contract** (`test_config.py`): every variable's default, every
refusal, and the boundaries.

**The pipeline's decisions** (`test_pipeline.py`) with all four subprocesses replaced:
that the duration ceiling is enforced *before* the download, that live streams are
refused, that the deadline is **one** budget across four steps and not four, that the
child environment is replaced rather than inherited, that no shell is used, and that
the request's temp directory is removed on success **and** on failure.

**The HTTP contract** (`test_server.py`) against a real socket: routing, status codes,
that a refused URL never reaches the pipeline, that `/health` answers while a
transcription holds the slot, and that the slot is released after a failure — without
which one failed request would wedge the instance forever.

**The image** (`test_image.sh`): that every binary runs, that ffmpeg's protocol list
is `file` alone, that the model matches its pinned digest, that PID 1 is uid 10001,
and that the allow-list holds over real HTTP.

### Three bugs the tests found

Worth naming, because each was invisible in a passing build:

- **`YT_LANGUAGE=-m` was accepted.** It becomes the operand of `-l` in whisper's
  argv, so a leading hyphen would be parsed as the next *flag* — turning a language
  setting into a way to choose the model file. Found by `test_config.py`.
- **`libgomp1` was missing.** whisper.cpp needs OpenMP at runtime even built static.
  The binary compiled, copied, and failed to *start*. Found by the build-time smoke
  test — which was itself masking it, because `| head -1` moves the exit status to
  `head`'s, so `set -e` never fired. Both are fixed.
- **The model was mode 0600.** `ADD` from a URL writes root-only, so the model was
  present, checksum-verified, and unreadable by the user the entrypoint drops to.
  The image built clean and *every* transcription failed with "failed to initialize
  whisper context". Found by running the container. The smoke test now checks
  readability **as the service user**, where as root it had passed either way.

## What was verified by running it

On this machine (Apple Silicon, `linux/arm64` under Docker):

- **A real video transcribed end to end.** `ps3kWOQRQnY`, a 55-second NASA clip:
  downloaded, decoded and transcribed in **6.81 s**, detected language `en`, 2
  segments, accurate text. The response in [The API](#the-api) is that run.
- **The duration ceiling holds, before downloading.** A 10809 s video against
  `YT_MAX_DURATION_S=60` returned **413** naming both numbers, and `/tmp` in the
  container was **empty** afterwards — nothing was fetched.
- **The allow-list holds over HTTP.** Suffix-attack, userinfo-attack, `file:` and
  unrelated hosts all **400**.
- **PID 1 is uid 10001.** `setpriv --no-new-privs` from the entrypoint; verified via
  `/proc/1/status`.
- **SIGTERM shuts it down cleanly**: `docker stop` returned in **0.6 s**, exit code
  **0**, with "signal 15, shutting down" then "stopped" in the log.
- **ffmpeg cannot reach the network**: `-protocols` lists `file` and nothing else.
- **A packer-shaped build works.** nodo's `COPY`-rewrite was applied and the image
  rebuilt from a simulated `.service/` context; it produced a working service.

What is **not** verified: **this has never been launched under a real nodo.**
`nodo pack .` was attempted and could not run here — the local checkout's `config.yaml`
fails validation with removed keys, and packing wants BuildKit on Linux. So the
`service.json` is written to PACKING.md and read against the packer's own
`zip_with_dockerfile.py`, but no service id has been produced from it, and nothing has
been through `resolve_network` or the firewall for real. Also unverified: any
architecture other than arm64, any model other than `base`, and long videos — the
longest transcribed was 55 seconds.

## What is deliberately not here

- **Cookies, credentials, or sign-in.** `--no-cookies` and `--no-cookies-from-browser`
  are passed explicitly, and the tests assert no cookie flag is ever offered. This
  service cannot fetch anything that needs an account, by construction.
- **A GPU.** `celaut.Sysresources` is `mem_limit`, `disk_space`, `cpu_period`,
  `cpu_quota`, `blkio_weight` — there is no accelerator field, so a service needing one
  could not be scheduled. whisper runs with `-ng`, on CPU. This is the same conclusion
  [`remote-browser`](https://github.com/celaut-basics/remote-browser) reached.
- **Concurrency.** One transcription at a time. Two whisper runs on 2 vCPU are each
  twice as slow and double the peak disk; a second caller gets 503 immediately, which
  is a better answer than a request that queues past its own timeout.
- **Persistence.** No cache, no state. A Celaut instance has no persistent volume for
  its own data, and a transcript is cheap to reproduce from a URL.
- **`mcp` as a protocol tag.** `demo-service` declares `["http", "mcp"]`, but `mcp` is
  not documented anywhere in nodo's `docs/` as an API protocol tag — the only MCP in
  there is the Unstoppable Skills server, which is a different thing. Declaring it
  would be inventing a contract, so this declares `["http"]`.
- **Video.** Only the audio track is fetched (`-f bestaudio/best`), which is also why a
  two-hour video moves ~50 MB instead of ~8 GB.

## What this depends on

**A pinned yt-dlp eventually stops working.** YouTube changes its extraction surface
often, and yt-dlp ships releases to keep up; this pins `2026.08.19` because a
downloader that updates itself is one whose behaviour is not what the spec was
reviewed with. The cost is that this line needs bumping periodically, and the symptom
will be `502` with yt-dlp's own message in `detail`.

**yt-dlp is frequently rate-limited or blocked from datacenter IPs.** A node running
this in a cloud may see failures that a workstation does not. The live test is opt-in
and says so, so a red live run and a green offline run are distinguishable.
