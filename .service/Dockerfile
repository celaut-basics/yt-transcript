# Every dependency is pinned by digest or by SHA-256, with no `latest` anywhere and
# no package pinned only to its suite. This service is content-addressed -- two packs
# of the same source tree have to produce the same id -- and it is also the thing the
# README argues should be sealed: a downloader and two decoders, fetching untrusted
# input. "Whatever the mirror served today" is not a set of parsers to make that
# argument about.
#
# Three build stages, one runtime. Only the final stage's filesystem is exported by
# nodo's packer, so the compilers that build ffmpeg and whisper.cpp are not in the
# service: the runtime holds four binaries, a model, and Python's standard library.

# ---------------------------------------------------------------------------- base
# Pinned by the index digest, which is the multi-arch one `debian:bookworm-slim`
# resolved to -- the same digest `celaut-basics/ergo-node` pins. Its linux/arm64
# member is sha256:6bd27d44e6c32a66bbd72d7cb2b76a8ae3497ec2e5274a81abd1b37f6013fa1f;
# pinning the index rather than that member is what lets the platform (from
# `architecture` in service.json) be a one-line change instead of a second digest to
# look up.
ARG BASE_DIGEST=sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

# ------------------------------------------------------------------------- toolchain
FROM debian:bookworm-slim@${BASE_DIGEST} AS toolchain

# apt reads a frozen snapshot of the Debian archive, not the live mirror. See the
# same block in the runtime stage, below, for the reason. To update, change
# DEBIAN_SNAPSHOT here and in the runtime stage together.
ARG DEBIAN_SNAPSHOT=20261004T000000Z
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      -e '/^URIs: /a Check-Valid-Until: no' \
      /etc/apt/sources.list.d/debian.sources \
    && [ "$(grep -c '^URIs: http://snapshot.debian.org/' /etc/apt/sources.list.d/debian.sources)" = 2 ]

# Not pinned to exact versions, unlike the runtime's packages below, and the reason is
# the asymmetry that matters: nothing installed here reaches the exported filesystem.
# A compiler that changes cannot change what this service *is* unless it changes what
# it *builds*, and what it builds is checked by the runtime's own smoke test. Pinning
# these would buy a build that breaks every time Debian patches gcc, for no property
# this image keeps. The snapshot above gives the same result without that cost: the
# newest version in the snapshot is the one installed, on every build.
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y \
        build-essential \
        cmake \
        ca-certificates \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------------- ffmpeg
# Built from source, configured down to the decoders this service actually needs.
#
# Debian's `ffmpeg` package is one apt line and would have been shorter. It is also
# ~70-90 MB of every demuxer, decoder, encoder, filter and protocol FFmpeg ships,
# each of them a parser, in an image whose entire argument is that a parser reading
# untrusted bytes should be confined and *small*. Shipping the general tool instead
# of the required decoder is exactly the cost `celaut-basics/file-as-service` names
# in its own table: "a full ffmpeg is 70-90 MB, a build that decodes exactly one
# codec is 5-15 MB".
#
# What is enabled, and why each one:
#   opus, vorbis, aac, mp3, flac   what YouTube serves as `bestaudio` -- Opus in
#                                  WebM for most, AAC in M4A for the rest
#   matroska, mov, mp3, ogg, ...   the containers those arrive in
#   pcm_s16le + wav                the only thing written: whisper.cpp's input
#   protocol=file                  and nothing else. `--disable-network` means this
#                                  binary cannot open a URL at all; the one program
#                                  in this image that reaches the network is yt-dlp.
#
# The measured result is **2.2 MB**, against ~70-90 MB for the distribution build.
#
# 7.1.5 is the current release of the 7.1 branch, which is FFmpeg's long-term one.
# The checksum is computed from the published tarball -- FFmpeg publishes a detached
# GPG signature (`.asc`) but no SHA256SUMS file, so this is *an* artifact pinned in a
# reviewed file rather than a checksum compared against an upstream document. That is
# weaker than a signed sums file and is stated rather than glossed over; verifying the
# release signature would be the improvement, and it needs FFmpeg's keyring in the
# build stage.
#
#   curl -sLO https://ffmpeg.org/releases/ffmpeg-7.1.5.tar.xz
#   shasum -a 256 ffmpeg-7.1.5.tar.xz
FROM toolchain AS ffmpeg
ARG FFMPEG_VERSION=7.1.5
ARG FFMPEG_SHA256=de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f
ADD https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz /tmp/ffmpeg.tar.xz
RUN echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && mkdir -p /tmp/ffmpeg-src \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg-src --strip-components=1
WORKDIR /tmp/ffmpeg-src
RUN ./configure \
        --prefix=/opt/ffmpeg \
        --disable-everything \
        --disable-autodetect \
        --disable-network \
        --disable-doc --disable-htmlpages --disable-manpages \
        --disable-podpages --disable-txtpages \
        --disable-programs --enable-ffmpeg \
        --disable-avdevice --disable-postproc --disable-sdl2 --disable-debug \
        --enable-demuxer=matroska,mov,mp3,wav,ogg,aac,flac \
        --enable-decoder=opus,vorbis,aac,aac_latm,mp3,flac,pcm_s16le,pcm_f32le \
        --enable-parser=opus,vorbis,aac,aac_latm,mpegaudio,flac \
        --enable-encoder=pcm_s16le \
        --enable-muxer=wav \
        --enable-protocol=file \
        --enable-filter=aresample,aformat,anull,atrim,format \
        --enable-bsf=aac_adtstoasc,extract_extradata \
        --enable-small \
    && make -j"$(nproc)" \
    && make install \
    && /opt/ffmpeg/bin/ffmpeg -version

