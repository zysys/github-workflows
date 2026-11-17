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
