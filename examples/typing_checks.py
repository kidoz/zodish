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
from typing import Any, assert_type

from zodish import Failure, Issue, Missing, Schema, Success, to_json_schema, z


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