# ---------------------------------------------------------------------- whisper.cpp
# Built from the release tag, statically linked against its own libraries so the
# runtime needs no ggml shared objects.
#
# `GGML_NATIVE=OFF` is the line that makes this reproducible and portable at once:
# with it on (the default) ggml compiles with `-march=native`, baking in whatever CPU
# features the *builder* had -- which produces a different binary on every machine and
# one that may SIGILL on the node that runs it. `armv8-a` is the baseline every
# arm64 host implements.
FROM toolchain AS whisper
ARG WHISPER_VERSION=1.9.4
ARG WHISPER_SHA256=57e280cee375ab02425b806ad5146b99f6eb9357e3c2b31357c8a6af2e2e44ae
ADD https://github.com/ggml-org/whisper.cpp/archive/refs/tags/v${WHISPER_VERSION}.tar.gz /tmp/whisper.tar.gz
RUN echo "${WHISPER_SHA256}  /tmp/whisper.tar.gz" | sha256sum -c - \
    && mkdir -p /tmp/whisper-src \
    && tar -xzf /tmp/whisper.tar.gz -C /tmp/whisper-src --strip-components=1
WORKDIR /tmp/whisper-src
RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_SERVER=OFF \
        -DGGML_NATIVE=OFF \
    && cmake --build build -j"$(nproc)" --target whisper-cli \
    && mkdir -p /opt/whisper/bin \
    && cp build/bin/whisper-cli /opt/whisper/bin/whisper-cli \
    && /opt/whisper/bin/whisper-cli --version

# ---------------------------------------------------------------------------- model
# ggml `base`, multilingual, pinned by the SHA-256 that is also its Git-LFS object id
# in ggerganov/whisper.cpp on Hugging Face -- so the digest pinned here is the one the
# hosting itself addresses the file by, not one this file's author asserted alone.
#
#   curl -s 'https://huggingface.co/api/models/ggerganov/whisper.cpp?blobs=true' \
#     | jq -r '.siblings[] | select(.rfilename=="ggml-base.bin") | .lfs.oid'
#
# **base and not small**, which is the one size choice worth defending:
#   tiny   77 MB   fast, and noticeably worse on accented speech
#   base  148 MB   what this ships
#   small 488 MB   better, at 3.3x the model and ~2.5x the CPU time
#
# base is the size at which the service still fits resources a node will actually
# grant it: the model is resident in the instance's memory for the length of a
# transcription, and `.service/service.json` declares 1.5 GB at most. Moving to small
# means +340 MB of image, +340 MB of resident memory, and a transcription that no
# longer finishes in roughly real time on the 2 vCPU declared -- which would put an
# hour-long video past the default request timeout. It is a one-line change here plus
# the resources in service.json, and the README says so.
#
# Multilingual rather than `.en`, because `YT_LANGUAGE` defaults to `auto` and a
# service that silently transcribed Spanish as English-shaped nonsense would be worse
# than one that is 0.1% behind `.en` on English.
FROM toolchain AS model
ARG WHISPER_MODEL=ggml-base.bin
ARG WHISPER_MODEL_SHA256=60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe
ADD https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${WHISPER_MODEL} /opt/whisper/models/${WHISPER_MODEL}
# 0644, explicitly. `ADD` from a URL writes the file mode 0600 owned by root, which
# is unreadable by the unprivileged user `service/entrypoint.sh` drops to -- so
# without this the image builds, the model is present, its checksum verifies, and
# every transcription fails at runtime with "failed to initialize whisper context".
# Found by running the container, not by reading the build log.
RUN echo "${WHISPER_MODEL_SHA256}  /opt/whisper/models/${WHISPER_MODEL}" | sha256sum -c - \
    && chmod 0644 /opt/whisper/models/${WHISPER_MODEL}

