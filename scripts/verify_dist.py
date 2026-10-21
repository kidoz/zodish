"""Verify the contents of the built wheel and source distribution."""

from __future__ import annotations

import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


WHEEL_REQUIRED = {
    "zodish/__init__.py",
    "zodish/schemas.py",
    "zodish/_native.pyi",
    "zodish/py.typed",
}
SDIST_REQUIRED = {
    "pyproject.toml",
    "Cargo.toml",
    "Cargo.lock",
    "README.md",
    "LICENSE",
    "rust/lib.rs",
    "rust/errors.rs",
    "rust/runtime.rs",
    "rust/schema.rs",
    "rust/schema/checks.rs",
    "rust/schema/evaluate.rs",
    "rust/schema/json.rs",
    "rust/schema/kind.rs",
    "rust/session.rs",
    "rust/session/callbacks.rs",
    "rust/session/composition.rs",
    "rust/session/containers.rs",
    "rust/session/dispatch.rs",
    "rust/values.rs",
    "src/zodish/__init__.py",
    "src/zodish/schemas.py",
    "src/zodish/_native.pyi",
    "src/zodish/py.typed",
    "tests/test_schemas.py",
    "tests/test_native.py",
    "examples/quickstart.py",
    "examples/typing_checks.py",
    "examples/verify_native.py",
    "benchmarks/bench_parse.py",
    ".github/workflows/tests.yml",
}


def _all(dist: Path, pattern: str) -> list[Path]:
    matches = sorted(dist.glob(pattern))
    if not matches:
        raise SystemExit(f"expected at least one {pattern} in {dist}")
    return matches


def _require(names: set[str], required: set[str], artifact: Path) -> None:
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"{artifact.name} is missing: {', '.join(missing)}")


def _wheel_version(wheel: Path) -> str:
    parts = wheel.name.split("-")
    if len(parts) < 5:
        raise SystemExit(f"invalid wheel filename: {wheel.name}")
    return parts[1]


def verify_wheel(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    _require(names, WHEEL_REQUIRED, wheel)
    natives = {
        name
        for name in names
        if name.startswith("zodish/_native.") and name.endswith((".so", ".pyd"))
    }
    if len(natives) != 1:
        raise SystemExit(
            f"{wheel.name} must contain exactly one native extension, found {sorted(natives)}"
        )
    if "abi3" not in wheel.stem.split("-"):
        raise SystemExit(f"{wheel.name} is not tagged with the stable ABI (abi3)")
    print(f"wheel ok: {wheel.name} ({natives.pop()})")
    return _wheel_version(wheel)


def verify_sdist(sdist: Path) -> str:
    with tarfile.open(sdist, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
        pyprojects = sorted(
            name
            for name in names
            if PurePosixPath(name).name == "pyproject.toml" and len(PurePosixPath(name).parts) == 2
        )
        if len(pyprojects) != 1:
            raise SystemExit(f"{sdist.name} must contain one top-level pyproject.toml")
        root = PurePosixPath(pyprojects[0]).parent
        _require(names, {str(root / name) for name in SDIST_REQUIRED}, sdist)
        pyproject_file = archive.extractfile(str(root / "pyproject.toml"))
        cargo_file = archive.extractfile(str(root / "Cargo.toml"))
        if pyproject_file is None or cargo_file is None:
            raise SystemExit(f"{sdist.name} is missing build metadata")
        pyproject = tomllib.loads(pyproject_file.read().decode())
        cargo = tomllib.loads(cargo_file.read().decode())

    python_version: str = str(pyproject["project"]["version"])
    cargo_version: str = str(cargo["package"]["version"])
    if python_version != cargo_version:
        raise SystemExit(
            f"version mismatch: pyproject {python_version}, Cargo.toml {cargo_version}"
        )
    expected_root = f"zodish-{python_version}"
    if root.name != expected_root:
        raise SystemExit(f"{sdist.name} root is {root.name!r}, expected {expected_root!r}")
    print(f"sdist ok: {sdist.name} ({python_version})")
    return python_version


def main(argv: list[str] | None = None) -> int:
    dist = Path(argv[0]) if argv else Path("dist")
    if not dist.is_dir():
        raise SystemExit(f"distribution directory not found: {dist}")
    wheels = _all(dist, "*.whl")
    sdists = _all(dist, "*.tar.gz")
    wheel_versions = {verify_wheel(wheel) for wheel in wheels}
    sdist_versions = {verify_sdist(sdist) for sdist in sdists}
    versions = wheel_versions | sdist_versions
    if len(versions) != 1:
        raise SystemExit(
            "artifact version mismatch: "
            f"wheels {sorted(wheel_versions)}, sdists {sorted(sdist_versions)}"
        )
    print(
        f"artifacts ok: {len(wheels)} wheel(s), {len(sdists)} sdist(s), "
        f"version {versions.pop()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
