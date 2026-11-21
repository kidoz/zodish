"""Verify that the installed package loaded its compiled Rust extension."""

import importlib.machinery
import sys


def main() -> None:
    gil_enabled = sys._is_gil_enabled()
    from zodish import _native, z

    if sys._is_gil_enabled() != gil_enabled:
        raise SystemExit("importing the native module changed the GIL state")
    if _native.BACKEND != "rust":
        raise SystemExit(f"unexpected native backend: {_native.BACKEND!r}")
    extension = _native.__file__ or ""
    if not any(extension.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
        raise SystemExit(f"native module is not an extension: {extension!r}")
    if z.object({"ok": z.boolean()}).parse({"ok": True}) != {"ok": True}:
        raise SystemExit("installed package failed a basic parse")
    print(f"native backend: {_native.BACKEND} ({extension})")


if __name__ == "__main__":
    main()
