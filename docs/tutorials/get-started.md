# Tutorial: Get started with Zodish

In this tutorial you will build a schema for user registration, accept valid
input, reject invalid input, inspect the issues, turn the parsed data into a
typed object, and export the schema as JSON Schema. It takes about ten minutes
and assumes CPython 3.14+.

## 1. Install Zodish

```bash
pip install zodish
```

If you need to build from source instead, follow
[Set up the development environment](../how-to/contribute.md#set-up-the-environment).

## 2. Define a schema

Create a file named `registration.py`:

```python
from zodish import z

registration_schema = z.object({
    "name": z.string().trim().min(1),
    "email": z.string().trim().lower().email(),
    "age": z.integer().min(18),
    "role": z.enum(["user", "admin"]).default("user"),
    "nickname": z.string().optional(),
}).strict()
```

Each method returns a new schema; schemas are immutable. Operations run in the
order you write them, so `.trim().min(1)` measures the trimmed value.

`.strict()` rejects keys that are not in the shape. Without it, unknown keys are
stripped from the output.

## 3. Parse valid input

Add:

```python
user = registration_schema.parse({
    "name": " Alex ",
    "email": "ALEX@example.com",
    "age": 30,
})

print(user)
```

Run the file:

```bash
python registration.py
```

```text
{'name': 'Alex', 'email': 'alex@example.com', 'age': 30, 'role': 'user'}
```

`parse` returns validated output or raises `ValidationError`. Notice that
`role` received its default and the missing `nickname` was omitted from the
output rather than set to `None`.

## 4. Reject invalid input without exceptions

Use `safe_parse` when invalid input is expected:

```python
result = registration_schema.safe_parse({
    "name": "",
    "email": "bad",
    "age": 12,
})

print(result.success)
```

```text
False
```

When `success` is `False`, `result.error` is a `ValidationError`. When it is
`True`, `result.data` contains the parsed value. The two outcomes are a
`Success[T] | Failure` discriminated union, so check `success` before reading
either attribute.

## 5. Inspect the issues

Validation collects every sibling failure, not just the first one:

```python
for issue in result.error.issues:
    print(issue.code, issue.path, issue.message)
```

```text
too_small ('name',) Must contain at least 1 characters
invalid_format ('email',) Invalid email address
too_small ('age',) Must be at least 18
```

Each issue has a `code`, a `message`, a tuple `path`, and structured `params`.
Paths contain object keys and collection indices, so nested failures tell you
exactly where the input went wrong.

For a JSON API response, serialize the whole error:

```python
import json

print(json.dumps(result.error.to_dict(), indent=2))
```

`to_dict()` merges each issue's params into a JSON-serializable dictionary.
See [Handle errors](../how-to/handle-errors.md) for `flatten`, `treeify`, and
`prettify`.

## 6. Transform the result into a typed object

Python type checkers cannot infer object shapes from a dictionary of schemas the
way TypeScript infers types from Zod. Transform the parsed dictionary into a
dataclass when you want a static type:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class User:
    name: str
    email: str
    age: int
    role: str
    nickname: str | None = None

user_schema = registration_schema.transform(lambda data: User(**data))
user = user_schema.parse({
    "name": " Alex ",
    "email": "ALEX@example.com",
    "age": 30,
})
print(user)
```

```text
User(name='Alex', email='alex@example.com', age=30, role='user', nickname=None)
```

A transform runs after validation succeeds. If it raises an exception, the
exception propagates; `safe_parse` catches only `ValidationError`.

## 7. Export JSON Schema

JSON Schema export describes the wire format, so it works best on a schema made
of representable operations:

```python
from zodish import to_json_schema

wire_schema = z.object({
    "name": z.string().min(1).meta({"title": "Name"}),
    "email": z.string().email(),
    "age": z.integer().min(18),
    "role": z.enum(["user", "admin"]).default("user"),
    "nickname": z.string().optional(),
}).strict()

print(to_json_schema(wire_schema, target="openapi-3.0", io="input"))
```

`to_json_schema` supports `draft-2020-12` (default) and `openapi-3.0`.
Operations such as `trim`, `transform`, and `refine` have no JSON Schema
equivalent; export a schema that describes the values you exchange over the
wire, or pass `unrepresentable="any"` to replace unrepresentable nodes with
`{}`. See [Export JSON Schema](../how-to/export-json-schema.md).

## Where to go next

- [Examples](../../examples/README.md) — runnable programs for a JSON API,
  settings, webhooks, validated models, and async checks.
- [Validate structured data](../how-to/validate-data.md) — objects, collections,
  unions, and intersections.
- [Handle missing values](../how-to/handle-missing-values.md) — `optional`,
  `nullable`, defaults, and fallbacks.
- [API reference](../reference/api.md) — every constructor and method at a
  glance.
- [Architecture](../explanation/architecture.md) — why validation runs in Rust
  and what stays in Python.
