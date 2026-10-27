# Examples

Runnable programs for the tasks the [quick start](../docs/tutorials/get-started.md)
stops short of. Each file checks its own result. From an environment where
Zodish is installed:

```bash
python examples/api_boundary.py
python examples/string_inputs.py
python examples/domain_events.py
python examples/account.py
python examples/registration.py
python examples/category_tree.py
python examples/async_signup.py
python examples/typed_objects.py
python examples/annotation_validation.py
```

`just examples` runs these together with `quickstart.py` and `typing_checks.py`.

| Example | What it shows |
| --- | --- |
| [quickstart.py](quickstart.py) | A first object schema, `safe_parse`, and a transform to a dataclass. |
| [api_boundary.py](api_boundary.py) | JSON in, camelCase wire names, decimal and datetime codecs, an error map, and a reversible schema for `encode` and OpenAPI, with TypedDict-typed results via `z.typed_object`. |
| [string_inputs.py](string_inputs.py) | Environment variables and query strings via `z.coerce` and `stringbool`. |
| [domain_events.py](domain_events.py) | A discriminated union for webhook payloads, including encode back to wire names. |
| [account.py](account.py) | `@z.dataclass`: constructor values, `parse`/`encode`, and which side JSON Schema can describe. |
| [registration.py](registration.py) | Aggregated field errors, then a cross-field refinement with one issue per path. |
| [category_tree.py](category_tree.py) | A recursive lazy schema, copied default lists, and `$ref` export. |
| [async_signup.py](async_signup.py) | `refine_async` beside a normal field check, awaited with `parse_async`. |
| [typed_objects.py](typed_objects.py) | Which object forms mypy can see: plain `z.object`, `z.typed_object`, transform, `@z.dataclass`, and `z.create_model`. |
| [annotation_validation.py](annotation_validation.py) | `z.adapter` for annotation-driven schemas and `@z.validate_call` for typed function boundaries. |

`typing_checks.py` is a mypy fixture. `verify_native.py` confirms that an
installed build loaded the Rust extension.
