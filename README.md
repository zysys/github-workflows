## Fork upstream quarantine

The reusable `fork.yml` and local `fork sync` use `scripts/fork.py sync` and its
shared quarantine policy. Upstream commits wait 72 hours, measured from the
maximum committer timestamp across their complete ancestry. Sync only
fast-forwards to the newest eligible first-parent commit; it never resets an
already-ahead mirror. Diverged mirrors and failed syncs stop the workflow.

Assembly selects the eligible upstream base independently of an already-ahead
mirror, without moving that mirror. Every carried branch is checked against
all fetched upstream branch ancestry: importing quarantined upstream commits
fails before merge/build and reports when they become eligible. Rebase those
carries onto the eligible base rather than bypassing the gate. `force` bypasses
only the already-built check, never quarantine. Own published fork releases
remain immediately installable (`minimum_release_age=0`): delaying our release
again is not an upstream quarantine.

This is a commit-timestamp grace period, not a release-publication-age policy
or protection against forged/backdated timestamps, rewritten or unpublished
upstream history, or copied upstream code with different commit identities.

# `zysys/github-workflows`

Reusable GitHub Actions workflows shared across `zysys` and personal forks.

## `protect-main-paths.yml`

Blocks pull requests targeting `main` (or another protected assembly/base branch) whenever they modify files outside the repository's allowed carry areas, while permitting clean upstream sync merges on forks.

When a pull request violates the policy, the workflow:

1. Fails the status check (`Protect main carry paths`) so branch protection / rulesets block merging.
2. Emits file-level `::error` annotations naming the offending files and commits.
3. Writes a Markdown report to `$GITHUB_STEP_SUMMARY` and (when `post_comment: true`) posts or updates a single sticky comment on the pull request with a violation table and copy-paste remediation commands.

### Usage

Create `.github/workflows/protect-main.yml` in the caller repository:

```yaml
name: protect-main

on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read

jobs:
  protect-main:
    uses: zysys/github-workflows/.github/workflows/protect-main-paths.yml@main
    permissions:
      contents: read
      pull-requests: write
    with:
      allowed_paths: |
        .omp-build/**
        .github/workflows/omp-build.yml
        .github/workflows/protect-main.yml
      upstream_repo: can1357/oh-my-pi
      upstream_branch: main
      remediation: |
        `main` on `zachbornheimer/oh-my-pi` only holds upstream `can1357/oh-my-pi:main` plus `.omp-build/` and `.github/workflows/{omp-build,protect-main}.yml`.
        Keep feature or fix commits on a topic branch off `origin/main` (`wt switch --create feat/<slug> --base origin/main`), push it to `origin`, and open a PR to `main` that only adds `.omp-build/branches/feat/<slug>.toml`.
```

### Inputs

