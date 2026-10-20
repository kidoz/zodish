# Missing values and defaults

Zodish separates "not supplied" from "supplied as `None`". This page explains the
model behind `optional`, `nullable`, `default`, and `catch`, and why wrapper order
changes the result.

## Three input states

| State | Example | Sentinel |
| --- | --- | --- |
| Missing | `parse()` or `{}` for a field | `MISSING` |
| `None` | `parse(None)` or `{"field": None}` | Python `None` |
| A value | `parse("Alex")` | The value itself |

A schema without a wrapper rejects both missing and `None` input. Wrappers opt
into one or both states:

| Wrapper | Accepts missing | Accepts `None` | Result for missing |
| --- | --- | --- | --- |
| `.optional()` | Yes | No | `MISSING` |
| `.nullable()` | No | Yes | — |
| `.nullish()` | Yes | Yes | `MISSING` |

This separation matters because "absent" and "explicitly null" often have
different meanings in APIs: an omitted field means "leave unchanged", while
`null` means "clear the value".

## Wrappers form a stack

Each wrapper wraps the previous schema. Validation enters the outermost wrapper
first, and that wrapper may short-circuit before the inner schema runs. This is
why order matters:

```python
z.string().default("guest").optional().parse() is MISSING   # outer optional wins
z.string().optional().default("guest").parse()              # inner optional, then default
```

The first schema is an optional around a default. Missing input is absorbed by
the outer wrapper, so the default never runs. The second is a default around an
optional; the default sees missing input first and supplies `"guest"`.

The same short-circuiting explains why an object field that is both optional and
defaulted can behave differently depending on which wrapper is outermost.

## Defaults are validated, not assumed

A default is a fallback for missing input, but it is not a bypass. The default is
copied and then passed through the schema, so constraints and transforms apply:

```python
z.string().min(3).default("ab").safe_parse().success   # False
```

`default_factory` is called for missing input and produces a fresh value each
time. It also runs through the schema.

This differs from Zod 4, where defaults short-circuit validation. The Zodish
behavior makes a schema self-consistent: any value that reaches the output has
satisfied the schema, whether it came from input or from a default.

## Object fields and omission

Missing optional object fields are omitted from the output. They are not returned
as `MISSING`, and they are not set to `None`. This keeps the output shape close to
the input: an absent key stays absent.

`MISSING` itself is the sentinel returned when an optional schema is parsed
directly with no input. It is also how the evaluator distinguishes "absent" from
"present but null" internally.

## `catch` is a fallback, not a default

`.catch(value)` covers both validation failures and missing input. It is useful
when a degraded value is preferable to an error. `.default(value)` covers only
missing input; invalid input still fails.

```python
z.integer().default(0).parse("bad")   # raises ValidationError
z.integer().catch(0).parse("bad")     # 0
```

`catch` copies its fallback. Pass a zero-argument callable when each parse needs a
fresh value.

## Related

- [Handle missing values and defaults](../how-to/handle-missing-values.md)
- [Errors reference](../reference/errors.md)
- [Architecture](architecture.md)
