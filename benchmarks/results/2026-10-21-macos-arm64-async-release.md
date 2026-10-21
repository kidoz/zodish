# Local asynchronous validation benchmark results

Median ± sample standard deviation over nine repeated samples; lower is better.
This is a local measurement on a release wheel, not a cross-platform or
statistical-significance claim.

- Date: 2026-10-21
- Python: 3.14.8 (64-bit)
- Platform: macOS-27.0.1-arm64-arm-64bit-Mach-O
- Zodish: 0.4.0 release candidate built from the working tree
- Build: release wheel, `abi3` CPython ≥ 3.14, macOS 11+ ARM64
- Benchmark script: `benchmarks/bench_async.py`
- Sync comparison: `benchmarks/bench_parse.py`

## Reproduction

```sh
python -m maturin build --release --locked --interpreter <python> \
  --out /tmp/zodish-release-final
python -m venv /tmp/zodish-wheel-314
/tmp/zodish-wheel-314/bin/python -m pip install \
  /tmp/zodish-release-final/zodish-0.4.0-cp314-abi3-macosx_11_0_arm64.whl
cd /tmp
/tmp/zodish-wheel-314/bin/python <repo>/benchmarks/bench_async.py \
  --repeat 9 --memory --json
/tmp/zodish-wheel-314/bin/python <repo>/benchmarks/bench_parse.py \
  --repeat 9 --json
```

Event loops are created outside timed iterations and schemas are warmed
before each scenario. The async harness reports median, sample standard
deviation, min/max, callback throughput, and parse throughput. Memory is
`tracemalloc` retained and peak bytes after one operation following a
garbage collection; it is not process RSS.

## Async scenarios

| Scenario | ops/s | µs/op ± sd | min | max | callbacks/s | parses/s | retained | peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `sync_baseline` | 2,384,738 | 0.419 ± 0.015 | 0.409 | 0.458 | 0 | 2,384,738 | 240 B | 256 B |
| `sync_graph` | 33,691 | 29.681 ± 0.149 | 29.292 | 29.818 | 0 | 33,691 | 1,392 B | 2,312 B |
| `immediate_coroutine` | 29,623 | 33.758 ± 0.299 | 33.353 | 34.182 | 118,491 | 29,623 | 1,727 B | 3,162 B |
| `sleep_zero` | 11,051 | 90.490 ± 0.896 | 89.718 | 92.416 | 44,204 | 11,051 | 1,725 B | 3,493 B |
| `sequential_callbacks` | 6,574 | 152.107 ± 3.909 | 148.564 | 161.418 | 52,594 | 6,574 | 1,669 B | 3,701 B |
| `nested_failures` | 27,692 | 36.112 ± 0.945 | 35.294 | 38.334 | 110,767 | 27,692 | 1,988 B | 4,308 B |
| `nested_failures_issues` | 26,420 | 37.850 ± 1.301 | 37.029 | 41.281 | 105,679 | 26,420 | 2,729 B | 5,968 B |
| `concurrent_parses` | 4,007 | 249.579 ± 12.470 | 246.698 | 286.928 | 256,432 | 64,108 | 12,822 B | 40,815 B |

- `sync_baseline` is direct synchronous `parse`, included as the reference
  point inside the same harness.
- `sync_graph` sends a wholly synchronous schema through `parse_async`; the
  measured cost includes the Python coroutine, driver, and event-loop
  `run_until_complete` call, but no callback suspension.
- `immediate_coroutine` crosses the native session boundary for four
  async callbacks that complete immediately.
- `sleep_zero` uses the same shape with `asyncio.sleep(0)` inside every
  callback, separating scheduler yield cost from session overhead.
- `sequential_callbacks` runs eight callback nodes in one object.
- `nested_failures` and `nested_failures_issues` measure aggregated async
  failures; the latter materializes public `Issue` objects.
- `concurrent_parses` measures a batch of 16 parses of one immutable schema
  on one event-loop thread. Per-operation values describe the batch;
  `parses/s` describes individual parses.

## Synchronous comparison

| Scenario | ops/s | µs/op |
| --- | ---: | ---: |
| `string_success` | 6,885,077 | 0.145 |
| `object_success` | 2,616,545 | 0.382 |
| `object_failure` | 922,552 | 1.084 |
| `object_failure_issues` | 403,485 | 2.478 |
| `array_1000` | 171,062 | 5.846 |
| `union_first` | 4,400,599 | 0.227 |
| `union_last` | 1,382,218 | 0.723 |
| `recursive_tree` | 98,993 | 10.102 |
| `refine_callback` | 5,627,462 | 0.178 |

These synchronous medians are consistent with the earlier local release
measurements. They are recorded here because the same isolated wheel and
interpreter produced both files; this run is not a performance gate.

## Interpretation

- The asynchronous path is dominated by Python coroutine and event-loop
  transport. `sync_graph` (about 30 µs/op) versus `sync_baseline` (about
  0.42 µs/op) is the cost of the public async interface around a parse that
  needs no native callback.
- An immediately completing callback is roughly 4 µs above `sync_graph`;
  a real scheduler yield via `asyncio.sleep(0)` adds roughly 61 µs per
  operation across four callbacks on this host.
- Eight sequential callbacks cost about 152 µs/op, so independent I/O should
  still be batched inside one callback rather than spread across many
  sibling callbacks when latency matters.
- Concurrent parses complete about 64,000 parses/s in aggregate while
  retaining independent native state per session. This scenario also had
  the largest dispersion (about 5% of its median) in this run.
- The memory columns are diagnostic one-operation snapshots. They should not
  be extrapolated to long-running workloads without a separate retention
  study.

## Reproducibility fingerprints

- wheel_sha256: `e8ab7d0efc2148126c13519755bd53afbc417aac91e5187bfef90148ee63af80`
- benchmark_sha256: `5944558c2d898de47ef761eddf678ee85484213c652b31d8de2fb358cf9a5d51`
- bench_parse_sha256: `1b03b812e304968a09be8931d96c9f39fd4cf67189e7591a4937ac9af87613c8`
- zodish_python_sha256: `f3f474be6ddab417dbf25269cdeb860e621a6530ba46cda14d48ec2b5e19fab1`
- zodish_native_sha256: `0d87abee05140365e120d319af6832019ba75158e419421ceb60a1798b29ebc3`

The listed hashes identify the benchmark source and installed 0.4.0 release
candidate wheel. A later build is reproducible only with the same source
tree, toolchain, and build profile.

## Raw data

- [Async raw samples](2026-10-21-macos-arm64-async-release.json)
- [Synchronous raw samples](2026-10-21-macos-arm64-sync-release.json)
