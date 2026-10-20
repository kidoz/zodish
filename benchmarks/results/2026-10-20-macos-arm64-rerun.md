# Local validation benchmark results

Mean ± sample standard deviation in **microseconds per operation**; lower is better.
This is a local measurement, not a cross-platform performance claim.
Missing cells indicate unsupported scenarios, not zero cost.

- Python: 3.14.8 (64-bit)
- Platform: macOS-27.0.1-arm64-arm-64bit-Mach-O
- GIL enabled: True
- Zodish build: release (caller supplied)
- marshmallow: 4.3.1
- msgspec: 0.22.0
- pydantic: 2.14.1
- pydantic_core: 2.50.1
- pyperf: 2.10.0
- zodish: 0.2.0

| Scenario | Zodish | Pydantic | msgspec | Marshmallow |
| --- | ---: | ---: | ---: | ---: |
| string_success | 0.138 ± 0.003 | 0.198 ± 0.003 | 0.288 ± 0.004 | 0.506 ± 0.006 |
| object_success | 0.373 ± 0.005 | 0.362 ± 0.004 | 0.629 ± 0.012 | 5.440 ± 0.733 |
| array_1000 | 5.891 ± 0.243 | 5.829 ± 0.087 | 3.406 ± 0.739 | 872.093 ± 169.323 |
| recursive_tree | 5.789 ± 0.224 | 3.417 ± 0.022 | 2.186 ± 0.032 | 144.182 ± 26.766 |
| refine_callback | 0.166 ± 0.002 | 0.219 ± 0.004 | — | 0.782 ± 0.087 |
| json_object | 0.565 ± 0.011 | 0.439 ± 0.008 | 0.570 ± 0.009 | 6.392 ± 0.046 |
| union_first | 0.215 ± 0.004 | 0.293 ± 0.003 | — | — |
| union_last | 0.686 ± 0.013 | 0.690 ± 0.009 | — | — |
| discriminated_first | 0.248 ± 0.003 | 0.336 ± 0.009 | — | — |
| discriminated_last | 0.297 ± 0.003 | 0.340 ± 0.005 | — | — |
| object_failure_single | 0.861 ± 0.017 | 0.724 ± 0.007 | 0.964 ± 0.010 | 8.911 ± 0.114 |
| object_failure_multi | 0.972 ± 0.019 | 0.798 ± 0.013 | 0.942 ± 0.017 | 12.312 ± 0.469 |
| error_string | 0.058 ± 0.001 | 0.507 ± 0.010 | 0.024 ± 0.000 | 0.280 ± 0.004 |
| schema_build | 9.604 ± 0.267 | 160.507 ± 4.731 | — | 65.753 ± 1.697 |

Measured samples per benchmark: 15.

## Interpretation

- Multi-error parsing returns three issues for Zodish, Pydantic and Marshmallow,
  but only the first issue for msgspec. Those cells measure different work.
- `error_string` formats an existing single-error exception. Libraries differ
  in eager versus deferred formatting and message detail; treat it as diagnostic.
- `schema_build` measures each adapter's factory, including Python wrapper creation.
  Pydantic uses an existing TypedDict; Marshmallow creates a schema class and instance;
  Zodish creates a schema graph. These are setup costs, not identical compiler work.
- JSON cases include decoding and validation. Zodish uses `parse_json`,
  Pydantic uses `validate_json`, msgspec uses `json.decode`, and Marshmallow
  uses stdlib `json.loads`.
- All timed inputs use the documented common subset. msgspec and Marshmallow
  additionally accept tuples for list fields; the preflight records that difference.
- Ordinary unions are left-to-right. Tagged unions are separate scenarios.
  Only Zodish and Pydantic participate in these dictionary-output union cases.
- Recursive schemas are prepared before timing, including Zodish's lazy target.

**High variability (standard deviation ≥ 10% of mean):**

- `object_success/marshmallow`
- `array_1000/msgspec`
- `array_1000/marshmallow`
- `recursive_tree/marshmallow`
- `refine_callback/marshmallow`

Repeat these measurements on an idle machine before interpreting small differences.

The variability check above is a simple descriptive flag, not a significance test.
Inspect the raw pyperf results for worker settings, warmups and stability warnings.

## Reproducibility fingerprints

- benchmark_sha256: `cae9f3e2f60f46eef499d4f442ca4a9daf30f8093fc13e24267fd2d6699e1a2c`
- zodish_python_sha256: `b928260c0ef767eb150e1097bbc37778ea8ad1090b63f54e16f29a7ced2615ee`
- zodish_native_sha256: `54e0970f4bedaae309e3d8089a8106627811a510ca00f00338d22bf5f9782d72`
