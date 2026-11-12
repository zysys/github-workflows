from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.protect_main_paths import (
    COMMENT_MARKER,
    evaluate_repo,
    main,
    parse_allowed_patterns,
    path_matches_any,
    render_markdown_report,
    resolve_upstream_from_event,
)


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _write_commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--no-verify", "--no-gpg-sign", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class ProtectMainPathsTest(unittest.TestCase):
    def test_parse_and_match_patterns(self) -> None:
        patterns = parse_allowed_patterns(
            """
            # comment
            ./.omp-build/
            .github/workflows/omp-build.yml, .github/workflows/protect-main.yml
            docs/**/*.md
            """
        )
        self.assertEqual(
            patterns,
            [
                ".omp-build/**",
                ".github/workflows/omp-build.yml",
                ".github/workflows/protect-main.yml",
                "docs/**/*.md",
            ],
        )
        self.assertTrue(
            path_matches_any(".omp-build/branches/feat/memory.toml", patterns)
        )
        self.assertTrue(path_matches_any(".omp-build/resolve-branches.ts", patterns))
        self.assertTrue(path_matches_any(".github/workflows/omp-build.yml", patterns))
        self.assertTrue(path_matches_any("docs/guide.md", patterns))
        self.assertTrue(path_matches_any("docs/nested/guide.md", patterns))
        self.assertFalse(
            path_matches_any("packages/coding-agent/src/index.ts", patterns)
        )
        self.assertFalse(path_matches_any(".github/workflows/other.yml", patterns))

    def test_allowed_carry_pr_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-b", "main")
            base_sha = _write_commit(
                repo,
                {
                    "packages/app.ts": "export const v = 1;\n",
                    ".omp-build/README.md": "carry\n",
                },
                "chore: initial base",
            )
            _git(repo, "checkout", "-b", "chore/register-archive")
            head_sha = _write_commit(
                repo,
                {".omp-build/branches/feat/archive.toml": 'reason = "archive"\n'},
                "ci(omp-build): register feat/archive",
            )

            res = evaluate_repo(
                str(repo),
                base_sha=base_sha,
                head_sha=head_sha,
                allowed_patterns=[".omp-build/**", ".github/workflows/**"],
                base_ref="main",
                head_ref="chore/register-archive",
            )
            self.assertTrue(res.ok)
            self.assertEqual(res.violations, [])
            self.assertEqual(
                res.allowed_files, [".omp-build/branches/feat/archive.toml"]
            )
            md = render_markdown_report(res, repository="zachbornheimer/oh-my-pi")
            self.assertIn(COMMENT_MARKER, md)
            self.assertIn("Protected branch check passed", md)

    def test_disallowed_pr_blocks_with_file_and_commit_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-b", "main")
            base_sha = _write_commit(
                repo,
                {"packages/app.ts": "export const v = 1;\n"},
                "chore: initial base",
            )
            _git(repo, "checkout", "-b", "feat/direct-to-main")
            head_sha = _write_commit(
                repo,
                {
                    "packages/app.ts": "export const v = 2;\n",
                    ".omp-build/branches/feat/direct.toml": 'reason = "ok"\n',
                },
                "feat: edit app directly on main PR",
            )

            res = evaluate_repo(
                str(repo),
                base_sha=base_sha,
                head_sha=head_sha,
                allowed_patterns=[".omp-build/**"],
                base_ref="main",
                head_ref="feat/direct-to-main",
            )
            self.assertFalse(res.ok)
            self.assertEqual(len(res.violations), 1)
            self.assertEqual(res.violations[0].file, "packages/app.ts")
            self.assertEqual(res.violations[0].commit, head_sha[:10])
            md = render_markdown_report(
                res,
                repository="zachbornheimer/oh-my-pi",
                remediation="Register a topic branch under `.omp-build/branches/` instead.",
            )
            self.assertIn("Pull request blocked", md)
            self.assertIn("`packages/app.ts`", md)
            self.assertIn("Register a topic branch under `.omp-build/branches/`", md)

    def test_clean_upstream_sync_merge_passes_while_dirty_merge_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-b", "upstream-main")
            u1 = _write_commit(
                repo,
                {"packages/core.ts": "export const core = 1;\n"},
                "feat: upstream initial",
            )
            _git(repo, "checkout", "-b", "main", u1)
            fork_base = _write_commit(
                repo,
                {".omp-build/branches/assembled.toml": "layer = 10\n"},
                "ci(omp-build): carry manifest",
            )
            _git(repo, "checkout", "upstream-main")
            u2 = _write_commit(
                repo,
                {
                    "packages/core.ts": "export const core = 2;\n",
                    "packages/new-upstream.ts": "export const up = true;\n",
                },
                "feat: upstream release 2",
            )

            # 1. Clean sync PR merging u2 into fork main
            _git(repo, "checkout", "-b", "chore/sync-upstream", fork_base)
            _git(
                repo,
                "merge",
                "--no-ff",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                "Merge upstream main",
                u2,
            )
            clean_sync_head = _git(repo, "rev-parse", "HEAD")

            clean_res = evaluate_repo(
                str(repo),
                base_sha=fork_base,
                head_sha=clean_sync_head,
                allowed_patterns=[".omp-build/**", ".github/workflows/**"],
                upstream_sha=u2,
                upstream_repo="can1357/oh-my-pi",
                upstream_branch="main",
            )
            self.assertTrue(clean_res.ok)
            self.assertEqual(clean_res.violations, [])
            self.assertEqual(
                sorted(clean_res.upstream_synced_files),
                ["packages/core.ts", "packages/new-upstream.ts"],
            )

            # 2. Topic branch merged into main (NOT in upstream) -> must block
            _git(repo, "checkout", "-b", "feat/rogue-topic", fork_base)
            rogue_tip = _write_commit(
                repo,
                {"packages/rogue.ts": "export const rogue = true;\n"},
                "feat: rogue change",
            )
            _git(repo, "checkout", "-b", "chore/bad-merge", fork_base)
            _git(
                repo,
                "merge",
                "--no-ff",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                "Merge rogue topic",
                rogue_tip,
            )
            bad_merge_head = _git(repo, "rev-parse", "HEAD")

            bad_res = evaluate_repo(
                str(repo),
                base_sha=fork_base,
                head_sha=bad_merge_head,
                allowed_patterns=[".omp-build/**"],
                upstream_sha=u2,
                upstream_repo="can1357/oh-my-pi",
                upstream_branch="main",
            )
            self.assertFalse(bad_res.ok)
            self.assertEqual(
                [v.file for v in bad_res.violations], ["packages/rogue.ts"]
            )

    def test_auto_upstream_resolution_and_cli_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            event_file = tmp_path / "event.json"
            event_file.write_text(
                json.dumps(
                    {
                        "repository": {
                            "parent": {
                                "full_name": "can1357/oh-my-pi",
                                "default_branch": "main",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            repo_name, branch_name = resolve_upstream_from_event(
                "auto", "", str(event_file)
            )
            self.assertEqual(repo_name, "can1357/oh-my-pi")
            self.assertEqual(branch_name, "main")

            repo = tmp_path / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            base_sha = _write_commit(repo, {"a.txt": "1\n"}, "chore: init")
            head_sha = _write_commit(repo, {"b.txt": "2\n"}, "feat: disallowed")
            summary_file = tmp_path / "summary.md"

            old_summary = os.environ.get("GITHUB_STEP_SUMMARY")
            os.environ["GITHUB_STEP_SUMMARY"] = str(summary_file)
            try:
                rc = main(
                    [
                        "--repo",
                        str(repo),
                        "--allowed-paths",
                        ".omp-build/**",
                        "--base-sha",
                        base_sha,
                        "--head-sha",
                        head_sha,
                        "--upstream-repo",
                        "none",
                        "--post-comment",
                        "false",
                    ]
                )
            finally:
                if old_summary is None:
                    os.environ.pop("GITHUB_STEP_SUMMARY", None)
                else:
                    os.environ["GITHUB_STEP_SUMMARY"] = old_summary

            self.assertEqual(rc, 1)
            self.assertIn("`b.txt`", summary_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
