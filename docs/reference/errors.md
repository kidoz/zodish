# Errors reference

This page describes the result types, issue fields, error codes, and error
formatting helpers.

## Result types

`schema.safe_parse(value)` returns a discriminated union on `success`:

| Type | `success` | Payload |
| --- | --- | --- |
| `Success[T]` | `True` | `data` |
| `Failure` | `False` | `error` |

`schema.parse(value)` returns parsed data or raises `ValidationError`.
`schema.encode(value)` reverse-validates decoded data and returns encoded data;
`safe_encode(value)` returns the same `Success[T] | Failure` union.
`ValidationError` subclasses `ValueError`. Native failures retain compact issue
data until `.issues` is first read, then materialize and cache the `Issue`
objects. Its combined `str()` message is formatted on first use.

## `Issue`

Each failure is an `Issue` with:

| Field | Type | Meaning |
| --- | --- | --- |
| `code` | `str` | Machine-readable category |
| `message` | `str` | Human-readable text |
| `path` | `tuple` | Object keys and collection indices |
| `params` | mapping | Structured details; varies by code |
| `branches` | `tuple` | Per-branch issues for `invalid_union`; otherwise empty |

`Issue.to_dict()` returns a JSON-serializable dictionary with `code`, `message`,
`path`, and the params merged in.

## `ValidationError`

| Member | Type | Meaning |
| --- | --- | --- |
| `issues` | `tuple[Issue, ...]` | Every collected issue |
| `to_dict()` | `dict` | `{"issues": [...]}` |
| `flatten()` | `dict` | `{"form_errors": [...], "field_errors": {...}}` |
| `treeify()` | `dict` | Nested `errors`, `properties`, and `items` |
| `prettify()` | `str` | Newline-separated human-readable lines |

## Per-parse message mapping

`parse`, `safe_parse`, `parse_json`, `safe_parse_json`, the matching async
entry points, and the codec `decode`, `safe_decode`, `decode_async`, and
`safe_decode_async` variants accept a keyword-only `error=` callable:

```python
def explain(issue):
    if issue.code == "too_small" and issue.path == ("age",):
        return f"Age must be at least {issue.params['minimum']}."
    return issue.message

result = schema.safe_parse(payload, error=explain)
```

The callable is not invoked during validation. It runs when issue data is first
materialized through `.issues`, `str(error)`, `.to_dict()`, `.flatten()`,
`.treeify()`, or `.prettify()`, and each issue is mapped at most once per
failure. It receives the original `Issue` and must return the replacement
`message`; code, path, params, and issue order are unchanged. Branch issues in
an `invalid_union` are mapped independently. A non-string return raises
`TypeError`, and exceptions raised by the mapper propagate at materialization.
`validate()`, `validate_json()`, `validate_async()`, and successful parses never
call the mapper. There is no locale catalog or global configuration.

## Issue codes

| Code | Reported when | Example params |
| --- | --- | --- |
| `too_small` | Value is below an inclusive/exclusive lower bound | `minimum`, `inclusive`, `origin` |
| `too_big` | Value is above an inclusive/exclusive upper bound | `maximum`, `inclusive`, `origin` |
| `not_multiple_of` | Value is not a multiple of the divisor | `divisor` |
| `invalid_type` | Wrong Python type, or a failed coercion | `expected` |
| `invalid_value` | Value does not match a literal, a `z.enum` choice, or a discriminator | `values` |
| `invalid_format` | String predicate or format check failed | `format`; JSON text adds `position`; predicates add `prefix`, `suffix`, or `substring` |
| `invalid_key` | An object or record key is not a string | `expected` |
| `unrecognized_key` | Strict object received an unknown key | `keys` |
| `invalid_length` | Tuple input has the wrong number of items | `expected` |
| `invalid_union` | No union branch matched | `branches` on the issue |
| `invalid_intersection_types` | An intersection branch did not produce a dictionary | None |
| `required` | Required input is missing | None |
| `custom` | A refinement or callback rejected the value | Varies |

Params are merged into each serialized issue. Codes not listed here should be
treated as new categories rather than parsed by message text. Malformed input to
`parse_json` and `parse_json_async` report `invalid_format` at the root with `format="json"` and a
`position`.

## Aggregation

- Object validation collects sibling failures instead of stopping at the first.
- Paths contain object keys and collection indices, such as
  `("users", 0, "age")`.
- A union produces one `invalid_union` issue whose `branches` contain each
  branch's issues.
- `z.enum()` produces one `invalid_value` issue, not a union failure.
- A discriminated union reports `required` at `(key,)` when the discriminator is
  missing, and `invalid_value` when it is unknown.

## Issues from callbacks

Any callback can raise `ValidationError` to contribute issues to the surrounding
validation. Supplied issue paths are preserved as written and are relative to the
current location. Raising `ValidationError([])` produces a `custom` issue at the
current path with the message `Invalid value`, so the rejection still aggregates
with sibling failures.

`safe_parse`, `safe_parse_async`, `safe_parse_json_async`, `safe_decode`,
`safe_decode_async`, `safe_encode`, and `safe_encode_async` catch only
`ValidationError`. Other exceptions from callbacks and asyncio cancellation
propagate.

## Related

- [Handle errors](../how-to/handle-errors.md)
- [Transform and refine values](../how-to/transform-and-refine.md)
- [API reference](api.md)
