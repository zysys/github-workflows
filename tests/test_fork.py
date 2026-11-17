from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import fork

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        env=ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class ForkAssembleTest(unittest.TestCase):
    """A bare `origin` holding main + topic branches, cloned fresh the way CI clones it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False)
        )
        os.environ.update({k: v for k, v in ENV.items() if k.startswith("GIT_CONFIG")})
        seed = self.tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        commit(
            seed,
            {"app.txt": "line one\nline two\nline three\n", "README": "upstream\n"},
            "upstream base",
        )
        git(seed, "switch", "-q", "-c", "feat/a")
        commit(seed, {"a.txt": "feature a\n"}, "add a")
        git(seed, "switch", "-q", "main")
        git(seed, "switch", "-q", "-c", "feat/b")
        commit(
            seed,
            {"app.txt": "line one\nline two (b)\nline three\n"},
            "b edits line two",
        )
        git(seed, "switch", "-q", "main")
        git(seed, "switch", "-q", "-c", "feat/c")
        commit(
            seed,
            {"app.txt": "line one\nline two (c)\nline three\n"},
            "c edits line two",
        )
        git(seed, "switch", "-q", "main")
        self.origin = self.tmp / "origin.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(seed), str(self.origin)],
            env=ENV,
            check=True,
        )
        self.seed = seed

    def clone(self, name: str) -> Path:
        dest = self.tmp / name
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(dest)], env=ENV, check=True
        )
        return dest

    def branches_file(self, *names: str) -> Path:
        path = self.tmp / "branches"
        path.write_text(
            "# carried, in order\n" + "\n".join(names) + "\n", encoding="utf-8"
        )
        return path

    def assemble(self, repo: Path, *args: str) -> tuple[int, dict]:
        out = self.tmp / f"result-{repo.name}.json"
        code = fork.main(
            ["assemble", "--repo", str(repo), "--fetch", "--out", str(out), *args]
        )
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_same_plan_gives_same_head_on_every_clone(self) -> None:
        branches = self.branches_file("feat/a", "feat/b")
        code_1, first = self.assemble(self.clone("one"), "--branches", str(branches))
        code_2, second = self.assemble(self.clone("two"), "--branches", str(branches))
        self.assertEqual((code_1, code_2), (0, 0))
        self.assertEqual(first["head"], second["head"])
        self.assertEqual([b["state"] for b in first["branches"]], ["merged", "merged"])

    def test_plan_rebuilds_the_identical_merge(self) -> None:
        code, planned = self.assemble(
            self.clone("plan"),
            "--branches",
            str(self.branches_file("feat/a", "feat/b")),
        )
        self.assertEqual(code, 0)
        plan = self.tmp / "plan.json"
        plan.write_text(json.dumps(planned), encoding="utf-8")
        rebuilt_repo = self.clone("rebuild")
        code, rebuilt = self.assemble(rebuilt_repo, "--plan", str(plan))
        self.assertEqual(code, 0)
        self.assertEqual(rebuilt["head"], planned["head"])

    def test_conflict_names_branch_and_files_and_leaves_clean_tree(self) -> None:
        repo = self.clone("conflict")
        code, result = self.assemble(
            repo, "--branches", str(self.branches_file("feat/b", "feat/c"))
        )
        self.assertEqual(code, fork.EXIT_CONFLICT)
        self.assertEqual((result["branch"], result["files"]), ("feat/c", ["app.txt"]))
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_recorded_resolution_replays_on_a_fresh_clone(self) -> None:
        branches = self.branches_file("feat/b", "feat/c")
        cache = self.tmp / "rr-cache"
        # Resolve once, by hand, the way `fork resolve` does.
        work = self.clone("resolve")
        code, _ = self.assemble(work, "--branches", str(branches), "--keep-conflict")
        self.assertEqual(code, fork.EXIT_CONFLICT)
        (work / "app.txt").write_text(
            "line one\nline two (b and c)\nline three\n", encoding="utf-8"
        )
        recorded = fork.main(["record", "--repo", str(work), "--rr-cache", str(cache)])
        self.assertEqual(recorded, 0)
        self.assertTrue(any(cache.iterdir()))
        # A clean clone with only the committed cache now assembles without help.
        code, result = self.assemble(
            self.clone("replay"), "--branches", str(branches), "--rr-cache", str(cache)
        )
        self.assertEqual(code, 0, result)
        merged = git(self.tmp / "replay", "show", f"{result['head']}:app.txt")
        self.assertEqual(merged, "line one\nline two (b and c)\nline three")

    def test_branch_upstream_already_merged_is_flagged(self) -> None:
        git(self.seed, "merge", "-q", "--ff-only", "feat/a")
        subprocess.run(
            ["git", "-C", str(self.seed), "push", "-q", str(self.origin), "main"],
            env=ENV,
            check=True,
        )
        code, result = self.assemble(
            self.clone("landed"), "--branches", str(self.branches_file("feat/a"))
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["branches"][0]["state"], "in_base")
        self.assertIn("feat/a is already in main", result["warnings"][0])

    def test_missing_branch_is_a_usage_error(self) -> None:
        code = fork.main(
            [
                "assemble",
                "--repo",
                str(self.clone("missing")),
                "--fetch",
                "--branches",
                str(self.branches_file("feat/nope")),
            ]
        )
        self.assertEqual(code, fork.EXIT_USAGE)

    def test_duplicate_branch_line_is_refused(self) -> None:
        with self.assertRaises(fork.AssemblyError):
            fork.read_branch_list(self.branches_file("feat/a", "feat/a"))


if __name__ == "__main__":
    unittest.main()
