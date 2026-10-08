"""What the environment is allowed to say, and what it means when it says nothing.

Separated from the server so it can be tested without opening a socket, and so the
whole contract of `envs` in `.service/service.json` is one file to read.

Every value is read once, at start, and refused loudly rather than clamped. A
service that silently ran with `YT_MAX_DURATION_S=abc` treated as the default would
be a service whose declared limit is not the limit it enforces -- and the limit is
the only thing standing between "transcribe this clip" and "download an eight-hour
livestream into an instance with a 4 GB disk".
"""

import ipaddress
import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

# The hosts a URL may name. Not a convenience list -- it is the security boundary
# this service is built around, and it is checked against the parsed URL's host
# rather than against the string, so `https://evil.example/?x=youtube.com` is not a
# YouTube URL and neither is `https://youtube.com.evil.example/`.
#
# `youtube-nocookie.com` is deliberately absent: it is the privacy-embed domain, it
# serves the same videos under the same ids, and a caller who has one can send the
# ordinary `watch?v=` form. One less host in the boundary for no lost capability.
ALLOWED_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
})

# Whisper's own list, from `whisper.cpp`'s `whisper.cpp:g_lang` (v1.9.4) -- the same
# set OpenAI's model card documents. `auto` is this service's spelling of "let the
# model decide", which whisper-cli spells `auto` too.
#
# Validated rather than passed through because `YT_LANGUAGE` reaches `whisper-cli`'s
# argv: it is an env var the node's operator sets, not caller input, but an argv this
# service builds is an argv this service should be able to state the shape of.
_LANG_RE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz-")

# The resolvers the guest uses when the node gives it none. nodo serves no DNS and
# writes no /etc/resolv.conf into a guest (`src/virtualizers/microvm/network.py`),
# so under a node the image's own file is all there is, and an image built by
# BuildKit carries no usable one. Three operators, so that one outage or one block
# does not stop every download. Three is also the limit: glibc reads at most three
# `nameserver` lines (MAXNS) and ignores the rest without a word.
DEFAULT_DNS_SERVERS = ("9.9.9.9", "1.1.1.1", "8.8.8.8")
MAX_DNS_SERVERS = 3


class ConfigError(ValueError):
    """The environment this service was launched with cannot be honoured."""


