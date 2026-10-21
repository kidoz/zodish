# How to build and verify distributions

Use this guide when you need a release wheel or source distribution and want to
confirm their contents and the native extension.

## Build the artifacts

Install maturin into the environment to make its CLI available, then build a
release wheel and source distribution:

```bash
python -m pip install 'maturin>=1.15,<2'
maturin build --release --locked --interpreter python --out dist
maturin sdist --out dist
python scripts/verify_dist.py dist
```

Artifacts are written to `dist/`.

If `just` is installed:

```bash
just dist         # build the wheel and sdist
just dist-check   # build and verify contents
```

`just dist-check` verifies the wheel layout, stable ABI tag, sdist contents, and
version agreement.

## Verify in a clean environment

Install an artifact and run the native check:

```bash
python examples/verify_native.py
```

This confirms that the compiled extension, rather than a source fallback, is
loaded.

## Publish from GitHub Actions

`.github/workflows/publish.yml` publishes to PyPI when a GitHub release is
published, or when the workflow is dispatched manually.

1. Sync the `pyproject.toml` and `Cargo.toml` versions and update `CHANGELOG.md`.
2. Create and publish a GitHub release whose tag matches the version, for
   example `v0.8.0`.
3. The workflow verifies the manifest versions and release tag, builds the
   sdist, builds abi3 wheels for glibc and musllinux Linux (x86_64 and aarch64),
   macOS (x86_64 and arm64), and Windows x64, verifies every artifact, and
   publishes with PyPI trusted publishing.

PyPI must have a trusted publisher configured for this repository:

| Field | Value |
| --- | --- |
| Owner | `kidoz` |
| Repository | `zodish` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

The publish job uses `id-token: write` and no API token. A manual dispatch also
publishes; use it only to retry a failed release, and expect PyPI to reject
files that already exist. Do not publish from an untrusted branch or with a tag
that does not match the manifest version.

## Know the artifact constraints

- GIL wheels use the CPython 3.14+ stable ABI and must match the operating
  system and architecture. An Apple Silicon macOS wheel cannot be used on Linux
  or Windows.
- Version-specific free-threaded `cp314t`/`cp315t` wheels are published for
  Linux x86_64/aarch64 glibc, macOS x86_64/arm64, and Windows x64.
- Installing a compatible prebuilt wheel does not require Rust. Building from
  source does.
- Free-threaded musllinux wheels are not published.

## Related

- [Contribute to Zodish](contribute.md)
- [Architecture](../explanation/architecture.md)
