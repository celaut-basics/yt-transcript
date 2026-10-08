# What this service needs from the node

`yt-transcript` asks for little: one HTTP slot, open egress, two vCPUs and some
disk. It needs no capability, no device, no share and no dependency service. This
file lists what it needs, and then the facts about nodo that decided its `network`
declaration. Each fact was checked against the nodo source (`dev`, October 2026).

## From the node

| | |
|---|---|
| **egress** | `["*"]`. The section below and the README say why it cannot be narrower. |
| **DNS** | None from the node. The service names its own resolvers (see [2](#2-a-guest-has-no-resolver-at-all)). |
| **an API slot** | TCP 8080, `protocol: ["http"]`. Reach it at the address that `nodo instances` shows, or through [`nodo tunnel`](https://github.com/celaut-project/nodo/blob/dev/docs/TUNNELING.md). |
| **CPU** | Two vCPUs (`cpu_quota` 200000 / `cpu_period` 100000). nodo boots `ceil(cpu_quota / cpu_period)` vCPUs, and one vCPU when no quota is set. whisper uses one thread per vCPU. |
| **memory** | 600 MB at init, 1.5 GB at most. Measured peak: 297 MiB. |
| **disk** | 3 GB at init, 4 GB at most. The filesystem is 290 MB. The rest is the scratch space of one request, on the writable rootfs. |

## From the host

**Nothing.** No kernel configuration, no device node, no module and no native
application. Everything here is userspace work on a buffer.

**Not a GPU.** `celaut.Sysresources` has `mem_limit`, `disk_space`, `cpu_period`,
`cpu_quota`, `blkio_weight` and `benchmark`, and no accelerator field. A service
cannot declare that it needs a GPU, so whisper runs with `-ng` and the image holds no
GPU runtime. [`remote-browser`](https://github.com/celaut-basics/remote-browser) has
the same result for the same reason.

**Why not `read_only_filesystem`.** On a read-only rootfs, `/tmp` is a tmpfs, so the
scratch space of a request is RAM. One hour of audio needs about 145 MB of scratch,
and `--max-filesize` allows 512 MB for the download. On the default writable rootfs
the scratch space is disk, which this service declares.

---

## 1. A hostname tag grants addresses, not name resolution

This fact decided the `network` declaration. It applies to **every** service that
reaches a named host. nodo now documents it in `docs/NETWORKS.md` → *Hostname tags*
and `docs/PACKING.md` → `network` (nodo#389).

`tags: ["www.youtube.com"]` looks like the narrow, correct declaration. This is what
the node does with it:

- `resolve_network` → `resolve_domain` (`src/manager/networks.py`) resolves the name
  **on the node** to IPv4 addresses, on ports 80 and 443 (or on the port that the
  entry's `formal` gives as `port=<n>`).
- The firewall writes one allow per address.
- No rule opens port 53, and nodo serves no DNS
  (`src/virtualizers/microvm/network.py`).

yt-dlp gets a URL and calls `getaddrinfo()`. Under a hostname tag that call fails
before yt-dlp uses the allow. The declaration looks correct, and the service makes no
request at all. Also, YouTube sends media from hosts such as
`rN---sn-XXXXXXXX.googlevideo.com`, which change per request. No list of tags can
name them.

A glob such as `*.googlevideo.com` is not a solution. The packer refuses it at pack
time (nodo#391). Thus the service declares `"*"`, the only declaration that works for
a program that looks up names.

## 2. A guest has no resolver at all

`"*"` opens all egress (`allow_all_egress_rule`, `src/utils/firewall/policy.py`), UDP
53 included. But open egress does not give the guest a resolver:

- nodo writes no `/etc/resolv.conf` into a guest. The note in
  `src/virtualizers/microvm/network.py` says that name resolution is the job of the
  service.
- The image has the `/etc/resolv.conf` that BuildKit exported, which is empty or
  absent. glibc then asks `127.0.0.1:53`, where nothing listens.

Before this was found, the service passed every test under Docker (Docker supplies a
resolver) and would have failed every request under nodo.

**What the service does.** `service/entrypoint.sh` runs `service/resolver.py` as
root before it drops privileges. When `/__config__` exists, the node started the
guest, and the step writes three public resolvers (`9.9.9.9`, `1.1.1.1`,
`8.8.8.8`). `YT_DNS_SERVERS` replaces them, for example with a resolver on the LAN of
the operator. Without `/__config__` and without `YT_DNS_SERVERS`, the step changes
nothing, so the Docker resolver stays.

**What would remove the need.** A DNS service that reads `network_resolution` and
answers for its siblings, which the `network.py` note proposes. It does not exist
yet. When it does, a narrower declaration can become possible.

**Decision** (maintainer, 2026-10-08, #5): keep the public resolvers as the default,
with the `YT_DNS_SERVERS` override, until the node has that DNS service. Then the
service uses the node's DNS service in place of the public resolvers.

## 3. Facts from the first version of this file that changed in nodo

The first version of this file reported two more problems. Current nodo has resolved
both, so this service no longer works around them:

- **Tags of one entry are synonyms.** `resolve_network` uses the first tag that
  resolves. nodo now documents this as the design: one entry is one destination
  under several names, and two destinations are two entries.
- **A glob no longer aborts a launch.** The packer refuses `*.example.com` at pack
  time (nodo#391). At launch, a name that does not resolve gives no peers for that
  tag and is written to the log. The launch continues.

---

## What the service does instead of a narrow declaration

It declares `["*"]`, gives the reason in the `prose`, and narrows the egress
**inside** the service, where it can enforce the limit:

- yt-dlp is the only program in the image that opens a socket.
- `ffmpeg` is built `--disable-network`. Its protocol list is `file` only, and the
  image tests check this.
- The URL must name one of **five exact hostnames**, compared after parsing, before
  any request (`service/urls.py`, and most of `tests/test_urls.py`).
- No shell runs anywhere. Every subprocess gets an argv list and a replaced
  environment, so an inherited `http_proxy` cannot send a fetch somewhere else.
  `--ignore-config` stops a yt-dlp config file from adding options.
  `--use-extractors default,-generic` removes the generic extractor, which would
  follow the links and redirects of a page to hosts that the allow-list never saw.
- No cookie and no credential exists in the image.

An operator who wants the node to enforce a limit uses `service_networks` in
`config.yaml`:

- **Whitelist.** A non-empty whitelist must match every tag (`docs/NETWORKS.md`).
  The policy matches each pattern with `fnmatch` against the tag `*`. A pattern such
  as `*youtube.com` does not match it, so the whitelist must hold `"*"`, which
  matches every tag.
- **`blacklist: ["*"]`** ("nothing beyond this node"). The node refuses this service
  at launch, with a message that names the rule. That is correct: a service that
  cannot reach YouTube has nothing to give.

## Children

This service starts no child services, so the ancestor chain
(`filter_networks_with_ancestors`) does not apply to it. If a parent service starts
`yt-transcript` as a dependency, that parent must also declare `["*"]`. Otherwise
this service boots with no egress, and every request fails with a 502 from yt-dlp.
