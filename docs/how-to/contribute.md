# How to contribute to Zodish

Use this guide when you change Zodish itself: building the native extension,
running the test and quality gates, and benchmarking.

## Set up the environment

Building from source requires Rust 1.85+ and CPython 3.14+.

`psm` is pinned to retain the Rust 1.85 minimum for `stacker`. CI checks that
minimum separately from the current stable toolchain; dependency updates must
pass both builds.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --group dev -e .
python examples/quickstart.py
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. There are
no third-party Python runtime dependencies. An editable install picks up Python
edits, but does **not** rebuild the Rust extension after sources under `rust/` change.

## Run the checks

From the repository root, with the environment activated:

```bash
python -m unittest discover -s tests -v
python examples/quickstart.py
python benchmarks/bench_parse.py --smoke
ruff check .
mypy
cargo fmt --check
cargo clippy --locked -- -D warnings
cargo deny check
```

Ruff and strict mypy settings live in `pyproject.toml`; the `dev` dependency
group installs both tools. `py.typed` is included for type checkers, and
`Cargo.lock` pins Rust dependencies. `deny.toml` defines the Rust dependency
policy; install `cargo-deny` with `cargo install cargo-deny --locked` to run
the check locally.

If `just` is installed, the same workflows are available as recipes:

```bash
just                  # list recipes
just install          # install dependencies
just check            # static checks (lint, mypy, rust fmt, clippy, cargo deny)
just ci               # static checks, tests, and examples
just bench            # benchmark harness
```

`PYTHON` selects the interpreter for recipes that run Python; it defaults to
`python3.14`. For example: `PYTHON=python just ci`.

## Rebuild after changing Rust

After editing any source under `rust/`, rebuild the extension before running tests:

```bash
python -m pip install -e .
```

An editable install does not rebuild Rust automatically. If a Rust change appears
to have no effect, rebuild and confirm that the compiled extension is loaded with
`python examples/verify_native.py`.

## Run benchmarks

```bash
python benchmarks/bench_parse.py
python benchmarks/bench_parse.py --json
python benchmarks/bench_parse.py --filter union
```

The harness reports median throughput over repeated runs and has no pass/fail
thresholds; results depend on the host, Python version, and native build profile.
The failing scenarios use `safe_parse`, and one of them reads `.issues`; the
other scenarios use `parse`. `--smoke` uses minimal counts for CI.

## Understand the CI gates

The [CI workflow](https://github.com/kidoz/zodish/blob/main/.github/workflows/tests.yml)
runs tests, the quickstart, and a benchmark smoke run on Ubuntu with Python 3.14
and 3.15. A `cargo-deny` job checks the Rust dependency graph for security
advisories, yanked releases, disallowed licenses, duplicate versions, wildcard
requirements, and unexpected registry sources. Its packaging job builds the
wheel and sdist on Ubuntu, macOS, and
Windows with Python 3.14, verifies both artifact contents, then installs each
artifact and runs the test suite, quickstart, and native-extension check against
it. The package job also re-runs the abi3 wheel smoke test under Python 3.15 on
Linux. A separate free-threaded job runs the suite on CPython 3.14t and 3.15t.
A non-publishing packaging preflight builds and smoke-tests cp314t and cp315t
wheels on every supported platform, and the publish workflow builds those same
wheels for Linux x86_64/aarch64 glibc, macOS x86_64/arm64, and Windows x64.
Free-threaded musllinux wheels are not published.

## Related

- [Build distributions](build-distributions.md)
- [Architecture](../explanation/architecture.md)
- [Get started](../tutorials/get-started.md)
