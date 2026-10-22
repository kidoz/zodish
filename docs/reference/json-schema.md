# JSON Schema reference

This page lists the JSON Schema export options and the exact subset of Zodish
supported by each target.

## API

```python
from zodish import to_json_schema

to_json_schema(schema, *, target="draft-2020-12", io="output", unrepresentable="throw")
schema.to_json_schema(...)
```

| Option | Values | Default | Meaning |
| --- | --- | --- | --- |
| `target` | `"draft-2020-12"`, `"openapi-3.0"` | `"draft-2020-12"` | Output dialect |
| `io` | `"output"`, `"input"` | `"output"` | Describe parsed output or accepted input |
| `unrepresentable` | `"throw"`, `"any"` | `"throw"` | Raise `UnrepresentableError` or emit `{}` for unsupported nodes |

`UnrepresentableError` includes the JSON path and a reason, for example
`<root>: trim schemas are not representable`.

## Supported schemas

| Zodish construct | JSON Schema output |
| --- | --- |
| `z.string()` with length, regex, format, starts/ends/includes | `type: string` with the matching keywords |
| `z.number()`, `z.integer()` with bounds and `multiple_of` | `type: number` or `type: integer` |
| `z.boolean()` | `type: boolean` |
| `z.none()` | `type: null` (draft only) |
| `z.unknown()`, `z.json()` | `{}` |
| `z.native_enum(EnumClass)` | Enum of JSON-compatible member values |
| `z.literal(value)` | `const` |
| `z.enum(values)` | `enum` |
| `z.array(item)` | `type: array` with `items` and length keywords |
| `z.set(item)` | `type: array`, `items`, `uniqueItems: true`, and length keywords |
| `z.tuple(*items)` | Draft: `prefixItems`; OpenAPI: not representable |
| `z.tuple(*items).rest(schema)` | Draft only: `prefixItems` plus `items` |
| `z.record(value_schema)` | `type: object` with `additionalProperties` |
| `z.record(key_schema, value_schema)` | Draft only, and only when keys produce strings |
| `z.map(key_schema, value_schema)` | Same representation and limits as a keyed record |
| `z.object(shape)` | `type: object` with `properties` and `required` |
| Object `.catchall(schema)` | Object with `additionalProperties` schema |
| `z.union(...)` | `anyOf` |
| `z.discriminated_union(...)` | `oneOf` |
| `z.intersection(...)` / `.and_()` | `allOf` when outputs are dictionaries |
| `.optional()`, `.nullable()`, `.default()` | Wrappers and object `required` lists |
| Coercion | Output type only |
| `z.string().stringbool()` | `boolean` in output mode; string enum in input mode |
| `z.codec(input, output, ...)` | `input` for `io="input"`; `output` for `io="output"` |
| `z.codecs.dataclass(Cls, fields=...)` | Object of encoded fields in input mode; object of decoded fields in output mode. Dataclass-default fields are optional in input and required in output |

## Required fields and object policies

- Output mode uses `additionalProperties: false` for stripping objects.
- Input mode omits `additionalProperties`, matching which input keys are
  accepted.
- Object fields with `.wire(name)` use wire names for input properties and
  `required` entries; output mode uses shape keys.
- `optional` is represented through object `required` lists; it has no standalone
  JSON Schema equivalent.
- Defaults are emitted as `default`; output mode requires a field whose default
  guarantees a value.

## Metadata

Metadata keys are merged into the generated node and override generated keys.
`title`, `description`, and `examples` are the common documentation keys. For
OpenAPI 3.0, `examples` is emitted as `example`. Metadata values are copied
as-is; use JSON-compatible values when serializing.

## Constraints with several parts

- Multiple string predicate patterns and multiple `multiple_of` constraints are
  emitted through `allOf`.
- Multiple raw regex constraints on one string remain unrepresentable.
- String formats are JSON Schema annotations; validators enforce them only when
  format assertion is enabled.
