"""Choose an object form that a type checker can see.

Run with: python examples/typed_objects.py

mypy 2.4 types a dict of different schema classes as ``dict[str, object]``.
The key names and each schema's output type are not part of that type, so
``z.object(...).parse`` is ``dict[str, Any]``. Declare the result type next
to the schema:

- ``z.typed_object(TypedDictClass, shape)`` returns that TypedDict from
  ``parse`` and ``encode``. ``strict`` keeps it. ``partial`` returns a plain
  object. The checker trusts the TypedDict and does not compare it with the
  field schemas. A ``dict[str, Any]`` value is not a valid argument. A
  matching dict literal is. ``.wire()`` makes the runtime keys differ from
  the keys the checker expects.
- ``.transform(fn)`` is typed as the function's return type.
- ``@z.dataclass`` with ``Annotated[T, schema]`` types the constructor and
  attributes. An annotation that cannot match its schema is rejected when
  the class is created. ``ValidatedModel.parse`` accepts ``dict[str, Any]``,
  so wire names stay a runtime concern.
- ``z.create_model(name, object_schema)`` builds a validated class from a
  plain ``z.object``. The checker sees ``type[Any]``.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, TypedDict, assert_type, cast

from zodish import Failure, ValidationError, z


class User(TypedDict):
    name: str
    age: int
    nickname: NotRequired[str]


class WiredUser(TypedDict):
    user_name: str


@dataclass(frozen=True)
class Profile:
    name: str
    age: int


def profile_from_object(data: dict[str, Any]) -> Profile:
    return Profile(name=data["name"], age=data["age"])


@z.dataclass(frozen=True)
class Account(z.ValidatedModel):
    name: Annotated[str, z.string().min(1)]
    age: Annotated[int, z.integer().nonnegative()]


def main() -> None:
    plain = z.object({
        "name": z.string().min(1),
        "age": z.integer().nonnegative(),
    })
    plain_user = plain.parse({"name": "Ada", "age": 30})
    assert_type(plain_user, dict[str, Any])
    assert_type(plain_user["name"], Any)
    assert plain_user == {"name": "Ada", "age": 30}

    user_schema = z.typed_object(User, {
        "name": z.string().min(1),
        "age": z.integer().nonnegative(),
        "nickname": z.string().optional(),
    })
    user = user_schema.parse({"name": "Ada", "age": 30})
    assert_type(user, User)
    assert_type(user["name"], str)
    assert_type(user["age"], int)
    assert_type(user.get("nickname"), str | None)
    assert user == {"name": "Ada", "age": 30}
    assert_type(user_schema.strict(), z.TypedObjectSchema[User])
    assert_type(user_schema.partial().parse({}), dict[str, Any])
    from_json = user_schema.parse_json('{"name": "Ada", "age": 30}')
    assert_type(from_json, User)
    assert user_schema.encode(user) == user

    if TYPE_CHECKING:
        raw: dict[str, Any] = {"name": "Ada", "age": 30}
        user_schema.parse(raw)  # type: ignore[arg-type]

    wired = z.typed_object(WiredUser, {
        "user_name": z.string().wire("userName"),
    })
    # The checker accepts the shape key here. Runtime requires the wire name.
    assert isinstance(wired.safe_parse({"user_name": "Ada"}), Failure)
    wired_user = wired.parse(cast(WiredUser, {"userName": "Ada"}))
    assert wired_user == {"user_name": "Ada"}
    assert_type(wired.encode(wired_user), WiredUser)
    assert wired.encode(wired_user) == {"userName": "Ada"}

    profile_schema = plain.transform(profile_from_object)
    profile = profile_schema.parse({"name": "Ada", "age": 30})
    assert_type(profile, Profile)
    assert profile == Profile("Ada", 30)

    account = Account(name="Ada", age=30)
    assert_type(account, Account)
    assert_type(account.age, int)
    assert_type(Account.parse({"name": "Ada", "age": 30}), Account)
    try:
        Account(name="Ada", age=-1)
    except ValidationError as error:
        assert error.issues[0].path == ("age",)
    else:
        raise AssertionError("negative age should be rejected")

    namespace: dict[str, object] = {}
    try:
        exec(
            "class Bad:\n"
            "    age: Annotated[int, z.string()]\n"
            "z.dataclass(Bad)\n",
            {"Annotated": Annotated, "z": z},
            namespace,
        )
    except TypeError:
        pass
    else:
        raise AssertionError("Annotated[int, z.string()] should be rejected")

    runtime_fields = z.object({
        "name": z.string().min(1),
        "age": z.integer().nonnegative(),
    })
    runtime_account = z.create_model("RuntimeAccount", runtime_fields)
    assert_type(runtime_account, type[Any])
    created = runtime_account(name="Ada", age=30)
    assert_type(created, Any)
    assert created.name == "Ada"
    assert created.age == 30
    try:
        z.create_model("StrictAccount", runtime_fields.strict())
    except TypeError:
        pass
    else:
        raise AssertionError("create_model should reject a strict object")

    print(plain_user)
    print(user)
    print(profile)
    print(created)


if __name__ == "__main__":
    main()
