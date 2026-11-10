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
upgrade an installed version. This versioned feed stays pinned until you choose
another channel release.

Public feed fetch, checksum comparison, removal/reinstall and ordinary relaunch
also pass in the guest; see [publication receipt](publication-receipt.json).

## Build the recipe

On a configured aerynOS Boulder host:

```sh
git clone https://github.com/snapetech-org/pleazyng-aerynos.git
cd pleazyng-aerynos/recipe
boulder build stone.yaml
```

The `pkg/` directory is required; keep it beside `stone.yaml`.

## Build and evidence

The first package is **3.0.22+181**, package release **1**, built from source
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

## Source and licensing

Pleazyng is GPL-3.0-only; see [LICENSE](LICENSE), the source archive attached to
the release, source input pins and the package's third-party notices. The recipe
is maintained downstream. No official aerynOS acceptance or affiliation is
claimed; no AI-authored contribution was submitted to that project.