# -------------------------------------------------------------------------- runtime
FROM debian:bookworm-slim@${BASE_DIGEST} AS runtime

# Nothing in this stage depends on the architecture: the packages are the same
# versions on arm64 and amd64, the yt-dlp zipapp below is Python, the model is
# data, and the ffmpeg and whisper stages compile for the platform BuildKit builds.
# The README says what an amd64 build needs from the whisper stage.

# apt reads a frozen snapshot of the Debian archive, not the live mirror.
#
# The live mirror keeps one version of each package per suite. An exact pin against it
# stops resolving as soon as Debian publishes a security update, and the build then
# fails in apt for a reason that has nothing to do with this service. The same failure
# stopped every image of `celaut-basics/remote-browser` more than once.
# snapshot.debian.org keeps every state of the archive at a fixed URL, so the pins
# below resolve for as long as that service exists.
#
# Check-Valid-Until is off because a Release file in a snapshot is old by design. The
# `grep` makes the build fail if the base image changes the layout of its sources
# file, rather than fall back to the live mirror without a word. To update, change
# DEBIAN_SNAPSHOT and the pins together, and in the toolchain stage as well.
ARG DEBIAN_SNAPSHOT=20261004T000000Z
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      -e '/^URIs: /a Check-Valid-Until: no' \
      /etc/apt/sources.list.d/debian.sources \
    && [ "$(grep -c '^URIs: http://snapshot.debian.org/' /etc/apt/sources.list.d/debian.sources)" = 2 ]

# Python 3.11 and the CA store, pinned to the exact versions in the snapshot above, the
# same discipline `celaut-basics/ergo-node` applies to its eight packages. A Debian
# security update cannot silently change this image.
#
# `python3.11` and not `python3`: the `python3` metapackage is a moving pointer, which
# is the one thing this file is built to avoid. The service is stdlib-only -- no pip,
# no wheels, no requirements.txt -- so what is installed is the interpreter and
# nothing else.
#
# `ca-certificates` is here for exactly one reason: yt-dlp speaks TLS to YouTube and
# without a trust store every fetch fails certificate verification. It is not pinned
# to a version the way the rest are, because the *contents* of a CA bundle are a
# moving target by design. The snapshot still fixes which version apt picks: the
# newest one in the snapshot, which is the one from the security suite.
#
# `setpriv` is not installed: it is part of `util-linux`, which is in the base image
# already (`/usr/bin/setpriv`). `service/entrypoint.sh` refuses to start if it is
# somehow absent rather than silently running as root.
# `libgomp1` is whisper.cpp's OpenMP runtime, and it is the one library the static
# link does not cover: ggml parallelises its matrix kernels with OpenMP, so
# `whisper-cli` needs `libgomp.so.1` at runtime even built with
# `BUILD_SHARED_LIBS=OFF`. Its absence was found by the build-time smoke test at the
# bottom of this file -- the binary built and copied fine and failed to *start*,
# which is exactly the failure a smoke test exists to turn into a build error rather
# than a broken service id. Pinned to the version matching the gcc 12 that compiled
# it in the toolchain stage.
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y \
        python3.11=3.11.2-6+deb12u8 \
        python3.11-minimal=3.11.2-6+deb12u8 \
        libpython3.11-stdlib=3.11.2-6+deb12u8 \
        libpython3.11-minimal=3.11.2-6+deb12u8 \
        libgomp1=12.2.0-14+deb12u1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3

