# How to validate structured data

Use this guide when you already have a schema and need to validate dictionaries,
lists, tuples, records, or alternative shapes. For a first schema, start with the
[get-started tutorial](../tutorials/get-started.md).

## Validate an object

```python
from zodish import z

user = z.object({"name": z.string(), "age": z.integer()}).strict()
user.parse({"name": "Alex", "age": 30})
# {"name": "Alex", "age": 30}
user.safe_parse({"name": "Alex", "age": 30, "admin": True}).success
# False
```

Objects accept dictionaries with string keys. The default policy strips extra
keys; `.strict()` rejects them and `.passthrough()` preserves them.

## Validate JSON text

`parse_json`, `safe_parse_json`, and `validate_json` accept `str`, `bytes`, or
`bytearray` input:

```python
user.parse_json(b'{"name": "Alex", "age": 30}')
user.safe_parse_json('{"name": "Alex", "age": 30}').success
user.validate_json(bytearray(b'{"name": "Alex", "age": 30}'))
```

A native fast path handles JSON-compatible objects and arrays with primitive
leaves, numeric bounds and multiples, literals and enums, nullable/optional
wrappers, optional object fields, and string/array length checks. Other schemas
fall back to standard JSON decoding and normal validation. Invalid JSON raises a
`ValidationError` with an `invalid_format` issue and a `position`; schema
failures use the usual issue codes and paths.

Async schemas use `parse_json_async` or `safe_parse_json_async`:

```python
await user.parse_json_async(b'{"name": "Alex", "age": 30}')
await user.safe_parse_json_async('{"name": "Alex", "age": 30}')
```


## Handle unknown object keys explicitly

Choose one policy and make it visible in the schema:

| Method | Behavior |
| --- | --- |
| default | Remove keys not in the shape |
| `.strict()` | Report `unrecognized_key` for unknown keys |
| `.passthrough()` | Keep unknown keys in the output |
| `.catchall(schema)` | Validate unknown keys with `schema` and keep them |

`catchall` is cleared by `.strict()`, `.strip()`, and `.passthrough()`.

```python
z.object({"id": z.integer()}).catchall(z.string())
```

## Edit an object shape

```python
base = z.object({"name": z.string(), "age": z.integer()})

base.extend({"email": z.string().email()})
base.pick("name")
base.omit("age")
base.partial()          # every field becomes optional
base.partial("age")     # only "age" becomes optional
base.required()         # every field rejects missing input
base.keyof()            # z.enum(["name", "age"])
```

Structural methods operate on the shape and return replacement schemas; they do
not mutate the original.

## Validate arrays

```python
z.array(z.integer())
z.array(z.integer()).min(1)
z.array(z.integer()).max(10)
z.array(z.integer()).length(3)
z.array(z.integer()).nonempty()
z.integer().array()     # shorthand for z.array(z.integer())
```

`.length(n)` on strings and arrays requires exactly `n` characters or items.

## Validate tuples and records

```python
z.tuple(z.string(), z.integer())           # fixed positions, returns a tuple
z.tuple(z.string()).rest(z.integer())      # trailing rest schema
z.record(z.string())                       # string keys, string values
z.record(z.integer(), z.string())          # key schema, value schema
```

Tuple schemas accept lists or tuples. Keyed records validate keys before values
and use the parsed keys in the output.

## Choose between a union, a discriminated union, and an intersection

Use a **union** when any alternative may match:

```python
z.union(z.string(), z.integer())
z.string().or_(z.integer())
```

Branches are tried in order; the first success wins. If none match, the failure
contains every branch's issues in `branches`.

Use a **discriminated union** when a dictionary key identifies the branch. This
avoids trying every alternative:

```python
event = z.discriminated_union("kind", {
    "created": z.object({"kind": z.literal("created"), "id": z.integer()}),
    "deleted": z.object({
        "kind": z.literal("deleted"),
        "id": z.integer(),
        "reason": z.string(),
    }),
})

event.parse({"kind": "created", "id": 1})
```

A missing discriminator reports `required` at `("kind",)`; an unknown value
reports one `invalid_value` issue. The selected branch validates the whole
dictionary, so include the discriminator in each branch to preserve it.

Use an **intersection** when the same input must satisfy several schemas:

```python
both = z.intersection(
    z.object({"name": z.string()}),
    z.object({"age": z.integer()}),
)
# or: z.object({"name": z.string()}).and_(z.object({"age": z.integer()}))

both.parse({"name": "Alex", "age": 30})
# {"name": "Alex", "age": 30}
```

Both sides must produce dictionaries, or validation reports
`invalid_intersection_types`. Nested dictionaries merge recursively; conflicting
non-dictionary values take the right side. Missing input is rejected, so wrap the
intersection in `.optional()` when absence is allowed.

## Validate literal values and enums

```python
z.literal("admin")
z.enum(["user", "admin"])
```

Literals compare both type and value, so `True`, `1`, and `1.0` are distinct.
`z.enum()` reports a single `invalid_value` issue rather than a union branch.

## What not to expect from object validation

- No automatic coercion: `"30"` is not an integer. Use
  [explicit coercion](../explanation/type-semantics.md#explicit-coercion) when
  string inputs are expected.
- Fields are required by default, including `z.unknown()`. See
  [Handle missing values](handle-missing-values.md).
- Constraints attached to a field run before object-level refinements. See
  [Transform and refine values](transform-and-refine.md).

## Related

- [Handle errors](handle-errors.md)
- [Handle missing values](handle-missing-values.md)
- [API reference](../reference/api.md)