- `.duration()`, `.http_url()`, and `.hex()` export as `pattern`.
  `.cidrv4()` and `.cidrv6()` export as non-standard `format` annotations, which
  validators ignore unless they support those names.
- Python regex patterns are emitted as `pattern`, so Python-specific syntax may
  not be portable.

## Recursive schemas

Give a lazy schema an `id` in metadata to export it as a reference:

```python
node = z.lazy(lambda: z.object({
    "name": z.string(),
    "children": node.array().default([]),
}).meta({"id": "Node"}))

to_json_schema(node)
# {"$ref": "#/$defs/Node", "$defs": {"Node": {...}}}
```

- The id may be on the lazy schema or its resolved root.
- Draft 2020-12 places definitions under `$defs`; OpenAPI 3.0 places them under
  `components/schemas` and references `#/components/schemas/<id>`. The returned
  OpenAPI mapping includes a root `components` object; merge it into the
  OpenAPI document.
- Every exported lazy must carry an id. A lazy without one raises
  `UnrepresentableError`; `unrepresentable="any"` emits `{}`.
- Duplicate ids for distinct schemas raise `UnrepresentableError`.
- The exporter resolves each lazy factory once per export, so factories must be
  deterministic. Factory failures raise `UnrepresentableError`.
- `id` is registry metadata and is not emitted as a JSON Schema keyword.
- For OpenAPI 3.0, metadata next to a `$ref` is emitted through `allOf` because
  Reference Object siblings are ignored.

## Not representable

These constructs raise `UnrepresentableError` unless `unrepresentable="any"`:

- `catch`, `refine`, `super_refine`, `transform`, `overwrite`, `preprocess`,
  `pipe`, `async_refine`, `async_super_refine`, `async_transform`,
  `async_preprocess`, `async_default_factory`, `async_catch`, and
  `async_overwrite`;
- `default_factory`;
- `instance_of`;
- `trim`, `lower`, and `upper`;
- regexes with non-default flags;
- Python object schemas: `z.date()`, `z.datetime()`, `z.time()`, `z.timedelta()`,
  `z.decimal()`, `z.bytes()`, and `z.path()`.

## Target-specific rules

### Draft 2020-12

- Tuples with a rest schema are supported.
- Keyed records require key schemas that produce strings. Integer, transformed,
  or otherwise non-string key schemas are rejected.
- One-argument records keep the default string-key behavior.
- `z.none()` exports as `type: null`.

### OpenAPI 3.0

- Tuples, keyed records, and null-only schemas are not representable.
- Use `.nullable()` for nullable fields. Standalone null literals and enum values
  containing `None` are rejected.
- `examples` metadata is emitted as `example`.

## Semantic limits

- JSON Schema cannot preserve Python's exact numeric-type distinctions, including
  `True`/`1`/`1.0` literals and int versus integral float.
- JSON Schema `multipleOf` uses exact numeric semantics. Runtime integer checks
  are exact, but float checks apply a small quotient tolerance, so results can
  differ for decimal values such as `0.3` with divisor `0.1`.
- Coercion exports as its output type; broader accepted runtime inputs are not
  represented.
- `stringbool` input mode enumerates the configured strings but not all case
  variants.
- Codecs export the side selected by `io`. The decoded side may contain
  Python-only values, such as `datetime`, and is then unrepresentable; export
  input mode or use a JSON-compatible output schema.
- `unknown` and `z.json()` become `{}`, which can describe only JSON values.
- Intersections export as `allOf`. Strip-object components omit
  `additionalProperties` inside the intersection so combined inputs validate.
  Intersections whose statically known outputs are not dictionaries are
  unrepresentable.
- Discriminated unions export as `oneOf`; include each discriminator literal in
  its branch for precise matching.

## Related

- [Export JSON Schema](../how-to/export-json-schema.md)
- [API reference](api.md)
- [Type semantics](../explanation/type-semantics.md)
