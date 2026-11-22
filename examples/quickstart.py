"""Run with: python examples/quickstart.py (after installing the package)."""

import json
from dataclasses import dataclass

from zodish import Failure, z


user_schema = z.object({
    "name": z.string().trim().min(1),
    "email": z.string().trim().lower().email(),
    "age": z.integer().min(18),
    "role": z.enum(["user", "admin"]).default("user"),
    "nickname": z.string().optional(),
}).strict()

user = user_schema.parse({
    "name": " Alex ", "email": "ALEX@example.com", "age": 30,
})
assert user == {"name": "Alex", "email": "alex@example.com", "age": 30, "role": "user"}
print(user)

result = user_schema.safe_parse({"name": "", "email": "bad", "age": 12})
assert isinstance(result, Failure)
assert [(issue.path, issue.code) for issue in result.error.issues] == [
    (("name",), "too_small"),
    (("email",), "invalid_format"),
    (("age",), "too_small"),
]
print(json.dumps(result.error.to_dict(), indent=2))


@dataclass(frozen=True)
class User:
    name: str
    age: int


typed_user_schema = z.object({
    "name": z.string(), "age": z.integer().nonnegative(),
}).transform(lambda data: User(**data))

typed_user: User = typed_user_schema.parse({"name": "Alex", "age": 30})
assert typed_user == User(name="Alex", age=30)
print(typed_user)
