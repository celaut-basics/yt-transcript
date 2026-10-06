"""Deciding whether a string is a YouTube URL this service will fetch.

This is the security boundary. Everything else in the service -- the argv-only
subprocess calls, the per-request temp directory, the duration ceiling -- narrows
what a *fetch* can do; this is what decides whether a fetch happens at all.

It is its own module, with no imports beyond the standard library's URL parser, for
one reason: it is the part worth testing exhaustively, and a test that has to start a
server to reach it is a test nobody runs.

The rule, stated once: **the host is compared, after parsing, against a fixed set.**
Not a substring search, not a suffix check, not a regular expression over the whole
URL. Each of those is a way to say yes to `https://youtube.com.attacker.example/`.
"""

from typing import Optional, Set
from urllib.parse import urlsplit, parse_qs

from config import ALLOWED_HOSTS


class UrlError(ValueError):
    """The URL is not one this service will fetch, and why."""


def _host_of(url: str) -> Optional[str]:
    """The lowercased hostname, with no port, or None if the URL does not parse.

    `urlsplit().hostname` is used rather than `.netloc` because it is what strips
    userinfo and the port: `https://www.youtube.com@evil.example/` has a *netloc*
    beginning `www.youtube.com` and a *hostname* of `evil.example`, and reading the
    first is the classic way to get this wrong.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    # A trailing dot is the DNS root and resolves identically, so `youtube.com.` is
    # the same host to a resolver and must be the same host here.
    return host.rstrip(".").lower()


def validate(url: object, allowed: Optional[Set[str]] = None) -> str:
    """Return the URL if this service will fetch it; raise UrlError if it will not.

    Returns the *original* string, deliberately: what is handed to yt-dlp should be
    what was checked, not a reconstruction of it. Normalising and then fetching the
    normalised form means the thing validated and the thing fetched are two different
    strings, which is where this class of check usually breaks.
    """
    hosts = ALLOWED_HOSTS if allowed is None else allowed

    if not isinstance(url, str):
        raise UrlError("`url` must be a string.")

    text = url.strip()
    if not text:
        raise UrlError("`url` is empty.")

    # A bound on a value that becomes an argv entry and a network request. YouTube's
    # own URLs are far under this; a megabyte of query string is not a video id.
    if len(text) > 2048:
        raise UrlError("`url` is longer than 2048 characters.")

    # Control characters, including the newline that would let a URL smuggle a second
    # line into anything that logs it. Checked before parsing, because urlsplit
    # silently strips some of them (\t, \n, \r) and would hand back a *different*
    # string than the one being judged.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise UrlError("`url` contains control characters.")

    parts = urlsplit(text)

    # Scheme before host: `file:///etc/passwd` has no host at all, and the error a
    # caller gets should name the actual problem. yt-dlp will happily read a `file:`
    # URL, which is the reason this check is not left implicit.
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlError(
            f"scheme {parts.scheme or '<none>'!r} is not http or https."
        )

    host = _host_of(text)
    if host is None:
        raise UrlError("`url` has no host.")

    if host not in hosts:
        raise UrlError(
            f"host {host!r} is not a YouTube host. "
            f"Accepted: {', '.join(sorted(hosts))}."
        )

    return text


def video_id(url: str) -> Optional[str]:
    """The video id, for the response, or None if the URL does not carry one.

    Reported rather than *used*: what gets downloaded is the URL, and yt-dlp is what
    understands YouTube's URL shapes. This exists so a caller can correlate a
    response with a request without parsing the URL again, and it returning None is
    not an error -- a playlist URL or a `/live/` URL is still a thing yt-dlp resolves.

    The id itself is checked against YouTube's shape (11 characters of the URL-safe
    base64 alphabet) so that a value this service puts in a JSON response is one it
    can describe, rather than whatever happened to be after `v=`.
    """
    host = _host_of(url)
    if host is None:
        return None
    parts = urlsplit(url)

    candidate = None
    if host == "youtu.be":
        # https://youtu.be/<id>
        segments = [s for s in parts.path.split("/") if s]
        if segments:
            candidate = segments[0]
    else:
        query = parse_qs(parts.query)
        values = query.get("v")
        if values:
            candidate = values[0]
        else:
            # /shorts/<id>, /live/<id>, /embed/<id>
            segments = [s for s in parts.path.split("/") if s]
            if len(segments) >= 2 and segments[0] in ("shorts", "live", "embed"):
                candidate = segments[1]

    if not candidate or len(candidate) != 11:
        return None
    allowed_chars = set(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789-_"
    )
    if not set(candidate) <= allowed_chars:
        return None
    return candidate
