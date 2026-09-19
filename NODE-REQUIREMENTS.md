# What this service needs from the node, and three things the spec cannot express

`yt-transcript` asks for very little: one HTTP slot, egress, and ordinary CPU. It
needs no capability, no device, no share, no dependency service. What follows is the
short list, and then the findings that are really about nodo rather than about this
service — written down because they were found by building against this checkout, and
because two of them would silently produce a service that looks configured and does
not work.

## From the node

| | |
|---|---|
| **egress** | `["*"]`. Why it cannot be narrower is below and in the README. |
| **an API slot** | TCP 8080, `protocol: ["http"]`. Reached however the operator reaches any service — a published port, or [`nodo tunnel`](https://github.com/celaut-project/nodo/blob/master/docs/TUNNELING.md). |
| **CPU** | Whatever the operator grants. The service reads its own CPU count and sizes whisper's thread pool to it, so a `cpu_quota` is honoured rather than oversubscribed. |
| **memory** | 600 MB at init, 1.5 GB at most. Measured peak: 297 MiB. |
| **disk** | 3 GB at init, 4 GB at most. The filesystem is 290 MB; the rest is one request's scratch. |

## From the host

**Nothing.** No kernel config, no device node, no module, no native application. This
is deliberate and is the difference between this service and
[`remote-browser`](https://github.com/celaut-basics/remote-browser)'s `stream/`, which
needs `CONFIG_INPUT_UINPUT`. Everything here is userspace arithmetic on a buffer.

**Not a GPU.** `celaut.Sysresources` has `mem_limit`, `disk_space`, `cpu_period`,
`cpu_quota` and `blkio_weight` and no accelerator field — there is no way to declare
that an instance needs one, so a service that needed one could not be scheduled
anywhere on this network. whisper is invoked with `-ng` and the image contains no GPU
runtime. `remote-browser` reached the same conclusion for the same reason.

---

## 1. A hostname tag grants addresses the guest cannot resolve

This is the finding that decided this service's `network` declaration, and it is not
specific to YouTube — it applies to **any** service that reaches a named host.

Declaring `tags: ["www.youtube.com"]` looks like the right, narrow thing to do. What
the node actually does with it:

- `resolve_network` → `resolve_domain` (`src/manager/networks.py`) resolves the tag
  **on the node**, to IPv4 A records, and builds `Instance.Uri` entries for ports
  **80 and 443**;
- the firewall writes one allow per address (`allow_connection_to_instance`), on the
  **forward** hook.

And then, from `src/virtualizers/microvm/network.py`:

> There is no rule for port 53: nodo does not serve DNS, and a guest that wants name
> resolution gets it from a service […] or inside its own container.

So the guest is granted **addresses**, over TCP, for hosts it has no way to **look
up** — `block_all` covers `("tcp", "udp")`, and nothing opens 53. Any program that
calls `getaddrinfo()` fails before it ever uses the allow it was given. The
declaration reads as a tight, correct confinement and produces a service that cannot
make a single request.

The module comment explains the reasoning, and it is sound: name resolution is not
nodo's business, the node delivers the *data* in `ConfigurationFile.network_resolution`,
and a service should read it from `__config__` rather than have a glibc convention
injected into a filesystem the node does not own. `ergo-node` works exactly that way —
it reads peers out of `__config__` and hands them to a program that takes addresses.

**The gap is for programs that take names rather than addresses.** `yt-dlp` is handed
a URL; `curl` is handed a URL; a TLS client needs the name for SNI and certificate
validation regardless. For those, the resolution in `__config__` is not usable
without a DNS server inside the guest to serve it — which is the "a service reads
`network_resolution` and serves DNS from it" design the comment points at, and which
does not exist yet as something a service can depend on.

Worth knowing: `resolve_domain` produces **ports 80 and 443 only**, hardcoded, with a
`TODO` noting it should come from the protocol stack. A hostname tag cannot express any
other port.

## 2. Only the first tag in a `network` entry is ever used

`resolve_network` walks `network.tags` and stops at the first that resolves:

```python
uris = resolve_domain(tag)
if uris:
    break
```

So an entry in the [`demo-service`](https://github.com/celaut-basics/demo-service)
style —

```json
{"tags": ["google.com", "www.google.com"], "prose": "..."}
```

— grants **`google.com`'s addresses only**. `www.google.com` is never resolved, and on
a domain where those differ (they often do, and for YouTube's five hosts they
certainly do) the guest is silently missing four of the five destinations it declared.

`demo-service` itself is shaped this way, and so is `ping`. They work because the
first tag is the one that matters to them.

No error, no warning, nothing in the log to distinguish "granted one of five" from
"granted five". The firewall logs one line per *entry*, not per tag.

`docs/NETWORKS.md` says something adjacent and stronger — "Every tag must pass […] A
network is not one destination, it is as many as it names" — but that is describing the
**operator policy** check in `service_networks`, which walks every tag. The *resolver*
does not. Two parts of the same file mean different things by "the tags", which is
worth reconciling.

**If this is intended**, `docs/PACKING.md`'s `network` section should say that extra
tags in one entry are alternates rather than a set, and the examples should stop
showing two hostnames in one entry. **If it is not**, the fix is to accumulate rather
than break.

## 3. A wildcard hostname is not unsupported — it aborts the launch

There is no syntax for `*.googlevideo.com`, which is the shape a CDN needs. That much
is a missing feature. The failure mode is the part to know:

`resolve_domain` raises `ValueError("Cannot resolve domain: …")` on a name that does
not resolve, and `*.googlevideo.com` does not — a wildcard is not a name a resolver
ever answers. Verified by running the resolver's own logic against that tag.

Nothing between `resolve_domain` and `build_network_resolution` catches it, so it
leaves the resolution path. What it reaches is the broad
`except Exception` wrapping `ch/execute.py`'s launch, which logs
`execute failed: ValueError: Cannot resolve domain: *.googlevideo.com`, tears down the
VM's firewall rules, and fails the launch.

So a service that tries the natural thing does not get a warning and a dropped
network — it gets a failed launch, reported as a DNS error naming a hostname, rather
than as "this declaration is not something the resolver supports". The distance
between the message and the cause is the cost: the declaration is in `service.json`
and the error is about resolving a name nobody meant literally.

(A tag with no dot, or with an uppercase letter, is *skipped* silently by the
`not tag.islower() or '.' not in tag` guard instead, so it never reaches
`resolve_domain`. `*` takes that path, which is why open egress works: it resolves to
no URIs, and the firewall matches the literal `"*"` separately in
`configure_guest_firewall_policy`.)

---

## What this service does instead

Declares `["*"]`, says why in the `prose`, and does the narrowing **inside** the
service, where it can actually be enforced:

- exactly one program in the image opens a socket (`yt-dlp`);
- `ffmpeg` is built `--disable-network` — its protocol list is `file`, asserted by the
  image tests — so the component that parses untrusted bytes cannot fetch any;
- the URL is checked against **five exact hostnames** after parsing, on
  `urlsplit().hostname`, before any request (`service/urls.py`, and the bulk of
  `tests/test_urls.py`);
- no shell, anywhere; every subprocess takes an argv list and a replaced environment,
  so an inherited `http_proxy` cannot redirect a fetch;
- no cookies and no credentials exist to leak.

An operator who wants the node to enforce it rather than trust it has
`service_networks` in `config.yaml`. Under a **whitelist**, this service needs `"*"`
explicitly listed — `docs/NETWORKS.md` is clear that a non-empty whitelist must cover
every tag, and `*` is matched as a tag here, not as a glob. Under
`blacklist: ["*"]` — "nothing beyond this node" — it is refused, correctly: a service
that cannot reach YouTube has nothing to offer, and being refused at launch with a
message naming the rule is the right outcome.

## One thing worth adding, if `grant_only` ever lands

`remote-browser`'s `NODE-REQUIREMENTS.md` asks for a `grant_only` flag, because
declaring a network both **grants** it to children and **takes** it for the declaring
VM. This service has no children, so it is unaffected — but the inverse of its problem
is what a narrower declaration here would need. If a DNS-serving sidecar ever becomes
the ecosystem's answer to finding #1, a service like this one would want to declare
*"my child may reach `*`, I may not"*, which is the same missing expressiveness seen
from the other side.
