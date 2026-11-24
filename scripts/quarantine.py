"""72-hour upstream committer-time grace; not protection against forged timestamps."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


class AssemblyError(RuntimeError):
    pass


def failure_text(proc: subprocess.CompletedProcess[str]) -> str:
    return " | ".join(
        stream.strip() for stream in (proc.stderr, proc.stdout) if stream.strip()
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    if check and proc.returncode:
        raise AssemblyError(f"git {' '.join(args)}: {failure_text(proc)}")
    return proc


def out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


GRACE_SECONDS = 72 * 60 * 60


def gh(*args: str) -> str:
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False, timeout=120
    )
    if proc.returncode:
        raise AssemblyError(f"gh {' '.join(args)}: {failure_text(proc)}")
    return proc.stdout.strip()


def upstream_graph(repo: Path, tips: list[str]) -> dict[str, int]:
    graph: dict[str, int] = {}
    for line in out(
        repo, "log", "--topo-order", "--reverse", "--format=%H %ct %P", *tips
    ).splitlines():
        sha, stamp, *parents = line.split()
        graph[sha] = max([int(stamp), *(graph[parent] for parent in parents)])
    return graph


def eligible_commit(repo: Path, tip: str, graph: dict[str, int], cutoff: int) -> str:
    for sha in out(repo, "rev-list", "--first-parent", tip).splitlines():
        if graph[sha] <= cutoff:
            return sha
    raise AssemblyError(
        "upstream quarantine: no commit has 72-hour-old complete ancestry"
    )


def sync_action(repo: Path, current: str, eligible: str) -> str:
    if out(repo, "rev-parse", current) == out(repo, "rev-parse", eligible):
        return "none"
    if (
        git(
            repo, "merge-base", "--is-ancestor", current, eligible, check=False
        ).returncode
        == 0
    ):
        return "fast_forward"
    if (
        git(
            repo, "merge-base", "--is-ancestor", eligible, current, check=False
        ).returncode
        == 0
    ):
        return "none"
    raise AssemblyError(
        "mirror diverged: move its commits to a topic branch; refusing to build"
    )


def observe_upstream(repo: Path, upstream: str) -> tuple[str, dict[str, int]]:
    branch = gh("api", f"repos/{upstream}", "--jq", ".default_branch")
    git(
        repo,
        "fetch",
        "--quiet",
        "--no-tags",
        f"https://github.com/{upstream}.git",
        "+refs/heads/*:refs/fork-upstream/*",
        "--prune",
    )
    tips = out(
        repo, "for-each-ref", "--format=%(objectname)", "refs/fork-upstream/"
    ).splitlines()
    tip = out(repo, "rev-parse", f"refs/fork-upstream/{branch}")
    return tip, upstream_graph(repo, tips)


def validate_upstream(
    repo: Path, plan: dict, graph: dict[str, int], cutoff: int
) -> None:
    for entry in [{"sha": plan["base"]}, *plan["branches"]]:
        for sha in out(repo, "rev-list", entry["sha"]).splitlines():
            if sha in graph and graph[sha] > cutoff:
                ready = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(graph[sha] + GRACE_SECONDS)
                )
                raise AssemblyError(
                    f"upstream quarantine: {sha} ancestry is too young; eligible at {ready}; rebase the carry onto the eligible base"
                )


def assembly_plan(repo: Path, plan: dict, upstream: str, *, pinned: bool) -> dict:
    tip, graph = observe_upstream(repo, upstream)
    cutoff = int(time.time()) - GRACE_SECONDS
    if not pinned:
        if (
            git(
                repo, "merge-base", "--is-ancestor", plan["base"], tip, check=False
            ).returncode
            != 0
        ):
            raise AssemblyError("mirror diverged from upstream; refusing assembly")
        plan["base"] = eligible_commit(repo, tip, graph, cutoff)
    validate_upstream(repo, plan, graph, cutoff)
    return plan


def sync(repo: Path, github_repo: str, mirror: str) -> dict:
    parent = gh("api", f"repos/{github_repo}", "--jq", ".parent.full_name // empty")
    if not parent:
        raise AssemblyError("no upstream parent; refusing unverified sync")
    tip, graph = observe_upstream(repo, parent)
    eligible = eligible_commit(repo, tip, graph, int(time.time()) - GRACE_SECONDS)
    current = gh(
        "api", f"repos/{github_repo}/git/ref/heads/{mirror}", "--jq", ".object.sha"
    )
    git(repo, "fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{mirror}")
    if (
        git(repo, "merge-base", "--is-ancestor", current, tip, check=False).returncode
        != 0
    ):
        raise AssemblyError("mirror diverged from upstream; refusing sync and assembly")
    action = sync_action(repo, current, eligible)
    if action == "fast_forward":
        gh(
            "api",
            "--method",
            "PATCH",
            f"repos/{github_repo}/git/refs/heads/{mirror}",
            "-f",
            f"sha={eligible}",
            "-F",
            "force=false",
        )
    return {
        "action": action,
        "base": eligible,
        "detail": f"72h upstream quarantine: eligible base {eligible}; mirror {current} ({action}); never rewound",
    }
