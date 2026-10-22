# Development command shortcuts for zodish.

python := env_var_or_default("PYTHON", "python3.14")

# List available recipes.
default:
    @just --list

# Install the package in editable mode with development tools.
install:
    {{ python }} -m pip install --group dev -e .

# Run the standard-library test suite.
test:
    {{ python }} -m unittest discover -s tests -v

# Run the example scripts.
examples:
    {{ python }} examples/quickstart.py
    {{ python }} examples/typing_checks.py

# Lint Python code with ruff.
lint:
    ruff check .

# Type-check Python code with mypy.
typecheck:
    mypy

# Check Rust formatting.
rust-fmt:
    cargo fmt --check

# Lint Rust code with clippy.
rust-clippy:
    cargo clippy --locked -- -D warnings

# Check Rust dependencies for advisories, licenses, duplicates, and sources.
rust-deny:
    cargo deny check

# Run all static checks (the CI lint and deny jobs).
check: lint typecheck rust-fmt rust-clippy rust-deny

# Run tests and examples after the static checks.
ci: check test examples

# Run the parse benchmarks.
bench:
    {{ python }} benchmarks/bench_parse.py

# Run the async parse benchmarks.
bench-async *args:
    {{ python }} benchmarks/bench_async.py {{ args }}

# Verify competitor semantics without collecting timings.
bench-check:
    {{ python }} benchmarks/bench_compare.py --check-only

# Run calibrated competitor benchmarks (install the benchmark group first).
bench-compare *args:
    {{ python }} benchmarks/bench_compare.py {{ args }}

# Build the release wheel and source distribution into dist/.
dist:
    rm -rf dist
    maturin build --release --locked --interpreter {{ python }} --out dist
    maturin sdist --out dist

# Build the distributions and verify their contents.
dist-check: dist
    {{ python }} scripts/verify_dist.py dist
