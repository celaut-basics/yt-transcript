"""Give the guest a DNS resolver, because under nodo nothing else does.

yt-dlp is handed a URL, and the first thing it does with it is `getaddrinfo()`. In a
nodo microVM that call has nowhere to go:

- nodo serves no DNS and writes no /etc/resolv.conf into the guest. The note in
  `src/virtualizers/microvm/network.py` says why: name resolution is the service's
  job, "inside its own container" or from another service.
- The image's own /etc/resolv.conf is what BuildKit exported, which is empty or
  absent. glibc then asks 127.0.0.1:53, where nothing listens.

The `"*"` network this service declares opens all egress, UDP 53 included, so a
public resolver is reachable. What is missing is only the file that names one. This
module writes it, once, at start, as root, before `entrypoint.sh` drops privileges.

The rule is small on purpose:

- `YT_DNS_SERVERS` set: write those servers, wherever this runs.
- Not set, and `/__config__` exists (the node wrote it, so this is a nodo guest):
  write `config.DEFAULT_DNS_SERVERS`.
- Not set, and no `/__config__` (Docker, a test harness): change nothing. That
  runtime already gives the container a working resolver, and replacing it would
  break, for example, Docker's embedded DNS on a user-defined network.

Kept apart from the server because it is the one part of this service that runs as
root, and the less code that does, the better.
"""

import os
import sys
from typing import Dict, Iterable, Optional, Tuple

import config

RESOLV_CONF = "/etc/resolv.conf"
# The default `config_declaration` path. nodo puts the ConfigurationFile there in
# every guest, and `/init` refuses to boot one without it, so its presence is a
# reliable sign that a node, and not some other runtime, started this.
NODE_CONFIG = "/__config__"


def plan(env: Dict[str, str], under_node: bool) -> Optional[Tuple[str, ...]]:
    """The servers to write, or None to leave the existing file alone."""
    explicit = config.dns_servers(env)
    if explicit:
        return explicit
    if under_node:
        return config.DEFAULT_DNS_SERVERS
    return None


def render(servers: Iterable[str]) -> str:
    """The resolv.conf text for these servers.

    `timeout:2 attempts:2` because glibc's default (5 s, 2 attempts, per server) lets
    one unreachable resolver hold a request for 10 s before the next is tried.
    """
    lines = ["# Written by yt-transcript at start (service/resolver.py)."]
    lines += [f"nameserver {server}" for server in servers]
    lines.append("options timeout:2 attempts:2")
    return "\n".join(lines) + "\n"


def write(text: str, path: str = RESOLV_CONF) -> None:
    """Replace the file. A symlink is removed first rather than written through."""
    if os.path.islink(path):
        os.unlink(path)
    with open(path, "w", encoding="ascii") as handle:
        handle.write(text)
    os.chmod(path, 0o644)


def main(
    env: Optional[Dict[str, str]] = None,
    path: str = RESOLV_CONF,
    node_config: str = NODE_CONFIG,
) -> int:
    env = dict(os.environ if env is None else env)
    try:
        servers = plan(env, under_node=os.path.isfile(node_config))
    except config.ConfigError as e:
        sys.stderr.write(f"[yt-transcript] FATAL: {e}\n")
        return 2
    if servers is None:
        sys.stderr.write(
            "[yt-transcript] not under a node and YT_DNS_SERVERS is unset: "
            "keeping the runtime's resolver\n"
        )
        return 0
    try:
        write(render(servers), path)
    except OSError as e:
        sys.stderr.write(f"[yt-transcript] FATAL: cannot write {path}: {e}\n")
        return 2
    sys.stderr.write(f"[yt-transcript] DNS resolvers: {' '.join(servers)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
