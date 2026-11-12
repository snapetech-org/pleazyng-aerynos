# Pleazyng for aerynOS

Independent x86_64 packages for aerynOS. This is a real, installable downstream
preview channel, separate from the official distribution repository.

## Install

Tested on aerynOS 2026.11. Review [qualification.json](qualification.json) and the
[recipe](recipe/stone.yaml) before enabling this preview.

```sh
sudo moss repo add pleazyng-aerynos https://github.com/snapetech-org/pleazyng-aerynos/raw/refs/heads/main/feed/stone.index
sudo moss -y pkg add pleazyng
```

If Pleazyng is already installed, inspect `sudo moss sync --dry-run`, then use
`sudo moss -y sync` to apply the selected updates. Moss `pkg add` alone does not
upgrade an installed version. Each snapshot also supplies a versioned
`stone.index` for clients that prefer a pinned package. The
[last-good receipt](sync-state/last-good.json) records the current qualified
source and feed URLs.

When moving from a pinned snapshot to the live feed, remove the old repository
entry with `sudo moss repo remove pleazyng-aerynos`, then repeat the `repo add`
command shown above. Removing the repository entry leaves the installed app in
place; `moss sync` performs the upgrade.

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

The metadata-only `source-sync.yml` receiver accepts a
`canonical-main-updated` repository dispatch or a manual wake. The event is only
a wake-up: the receiver fetches the canonical repository identity and current
`main` itself, checks ancestry, and updates only the source pointer with a
bounded compare-and-swap. It never builds an app, qualifies a package or advances
the feed. The canonical push hook needs existing Contents-write access to this
repository; polling remains a catch-up path, not a guaranteed delivery interval.

Source tracking has its own concurrency group. A publication push may replay
its unpublished commit over concurrent source-pointer-only updates. Changes to
the feed, hold, qualification or any other remote path stop publication for
reconciliation instead; published history is never rewritten.

The canonical release controller wakes this consumer after core publication;
a workflow scheduled every five minutes provides catch-up, subject to GitHub
scheduling and runner availability. The delivery controller processes releases from
3.0.29 onward in version order. It verifies the attested release checksums and successful final core workflow,
then verifies the published Linux archive and its original compiler attestation,
and stages those exact public bytes for native QA. Staging uses anonymous release
downloads rather than a cross-repository Actions artifact token. Original Linux
job, retained artifact identity and certificate source/run checks still apply. It never recompiles
the application or starts an all-platform build.

Every candidate must pass a fresh native aerynOS Boulder build, Moss install and
upgrade, dependency checks, sign-in, visible playback and audio, pause, seek,
fullscreen, background return, saved-position resume and bookmark resume.
Only then are immutable package assets published and the feed advanced.
After promotion, a second fresh native guest installs anonymously from the live
feed, removes and reinstalls from the pinned release feed, and launches the app.
It verifies the index digests, installed package version and runtime source.
The public-install result is recorded in the release receipt, with logs and
screenshots retained by Actions. A failed public-install check retries that
check after thirty minutes without compiling or repackaging the app.
A failed candidate leaves the previous feed available and retries after thirty
minutes. Resolution failures are recorded too, and a new release remains eligible
while an older delivery backs off. Once a newer release is qualified, older
undelivered releases are recorded as superseded; they cannot roll the feed back. Manual delivery can retry immediately after a repair. Completed release
receipts prevent duplicate publication; older or divergent sources cannot replace
newer qualified packages.

The remote version tag must resolve to the exact attested Linux compiler commit.
Both lightweight and annotated tags are supported. For ordinary releases, the
assembler uses that same commit. The frozen source may be an ancestor of current
`main`; later development does not change the release identity.

A failed Apple build can use the closed `frozen-apple-packaging-v1` recovery
contract. An approval committed in the actual workflow revision pins the exact
repair commit and every changed packaging file's Git blob. Independent commit,
complete tree and diff checks require that repair to have the frozen source as
its sole parent; application, version and native dependency inputs cannot change.
The public recovery receipt must be bound by the assembler's attested checksums,
match the actual approved checkout, and retain the original Linux certificate,
digest and artifact identity. Original embedded CI/freeze, reused platform jobs
and actual signed Apple archive steps are checked against GitHub's public APIs.
These checks run again before promotion. A receipt claiming equivalence alone
is rejected. `core-release.json` records the separate product, packaging and
workflow sources, compiler/assembler run IDs and recovery proof digest.

A completed tvOS output may be reused while repairing iOS. Its producer must
have passed signed build, attestation and upload steps for a separately approved
repair of the same frozen product. The consumer verifies that producer's Git
approval and artifact identity, then checks both published tvOS IPAs against
their original producer certificates and the final attested checksums.

Deployment status is recorded in [promotion-hold.json](sync-state/promotion-hold.json).
While its hold is active, delivery does not stage or publish anything. The
scheduled workflow must also be enabled explicitly; the controller never
re-enables schedules itself. Native qualification artifacts are produced and
consumed within the same delivery run; manual cross-repository QA reuse is not
supported. See [Actions](https://github.com/snapetech-org/pleazyng-aerynos/actions),
[delivery status](sync-state/status.json), and
[completed release receipts](sync-state/releases.json).

No official aerynOS repository submission is part of this independent
distribution channel.

## Hold and resume delivery

Use the serialized hold workflow for planned maintenance. First disable the
scheduled reconciler so another poll cannot replace a pending hold request:

```sh
gh workflow disable reconcile.yml --repo snapetech-org/pleazyng-aerynos
gh workflow run hold.yml --repo snapetech-org/pleazyng-aerynos \
  -f active=true -f reason='Package channel maintenance'
```

Wait for that hold run to succeed and confirm `promotion-hold.json` is active.
The hold workflow shares the publisher's concurrency group and preserves active
work. An already active publication may finish before the queued hold takes
effect. For immediate intervention, cancel the active channel run first.

The publisher fetches the latest hold and feed state again after private draft
uploads, immediately before making the release public. A hold committed during
uploads leaves the assets in a draft and prevents feed promotion. Direct file
edits outside the serialized workflow can still race the final GitHub API call;
GitHub does not provide a transaction spanning a Git commit and release
visibility. The ordinary fast-forward feed push rejects an intervening commit.
Do not delete or overwrite published assets to simulate rollback.

To resume, dispatch `hold.yml` with `active=false` and a reason. After it succeeds,
explicitly enable `reconcile.yml` and dispatch it once. The complete core and
native qualification gates remain in force while the configuration hold is off.

## Source and licensing

Pleazyng is GPL-3.0-only; see [LICENSE](LICENSE), the source archive attached to
the release, source input pins and the package's third-party notices. The recipe
is maintained downstream. No official aerynOS acceptance or affiliation is
claimed; no AI-authored contribution was submitted to that project.
