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
| `422` | a playlist or channel URL (send one video), a live/upcoming stream, or a video with no duration — none of them can be bounded |
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
| `YT_WHISPER_THREADS` | *(the instance's CPUs)* | whisper's `-t`. `0` means "as many CPUs as this process may use" (`sched_getaffinity`). Under nodo that is the 2 vCPUs that `service.json` declares. whisper's own default of 4 would oversubscribe that. |
| `YT_LANGUAGE` | `auto` | Language code (`en`, `es`, `zh-tw`) or `auto` to detect. |
| `YT_REQUEST_TIMEOUT_S` | `4 × YT_MAX_DURATION_S` | Whole-request budget, shared across probe, download, decode and transcribe. |
| `YT_DNS_SERVERS` | *(see below)* | One to three resolver IP addresses, separated by spaces or commas. Under a node the default is `9.9.9.9 1.1.1.1 8.8.8.8`; outside a node the default is to keep the resolver of the runtime. |

Pass them at launch with `nodo execute -e <name> <value>`. nodo delivers each
declared name as a real environment variable of the entrypoint, and also in
`/__config__`. Every one is refused loudly rather than clamped: `YT_MAX_DURATION_S=abc` stops the
service at start instead of silently becoming 3600. A limit that reverts to its
default when mistyped is not the limit the spec declares — and this one is what stands
between a clip and an eight-hour livestream on a 4 GB disk.

## The network it asks for

**`["*"]` — open egress.** A narrower declaration does not work for this service.
[`NODE-REQUIREMENTS.md`](NODE-REQUIREMENTS.md) gives the details, with the nodo source
for each fact.

**YouTube's media hosts cannot be listed.** The audio comes from a host of the form
`rN---sn-XXXXXXXX.googlevideo.com`, and the label changes per request. A glob such as
`*.googlevideo.com` is not a solution: the packer refuses it (nodo#391).

**A hostname tag gives addresses, not name resolution.** nodo resolves the tag on the
node and opens those addresses on TCP 80 and 443. It opens no port 53 and serves no
DNS. yt-dlp gets a URL and must look up the name itself, so under a hostname tag it
fails before it sends a request. nodo documents this in `docs/NETWORKS.md` →
*Hostname tags* (nodo#389), and tells such a service to declare `"*"` and narrow
inside the image.

**Under a node, a guest has no resolver.** nodo writes no `/etc/resolv.conf`, and the
image has no usable one. So the entrypoint writes one before it drops privileges
(`service/resolver.py`): public resolvers by default, or the addresses in
`YT_DNS_SERVERS`. Outside a node (Docker) it keeps the resolver of the runtime.

**The service limits the egress, not the node:**

- yt-dlp is the only program in this image that opens a socket.
- `ffmpeg` is compiled `--disable-network`, and its protocol list is `file` only. It
  cannot open a URL, and the image tests check this.
- The URL that reaches yt-dlp must name one of **five exact hostnames**
  (`service/urls.py`). The check uses `urlsplit().hostname`, after parsing.
- No shell runs. yt-dlp reads no config file (`--ignore-config`). No cookie and no
  credential exists to leak.
- yt-dlp runs without its generic extractor (`--use-extractors default,-generic`).
  That extractor fetches any page and follows its links and redirects. Without it, yt-dlp
  refuses a URL that no YouTube extractor claims, and sends no request.

An operator who wants the node to enforce a limit uses `service_networks` in
`config.yaml`. `NODE-REQUIREMENTS.md` gives the result under each policy.

## Packing and running it on a node

These are the commands of the current nodo CLI (`docs/USAGE.md`,
`docs/skill/SKILL.md`). Do not pack only to test a code change: a pack can take an
hour. Use the Docker loop below for that.

```sh
nodo pack /path/to/yt-transcript        # prints the service id (content hash)
nodo pack /path/to/yt-transcript --local --detach --json   # local BuildKit, in the background
nodo packs                              # the state of the packs
```

`nodo pack` reads `.service/` (`Dockerfile`, `service.json`, `pack_config.json`).
The packer builds for the `architecture` in `service.json`, `linux/arm64`, and it
must match the architecture of the host that packs. The `include` list packs only
`service/`, and the packer rewrites `COPY ./service` to `COPY service/service`.

```sh
nodo inspect yt-transcript              # the declared envs: only these can be passed with -e
nodo estimate yt-transcript             # feasibility and cost before a launch
nodo execute -e YT_MAX_DURATION_S 1800 -e YT_LANGUAGE en yt-transcript
nodo instances                          # the instance id and the API address
curl -s -X POST http://<api address>/transcribe \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=ps3kWOQRQnY"}'
nodo tunnel <instance id> 8080          # to reach the slot from another host
nodo observe <instance id>              # CPU, memory and the network flows
sudo nodo kill <instance id>
```

The address that `nodo instances` shows is reachable from the node host only. Use
`nodo tunnel` to reach it from somewhere else.

`nodo ggconf` is not useful here. This service does not call the gateway and has no
dependencies, so `__config__` and `.dependencies` give it nothing.

### Locally, without a node

```sh
docker buildx build --platform linux/arm64 -f .service/Dockerfile -t yt-transcript:test --load .

# The entrypoint is named explicitly: the Dockerfile deliberately sets no ENTRYPOINT
# or CMD, because nodo reads it from `init.entry_path` in service.json instead.
docker run -d -p 8080:8080 --entrypoint /service/entrypoint.sh yt-transcript:test
```

The Dockerfile uses only `./`-prefixed `COPY` sources, so the same file builds from
the repository root here and from the packer's `.service/` context.

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

`linux/arm64`, the same as `ergo-node`, `bitcoin-node` and `remote-browser`. The
runtime stage does not depend on the architecture: the Debian digest is a multi-arch
index, the package versions are the same on arm64 and amd64, the yt-dlp zipapp is
Python, and the model is data. ffmpeg and whisper.cpp compile for the platform that
BuildKit builds.

**For amd64, change `architecture` in `.service/service.json`, and look at the
whisper stage.** `GGML_NATIVE=OFF` keeps the binary portable. On arm64 the baseline
is armv8-a. On x86-64, with no other flag, ggml then builds without AVX, which makes
whisper several times slower. An amd64 build needs `-DGGML_AVX=ON -DGGML_AVX2=ON
-DGGML_FMA=ON -DGGML_F16C=ON` (the hosts must then have AVX2). Nobody has built or
measured it, so this repository does not ship an amd64 tree.

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
sh tests/run.sh                                  # 158 offline tests, ~2 s
sh tests/test_image.sh                           # checks against a built image
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
It also checks one connection at a time: that a body the server did not read closes
the connection (otherwise a client can hide a second request in it), and that a
silent client is dropped after the idle timeout.

**The resolver step** (`test_resolver.py`): public resolvers under a node, no change
outside one, `YT_DNS_SERVERS` everywhere, and a bad value that stops the start.

**The pack tree** (`test_layout.py`), read the way `nodo pack` reads it: the
entrypoint exists and is executable, the declared envs are the envs the code reads,
the API port is the default port, the CPU quota gives two vCPUs, and the Dockerfile
has `./` COPY sources and no `CMD`, `ENTRYPOINT`, `EXPOSE` or `ENV`.

**The image** (`test_image.sh`): that every binary runs, that ffmpeg's protocol list
is `file` alone, that the model matches its pinned digest, that PID 1 is uid 10001,
and that the allow-list holds over real HTTP.

### Found in review against the current nodo

Two faults only show under a node, so the Docker runs above could not find them:

- **No DNS in the guest.** nodo writes no `/etc/resolv.conf`, and the exported image
  has no usable one. Every yt-dlp lookup would fail, so every request would return
  502. Fixed by `service/resolver.py`.
- **One vCPU, not two.** nodo boots `ceil(cpu_quota / cpu_period)` vCPUs, and one when
  `service.json` sets no quota. The code and the timeout assumed two. Fixed in
  `resources`.

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

What is **not** verified: **this has never been launched under a real nodo.** No
service id has been produced from this tree, and nothing has been through
`resolve_network` or the firewall for real. The `service.json`, the pack tree and the
DNS step were checked against the nodo `dev` source (`docs/PACKING.md`,
`src/packers/`, `bash/build_ch_initramfs.sh`, `src/virtualizers/microvm/`), and
`tests/test_layout.py` checks the packer rules, but static reading is not a launch. Also unverified: any
architecture other than arm64, any model other than `base`, and long videos — the
longest transcribed was 55 seconds.

## What is deliberately not here

- **Cookies, credentials, or sign-in.** `--no-cookies` and `--no-cookies-from-browser`
  are passed explicitly, and the tests assert no cookie flag is ever offered. This
  service cannot fetch anything that needs an account, by construction.
- **A GPU.** `celaut.Sysresources` is `mem_limit`, `disk_space`, `cpu_period`, `benchmark`,
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

**yt-dlp may need a JavaScript runtime for YouTube.** Recent yt-dlp releases warn
that YouTube extraction without an external JavaScript runtime (for example Deno) is
deprecated, and that some formats can then be missing. This image has no JavaScript
runtime. The live run above found an audio format with the pinned release. If a later
release needs one, the symptom is `502` with "Requested format is not available" in
`detail`. The fix is a pinned Deno binary in the runtime stage.

**yt-dlp is frequently rate-limited or blocked from datacenter IPs.** A node running
this in a cloud may see failures that a workstation does not. The live test is opt-in
and says so, so a red live run and a green offline run are distinguishable.
