# How to export JSON Schema

Use this guide when you need a JSON Schema or OpenAPI schema from a Zodish
schema, for example to document an API or to feed another validator.

## Export the default dialect

```python
from zodish import to_json_schema, z

schema = z.object({
    "name": z.string().min(1).meta({"title": "Name"}),
    "age": z.integer().min(18),
})

to_json_schema(schema)
# {"type": "object", "properties": {...}, "required": [...],
#  "additionalProperties": False}
```

`schema.to_json_schema(...)` is equivalent. The default target is
`draft-2020-12` and the default `io` mode is `output`.

## Choose a target and an I/O mode

```python
to_json_schema(schema, target="openapi-3.0", io="input")
```

| Option | Values | Effect |
| --- | --- | --- |
| `target` | `"draft-2020-12"` (default), `"openapi-3.0"` | Output dialect |
| `io` | `"output"` (default), `"input"` | Whether the schema describes parsed output or accepted input |
| `unrepresentable` | `"throw"` (default), `"any"` | Fail on unsupported nodes or replace them with `{}` |

`io="output"` reflects defaults and the stripping object policy:
`additionalProperties: false` is emitted and fields that always have a value are
required. `io="input"` omits `additionalProperties` and does not require fields
that may be absent from input.

## Attach metadata

`title`, `description`, `examples`, and other metadata keys are merged into the
generated node and override generated keys:

```python
z.string().min(1).meta({"title": "Name", "examples": ["Alex"]})
```

For OpenAPI 3.0, `examples` is emitted as the singular `example`. Metadata values
are copied as-is; use JSON-compatible values when serializing the result.

## Handle unrepresentable nodes

Export raises `UnrepresentableError` when a node has no JSON Schema equivalent.
Pass `unrepresentable="any"` to emit `{}` for those nodes instead:

```python
from zodish import UnrepresentableError, to_json_schema

try:
    to_json_schema(z.string().trim())
except UnrepresentableError as error:
    print(error)   # <root>: trim schemas are not representable

to_json_schema(z.string().trim(), unrepresentable="any")
# {}
```

Operations with no equivalent include `catch`, `refine`/`super_refine`,
`transform`, `overwrite`, `preprocess`, `pipe`, `lazy`, `default_factory`,
`instance_of`, `trim`/`lower`/`upper`, and regexes with non-default flags.

## Avoid target-specific dead ends

For OpenAPI 3.0:

- tuples, keyed records, and null-only schemas are not representable;
- use `.nullable()` for nullable fields;
- standalone null literals and enum values containing `None` are rejected.

For draft JSON Schema:

- tuples with an optional rest are supported;
- keyed records require key schemas that produce strings; integer, transformed,
  or other non-string keys are rejected.

## Know the semantic limits

- Python object schemas such as `z.date()`, `z.datetime()`, `z.time()`,
  `z.timedelta()`, `z.decimal()`, `z.bytes()`, `z.path()`, and
  `z.instance_of()` are not representable.
- `z.json()` exports as `{}`; native enums export as their JSON-compatible member
  values.
- JSON Schema cannot preserve Python's exact numeric-type distinctions, including
  `True`/`1`/`1.0` literals and int versus integral float.
- Runtime float `multiple_of` checks use a small quotient tolerance; JSON Schema
  `multipleOf` uses exact numeric semantics.
- String formats are annotations; validators enforce them only when format
  assertion is enabled.
- Python regex patterns are emitted as `pattern`, so Python-specific syntax may
  not be portable.
- Codecs export the input schema for `io="input"` and the output schema for
  `io="output"`. A decoded side containing Python-only values, such as
  `datetime`, is not representable.

See the [JSON Schema reference](../reference/json-schema.md) for the complete
support matrix.

## Related

- [JSON Schema reference](../reference/json-schema.md)
- [API reference](../reference/api.md)
- [Type semantics](../explanation/type-semantics.md)
