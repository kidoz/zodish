"""Validate annotation-driven lists, function arguments, and return values.

Run with: python examples/annotation_validation.py
"""

from typing import Annotated, Any, cast

from zodish import Failure, ValidationError, z


@z.dataclass
class User:
    name: str
    age: int


@z.validate_call
def total(value: int, *values: int, scale: int = 1) -> int:
    return (value + sum(values)) * scale


@z.validate_call
def remaining(capacity: int, used: int) -> Annotated[int, z.integer().nonnegative()]:
    return capacity - used


def main() -> None:
    users = z.adapter(list[User])
    parsed = users.parse([{"name": "Alex", "age": 30}])
    assert parsed == [User("Alex", 30)]
    assert isinstance(users.safe_parse([{"name": "Alex", "age": "old"}]), Failure)

    assert total(1, 2, 3, scale=2) == 12
    try:
        total(cast(Any, "bad"))
    except ValidationError as error:
        assert error.issues[0].path == ("value",)
    else:
        raise AssertionError("validate_call accepted an invalid argument")

    assert remaining(10, 3) == 7
    try:
        remaining(10, 12)
    except ValidationError as error:
        assert [(issue.path, issue.code) for issue in error.issues] == [((), "too_small")]
    else:
        raise AssertionError("validate_call accepted an invalid return value")

    print("Annotation adapters and validate_call passed")


if __name__ == "__main__":
    main()
