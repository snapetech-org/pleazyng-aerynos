# Pleazyng for aerynOS

Independent x86_64 packages for aerynOS. This is a real, installable downstream
preview channel, separate from the official distribution repository.

## Install

Tested on aerynOS 2026.11. Review [qualification.json](qualification.json) and the
[recipe](recipe/stone.yaml) before enabling this preview.

```sh
sudo moss repo add pleazyng-aerynos https://github.com/snapetech-org/pleazyng-aerynos/releases/download/snapshot-3.0.22-181-r1/stone.index
sudo moss -y pkg add pleazyng
```

If Pleazyng is already installed, inspect `sudo moss sync --dry-run`, then use
`sudo moss -y sync` to apply the selected updates. Moss `pkg add` alone does not
upgrade an installed version. Each snapshot also supplies a versioned
`stone.index` for clients that prefer a pinned package. The
[last-good receipt](sync-state/last-good.json) records the current qualified
source and feed URLs.

The initial seed passed public feed fetch, checksum comparison, removal/reinstall
and ordinary relaunch; see its [seed receipt](seed-publication-receipt.json).
Current source and per-snapshot checks appear in the last-good receipt.

## Build the recipe

On a configured aerynOS Boulder host:

```sh
git clone https://github.com/snapetech-org/pleazyng-aerynos.git
cd pleazyng-aerynos/recipe
boulder build stone.yaml
```

The `pkg/` directory is required; keep it beside `stone.yaml`.

## Qualification history

The initial seed package is **3.0.22+181**, package release **1**, built from source
snapshot `a13b139cd75b8521deedb8dc8ee00928754aac11`. The release carries the exact
tested `.stone`, Moss index, original flat application archive, SHA-256 list and
complete Pleazyng source archive. [Native input pins](source-inputs) identify
separately built components; their build scripts, licenses and upstream source
locations are retained in the source archive and installed notices.

Boulder build, Moss upgrade, 402 ELF dependency checks, visible H.264 playback,
audio, pause/seek/fullscreen and persisted connection pass. With no preference,
DV/FEL and compatibility fallback are On. An explicit Off survives upgrade.
The authored Profile 7 BL+EL fixture displays via `opengl-next`. Rejecting only
experimental renderer creation with the [QA shim](evidence/render-fault.c)
selects legacy `opengl` automatically and still displays the pattern. The shim
is removed after testing and is not part of the package.

This is software rendering in a VM. Physical HDR output, hardware decoding and
full FEL reference picture accuracy are unqualified. This preview has no release
attestation and is not an all-platform core application release.

The immutable v3.0.7 input failed visible-video QA and is not distributed here.
See the [historical record](https://github.com/snapetech-org/pleazyng/blob/main/docs/maintainers/aerynos-qualification.md).

## Native input qualification

Fresh sign-in uses a real pointer click in the observed password field before
keyboard typing. A semantic accessibility Tap alone can leave the native
Wayland window without keyboard focus. The QA harness also keeps one
accessibility client connected throughout the scenario; repeated short-lived
clients reproduced a GTK event-listener crash during key navigation.

The seed bundle separately lacked its input-method module cache. New recipes
build and relocate the cache against all shipped GTK modules, preserving locale
input methods. No simple input-method override is part of qualification.

The corrected fresh native scenario passes locally. Each published snapshot's
qualification receipt lists its own completed checks. Earlier seed
saved-connection results do not retroactively establish fresh-login qualification.

## Automatic release delivery

Canonical `main` is tracked independently in
[sync-state/canonical-main.json](sync-state/canonical-main.json). A new commit
updates source metadata without compiling it or changing the package feed.
This remains distinct from the original Linux build source and final release
assembler recorded in each delivery receipt.

The delivery controller checks published canonical releases every five minutes,
subject to GitHub scheduling and runner availability. It processes releases from
3.0.29 onward in version order. It verifies the attested release checksums and successful final core workflow,
then verifies the published Linux archive and its original compiler attestation, then reuses the original core Linux artifact. It never recompiles
the application or starts an all-platform build.

Every candidate must pass a fresh native aerynOS Boulder build, Moss install and
upgrade, dependency checks, sign-in, visible playback and audio, pause, seek,
fullscreen, background return, saved-position resume and bookmark resume.
Only then are immutable package assets published and the feed advanced.
A failed candidate leaves the previous feed available and retries after thirty
minutes. Manual delivery can retry immediately after a repair. Completed release
receipts prevent duplicate publication; older or divergent sources cannot replace
newer qualified packages.

For recovered core releases, the Linux package and source archive retain the
original compiler source and producer identity. A separate `core-release.json`
binds those bytes to the published all-platform release. It does not represent
an overall failed original producer run as a successful core release.

Deployment status is recorded in [promotion-hold.json](sync-state/promotion-hold.json).
While its hold is active, delivery does not stage or publish anything. The
scheduled workflow must also be enabled explicitly; the controller never
re-enables schedules itself. A successful canonical native QA preflight can be
reused through the manual `qualified_run_id` input; its trusted workflow identity,
completed checks, source, version and exact Linux bytes are checked again before
publication. See [Actions](https://github.com/snapetech-org/pleazyng-aerynos/actions),
[delivery status](sync-state/status.json), and
[completed release receipts](sync-state/releases.json).

The current install command remains pinned to the tested seed until the first
new qualified package promotes the live feed. No official aerynOS repository
submission is part of this independent distribution channel.

## Source and licensing

Pleazyng is GPL-3.0-only; see [LICENSE](LICENSE), the source archive attached to
the release, source input pins and the package's third-party notices. The recipe
is maintained downstream. No official aerynOS acceptance or affiliation is
claimed; no AI-authored contribution was submitted to that project.