| Input             | Required | Default  | Description                                                                                                                                                                                  |
| ----------------- | -------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `allowed_paths`   | **Yes**  | —        | Newline- or comma-separated path globs allowed on the target branch (e.g. `.omp-build/**`, `.github/workflows/**`).                                                                          |
| `upstream_repo`   | No       | `"auto"` | Upstream `owner/repo` whose clean sync merges are allowed onto `main`. `"auto"` reads `github.event.repository.parent.full_name` on GitHub forks; `"none"` disables upstream sync exemption. |
| `upstream_branch` | No       | `""`     | Upstream branch name for sync detection (defaults to the fork parent's default branch or `"main"`).                                                                                          |
| `remediation`     | No       | `""`     | Custom Markdown instructions shown under **How to fix** when a PR is blocked.                                                                                                                |
| `post_comment`    | No       | `true`   | Post or update a single sticky comment (`<!-- zysys:protect-main-paths -->`) on blocked PRs.                                                                                                 |

## `go-release.yml`

Tests a Go module, cross-compiles it for `darwin-arm64`, `linux-x64` and `linux-arm64`, and publishes a GitHub release marked `--latest`. Assets are `<binary>-<target>.tar.gz` (the binary at the archive root) plus `SHA256SUMS`, so `mise use -g github:<owner>/<repo>` installs it. Builds use `CGO_ENABLED=0`, `-trimpath`, and set `main.version` to the release tag (a missing `main.version` is ignored by the linker).

### Usage

Create `.github/workflows/release.yml` in the caller repository:

```yaml
name: release

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  release:
    uses: zysys/github-workflows/.github/workflows/go-release.yml@main
    permissions:
      contents: write
```

Then in mise config:

```toml
"github:<owner>/<repo>" = { version = "latest", minimum_release_age = "0" }
```

### Inputs

| Input             | Required | Default   | Description                                       |
| ----------------- | -------- | --------- | ------------------------------------------------- |
| `binary`          | No       | repo name | Binary name and asset prefix.                     |
| `package`         | No       | `.`       | Go package to build (e.g. `./cmd/tool`).          |
| `go_version_file` | No       | `go.mod`  | File `setup-go` reads the toolchain version from. |
| `run_tests`       | No       | `true`    | Run `go test ./...` before building.              |

## `fork.yml`

Builds and ships a customized fork of someone else's CLI, in any language, so `mise` installs it. Every fork has the same layout:

- The upstream-named branch (`main` or `master`) mirrors upstream and only ever fast-forwards.
- The default branch `fork` holds only this caller, `.fork/branches` (the topic branches to carry, in merge order) and `.fork/rr-cache` (recorded conflict resolutions, replayed with `git rerere`).
- Each customization is a topic branch cut from the mirror, so it is also a clean upstream pull request.

A run fast-forwards the mirror, merges the mirror plus every listed branch (`scripts/fork.py assemble`, deterministic), then runs `check` on that merged tree beside each target build, publishes a **prerelease**, installs that exact tag with `mise` on each target and runs `smoke`, then promotes it to latest. A conflict, a red check or a failed install stops the run before machines see anything: `mise` never installs a prerelease as `latest`. A run whose merge equals the latest release's `build_commit:` is skipped unless `force` is set.

The local `fork` tool (`~/.omp/agent/tools/fork`) writes the caller, lands topic branches, records conflict resolutions and fast-forwards every mirror daily.

### Usage

`.github/workflows/fork.yml` on the fork's `fork` branch; `uses:` and `scripts_ref` name the same commit:

```yaml
name: fork

on:
  push:
    branches: [fork]
  schedule:
    - cron: "17 6 * * *"
  workflow_dispatch:

concurrency:
  group: fork-release
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  release:
    uses: zysys/github-workflows/.github/workflows/fork.yml@<sha> # v2.0.0
    permissions:
      contents: write
    with:
      scripts_ref: <sha>
      mirror: master
      binary: hexyl
      check: cargo test --locked
      build: cargo build --release --locked
      artifact: target/release/hexyl
      smoke: '"$BIN" --version'
```

### Inputs

| Input             | Required | Default                 | Description                                                                         |
| ----------------- | -------- | ----------------------- | ----------------------------------------------------------------------------------- |
| `binary`          | Yes      | -                       | Executable name; assets are `<binary>-<target>.tar.gz`.                             |
| `scripts_ref`     | Yes      | -                       | Commit of this repository whose `scripts/fork.py` the run uses; equals the `uses:`. |
| `check`           | Yes      | -                       | Gate on the merged tree before any build; `true` only when there is nothing to run. |
| `build`           | Yes      | -                       | Command that builds the binary at the root of the merged tree.                      |
| `artifact`        | Yes      | -                       | Path of the built binary, relative to the merged tree.                              |
| `mirror`          | No       | `main`                  | The fork's branch that mirrors upstream's default branch.                           |
| `smoke`           | No       | `"$BIN" --version`      | Run against the built and then the installed binary; `$BIN` is its path.            |
| `tools`           | No       | -                       | Space-separated `mise` tool specs installed before check and build.                 |
| `targets`         | No       | darwin-arm64, linux-x64 | JSON array of `{"target", "os"}`; each builds on that runner.                       |
| `build_env`       | No       | -                       | Newline-separated `KEY=VALUE` lines exported for check and build.                   |
| `cache_path`      | No       | -                       | Extra build output cached across runs.                                              |
| `cache_key_paths` | No       | -                       | Space-separated paths whose tree hashes key `cache_path`.                           |
| `publish`         | No       | `true`                  | Release, verify and promote; pass `false` for pull requests.                        |
| `force`           | No       | `false`                 | Build even when the latest release already ships this merge.                        |

## `rust-release.yml`

Builds a Rust binary on one native runner per target and publishes a GitHub release marked `--latest` (or `--prerelease`). Assets are `<binary>-<target>.tar.gz` (the binary at the archive root) plus `SHA256SUMS`, so `mise use -g github:<owner>/<repo>` installs it.

Rust does not cross-compile the way Go does, so each target gets its own OS runner: `macos-latest` for `darwin-arm64`, `ubuntu-22.04` for `linux-x64`, `ubuntu-22.04-arm` for `linux-arm64`. A caller that only needs some targets passes its own `targets` array; `darwin-arm64` plus `linux-x64` is what a Mac-plus-Linux fleet needs.

Every build runs the binary's `--version` before it is packed, so a binary that cannot start on its own runner never becomes a release asset.

### Usage

Assemble in the caller repository, then hand the build and publish to this workflow (this is what `zachbornheimer/mise` does):

```yaml
jobs:
  assemble:
    # ...merge the carried branches, push the assembly, and compute a tag...
    outputs:
      tag: ${{ steps.decide.outputs.tag }}
      build-sha: ${{ steps.decide.outputs.build-sha }}
  release:
    needs: [assemble]
    if: needs.assemble.outputs.skip != 'true'
    permissions:
      contents: write
    uses: zysys/github-workflows/.github/workflows/rust-release.yml@main
    with:
      binary: mise
      ref: mise-build
      tag: ${{ needs.assemble.outputs.tag }}
      release_target: ${{ needs.assemble.outputs.build-sha }}
      targets: |
        [{"target":"darwin-arm64","os":"macos-latest"},
         {"target":"linux-x64","os":"ubuntu-22.04"}]
      build_env: |
        MACOSX_DEPLOYMENT_TARGET=12.0
```

### Inputs

| Input             | Required | Default                              | Description                                                   |
| ----------------- | -------- | ------------------------------------ | ------------------------------------------------------------- |
| `binary`          | Yes      | -                                    | Cargo binary (`--bin <binary>`) and asset prefix.             |
| `tag`             | Yes      | -                                    | Release tag to publish.                                       |
| `targets`         | No       | darwin-arm64, linux-x64, linux-arm64 | JSON array of `{"target", "os"}`; each builds on that runner. |
| `ref`             | No       | caller's commit                      | Ref the builds check out.                                     |
| `release_target`  | No       | caller's commit                      | Commit the release points at.                                 |
| `notes`           | No       | generated line                       | Release body; callers put machine-readable lines here.        |
| `prerelease`      | No       | `false`                              | Publish as a prerelease (mise ignores prereleases).           |
| `cargo_args`      | No       | -                                    | Extra arguments after `cargo build --release --bin <binary>`. |
| `build_env`       | No       | -                                    | Newline-separated `KEY=VALUE` lines exported for every build. |
| `timeout_minutes` | No       | `120`                                | Per-target build timeout.                                     |
