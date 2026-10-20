"""Run with: python examples/quickstart.py (after installing the package)."""

import json
from dataclasses import dataclass

from zodish import z


user_schema = z.object({
    "name": z.string().trim().min(1),
    "email": z.string().trim().lower().email(),
    "age": z.integer().min(18),
    "role": z.enum(["user", "admin"]).default("user"),
    "nickname": z.string().optional(),
}).strict()

user = user_schema.parse({
    "name": " Alex ", "email": " ALEX@example.com ", "age": 30,
})
print(user)

result = user_schema.safe_parse({"name": "", "email": "invalid", "age": 12})
if not result.success:
    print(json.dumps(result.error.to_dict(), indent=2))


@dataclass(frozen=True)
class User:
    name: str
    age: int


typed_user_schema = z.object({
    "name": z.string(), "age": z.integer().nonnegative(),
}).transform(lambda data: User(**data))

typed_user: User = typed_user_schema.parse({"name": "Alex", "age": 30})
print(typed_user)
