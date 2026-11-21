"""Regression checks for the release artifact and publishing boundaries."""

import contextlib
import io
import re
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.verify_dist import SDIST_REQUIRED, verify_sdist


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_test_workflow_uses_a_read_only_token(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^permissions:\n  contents: read\n\S")
        self.assertNotRegex(workflow, r"(?m)^\s*[\w-]+: (?:write|write-all)\s*$")

    def test_maturin_excludes_local_install_metadata(self):
        with (ROOT / "pyproject.toml").open("rb") as manifest:
            excluded = tomllib.load(manifest)["tool"]["maturin"]["exclude"]
        self.assertIn(".ai-standard-install.json", excluded)

    def test_sdist_verifier_rejects_local_install_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            sdist = Path(temp) / "zodish-1.0.tar.gz"
            for extra in (None, ".ai-standard-install.json"):
                with self.subTest(extra=extra):
                    files = dict.fromkeys(SDIST_REQUIRED, b"")
                    files["pyproject.toml"] = b'[project]\nversion = "1.0"\n'
                    files["Cargo.toml"] = b'[package]\nversion = "1.0"\n'
                    if extra:
                        files[extra] = b"{}"
                    with tarfile.open(sdist, "w:gz") as archive:
                        for name, data in files.items():
                            member = tarfile.TarInfo(f"zodish-1.0/{name}")
                            member.size = len(data)
                            archive.addfile(member, io.BytesIO(data))
                    if extra:
                        with self.assertRaisesRegex(SystemExit, re.escape(extra)):
                            verify_sdist(sdist)
                    else:
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(verify_sdist(sdist), "1.0")

    def test_release_builds_and_verifies_the_same_commit(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        jobs = dict(
            re.findall(
                r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                workflow.split("\njobs:\n", 1)[1],
                re.M | re.S,
            )
        )
        resolve = jobs["verify-version"]
        self.assertIn("github.sha", resolve)
        self.assertIn("format('refs/tags/{0}', inputs.tag)", resolve)
        self.assertIn("git rev-parse HEAD", resolve)
        for name in (
            "build-sdist",
            "build-wheels",
            "build-free-threaded-wheels",
            "verify-artifacts",
        ):
            with self.subTest(job=name):
                self.assertEqual(
                    re.findall(r"^\s+ref: (.+)$", jobs[name], re.M),
                    ["${{ needs.verify-version.outputs.commit }}"],
                )
        verify = jobs["verify-artifacts"]
        self.assertNotIn("id-token:", verify)
        self.assertLess(
            verify.index("python scripts/verify_dist.py"), verify.index("upload-artifact@")
        )
        self.assertIn("steps.verified.outputs.artifact-id", verify)

    def test_publisher_only_downloads_verified_artifact_and_publishes(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
        publish = workflow.split("\n  publish:\n", 1)[1]
        self.assertIn("needs: verify-artifacts", publish)
        self.assertIn("if: needs.verify-artifacts.outputs.artifact-id != ''", publish)
        self.assertIn("environment: pypi", publish)
        self.assertIn("id-token: write", publish)
        self.assertIn("artifact-ids: ${{ needs.verify-artifacts.outputs.artifact-id }}", publish)
        self.assertEqual(
            re.findall(r"^\s+(?:- )?uses: ([^@]+)@[0-9a-f]{40}", publish, re.M),
            ["actions/download-artifact", "pypa/gh-action-pypi-publish"],
        )
        self.assertNotRegex(publish, r"(?m)^\s+(?:- )?run:")