def _int_env(
    env: Dict[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    try:
        value = int(text, 10)
    except ValueError:
        raise ConfigError(
            f"{name}={text!r} is not a whole number. "
            f"Leave it unset for the default ({default})."
        ) from None
    if value < minimum or value > maximum:
        raise ConfigError(
            f"{name}={value} is outside the accepted range "
            f"[{minimum}, {maximum}]."
        )
    return value


def dns_servers(env: Dict[str, str]) -> Optional[Tuple[str, ...]]:
    """`YT_DNS_SERVERS` as a tuple of IP addresses, or None if it is not set.

    The value is one to three IP addresses, separated by spaces or commas. Each one
    is parsed as an address, not matched as text: it is written into
    /etc/resolv.conf by root, so a value that could carry a second line or a
    hostname is refused rather than written.
    """
    raw = env.get("YT_DNS_SERVERS")
    if raw is None or not raw.strip():
        return None
    tokens = raw.replace(",", " ").split()
    if len(tokens) > MAX_DNS_SERVERS:
        raise ConfigError(
            f"YT_DNS_SERVERS names {len(tokens)} servers. glibc reads at most "
            f"{MAX_DNS_SERVERS}, so give {MAX_DNS_SERVERS} or fewer."
        )
    servers = []
    for token in tokens:
        try:
            servers.append(str(ipaddress.ip_address(token)))
        except ValueError:
            raise ConfigError(
                f"YT_DNS_SERVERS: {token!r} is not an IP address. Give addresses, "
                "not names: nothing can resolve a name before a resolver is set."
            ) from None
    return tuple(servers)


def available_cpus() -> int:
    """The CPUs this process may run on, which is not always every CPU the host has.

    `sched_getaffinity` honours a cpuset (`docker run --cpuset-cpus`), where
    `os.cpu_count()` does not. In a nodo microVM the two agree: the VM is booted with
    `ceil(cpu_quota / cpu_period)` vCPUs from `resources.at_init`.
    """
    try:
        return len(os.sched_getaffinity(0)) or 1
    except (AttributeError, OSError):
        return os.cpu_count() or 1


@dataclass(frozen=True)
class Config:
    port: int
    max_duration_s: int
    threads: int
    language: str
    request_timeout_s: int
    # Read here so that a bad value also stops the server, not only the entrypoint
    # step that writes it (`service/resolver.py`). Empty means "not set".
    dns_servers: Tuple[str, ...] = ()

    @property
    def model_path(self) -> str:
        return MODEL_PATH


# Where the Dockerfile puts the model and the two binaries. Not configurable: they
# are part of the content-addressed filesystem, and a path that could be pointed
# somewhere else would be a way to run a model this spec did not pin.
MODEL_PATH = "/opt/whisper/models/ggml-base.bin"
WHISPER_BIN = "/opt/whisper/bin/whisper-cli"
FFMPEG_BIN = "/opt/ffmpeg/bin/ffmpeg"
YTDLP_BIN = "/opt/yt-dlp/bin/yt-dlp"
# The JavaScript runtime that yt-dlp runs YouTube's challenge script in.
DENO_BIN = "/opt/deno/bin/deno"


def load(
    env: Optional[Dict[str, str]] = None,
    cpu_count: Optional[Callable[[], int]] = None,
) -> Config:
    """Read the environment into a Config, or raise ConfigError explaining why not.

    `cpu_count` is injected so the thread default can be tested without depending on
    the machine the tests run on.
    """
    env = dict(os.environ if env is None else env)
    count = cpu_count or available_cpus

    port = _int_env(env, "YT_PORT", default=8080, minimum=1, maximum=65535)

    # 3600 s because that is the length at which this service's *declared* resources
    # stop being honest rather than a number chosen for taste. An hour of Opus at
    # YouTube's ~64 kbit/s is ~29 MB, its 16 kHz mono PCM intermediate is 115 MB, and
    # whisper base transcribes it in roughly real time on the 2 vCPU this declares --
    # all of which fit what `.service/service.json` asks for. Four hours does not.
    max_duration_s = _int_env(
        env, "YT_MAX_DURATION_S", default=3600, minimum=1, maximum=86400
    )

    # 0 means "as many as the instance was given". whisper.cpp's own default is 4,
    # which on a 2 vCPU instance oversubscribes and on a 16-core host leaves most of
    # it idle; neither is what the operator asked for by setting a CPU quota.
    threads = _int_env(env, "YT_WHISPER_THREADS", default=0, minimum=0, maximum=256)
    if threads == 0:
        threads = max(1, count())

    language = (env.get("YT_LANGUAGE") or "auto").strip().lower()
    # The leading-hyphen check is the one that matters, and it is not pedantry: this
    # value is passed as the operand of `-l` in whisper-cli's argv, so a value of
    # `-m` would be read by the option parser as the *next flag* rather than as this
    # one's argument -- turning a language setting into a way to choose the model
    # file. Caught by `tests/test_config.py`, which is why the check is here and not
    # only in the character set.
    if (
        not language
        or not set(language) <= _LANG_RE_CHARS
        or language.startswith("-")
        or language.endswith("-")
    ):
        raise ConfigError(
            f"YT_LANGUAGE={language!r} is not a language code such as 'en', 'es' "
            "or 'auto'."
        )

    # The whole request, end to end: probe, download, decode, transcribe. Default is
    # 4x the duration ceiling because whisper base runs at roughly real time on the
    # declared CPU and the download is not free either -- a timeout under the work it
    # authorises would fail every long request by construction.
    request_timeout_s = _int_env(
        env,
        "YT_REQUEST_TIMEOUT_S",
        default=max_duration_s * 4,
        minimum=1,
        maximum=86400 * 2,
    )

    return Config(
        port=port,
        max_duration_s=max_duration_s,
        threads=threads,
        language=language,
        request_timeout_s=request_timeout_s,
        dns_servers=dns_servers(env) or (),
    )
