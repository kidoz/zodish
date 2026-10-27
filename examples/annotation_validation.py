from typing import Any, cast

from zodish import Failure, ValidationError, z


@z.dataclass
class User:
    name: str
    age: int


users = z.adapter(list[User])
parsed = users.parse([{"name": "Alex", "age": 30}])
assert parsed == [User("Alex", 30)]
assert isinstance(users.safe_parse([{"name": "Alex", "age": "old"}]), Failure)


@z.validate_call
def total(value: int, *values: int, scale: int = 1) -> int:
    return (value + sum(values)) * scale


assert total(1, 2, 3, scale=2) == 12
try:
    total(cast(Any, "bad"))
except ValidationError as error:
    assert error.issues[0].path == ("value",)
else:
    raise AssertionError("validate_call accepted an invalid argument")

print("Annotation adapters and validate_call passed")
