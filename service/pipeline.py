"""URL in, transcript out: probe, download, decode, transcribe.

Four subprocesses, each of them handed a list and never a string. There is no shell
anywhere in this service -- not `shell=True`, not a `sh -c`, not a format string that
becomes a command -- because the one input this service takes is a URL from an
untrusted caller and the one thing it does with it is pass it to a downloader.

The order matters and is the point of the probe step:

1. **Probe** the URL for metadata only (`--skip-download`). Nothing is fetched but a
   JSON blob, and the duration in it is checked against the ceiling.
2. **Download** the audio track only, at a bounded size, into a directory that exists
   for this request.
3. **Decode** to the 16 kHz mono PCM whisper wants. whisper.cpp reads a handful of
   formats itself, but "a handful" is decided by what YouTube served; converting
   first means exactly one audio path through this service.
4. **Transcribe**, then delete the directory.

Checking the duration before downloading rather than after is what makes the limit a
limit. A ceiling enforced on a file already on disk is a disk-full waiting to happen
on an instance that declared 4 GB.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import whisper as whisper_output


class PipelineError(RuntimeError):
    """A request failed. `status` is what the caller should be told."""

    def __init__(self, message: str, status: int = 502, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


class Deadline:
    """How long is left of this request's budget.

    Passed down rather than re-derived at each step: four steps each given the full
    timeout is a request that can take four times as long as the one number that was
    supposed to bound it.
    """

    def __init__(self, seconds: float, now=time.monotonic):
        self._now = now
        self._end = now() + seconds

    def remaining(self) -> float:
        return self._end - self._now()

    def check(self, step: str) -> float:
        left = self.remaining()
        if left <= 0:
            raise PipelineError(
                f"request timed out before {step}",
                status=504,
            )
        return left


def _run(
    argv: List[str],
    deadline: Deadline,
    step: str,
    cwd: Optional[str] = None,
) -> Tuple[int, str, str]:
    """One subprocess, argv-only, bounded by what is left of the request budget.

    `env` is replaced rather than inherited. The instance's environment holds this
    service's own configuration and whatever else the node put there; none of it is
    anything yt-dlp or ffmpeg needs, and a downloader that reads `http_proxy` out of
    an environment this service did not curate is a downloader with a destination
    this service did not declare.
    """
    timeout = deadline.check(step)
    clean_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": cwd or "/tmp",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        # yt-dlp writes a cache; point it inside the request's own directory so it
        # goes when the directory does, rather than accumulating in $HOME.
        "XDG_CACHE_HOME": os.path.join(cwd, ".cache") if cwd else "/tmp/.cache",
    }
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=clean_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PipelineError(f"{step} exceeded the request timeout", status=504) from None
    except FileNotFoundError as e:
        raise PipelineError(f"{step}: {e.filename} is not present in this image", status=500) from None
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


def _tail(text: str, lines: int = 3, limit: int = 600) -> str:
    """The last few lines of a tool's stderr, bounded.

    Returned to the caller because a download that failed because the video is
    private and one that failed because YouTube refused this IP are different
    problems, and a service that flattens both to "download failed" makes its
    operator guess. Bounded because it is attacker-influenced text going into a JSON
    response.
    """
    kept = [line for line in text.strip().splitlines() if line.strip()][-lines:]
    joined = " | ".join(kept)
    return joined[:limit]


# The options every yt-dlp call gets, so the two calls cannot drift apart.
_YTDLP_COMMON = (
    "--no-playlist",              # a watch URL inside a playlist is one video
    "--ignore-config",            # no config file can add options to this argv
    # The generic extractor is what yt-dlp falls back to for any URL that no site
    # extractor claims. It fetches the page and follows the links, redirects and
    # embeds in it, so it could send this process to a host that `urls.py` never
    # allowed. Without it, a URL that no YouTube extractor claims is refused by
    # yt-dlp, with no request.
    "--use-extractors", "default,-generic",
    "--no-warnings",
    "--no-progress",
    # `--no-call-home` is deliberately absent: it is deprecated as of the pinned
    # 2026.08.19 and prints a deprecation notice on every invocation, which ends
    # up in the `detail` this service returns to callers. yt-dlp phones home
    # nowhere by default now, so the flag bought nothing and cost a warning.
    "--no-cookies",               # and nothing reads a cookie file: see README
    "--no-cookies-from-browser",
    "--socket-timeout", "30",
    "--retries", "2",
)


def probe(url: str, cfg: config.Config, deadline: Deadline) -> Dict[str, Any]:
    """Metadata only. Nothing is downloaded, and the duration decides the rest."""
    argv = [
        config.YTDLP_BIN,
        "--skip-download",
        "--dump-single-json",
        *_YTDLP_COMMON,
        # A URL that is only a playlist or a channel (`/playlist?list=`, `/@name`) is
        # still a playlist under --no-playlist. Without this, the probe extracts
        # every entry of it, which for a channel is thousands of requests, before
        # the playlist is refused below.
        "--flat-playlist",
        "--",                     # everything after this is an operand, not a flag
        url,
    ]
    code, out, err = _run(argv, deadline, "metadata probe")
    if code != 0:
        raise PipelineError(
            "could not read the video's metadata",
            status=502,
            detail=_tail(err),
        )
    try:
        info = json.loads(out)
    except ValueError:
        raise PipelineError("yt-dlp returned metadata that is not JSON", status=502) from None
    if not isinstance(info, dict):
        raise PipelineError("yt-dlp returned metadata that is not an object", status=502)

    if info.get("_type") in ("playlist", "multi_video"):
        raise PipelineError(
            "the URL is a playlist or a channel, not one video. Send the URL of "
            "one video",
            status=422,
        )

    # A live stream has no duration and no end; it is refused rather than started.
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise PipelineError(
            "the URL is a live or upcoming stream, which has no duration to bound",
            status=422,
        )

    duration = info.get("duration")
    if duration is None:
        raise PipelineError(
            "the video reports no duration, so it cannot be checked against "
            f"YT_MAX_DURATION_S ({cfg.max_duration_s}s)",
            status=422,
        )
    try:
        duration_s = float(duration)
    except (TypeError, ValueError):
        raise PipelineError("the video's duration is not a number", status=502) from None

    if duration_s > cfg.max_duration_s:
        raise PipelineError(
            f"the video is {int(duration_s)}s long, over this instance's "
            f"YT_MAX_DURATION_S of {cfg.max_duration_s}s",
            status=413,
        )

    return {
        "duration_s": duration_s,
        "video_id": info.get("id") if isinstance(info.get("id"), str) else None,
        "title": info.get("title") if isinstance(info.get("title"), str) else None,
    }


def download_audio(url: str, workdir: str, deadline: Deadline) -> str:
    """The audio track, and only the audio track, into this request's directory.

    `-f bestaudio/best` asks YouTube's servers for an audio-only stream, so a
    two-hour 4K video moves ~50 MB rather than ~8 GB. `--max-filesize` is the
    backstop for the case where the format selector picks something unexpected: the
    duration ceiling bounds *time*, this bounds *bytes*, and the instance's disk is
    finite in bytes.
    """
    template = os.path.join(workdir, "audio.%(ext)s")
    argv = [
        config.YTDLP_BIN,
        *_YTDLP_COMMON,
        "-f", "bestaudio/best",
        "--no-part",
        "--max-filesize", "512m",
        "-o", template,
        "--",
        url,
    ]
    code, _out, err = _run(argv, deadline, "audio download", cwd=workdir)
    if code != 0:
        raise PipelineError(
            "could not download the audio track",
            status=502,
            detail=_tail(err),
        )

    downloaded = sorted(
        name for name in os.listdir(workdir)
        if name.startswith("audio.") and not name.endswith(".cache")
    )
    if not downloaded:
        raise PipelineError(
            "yt-dlp reported success but wrote no audio file "
            "(--max-filesize may have skipped it)",
            status=502,
        )
    return os.path.join(workdir, downloaded[0])


def to_wav(source: str, workdir: str, deadline: Deadline) -> str:
    """16 kHz mono signed-16 PCM, which is the only thing whisper.cpp accepts.

    The rate is not a preference: whisper's mel front-end is built for 16 kHz and
    whisper.cpp refuses anything else outright (`WHISPER_SAMPLE_RATE`).
    """
    target = os.path.join(workdir, "audio.wav")
    argv = [
        config.FFMPEG_BIN,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-i", source,
        "-vn",                    # drop any video stream the container carries
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        "-f", "wav",
        "-y",
        target,
    ]
    code, _out, err = _run(argv, deadline, "audio decode", cwd=workdir)
    if code != 0:
        raise PipelineError(
            "could not decode the downloaded audio",
            status=502,
            detail=_tail(err),
        )
    if not os.path.exists(target) or os.path.getsize(target) == 0:
        raise PipelineError("the decoded audio file is empty", status=502)
    return target


def transcribe(wav: str, workdir: str, cfg: config.Config, deadline: Deadline) -> Dict[str, Any]:
    """whisper.cpp over the wav, read back from the JSON it writes."""
    prefix = os.path.join(workdir, "transcript")
    argv = [
        config.WHISPER_BIN,
        "-m", config.MODEL_PATH,
        "-f", wav,
        "-l", cfg.language,
        "-t", str(cfg.threads),
        "-oj",
        "-of", prefix,
        "-np",                    # nothing on stdout but what is asked for
        "-nt",
        "-ng",                    # no GPU: celaut.Sysresources cannot declare one
    ]
    code, _out, err = _run(argv, deadline, "transcription", cwd=workdir)
    if code != 0:
        raise PipelineError(
            "transcription failed",
            status=500,
            detail=_tail(err),
        )

    path = prefix + ".json"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as e:
        raise PipelineError(f"whisper-cli wrote no JSON output: {e}", status=500) from None

    try:
        return whisper_output.parse(raw)
    except whisper_output.WhisperOutputError as e:
        raise PipelineError(str(e), status=500) from None


def run(url: str, cfg: config.Config, now=time.monotonic) -> Dict[str, Any]:
    """The whole thing, in one request's own directory, which is always removed."""
    deadline = Deadline(cfg.request_timeout_s, now=now)
    started = now()

    metadata = probe(url, cfg, deadline)

    workdir = tempfile.mkdtemp(prefix="yt-transcript-")
    try:
        os.chmod(workdir, 0o700)
        source = download_audio(url, workdir, deadline)
        wav = to_wav(source, workdir, deadline)
        result = transcribe(wav, workdir, cfg, deadline)
    finally:
        # Unconditional: a failed request must not leave an instance's disk holding
        # a partial download, and the next request should find the disk it was
        # promised.
        shutil.rmtree(workdir, ignore_errors=True)

    return {
        "text": result["text"],
        "segments": result["segments"],
        "language": result["language"],
        "video_id": metadata["video_id"],
        "duration_s": metadata["duration_s"],
        "elapsed_s": round(now() - started, 2),
    }
