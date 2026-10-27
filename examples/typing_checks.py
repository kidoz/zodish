"""Static type assertions checked by mypy in CI."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime as DateTime
from datetime import time as Time
from datetime import timedelta as TimeDelta
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    NewType,
    NotRequired,
    TypedDict,
    assert_type,
)

from zodish import Failure, Issue, Missing, Schema, Success, to_json_schema, z


class TypedUser(TypedDict):
    name: str
    age: int


class RequiredKeyUser(TypedDict):
    age: int
    nickname: NotRequired[str]


required_key_user = z.typed_object(RequiredKeyUser, {
    "age": z.integer().optional(),
    "nickname": z.string().optional(),
})
assert_type(required_key_user.parse({"age": 1}), RequiredKeyUser)
assert_type(required_key_user.strict(), z.TypedObjectSchema[RequiredKeyUser])
assert_type(required_key_user.safe_parse({"age": 1}), Success[RequiredKeyUser] | Failure)


assert_type(z.adapter(list[int]).parse([1]), list[int])


@z.validate_call
def typed_add(left: int, right: int = 0) -> int:
    return left + right


assert_type(typed_add(1), int)
assert_type(z.string().parse("x"), str)
assert_type(to_json_schema(z.string()), dict[str, Any])
assert_type(z.string().to_json_schema(), dict[str, Any])
assert_type(
    to_json_schema(z.string(), target="openapi-3.0", io="input"),
    dict[str, Any],
)
assert_type(z.integer().positive().parse(1), int)
assert_type(z.integer().gt(1).lt(10).parse(2), int)
assert_type(z.integer().gte(1).lte(10).parse(2), int)
assert_type(z.number().multiple_of(0.25).parse(0.5), int | float)
assert_type(
    z.string().starts_with("a").ends_with("z").includes("m").parse("amz"),
    str,
)
assert_type(z.string().ipv6().parse("::1"), str)
assert_type(z.number().parse(1.5), int | float)
assert_type(z.boolean().parse(True), bool)
assert_type(z.string().array().parse(["x"]), list[str])
assert_type(z.set(z.integer()).parse({1}), set[int])
assert_type(z.map(z.string(), z.integer()).parse({"x": 1}), dict[str, int])
typed_user = z.typed_object(TypedUser, {
    "name": z.string(),
    "age": z.integer(),
})
assert_type(typed_user, z.TypedObjectSchema[TypedUser])
assert_type(typed_user.parse({"name": "Alex", "age": 30}), TypedUser)
assert_type(
    typed_user.safe_parse({"name": "Alex", "age": 30}),
    Success[TypedUser] | Failure,
)
assert_type(typed_user.strict(), z.TypedObjectSchema[TypedUser])
wired_name = z.string().wire("userName")
assert_type(wired_name, Schema[str, str])
assert_type(z.string().wire_names(("displayName", "name")), Schema[str, str])
assert_type(
    z.string().refine_with_context(lambda value, context: value == context).parse(
        "x", context="x",
    ),
    str,
)
assert_type(wired_name.parse("Alex"), str)
UserId = NewType("UserId", int)
branded_id = z.integer().positive().brand(UserId)
assert_type(branded_id, Schema[UserId, int])
assert_type(branded_id.parse(1), UserId)
assert_type(z.set(z.integer()), z.SetSchema[int, int])
assert_type(z.map(z.string(), z.integer()), z.MapSchema[str, int, int])
assert_type(z.record(z.integer()).parse({"x": 1}), dict[str, int])
assert_type(z.string().nullable().parse(None), str | None)
assert_type(z.string().optional().parse(), str | Missing)
assert_type(z.string().transform(len).parse("x"), int)
assert_type(z.string().catch(None).parse("x"), str | None)
assert_type(z.string().nonoptional().parse("x"), str)
assert_type(z.string().overwrite(str.upper).parse("x"), str)
assert_type(z.string().validate("x"), bool)
assert_type(z.integer().parse_json(b"1"), int)
assert_type(z.integer().parse_json("1"), int)
assert_type(z.integer().parse_json(bytearray(b"1")), int)
assert_type(z.integer().safe_parse_json(b"1"), Success[int] | Failure)
assert_type(z.integer().validate_json(b"1"), bool)
async def _async_typing_checks() -> None:
    async def positive(value: int) -> bool:
        return value > 0

    async def stringify(value: int) -> str:
        return str(value)

    async def no_issues(value: int) -> None:
        return None

    checked = z.integer().refine_async(positive)
    assert_type(await checked.parse_async(1), int)
    assert_type(await checked.safe_parse_async(1), Success[int] | Failure)
    sup = z.integer().super_refine_async(no_issues)
    assert_type(await sup.parse_async(1), int)
    transformed = z.integer().transform_async(stringify)
    assert_type(await transformed.parse_async(1), str)
    composed = z.object({
        "value": z.integer().refine_async(positive),
        "values": z.integer().transform_async(stringify).array(),
    })
    assert_type(await composed.parse_async({"value": 1, "values": [1]}), dict[str, Any])
    assert_type(await z.integer().parse_json_async(b"1"), int)
    assert_type(
        await z.integer().safe_parse_json_async(b"1"),
        Success[int] | Failure,
    )
    assert_type(await z.integer().validate_async(1), bool)

    async def stringify_input(value: Any) -> str:
        return str(value)

    async def bump(value: int) -> int:
        return value + 1

    async def default_int() -> int:
        return 1

    async def fallback_text() -> str:
        return "fallback"

    preprocessed = z.preprocess_async(stringify_input, z.string())
    assert_type(await preprocessed.parse_async(1), str)
    overwritten = z.integer().overwrite_async(bump)
    assert_type(await overwritten.parse_async(1), int)
    defaulted = z.integer().default_factory_async(default_int)
    assert_type(await defaulted.parse_async(), int)
    caught = z.integer().catch_async(fallback_text)
    assert_type(await caught.parse_async(1), int | str)

    async def async_decode(value: str) -> int:
        return int(value)

    async def async_encode(value: int) -> str:
        return str(value)

    async_codec = z.codec_async(
        z.string(),
        z.integer(),
        decode=async_decode,
        encode=async_encode,
    )
    assert_type(async_codec, z.AsyncCodecSchema[int, str])
    assert_type(await async_codec.decode_async("1"), int)
    assert_type(await async_codec.safe_decode_async("1"), Success[int] | Failure)
    assert_type(await async_codec.encode_async(1), str)
    assert_type(await async_codec.safe_encode_async(1), Success[str] | Failure)
    assert_type(
        async_codec.brand(UserId),
        z.AsyncCodecSchema[UserId, str],
    )
    assert_type(
        async_codec.optional(),
        z.AsyncCodecSchema[int | Missing, str | Missing],
    )
    assert_type(z.codecs.array(async_codec), z.CodecSchema[list[int], list[str]])


assert_type(z.string().array().unwrap(), Schema[str, str])
assert_type(z.string().optional().unwrap(), Schema[Any, Any])
assert_type(z.record(z.integer()).unwrap(), Schema[int, int])
assert_type(
    z.object({"a": z.integer()}).required().parse({"a": 1}),
    dict[str, Any],
)
assert_type(z.object({"a": z.integer()}).keyof().parse("a"), str)
assert_type(
    z.object({"a": z.integer(), "b": z.string()}).partial("a").parse({"b": "x"}),
    dict[str, Any],
)
assert_type(z.string().length(2).parse("ab"), str)
assert_type(z.array(z.integer()).length(1).parse([1]), list[int])
assert_type(
    z.object({"a": z.integer()}).catchall(z.string()).parse({"a": 1, "x": "y"}),
    dict[str, Any],
)
assert_type(
    z.record(z.string(), z.integer()).parse({"a": 1}),
    dict[str, int],
)
assert_type(
    z.record(z.integer(), z.string()).parse({1: "a"}),
    dict[int, str],
)
assert_type(z.tuple().parse([]), tuple[()])
assert_type(z.tuple(z.string()).parse(["a"]), tuple[str])
assert_type(
    z.tuple(z.string(), z.integer()).parse(["a", 1]),
    tuple[str, int],
)
assert_type(
    z.tuple(z.string(), z.integer(), z.boolean()).parse(["a", 1, True]),
    tuple[str, int, bool],
)
assert_type(
    z.tuple(
        z.string(), z.integer(), z.boolean(), z.none(),
    ).parse(["a", 1, True, None]),
    tuple[str, int, bool, None],
)
assert_type(
    z.tuple(
        z.string(), z.integer(), z.boolean(), z.none(), z.unknown(),
    ).parse(["a", 1, True, None, object()]),
    tuple[Any, ...],
)
assert_type(
    z.tuple(z.string()).rest(z.integer()).parse(["a", 1]),
    tuple[Any, ...],
)
assert_type(
    z.intersection(
        z.object({"a": z.integer()}), z.object({"b": z.string()}),
    ).parse({"a": 1, "b": "x"}),
    Any,
)
assert_type(
    z.object({"a": z.integer()}).and_(
        z.object({"b": z.string()}),
    ).parse({"a": 1, "b": "x"}),
    Any,
)
assert_type(z.string().url().parse("https://example.com"), str)
assert_type(z.date().parse(Date(2026, 10, 20)), Date)
assert_type(z.datetime().parse(DateTime(2026, 10, 20)), DateTime)

iso_datetime = z.codec(
    z.string().datetime(),
    z.datetime(),
    decode=lambda value: DateTime.fromisoformat(value),
    encode=lambda value: value.isoformat(),
)
assert_type(iso_datetime, z.CodecSchema[DateTime, str])
assert_type(iso_datetime.parse("2026-10-20T12:30:00"), DateTime)
assert_type(iso_datetime.decode("2026-10-20T12:30:00"), DateTime)
assert_type(iso_datetime.encode(DateTime(2026, 10, 20)), str)
assert_type(
    iso_datetime.safe_decode("2026-10-20T12:30:00"),
    Success[DateTime] | Failure,
)
assert_type(
    iso_datetime.safe_encode(DateTime(2026, 10, 20)),
    Success[str] | Failure,
)
assert_type(iso_datetime.input_schema, Schema[str, Any])
assert_type(iso_datetime.output_schema, Schema[DateTime, Any])
assert_type(
    z.object({"created": iso_datetime}).encode(
        {"created": DateTime(2026, 10, 20)},
    ),
    dict[str, Any],
)
assert_type(
    z.array(iso_datetime).encode([DateTime(2026, 10, 20)]),
    list[str],
)
assert_type(
    iso_datetime.optional().encode(DateTime(2026, 10, 20)),
    str | Missing,
)
assert_type(
    z.codecs.array(iso_datetime),
    z.CodecSchema[list[DateTime], list[str]],
)
assert_type(
    iso_datetime.record(),
    z.CodecSchema[dict[str, DateTime], dict[str, str]],
)
assert_type(
    iso_datetime.optional(),
    z.CodecSchema[DateTime | Missing, str | Missing],
)
assert_type(
    iso_datetime.nullable(),
    z.CodecSchema[DateTime | None, str | None],
)
assert_type(iso_datetime.nonoptional(), z.CodecSchema[DateTime, str])
assert_type(
    z.codecs.union(iso_datetime, z.string()),
    z.CodecSchema[Any, Any],
)
assert_type(
    z.codecs.tuple(iso_datetime, z.string()),
    z.CodecSchema[Any, Any],
)

assert_type(z.codecs.iso_date(), z.CodecSchema[Date, str])
assert_type(z.codecs.iso_time(), z.CodecSchema[Time, str])
assert_type(z.codecs.iso_datetime(), z.CodecSchema[DateTime, str])
assert_type(z.codecs.decimal(), z.CodecSchema[Decimal, str])
assert_type(z.codecs.json_text(), z.CodecSchema[Any, str])
assert_type(z.codecs.path(), z.CodecSchema[PurePath, str])
assert_type(z.codecs.bytes_base64(), z.CodecSchema[bytes, str])
assert_type(z.codecs.bytes_base64(urlsafe=True), z.CodecSchema[bytes, str])
assert_type(z.codecs.iso_date().parse("2026-10-20"), Date)
assert_type(z.codecs.iso_time().parse("12:30:00"), Time)
assert_type(z.codecs.iso_datetime().parse("2026-10-20T12:30:00"), DateTime)
assert_type(z.codecs.decimal().parse("1.5"), Decimal)
assert_type(z.codecs.json_text().parse("{}"), Any)
assert_type(z.codecs.path().parse("/tmp/x"), PurePath)
assert_type(z.codecs.bytes_base64().encode(b"x"), str)


@dataclass(frozen=True)
class CodecUser:
    name: str
    age: int


type NonNegativeAge = Annotated[int, z.integer().min(0)]
type EncodedDateTime = Annotated[DateTime, z.codecs.iso_datetime()]
type Items[T] = list[T]
type Pair[T, U = T] = tuple[T, U]
type AgeHistory[T = NonNegativeAge] = Items[T]


@dataclass(frozen=True)
class AnnotatedUser:
    age: NonNegativeAge
    created: EncodedDateTime
    history: AgeHistory


annotated_user_codec = z.codecs.dataclass(AnnotatedUser)
assert_type(annotated_user_codec, z.CodecSchema[AnnotatedUser, dict[str, Any]])
annotated_user = annotated_user_codec.parse({
    "age": 30, "created": "2026-10-21T12:00:00Z", "history": [20, 25],
})
assert_type(annotated_user, AnnotatedUser)
assert_type(annotated_user.age, int)
assert_type(annotated_user.created, DateTime)
assert_type(annotated_user.history, list[int])
assert_type(annotated_user_codec.encode(annotated_user), dict[str, Any])


@dataclass(frozen=True)
class GenericAliasBatch:
    ages: Items[NonNegativeAge]
    nested: Items[Items[NonNegativeAge]]
    dates: Items[EncodedDateTime]
    pair: Pair[NonNegativeAge]


generic_alias_codec = z.codecs.dataclass(GenericAliasBatch)
assert_type(generic_alias_codec, z.CodecSchema[GenericAliasBatch, dict[str, Any]])
generic_batch = generic_alias_codec.parse({
    "ages": [1], "nested": [[2]], "dates": ["2026-10-21T12:00:00Z"], "pair": [3, 4],
})
assert_type(generic_batch, GenericAliasBatch)
assert_type(generic_batch.ages, list[int])
assert_type(generic_batch.nested, list[list[int]])
assert_type(generic_batch.dates, list[DateTime])
assert_type(generic_batch.pair, tuple[int, int])
assert_type(generic_alias_codec.encode(generic_batch), dict[str, Any])

codec_user = z.codecs.dataclass(CodecUser)
assert_type(codec_user, z.CodecSchema[CodecUser, dict[str, Any]])
assert_type(
    z.codecs.dataclass(CodecUser, fields={"age": z.integer()}),
    z.CodecSchema[CodecUser, dict[str, Any]],
)
assert_type(codec_user.parse({"name": "x", "age": 1}), CodecUser)
assert_type(codec_user.encode(CodecUser("x", 1)), dict[str, Any])
assert_type(
    codec_user.safe_encode(CodecUser("x", 1)),
    Success[dict[str, Any]] | Failure,
)
assert_type(
    z.object({"user": codec_user}).parse({"user": {"name": "x", "age": 1}}),
    dict[str, Any],
)


@dataclass(frozen=True)
class CodecTeam:
    lead: CodecUser
    members: list[CodecUser]


team_codec = z.codecs.dataclass(CodecTeam)
assert_type(
    team_codec.parse({
        "lead": {"name": "x", "age": 1},
        "members": [{"name": "y", "age": 2}],
    }),
    CodecTeam,
)
assert_type(
    team_codec.encode(CodecTeam(CodecUser("x", 1), [CodecUser("y", 2)])),
    dict[str, Any],
)
assert_type(z.time().parse(Time(12, 30)), Time)
assert_type(z.timedelta().parse(TimeDelta(days=1)), TimeDelta)
assert_type(z.decimal().parse(Decimal("1.5")), Decimal)
assert_type(z.bytes().parse(b"x"), bytes)
assert_type(z.path().parse(PurePath("x")), PurePath)
assert_type(z.json().parse({"a": 1}), Any)


class Color(Enum):
    RED = "red"
    BLUE = "blue"


assert_type(z.native_enum(Color).parse(Color.RED), Color)
assert_type(z.coerce.number().parse(3), int | float)
assert_type(z.coerce.number().min(1).parse(2), int | float)
assert_type(z.coerce.string().parse("x"), str)
assert_type(z.coerce.boolean().parse(True), bool)
assert_type(z.string().stringbool().parse("true"), bool)
assert_type(z.string().or_(z.integer()).parse(1), str | int)
assert_type(z.union(z.string(), z.integer()).parse(1), str | int)
assert_type(z.preprocess(str, z.string()).parse(42), str)
assert_type(z.string().transform(len).array().parse(["a"]), list[int])
assert_type(z.record(z.string().transform(len)).parse({"a": "bb"}), dict[str, int])
assert_type(z.string().transform(len).pipe(z.integer().min(2)).parse("abc"), int)
assert_type(z.string().min(3, message="Too short").parse("abc"), str)
assert_type(
    z.string().refine(lambda value: True, path=("name",)).parse("x"),
    str,
)

event = z.discriminated_union("kind", {
    "created": z.object({"kind": z.literal("created"), "id": z.integer()}),
    "deleted": z.object({"kind": z.literal("deleted"), "id": z.integer()}),
})
assert_type(event.parse({"kind": "created", "id": 1}), Any)

checked = z.object({"a": z.integer()}).super_refine(
    lambda value: Issue("custom", "bad", ("a",), params={"minimum": 1})
    if value["a"] < 0
    else None,
)
assert_type(checked.parse({"a": 1}), dict[str, Any])

failure = z.string().min(3).safe_parse("x")
if not failure.success:
    assert_type(failure.error.flatten(), dict[str, Any])
    assert_type(failure.error.treeify(), dict[str, Any])
    assert_type(failure.error.prettify(), str)

plain: Schema[str] = z.string()
assert_type(plain.parse("x"), str)

named = z.string().meta({"title": "Name"}).min(1)
assert_type(named.parse("x"), str)
assert_type(named.metadata, Mapping[str, Any])
assert_type(z.string().describe("Name").parse("x"), str)
object_meta = z.object({"a": z.integer()}).meta({"title": "A"}).extend({"b": z.string()})
assert_type(object_meta.parse({"a": 1, "b": "x"}), dict[str, Any])

item = z.string().meta({"title": "Tag"})
array = z.array(item)
assert_type(array.item, Schema[str, str])
assert_type(z.record(item).value_schema, Schema[str, str])
assert_type(z.tuple(item).items[0], Schema[Any, Any])
assert_type(z.union(item, z.integer()).options[0], Schema[Any, Any])
assert_type(
    z.discriminated_union("kind", {"a": item}).options["a"],
    Schema[Any, Any],
)

transformed: Schema[int, str] = z.string().transform(len)
assert_type(transformed.parse("x"), int)
assert_type(transformed.default("x").parse(), int)
assert_type(transformed.default_factory(lambda: "x").parse(), int)

# default() and default_factory() accept input values, not transformed outputs.
transformed.default(0)  # type: ignore[arg-type]


def make_int() -> int:
    return 0


transformed.default_factory(make_int)  # type: ignore[arg-type]

result = z.string().safe_parse("x")
if result.success:
    assert_type(result.data, str)
else:
    assert_type(result.error.issues[0].message, str)


@dataclass
class User:
    name: str


schema = z.object({"name": z.string()}).transform(lambda data: User(**data))
assert_type(schema.parse({"name": "Alex"}), User)


@z.dataclass
class ValidatedUser:
    name: Annotated[str, z.string().min(1)]
    age: int
    nickname: str | None = None


validated_user = ValidatedUser(name="Alex", age=30)
assert_type(validated_user, ValidatedUser)
assert_type(validated_user.name, str)
assert_type(validated_user.age, int)
assert_type(validated_user.nickname, str | None)
if TYPE_CHECKING:
    ValidatedUser(name="Alex", age="30")  # type: ignore[arg-type]


@z.dataclass(frozen=True)
class FrozenValidated:
    name: str


frozen_validated = FrozenValidated(name="Alex")
assert_type(frozen_validated.name, str)
if TYPE_CHECKING:
    frozen_validated.name = "Other"  # type: ignore[misc]


@z.dataclass(validate_assignment=True)
class AssignmentValidated:
    name: str
    age: int = 0


assignment_validated = AssignmentValidated(name="Alex")
assert_type(assignment_validated.name, str)
assignment_validated.name = "Bob"
if TYPE_CHECKING:
    assignment_validated.name = 1  # type: ignore[assignment]


@z.dataclass(kw_only=True)
class KeywordValidated:
    name: str


KeywordValidated(name="Alex")
if TYPE_CHECKING:
    KeywordValidated("Alex")  # type: ignore[call-arg]


@dataclass
class ExistingValidated:
    name: str


WrappedValidated = z.dataclass(ExistingValidated)
assert_type(WrappedValidated, type[ExistingValidated])
assert_type(WrappedValidated(name="Alex"), ExistingValidated)
if TYPE_CHECKING:
    WrappedValidated(name=1)  # type: ignore[arg-type]


@dataclass
class ExistingAssignment:
    value: int


WrappedAssignment = z.dataclass(ExistingAssignment, validate_assignment=True)
assert_type(WrappedAssignment, type[ExistingAssignment])
assert_type(WrappedAssignment(value=1), ExistingAssignment)


@dataclass
class TypingLeftBase:
    left: int


@dataclass
class TypingRightBase:
    right: str


@z.dataclass
class TypingCombined(TypingLeftBase, TypingRightBase):
    extra: int = 0


typing_combined = TypingCombined(left=1, right="x")
assert_type(typing_combined.left, int)
assert_type(typing_combined.right, str)
assert_type(typing_combined.extra, int)
if TYPE_CHECKING:
    TypingCombined(left="x", right="x")  # type: ignore[arg-type]


RuntimeModel = z.create_model("RuntimeModel", {"name": z.string()})
assert_type(RuntimeModel, type[Any])


@z.dataclass
class GenericValidated[generic_type]:
    value: generic_type


@z.dataclass
class RecursiveValidated:
    value: int
    child: RecursiveValidated | None = None


recursive_validated = RecursiveValidated(
    value=1,
    child=RecursiveValidated(value=2),
)
assert_type(recursive_validated.child, RecursiveValidated | None)


@z.dataclass
class IntValidated(GenericValidated[int]):
    pass


generic_validated = GenericValidated[int](value=1)
assert_type(generic_validated.value, int)
int_validated = IntValidated(value=1)
assert_type(int_validated.value, int)
if TYPE_CHECKING:
    IntValidated(value="wrong")  # type: ignore[arg-type]


async def _positive_async(value: int) -> bool:
    return value > 0


@z.dataclass(async_fields=True)
class AsyncValidated(z.ValidatedModel):
    value: Annotated[int, z.integer().refine_async(_positive_async)]


if TYPE_CHECKING:
    async def _typed_async_construction() -> None:
        assert_type(
            await AsyncValidated.acreate(value=1),
            AsyncValidated,
        )


@z.dataclass
class TypedValidatedModel(z.ValidatedModel):
    name: str
    age: int = 0


if TYPE_CHECKING:
    assert_type(
        TypedValidatedModel.parse({"name": "x"}),
        TypedValidatedModel,
    )
    assert_type(
        TypedValidatedModel.safe_parse({"name": "x"}),
        Success[TypedValidatedModel] | Failure,
    )
    assert_type(
        TypedValidatedModel.parse_json('{"name": "x"}'),
        TypedValidatedModel,
    )
    assert_type(
        TypedValidatedModel.encode(TypedValidatedModel("x")),
        dict[str, Any],
    )
    assert_type(TypedValidatedModel.to_json_schema(), dict[str, Any])

    async def _typed_validated_model_async() -> None:
        assert_type(
            await TypedValidatedModel.parse_async({"name": "x"}),
            TypedValidatedModel,
        )
        assert_type(
            await TypedValidatedModel.safe_parse_async({"name": "x"}),
            Success[TypedValidatedModel] | Failure,
        )
        assert_type(
            await TypedValidatedModel.encode_async(
                TypedValidatedModel("x"),
            ),
            dict[str, Any],
        )