# ---------------------------------------------------------------------------- yt-dlp
# The release binary, pinned by the SHA-256 from the `SHA2-256SUMS` file yt-dlp
# publishes alongside it -- an upstream document, which is what the ffmpeg tarball
# above does *not* have.
#
#   curl -sL https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/SHA2-256SUMS
#
# The plain `yt-dlp` artifact, which is a zipapp: a zip of yt-dlp's own Python with a
# `#!/usr/bin/env python3` header, run by the interpreter installed above. The
# alternative artifacts (`yt-dlp_linux`, `yt-dlp_linux_aarch64`) are PyInstaller
# bundles that carry a second, unpinned CPython inside them -- a whole interpreter
# this file would be shipping without naming its version. One Python in the image,
# pinned by Debian's version, is the point.
#
# yt-dlp is the one program here that reaches the network, and it is pinned to a
# release rather than tracking: a downloader that updates itself is a downloader whose
# behaviour is not the behaviour this spec was reviewed with. That has a cost, stated
# in the README: YouTube changes its extraction surface often, and a pinned yt-dlp
# eventually stops working and needs this line bumped.
ARG YTDLP_VERSION=2026.08.19
ARG YTDLP_SHA256=1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6
ADD https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/yt-dlp /opt/yt-dlp/bin/yt-dlp
# 0755 for the same reason the model is chmod'd 0644: `ADD` from a URL writes 0600,
# and this file has to be readable and executable by the unprivileged service user.
RUN echo "${YTDLP_SHA256}  /opt/yt-dlp/bin/yt-dlp" | sha256sum -c - \
    && chmod 0755 /opt/yt-dlp/bin/yt-dlp

COPY --from=ffmpeg  /opt/ffmpeg/bin/ffmpeg          /opt/ffmpeg/bin/ffmpeg
COPY --from=whisper /opt/whisper/bin/whisper-cli    /opt/whisper/bin/whisper-cli
COPY --from=model   /opt/whisper/models             /opt/whisper/models

# The unprivileged user the entrypoint drops to. Created here rather than assumed,
# with a fixed uid so the exported filesystem's ownership is the same on every build.
# No shell and no home: nothing in this service logs in.
RUN groupadd --gid 10001 ytt \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ytt

COPY ./service /service
RUN chmod 0755 /service/entrypoint.sh \
    && chmod 0644 /service/*.py

# The build-time smoke test: every binary this service will exec, actually exec'd,
# plus the model and the Python modules importing cleanly. It runs during the build,
# so an image that cannot possibly work never becomes a service id -- and it has
# already earned its place: it is what caught `libgomp1` missing above.
#
# Note the absence of `| head -1` on these. Piping a command into `head` moves the
# exit status to `head`'s, which is always 0, so under `set -e` a binary that fails
# to start is a build that succeeds. That is precisely how the libgomp failure first
# showed up as a passing build with an error message in the log. Output is discarded
# with `>/dev/null` where it is noisy, which does not touch the status.
RUN set -eu; \
    /opt/ffmpeg/bin/ffmpeg -version > /dev/null; \
    /opt/whisper/bin/whisper-cli --version > /dev/null 2>&1; \
    /usr/bin/python3 /opt/yt-dlp/bin/yt-dlp --version > /dev/null; \
    /usr/bin/python3 --version > /dev/null; \
    test -s /opt/whisper/models/ggml-base.bin; \
    command -v setpriv > /dev/null; \
    id ytt > /dev/null; \
    cd /service && /usr/bin/python3 -c "import config, urls, whisper, pipeline, server, resolver"; \
    setpriv --reuid 10001 --regid 10001 --clear-groups /bin/sh -c '\
        head -c 4 /opt/whisper/models/ggml-base.bin > /dev/null \
        && /opt/ffmpeg/bin/ffmpeg -version > /dev/null \
        && /opt/whisper/bin/whisper-cli --version > /dev/null 2>&1 \
        && /usr/bin/python3 /opt/yt-dlp/bin/yt-dlp --version > /dev/null'; \
    echo 'smoke test: binaries run, model readable by the service user, modules import'
