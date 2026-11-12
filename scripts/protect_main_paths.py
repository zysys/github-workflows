#!/usr/bin/env python3
"""Guard pull requests targeting a protected branch (`main`) so only allowed carry paths
(or clean upstream sync merges on forks) may land.

Invariants enforced:
1. Per-commit check: every commit in `BASE_SHA..HEAD_SHA` that is not an ancestor of
   `upstream/<upstream_branch>` may only modify files matching `allowed_paths` (except a
   merge commit bringing in `upstream/<upstream_branch>`, where files outside `allowed_paths`
   must match either the first parent or the upstream parent).
2. Net tree invariant: for every file `f` changed between `BASE_SHA` and `HEAD_SHA` that does
   not match `allowed_paths`, its blob at `HEAD_SHA` must equal either its blob at `BASE_SHA`
   or (when `upstream` is configured and merged) its blob at `merge-base(HEAD_SHA, UPSTREAM_SHA)`.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import PurePosixPath

COMMENT_MARKER = "<!-- zysys:protect-main-paths -->"
HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class Violation:
    file: str
    commit: str
    subject: str
    reason: str


@dataclass
class CheckResult:
    ok: bool
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    allowed_patterns: list[str]
    upstream_repo: str = ""
    upstream_branch: str = ""
    upstream_sha: str = ""
    changed_files: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)
    upstream_synced_files: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)


def _strip_leading_rel(value: str) -> str:
    s = value.strip()
    while s.startswith(("./", "/")):
        if s.startswith("./"):
            s = s[2:]
        elif s.startswith("/"):
            s = s[1:]
    return s


def parse_allowed_patterns(raw: str) -> list[str]:
    patterns: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        cleaned = _strip_leading_rel(line)
        if not cleaned or cleaned.startswith("#"):
            continue
        if cleaned.endswith("/"):
            cleaned = f"{cleaned}**"
        if cleaned not in patterns:
            patterns.append(cleaned)
    return patterns


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    norm = _strip_leading_rel(pattern)
    if norm.endswith("/"):
        norm = f"{norm}**"
    out = ["^"]
    i = 0
    n = len(norm)
    while i < n:
        if norm[i : i + 3] == "**/":
            out.append("(?:.+/|)")
            i += 3
        elif norm[i : i + 3] == "/**":
            out.append("(?:/.*|)")
            i += 3
        elif norm[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif norm[i] == "*":
            out.append("[^/]*")
            i += 1
        elif norm[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(norm[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def path_matches_any(rel_path: str, patterns: list[str]) -> bool:
    norm = str(PurePosixPath(_strip_leading_rel(rel_path)))
    if norm in ("", "."):
        return False
    return any(_glob_to_regex(p).match(norm) is not None for p in patterns)


def _git(repo: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _blob_at(repo: str, rev: str, path: str) -> str:
    if not rev:
        return "<absent>"
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "-q", "--verify", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "<absent>"


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    if not ancestor or not descendant:
        return False
    proc = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def evaluate_repo(
    repo: str,
    *,
    base_sha: str,
    head_sha: str,
    allowed_patterns: list[str],
    base_ref: str = "main",
    head_ref: str = "HEAD",
    upstream_sha: str = "",
    upstream_repo: str = "",
    upstream_branch: str = "main",
) -> CheckResult:
    if not allowed_patterns:
        raise ValueError("allowed_patterns must not be empty")

    base_resolved = _git(repo, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
    head_resolved = _git(repo, "rev-parse", "--verify", f"{head_sha}^{{commit}}")
    upstream_resolved = ""
    upstream_mb = ""
    if upstream_sha:
        upstream_resolved = _git(
            repo, "rev-parse", "--verify", f"{upstream_sha}^{{commit}}"
        )
        mb_proc = subprocess.run(
            ["git", "-C", repo, "merge-base", head_resolved, upstream_resolved],
            capture_output=True,
            text=True,
            check=False,
        )
        if mb_proc.returncode == 0:
            upstream_mb = mb_proc.stdout.strip()

    violations: list[Violation] = []
    seen_violation_files: set[str] = set()

    rev_range = [f"{base_resolved}..{head_resolved}"]
    if upstream_resolved:
        rev_range.extend(["--not", upstream_resolved])

    commits_out = _git(repo, "rev-list", "--reverse", *rev_range)
    commits = [c for c in commits_out.splitlines() if c]

    for commit in commits:
        parents_line = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()
        parents = parents_line[1:]
        subject = _git(repo, "show", "-s", "--format=%s", commit)
        if len(parents) <= 1:
            diff_args = (
                ["diff-tree", "--no-commit-id", "--name-only", "-r", commit]
                if parents
                else [
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "--root",
                    commit,
                ]
            )
            changed = _git(repo, *diff_args).splitlines()
            for f in changed:
                if not f or path_matches_any(f, allowed_patterns):
                    continue
                violations.append(
                    Violation(
                        file=f,
                        commit=commit[:10],
                        subject=subject,
                        reason="commit modifies path outside allowed areas",
                    )
                )
                seen_violation_files.add(f)
        else:
            p1 = parents[0]
            upstream_parents = [
                p
                for p in parents[1:]
                if upstream_resolved and _is_ancestor(repo, p, upstream_resolved)
            ]
            changed_from_p1 = _git(repo, "diff", "--name-only", p1, commit).splitlines()
            for f in changed_from_p1:
                if (
                    not f
                    or f in seen_violation_files
                    or path_matches_any(f, allowed_patterns)
                ):
                    continue
                blob_c = _blob_at(repo, commit, f)
                if blob_c == _blob_at(repo, p1, f):
                    continue
                if any(blob_c == _blob_at(repo, up, f) for up in upstream_parents):
                    continue
                violations.append(
                    Violation(
                        file=f,
                        commit=commit[:10],
                        subject=subject,
                        reason="merge commit introduces non-upstream change outside allowed areas",
                    )
                )
                seen_violation_files.add(f)

    net_changed = [
        f
        for f in _git(
            repo, "diff", "--name-only", base_resolved, head_resolved
        ).splitlines()
        if f
    ]
    allowed_files: list[str] = []
    upstream_synced_files: list[str] = []

    for f in net_changed:
        if path_matches_any(f, allowed_patterns):
            allowed_files.append(f)
            continue
        blob_head = _blob_at(repo, head_resolved, f)
        blob_base = _blob_at(repo, base_resolved, f)
        if blob_head == blob_base:
            continue
        if upstream_mb and blob_head == _blob_at(repo, upstream_mb, f):
            upstream_synced_files.append(f)
            continue
        if f not in seen_violation_files:
            head_subject = _git(repo, "show", "-s", "--format=%s", head_resolved)
            violations.append(
                Violation(
                    file=f,
                    commit=head_resolved[:10],
                    subject=head_subject,
                    reason="net PR diff modifies path outside allowed areas",
                )
            )
            seen_violation_files.add(f)

    return CheckResult(
        ok=len(violations) == 0,
        base_ref=base_ref,
        head_ref=head_ref,
        base_sha=base_resolved,
        head_sha=head_resolved,
        allowed_patterns=allowed_patterns,
        upstream_repo=upstream_repo,
        upstream_branch=upstream_branch,
        upstream_sha=upstream_resolved,
        changed_files=net_changed,
        allowed_files=allowed_files,
        upstream_synced_files=upstream_synced_files,
        violations=violations,
    )


def render_markdown_report(
    result: CheckResult,
    *,
    repository: str = "",
    remediation: str = "",
) -> str:
    repo_label = f"`{repository}` " if repository else ""
    patterns_md = ", ".join(f"`{p}`" for p in result.allowed_patterns)
    lines: list[str] = [COMMENT_MARKER]

    if result.ok:
        lines.append(f"### ✅ Protected branch check passed (`{result.base_ref}`)")
        lines.append("")
        lines.append(
            f"All changes targeting {repo_label}`{result.base_ref}` stay within the allowed carry areas ({patterns_md})"
            + (
                f" or sync cleanly from `{result.upstream_repo}:{result.upstream_branch}`."
                if result.upstream_repo
                else "."
            )
        )
        if result.allowed_files:
            lines.append(
                f"- **Allowed carry files touched ({len(result.allowed_files)}):** "
                + ", ".join(f"`{f}`" for f in result.allowed_files[:15])
                + (
                    f" (+{len(result.allowed_files) - 15} more)"
                    if len(result.allowed_files) > 15
                    else ""
                )
            )
        if result.upstream_synced_files:
            lines.append(
                f"- **Upstream sync files ({len(result.upstream_synced_files)}):** "
                f"cleanly synced from `{result.upstream_repo}:{result.upstream_branch}` (`{result.upstream_sha[:10]}`)"
            )
        return "\n".join(lines) + "\n"

    lines.append(
        f"### ❌ Pull request blocked: `{result.base_ref}` only accepts changes in allowed areas"
    )
    lines.append("")
    lines.append(
        f"Branch `{result.base_ref}` {f'in {repo_label}' if repo_label else ''}is protected. "
        f"Pull requests targeting `{result.base_ref}` may only modify:"
    )
    for p in result.allowed_patterns:
        lines.append(f"- `{p}`")
    if result.upstream_repo:
        lines.append(
            f"- Clean upstream sync merges from `{result.upstream_repo}:{result.upstream_branch}`"
        )
    lines.append("")
    lines.append(
        f"#### Disallowed changes ({len(result.violations)} violation{'s' if len(result.violations) != 1 else ''})"
    )
    lines.append("")
    lines.append("| File | Commit | Commit Subject | Why blocked |")
    lines.append("| --- | --- | --- | --- |")
    for v in result.violations[:40]:
        safe_subject = v.subject.replace("|", "\\|")
        lines.append(f"| `{v.file}` | `{v.commit}` | {safe_subject} | {v.reason} |")
    if len(result.violations) > 40:
        lines.append(f"| _...and {len(result.violations) - 40} more_ | | | |")
    lines.append("")
    lines.append("#### How to fix")
    lines.append("")
    if remediation.strip():
        lines.append(remediation.strip())
    else:
        slug = (
            result.head_ref
            if result.head_ref and result.head_ref not in ("HEAD", "main")
            else "feat/<slug>"
        )
        lines.extend(
            [
                f"1. **Keep feature/fix code on its own topic branch** (`{slug}`), never in a PR directly targeting `{result.base_ref}`.",
                f"2. If this repository assembles topic branches via `.omp-build/branches/`, push `{slug}` to the fork and open a PR to `{result.base_ref}` that **only** adds `.omp-build/branches/{slug}.toml`:",
                "   ```bash",
                "   git fetch origin main",
                f"   wt switch --create chore/register-{slug.replace('/', '-')} --base origin/{result.base_ref}",
                f"   # create .omp-build/branches/{slug}.toml",
                "   wt step commit",
                f"   gh pr create --base {result.base_ref} --fill",
                "   ```",
            ]
        )
    return "\n".join(lines) + "\n"


def emit_annotations(result: CheckResult) -> None:
    if result.ok:
        print(
            f"::notice title=Protected branch check passed::All changes targeting {result.base_ref} match allowed areas."
        )
        return
    allowed_str = ", ".join(result.allowed_patterns)
    seen: set[str] = set()
    for v in result.violations:
        if v.file in seen:
            continue
        seen.add(v.file)
        print(
            f"::error file={v.file},title=Disallowed path on {result.base_ref}::"
            f"'{v.file}' (commit {v.commit}: {v.subject}) is outside allowed paths [{allowed_str}]. "
            f"Land code changes on a topic branch instead of {result.base_ref}."
        )


def resolve_upstream_from_event(
    upstream_repo_input: str,
    upstream_branch_input: str,
    event_path: str,
) -> tuple[str, str]:
    raw = upstream_repo_input.strip()
    branch = upstream_branch_input.strip() or "main"
    if raw.lower() in ("none", "false", "off"):
        return ("", branch)
    if raw and raw.lower() != "auto":
        return (raw, branch)
    if not event_path or not os.path.isfile(event_path):
        return ("", branch)
    try:
        with open(event_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        parent = (payload.get("repository") or {}).get("parent") or {}
        parent_full = str(parent.get("full_name") or "").strip()
        parent_branch = str(parent.get("default_branch") or "").strip()
        if parent_full:
            return (
                parent_full,
                upstream_branch_input.strip() or parent_branch or "main",
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"::debug::Could not read upstream parent from event payload: {exc}")
    return ("", branch)


def fetch_upstream_sha(repo: str, upstream_repo: str, upstream_branch: str) -> str:
    if not upstream_repo:
        return ""
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", upstream_repo):
        raise ValueError(f"Invalid upstream_repo format: {upstream_repo!r}")
    remote_ref = f"refs/remotes/protect_main_upstream/{upstream_branch}"
    url = f"https://github.com/{upstream_repo}.git"
    _git(
        repo,
        "fetch",
        "--quiet",
        "--no-tags",
        url,
        f"+refs/heads/{upstream_branch}:{remote_ref}",
    )
    return _git(repo, "rev-parse", "--verify", f"{remote_ref}^{{commit}}")


def _https_json_request(
    *,
    api_url: str,
    path: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> tuple[int, str]:
    parsed = urllib.parse.urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Expected https API URL, got {api_url!r}")
    base_path = parsed.path.rstrip("/")
    full_path = f"{base_path}/{path.lstrip('/')}"
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=15)
    try:
        conn.request(method, full_path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        return (resp.status, data)
    finally:
        conn.close()


def upsert_pr_comment(
    *,
    api_url: str,
    repository: str,
    pr_number: int,
    token: str,
    body: str,
    only_when_blocked_or_existing: bool = True,
    is_blocked: bool,
) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "zysys-protect-main-paths",
    }
    try:
        status, raw = _https_json_request(
            api_url=api_url,
            path=f"repos/{repository}/issues/{pr_number}/comments?per_page=100",
            method="GET",
            headers=headers,
        )
        if status >= 400:
            print(
                f"::notice title=PR comment skipped::Could not list PR comments (HTTP {status}). Job summary still written."
            )
            return
        comments = json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"::notice title=PR comment skipped::{exc}")
        return

    existing_id: int | None = None
    for item in comments:
        if COMMENT_MARKER in str(item.get("body") or ""):
            existing_id = int(item["id"])
            break

    if not is_blocked and existing_id is None and only_when_blocked_or_existing:
        return

    payload_bytes = json.dumps({"body": body}).encode("utf-8")
    target_path = (
        f"repos/{repository}/issues/comments/{existing_id}"
        if existing_id is not None
        else f"repos/{repository}/issues/{pr_number}/comments"
    )
    method = "PATCH" if existing_id is not None else "POST"
    try:
        write_status, _ = _https_json_request(
            api_url=api_url,
            path=target_path,
            method=method,
            headers={**headers, "Content-Type": "application/json"},
            body=payload_bytes,
        )
        if write_status >= 400:
            print(
                f"::notice title=PR comment skipped::Could not {method} PR comment (HTTP {write_status}). Job summary still written."
            )
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"::notice title=PR comment skipped::{exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Block pull requests to main that modify paths outside allowed carry areas."
    )
    parser.add_argument("--repo", default=os.environ.get("REPO_PATH", "."))
    parser.add_argument("--allowed-paths", default=os.environ.get("ALLOWED_PATHS", ""))
    parser.add_argument("--base-sha", default=os.environ.get("BASE_SHA", ""))
    parser.add_argument("--head-sha", default=os.environ.get("HEAD_SHA", "HEAD"))
    parser.add_argument("--base-ref", default=os.environ.get("BASE_REF", "main"))
    parser.add_argument("--head-ref", default=os.environ.get("HEAD_REF", "HEAD"))
    parser.add_argument(
        "--upstream-repo", default=os.environ.get("UPSTREAM_REPO", "auto")
    )
    parser.add_argument(
        "--upstream-branch", default=os.environ.get("UPSTREAM_BRANCH", "")
    )
    parser.add_argument("--upstream-sha", default=os.environ.get("UPSTREAM_SHA", ""))
    parser.add_argument("--remediation", default=os.environ.get("REMEDIATION", ""))
    parser.add_argument(
        "--post-comment",
        default=os.environ.get("POST_COMMENT", "true"),
    )
    args = parser.parse_args(argv)

    allowed_patterns = parse_allowed_patterns(args.allowed_paths)
    if not allowed_patterns:
        print(
            "::error title=Invalid configuration::allowed_paths input is empty.",
            file=sys.stderr,
        )
        return 2

    if not args.base_sha or not HEX_SHA_RE.match(args.base_sha):
        print(
            f"::error title=Invalid base SHA::base_sha {args.base_sha!r} is missing or not a git SHA.",
            file=sys.stderr,
        )
        return 2

    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    upstream_repo, upstream_branch = resolve_upstream_from_event(
        args.upstream_repo, args.upstream_branch, event_path
    )
    upstream_sha = args.upstream_sha.strip()
    if not upstream_sha and upstream_repo:
        upstream_sha = fetch_upstream_sha(args.repo, upstream_repo, upstream_branch)

    result = evaluate_repo(
        args.repo,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
        allowed_patterns=allowed_patterns,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        upstream_sha=upstream_sha,
        upstream_repo=upstream_repo,
        upstream_branch=upstream_branch,
    )

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    report_md = render_markdown_report(
        result,
        repository=repository,
        remediation=args.remediation,
    )

    emit_annotations(result)
    print(report_md)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report_md)

    post_comment_enabled = args.post_comment.strip().lower() in ("1", "true", "yes")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    pr_number_str = os.environ.get("PR_NUMBER", "").strip()
    if not pr_number_str and event_path and os.path.isfile(event_path):
        try:
            with open(event_path, encoding="utf-8") as fh:
                payload = json.load(fh)
            num = (payload.get("pull_request") or {}).get("number")
            if num:
                pr_number_str = str(num)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(
                f"::debug::Could not read pull_request.number from event payload: {exc}"
            )

    if post_comment_enabled and token and repository and pr_number_str.isdigit():
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        upsert_pr_comment(
            api_url=api_url,
            repository=repository,
            pr_number=int(pr_number_str),
            token=token,
            body=report_md,
            is_blocked=not result.ok,
        )

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
