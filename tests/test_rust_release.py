"""Structure of the reusable rust-release workflow.

The workflow is the only place a fork's release assets are named, so these
assertions pin the contract mise's `github:` backend relies on: one archive per
target called `<binary>-<target>.tar.gz`, a SHA256SUMS beside them, and a
`--latest` release unless the caller asked for a prerelease.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/rust-release.yml"


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def step_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in steps(job))


class RustReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load()

    def test_called_with_a_binary_and_a_tag(self) -> None:
        inputs = self.workflow[True]["workflow_call"]["inputs"]
        self.assertEqual(inputs["binary"]["type"], "string")
        self.assertEqual(inputs["tag"]["type"], "string")
        self.assertTrue(inputs["binary"].get("required"))
        self.assertTrue(inputs["tag"].get("required"))

    def test_default_targets_build_on_their_own_os(self) -> None:
        inputs = self.workflow[True]["workflow_call"]["inputs"]
        targets = inputs["targets"]["default"]
        parsed = json.loads(" ".join(targets.split()))
        by_target = {entry["target"]: entry["os"] for entry in parsed}
        self.assertEqual(by_target.get("darwin-arm64"), "macos-latest")
        self.assertTrue(by_target.get("linux-x64", "").startswith("ubuntu"))
        self.assertTrue(by_target.get("linux-arm64", "").startswith("ubuntu"))
        for entry in parsed:
            self.assertTrue(entry["os"], entry)

    def test_build_matrix_comes_from_the_targets_input(self) -> None:
        build = self.workflow["jobs"]["build"]
        include = build["strategy"]["matrix"]["include"]
        self.assertEqual(include, "${{ fromJSON(inputs.targets) }}")
        self.assertEqual(build["runs-on"], "${{ matrix.os }}")

    def test_build_checks_the_binary_runs_before_publishing(self) -> None:
        body = step_text(self.workflow["jobs"]["build"])
        self.assertIn('cargo build --release --bin "$INPUT_BINARY"', body)
        self.assertIn("--version", body)

    def test_each_target_publishes_one_named_archive(self) -> None:
        build = self.workflow["jobs"]["build"]
        tar = next(s for s in steps(build) if s.get("name") == "Tar the binary")
        self.assertIn("${INPUT_BINARY}-${TARGET}.tar.gz", tar["run"])
        upload = next(
            s for s in steps(build) if "upload-artifact" in str(s.get("uses"))
        )
        self.assertIn(
            "${{ inputs.binary }}-${{ matrix.target }}.tar.gz", upload["with"]["path"]
        )
        self.assertEqual(upload["with"]["if-no-files-found"], "error")

    def test_release_waits_for_every_target_and_checksums(self) -> None:
        release = self.workflow["jobs"]["release"]
        self.assertEqual(release["needs"], "build")
        self.assertEqual(release["permissions"]["contents"], "write")
        body = step_text(release)
        self.assertIn("sha256sum ./*.tar.gz > SHA256SUMS", body)
        self.assertIn('assets/"$BINARY"-*.tar.gz assets/SHA256SUMS', body)

    def test_release_is_latest_unless_the_caller_asked_for_a_prerelease(self) -> None:
        body = step_text(self.workflow["jobs"]["release"])
        self.assertIn("channel=(--latest)", body)
        self.assertIn("channel=(--prerelease)", body)
        self.assertIn('if [ "$PRERELEASE" = "true" ]', body)

    def test_release_points_at_the_callers_commit_unless_told_otherwise(self) -> None:
        body = step_text(self.workflow["jobs"]["release"])
        self.assertIn('target="${RELEASE_TARGET:-$REF}"', body)
        self.assertIn('--target "$target"', body)


if __name__ == "__main__":
    unittest.main()
