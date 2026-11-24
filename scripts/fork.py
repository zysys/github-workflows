#!/usr/bin/env python3
"""Assemble a fork build: the fork's mirror branch (upstream's default branch, fast-forwarded
daily) plus the topic branches listed in `.fork/branches`, merged in order, with conflict
resolutions replayed from a committed rerere cache.

One script owns the merge rules for both sides: the reusable `fork.yml` workflow runs it in
CI and the local `fork` tool runs it to reproduce and resolve a conflict.

    fork.py assemble --repo DIR --branches FILE [--base-branch main] [--rr-cache DIR] [--fetch]
                     [--plan PLAN.json] [--keep-conflict] [--out OUT.json]
    fork.py record --repo DIR --rr-cache DIR

`assemble` prints (and writes to --out) one JSON object:
    {"status": "ok", "base": sha, "head": sha, "tree": sha,
     "branches": [{"name", "sha", "state": "merged"|"in_base"}], "warnings": [...]}
or, exit 3, {"status": "conflict", "branch", "files": [...], ...}.

Merge commits use a fixed identity and the base commit's date, so every runner that
assembles the same plan produces the same head SHA.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

IDENTITY = ("fork-assemble", "fork-assemble@users.noreply.github.com")
EXIT_CONFLICT = 3
# The same merge on every machine: no hooks, no signing, one conflict style (rerere keys
# recorded resolutions on the conflict text), rerere on.
GIT_CONFIG = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "merge.conflictStyle=merge",
    "-c",
    "rerere.enabled=true",
    "-c",
    "rerere.autoupdate=true",
]
EXIT_USAGE = 2


class AssemblyError(RuntimeError):
    pass


def git(
    repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *GIT_CONFIG, *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssemblyError(
            f"git {' '.join(args)} failed ({proc.returncode}): {failure_text(proc)}"
        )
    return proc


def failure_text(proc: subprocess.CompletedProcess[str]) -> str:
    """Why a git command failed. `git commit` and `git merge` explain themselves on stdout."""
    return " | ".join(
        stream.strip() for stream in (proc.stderr, proc.stdout) if stream.strip()
    )


def out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def read_branch_list(path: Path) -> list[str]:
    """One branch per line; blank lines and `#` comments are ignored; duplicates are an error."""
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line in names:
            raise AssemblyError(f"{path}: branch {line} is listed twice")
        names.append(line)
    return names


def rr_cache_dir(repo: Path) -> Path:
    common = Path(out(repo, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else repo / common) / "rr-cache"


def install_rr_cache(repo: Path, cache: Path | None) -> None:
    if cache is None or not cache.is_dir():
        return
    target = rr_cache_dir(repo)
    target.mkdir(parents=True, exist_ok=True)
    for entry in cache.iterdir():
        if entry.is_dir():
            shutil.copytree(entry, target / entry.name, dirs_exist_ok=True)


def record_rr_cache(repo: Path, cache: Path) -> list[str]:
    """Record the resolutions in the working tree (`git rerere`), then copy every entry that holds
    a resolution (a postimage) into `cache`."""
    git(repo, "rerere")
    source = rr_cache_dir(repo)
    copied: list[str] = []
    if not source.is_dir():
        return copied
    cache.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if not entry.is_dir() or not any(
            p.name.startswith("postimage") for p in entry.iterdir()
        ):
            continue
        dest = cache / entry.name
        dest.mkdir(exist_ok=True)
        for item in entry.iterdir():
            if item.name.startswith(("preimage", "postimage")):
                shutil.copy2(item, dest / item.name)
        copied.append(entry.name)
    return copied


def fetch(repo: Path, base_branch: str, branches: list[str]) -> None:
    refspecs = [
        f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in [base_branch, *branches]
    ]
    git(repo, "fetch", "--quiet", "--no-tags", "origin", *refspecs)


def quarantine_policy():
    if __package__:
        from . import quarantine
    else:
        import quarantine
    return quarantine


def resolve_plan(repo: Path, base_branch: str, branches: list[str]) -> dict:
    proc = git(
        repo,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
        check=False,
    )
    if proc.returncode != 0:
        raise AssemblyError(f"the mirror branch {base_branch} is not on origin")
    base = proc.stdout.strip()
    resolved = []
    for name in branches:
        proc = git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{name}^{{commit}}",
            check=False,
        )
        if proc.returncode != 0:
            raise AssemblyError(
                f"branch {name} is listed in .fork/branches but is not on origin: push it or remove the line"
            )
        resolved.append({"name": name, "sha": proc.stdout.strip()})
    return {"base_branch": base_branch, "base": base, "branches": resolved}


def merge_env(repo: Path, base: str) -> dict[str, str]:
    date = out(repo, "show", "-s", "--format=%ct", base)
    stamp = f"@{date} +0000"
    return {
        "GIT_AUTHOR_NAME": IDENTITY[0],
        "GIT_AUTHOR_EMAIL": IDENTITY[1],
        "GIT_COMMITTER_NAME": IDENTITY[0],
        "GIT_COMMITTER_EMAIL": IDENTITY[1],
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
    }


def unmerged(repo: Path) -> list[str]:
    return [
        f for f in out(repo, "diff", "--name-only", "--diff-filter=U").splitlines() if f
    ]


def assemble(repo: Path, plan: dict, *, keep_conflict: bool) -> dict:
    base = plan["base"]
    env = merge_env(repo, base)
    git(repo, "checkout", "--quiet", "--detach", base)
    result_branches = []
    warnings: list[str] = []
    for entry in plan["branches"]:
        name, sha = entry["name"], entry["sha"]
        if (
            git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode
            != 0
        ):
            raise AssemblyError(
                f"{name} at {sha} is not in this clone: fetch the fork's branches first"
            )
        if (
            git(
                repo, "merge-base", "--is-ancestor", sha, "HEAD", check=False
            ).returncode
            == 0
        ):
            state = (
                "in_base"
                if git(
                    repo, "merge-base", "--is-ancestor", sha, base, check=False
                ).returncode
                == 0
                else "merged"
            )
            if state == "in_base":
                warnings.append(
                    f"{name} is already in main (upstream merged it): remove it from .fork/branches"
                )
            result_branches.append({**entry, "state": state})
            continue
        message = f"fork: merge {name}"
        proc = git(
            repo,
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            message,
            sha,
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            files = unmerged(repo)
            if files:
                if not keep_conflict:
                    git(repo, "merge", "--abort", check=False)
                return {
                    "status": "conflict",
                    "base": base,
                    "branch": name,
                    "sha": sha,
                    "files": files,
                    "merged": result_branches,
                    "hint": f"{name} conflicts with main or an earlier branch in {', '.join(files)}. "
                    "Resolve it once with `fork resolve`; the resolution is recorded in .fork/rr-cache "
                    "and replayed on every later build.",
                }
            if (
                git(
                    repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False
                ).returncode
                != 0
            ):
                raise AssemblyError(
                    f"merging {name} failed ({proc.returncode}) before any conflict: {failure_text(proc)}"
                )
            # rerere resolved and staged every conflict: finish the merge deterministically.
            git(repo, "commit", "--no-verify", "-m", message, env=env)
        result_branches.append({**entry, "state": "merged"})
    head = out(repo, "rev-parse", "HEAD")
    return {
        "status": "ok",
        "base_branch": plan.get("base_branch", "main"),
        "base": base,
        "head": head,
        "tree": out(repo, "rev-parse", "HEAD^{tree}"),
        "branches": result_branches,
        "warnings": warnings,
    }


def cmd_assemble(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    install_rr_cache(repo, Path(args.rr_cache) if args.rr_cache else None)
    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if args.fetch:
            fetch(
                repo,
                plan.get("base_branch", args.base_branch),
                [b["name"] for b in plan["branches"]],
            )
    else:
        names = read_branch_list(Path(args.branches)) if args.branches else []
        if args.fetch:
            fetch(repo, args.base_branch, names)
        plan = resolve_plan(repo, args.base_branch, names)
    if args.upstream:
        plan = quarantine_policy().assembly_plan(
            repo, plan, args.upstream, pinned=bool(args.plan)
        )
    result = assemble(repo, plan, keep_conflict=args.keep_conflict)
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    for warning in result.get("warnings", []):
        print(f"::warning title=fork branch::{warning}", file=sys.stderr)
    if result["status"] == "conflict":
        print(f"::error title=Assembly conflict::{result['hint']}", file=sys.stderr)
        return EXIT_CONFLICT
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    copied = record_rr_cache(Path(args.repo).resolve(), Path(args.rr_cache))
    print(json.dumps({"recorded": copied}))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            quarantine_policy().sync(Path(args.repo), args.github_repo, args.mirror)
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("assemble", help="merge main + listed branches")
    a.add_argument("--repo", required=True)
    a.add_argument(
        "--upstream", help="upstream owner/repo: enforce 72-hour ancestry grace"
    )
    a.add_argument("--branches", help=".fork/branches file (ignored with --plan)")
    a.add_argument(
        "--base-branch",
        default="main",
        help="mirror branch on origin the build starts from",
    )
    a.add_argument(
        "--plan", help="JSON from an earlier assemble: merge exactly these SHAs"
    )
    a.add_argument("--rr-cache", help="committed rerere cache to replay")
    a.add_argument(
        "--fetch",
        action="store_true",
        help="fetch main and the listed branches from origin first",
    )
    a.add_argument(
        "--keep-conflict",
        action="store_true",
        help="leave a conflicted merge in place to resolve",
    )
    a.add_argument("--out", help="also write the JSON result here")
    a.set_defaults(func=cmd_assemble)
    r = sub.add_parser(
        "record", help="copy recorded resolutions into the committed rerere cache"
    )
    r.add_argument("--repo", required=True)
    r.add_argument("--rr-cache", required=True)
    r.set_defaults(func=cmd_record)
    s = sub.add_parser("sync", help="fast-forward only to quarantined upstream")
    s.add_argument("--repo", required=True)
    s.add_argument("--github-repo", required=True)
    s.add_argument("--mirror", required=True)
    s.set_defaults(func=cmd_sync)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (AssemblyError, quarantine_policy().AssemblyError) as exc:
        print(f"::error title=fork assemble::{exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
