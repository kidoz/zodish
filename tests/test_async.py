"""Async validation tests for native continuation composition."""

import asyncio
import gc
import threading
import unittest
import weakref
from dataclasses import dataclass
from typing import Annotated, NewType, NotRequired, TypedDict

from zodish import MISSING, Failure, Issue, Success, ValidationError, z
from zodish._native import AsyncSession


class AsyncValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_object_required_output(self):
        class Fields(TypedDict):
            first: int
            second: int
            defaulted: int
            extra: NotRequired[str]

        events = []

        async def convert(value):
            events.append(("convert", value))
            return MISSING if value is MISSING else value * 2

        async def default():
            events.append(("default",))
            return 3

        schema = z.typed_object(Fields, {
            "first": z.integer().optional().transform_async(convert),
            "second": z.integer().optional(),
            "defaulted": z.integer().default_factory_async(default),
            "extra": z.string().optional(),
        })
        failure = await schema.safe_parse_async({})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [(issue.code, issue.path) for issue in failure.error.issues],
            [("required", ("first",)), ("required", ("second",))],
        )
        self.assertEqual(events, [("convert", MISSING), ("default",)])
        events.clear()
        self.assertEqual(await schema.parse_async({"first": 1, "second": 2}), {
            "first": 2, "second": 2, "defaulted": 3,
        })
        self.assertEqual(events, [("convert", 1), ("default",)])
        events.clear()
        self.assertEqual(await schema.parse_async({"first": 1, "second": 2, "defaulted": 4}), {
            "first": 2, "second": 2, "defaulted": 4,
        })
        self.assertEqual(events, [("convert", 1)])
        for rebuilt in (
            schema.strict(), schema.strip(), schema.passthrough(), schema.catchall(z.integer()),
        ):
            with self.subTest(policy=rebuilt.unknown_keys):
                failure = await rebuilt.safe_parse_async({})
                self.assertEqual(
                    [issue.path for issue in failure.error.issues], [("first",), ("second",)],
                )
        failure = await schema.safe_parse_json_async('{}')
        self.assertEqual(
            [issue.path for issue in failure.error.issues], [("first",), ("second",)],
        )
        with self.assertRaisesRegex(TypeError, "parse_async"):
            schema.parse({"first": 1, "second": 2})

    async def test_dataclass_annotated_async_fields(self):
        events = []

        async def decode(value):
            events.append(("decode", value))
            return int(value)

        async def encode(value):
            events.append(("encode", value))
            return str(value)

        async def is_positive(value):
            return value > 0

        field_codec = z.codec_async(
            z.string(), z.integer().min(0), decode=decode, encode=encode,
        )
        type Count = Annotated[int, field_codec]
        type Items[T] = list[T]
        type Counts[T = Count] = Items[T]

        @dataclass(frozen=True)
        class Payload:
            counts: Counts
            positive: Annotated[int, z.integer().refine_async(is_positive)] = 1

        codec = z.codecs.dataclass(Payload)
        value = await codec.decode_async({"counts": ["2"]})
        self.assertEqual(value, Payload([2]))
        self.assertEqual(events, [("decode", "2")])
        self.assertEqual(
            await codec.encode_async(value), {"counts": ["2"], "positive": 1},
        )
        self.assertEqual(events, [("decode", "2"), ("encode", 2)])
        # Plain fields validate before the codec conversion callback runs.
        failure = await codec.safe_parse_async({"counts": [0], "positive": 0})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [issue.path for issue in failure.error.issues], [("positive",)],
        )
        self.assertEqual(events, [("decode", "2"), ("encode", 2)])
        failure = await codec.safe_parse_async({"counts": ["-1"]})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("counts", 0))
        failure = await codec.safe_encode_async(Payload([-1], 0))
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [issue.path for issue in failure.error.issues],
            [("counts", 0), ("positive",)],
        )
        with self.assertRaisesRegex(TypeError, "parse_async"):
            codec.parse({"counts": ["2"]})

        @dataclass(frozen=True)
        class Nested:
            counts: Items[Items[Count]]

        nested = z.codecs.dataclass(Nested)
        self.assertEqual(await nested.parse_async({"counts": [["3"]]}), Nested([[3]]))
        self.assertEqual(await nested.encode_async(Nested([[3]])), {"counts": [["3"]]})
        failure = await nested.safe_parse_async({"counts": [["-1"]]})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("counts", 0, 0))
        failure = await nested.safe_encode_async(Nested([[-1]]))
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("counts", 0, 0))

    async def test_sync_schema_through_async_parser(self):
        schema = z.object({"value": z.integer().positive()})
        self.assertEqual(await schema.parse_async({"value": 1}), {"value": 1})
        failure = await schema.safe_parse_async({"value": 0})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("value",))

    async def test_missing_root_containers_match_sync(self):
        async def async_always(value):
            return True

        def sync_always(value):
            return True

        sync_lazy = z.lazy(lambda: z.object({
            "value": z.integer().refine(sync_always),
        }))
        async_lazy = z.lazy(lambda: z.object({
            "value": z.integer().refine_async(async_always),
        }))
        pairs = [
            (
                "object",
                z.object({"value": z.integer().refine(sync_always)}),
                z.object({"value": z.integer().refine_async(async_always)}),
                MISSING,
            ),
            (
                "array",
                z.array(z.integer().refine(sync_always)),
                z.array(z.integer().refine_async(async_always)),
                MISSING,
            ),
            (
                "tuple",
                z.tuple(z.integer().refine(sync_always)),
                z.tuple(z.integer().refine_async(async_always)),
                MISSING,
            ),
            (
                "record",
                z.record(z.integer().refine(sync_always)),
                z.record(z.integer().refine_async(async_always)),
                MISSING,
            ),
            (
                "intersection",
                z.intersection(
                    z.object({"left": z.integer().refine(sync_always)}),
                    z.object({"right": z.integer().refine(sync_always)}),
                ),
                z.intersection(
                    z.object({"left": z.integer().refine_async(async_always)}),
                    z.object({"right": z.integer().refine_async(async_always)}),
                ),
                MISSING,
            ),
            (
                "discriminated union",
                z.discriminated_union("kind", {
                    "a": z.object({
                        "kind": z.literal("a"),
                        "value": z.integer().refine(sync_always),
                    }),
                }),
                z.discriminated_union("kind", {
                    "a": z.object({
                        "kind": z.literal("a"),
                        "value": z.integer().refine_async(async_always),
                    }),
                }),
                MISSING,
            ),
            (
                "union of containers",
                z.union(
                    z.object({"value": z.integer().refine(sync_always)}),
                    z.array(z.integer().refine(sync_always)),
                ),
                z.union(
                    z.object({"value": z.integer().refine_async(async_always)}),
                    z.array(z.integer().refine_async(async_always)),
                ),
                MISSING,
            ),
            ("lazy", sync_lazy, async_lazy, MISSING),
            (
                "missing nested field",
                z.object({"child": z.object({
                    "value": z.integer().refine(sync_always),
                })}),
                z.object({"child": z.object({
                    "value": z.integer().refine_async(async_always),
                })}),
                {},
            ),
        ]

        for name, sync_schema, async_schema, value in pairs:
            with self.subTest(name=name):
                sync_result = sync_schema.safe_parse(value)
                async_result = await async_schema.safe_parse_async(value)
                self.assertIsInstance(sync_result, Failure)
                self.assertIsInstance(async_result, Failure)
                self.assertEqual(
                    async_result.error.to_dict(),
                    sync_result.error.to_dict(),
                )

        missing_object = await z.object({
            "value": z.integer().refine_async(async_always),
        }).safe_parse_async(MISSING)
        self.assertIsInstance(missing_object, Failure)
        self.assertEqual(missing_object.error.issues[0].code, "required")

    async def test_async_set_and_map_composition(self):
        async def positive(value):
            await asyncio.sleep(0)
            return value > 0

        async def nonempty(value):
            await asyncio.sleep(0)
            return bool(value)

        values = z.set(z.integer().refine_async(positive))
        self.assertEqual(await values.parse_async({1, 2}), {1, 2})
        self.assertEqual(
            await z.object({"values": values}).parse_async({
                "values": {1, 2},
            }),
            {"values": {1, 2}},
        )
        failure = await values.safe_parse_async({0, 1})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [(issue.code, issue.path) for issue in failure.error.issues],
            [("custom", (0,))],
        )

        mutable = {1, 2}

        async def mutate(value):
            mutable.clear()
            return True

        snapshotted = z.set(z.integer().refine_async(mutate))
        self.assertEqual(await snapshotted.parse_async(mutable), {1, 2})

        mapping = z.map(
            z.string().refine_async(nonempty),
            z.integer().refine_async(positive),
        )
        self.assertEqual(await mapping.parse_async({"a": 1}), {"a": 1})
        bad_key = await mapping.safe_parse_async({"": 1})
        self.assertIsInstance(bad_key, Failure)
        self.assertEqual(bad_key.error.issues[0].path, ())
        bad_value = await mapping.safe_parse_async({"a": 0})
        self.assertIsInstance(bad_value, Failure)
        self.assertEqual(bad_value.error.issues[0].path, ("a",))

    async def test_async_matches_sync_across_schema_families(self):
        async def always(value):
            return True

        async def nonempty(value):
            return bool(value)

        async def stringify(value):
            return str(value)

        async def super_issues(value):
            return [
                Issue("custom", "first", ("x",)),
                Issue("custom", "second", ("y",)),
            ]

        async def default_factory():
            return 4

        async def catch_factory():
            return 9

        async def preprocess(value):
            return str(value)

        async def resolved(value):
            return value

        class Boom(Exception):
            pass

        async def explode(value):
            raise Boom("boom")

        def sync_always(value):
            return True

        def sync_nonempty(value):
            return bool(value)

        def sync_stringify(value):
            return str(value)

        def sync_super_issues(value):
            return [
                Issue("custom", "first", ("x",)),
                Issue("custom", "second", ("y",)),
            ]

        def sync_explode(value):
            raise Boom("boom")

        pairs = [
            (
                "string",
                z.string().trim().min(2).max(5).regex("^[a-z ]+$"),
                z.string().trim().min(2).max(5).regex("^[a-z ]+$")
                .refine_async(always),
                [" ab ", "A", 1, "", "abcdef", MISSING],
            ),
            (
                "object",
                z.object({
                    "id": z.integer().positive(),
                    "name": z.string().optional(),
                    "items": z.string().refine(sync_nonempty).array(),
                }).catchall(z.integer()),
                z.object({
                    "id": z.integer().positive().refine_async(always),
                    "name": z.string().optional().refine_async(always),
                    "items": z.string().refine_async(nonempty).array(),
                }).catchall(z.integer().refine_async(always)),
                [
                    {"id": 1, "items": ["x"], "extra": 2},
                    {"id": 0, "items": [""]},
                    {"id": 1, "items": ["x"], "extra": "bad"},
                    {"id": 1, "items": ["x"], "name": None},
                    MISSING,
                ],
            ),
            (
                "tuple",
                z.tuple(z.string(), z.integer()).rest(z.integer()),
                z.tuple(z.string(), z.integer().refine_async(always))
                .rest(z.integer().refine_async(always)),
                [["a", 1], ["a", 1, 2], ["a"], ["a", 1, "b"], MISSING],
            ),
            (
                "record",
                z.record(z.string().min(1), z.integer()),
                z.record(
                    z.string().min(1).refine_async(always),
                    z.integer().refine_async(always),
                ),
                [{"a": 1}, {"": 1}, {"a": "bad"}, {"a": 1, 2: 3}, MISSING],
            ),
            (
                "union",
                z.union(z.integer().positive(), z.string().min(1)),
                z.union(
                    z.integer().positive().refine_async(always),
                    z.string().min(1).refine_async(always),
                ),
                [1, 0, "", "x", None, MISSING],
            ),
            (
                "intersection",
                z.intersection(
                    z.object({"a": z.integer()}),
                    z.object({"b": z.string()}),
                ),
                z.intersection(
                    z.object({"a": z.integer().refine_async(always)}),
                    z.object({"b": z.string().refine_async(always)}),
                ),
                [{"a": 1, "b": "x"}, {"a": 1}, {"a": "x", "b": "x"}, MISSING],
            ),
            (
                "discriminated union",
                z.discriminated_union("kind", {
                    "a": z.object({
                        "kind": z.literal("a"),
                        "value": z.integer(),
                    }),
                    "b": z.object({
                        "kind": z.literal("b"),
                        "value": z.string(),
                    }),
                }),
                z.discriminated_union("kind", {
                    "a": z.object({
                        "kind": z.literal("a"),
                        "value": z.integer().refine_async(always),
                    }),
                    "b": z.object({
                        "kind": z.literal("b"),
                        "value": z.string().refine_async(always),
                    }),
                }),
                [
                    {"kind": "a", "value": 1},
                    {"kind": "b", "value": "x"},
                    {"kind": "c", "value": 1},
                    {"value": 1},
                    [],
                    MISSING,
                ],
            ),
            (
                "pipe and wrappers",
                z.object({
                    "o": z.string().optional(),
                    "n": z.string().nullable(),
                    "d": z.string().default("d"),
                    "c": z.integer().positive().catch(5),
                    "p": z.integer().transform(sync_stringify)
                    .pipe(z.string().min(2)),
                }),
                z.object({
                    "o": z.string().optional(),
                    "n": z.string().nullable().refine_async(always),
                    "d": z.string().default("d"),
                    "c": z.integer().positive().refine_async(always).catch(5),
                    "p": z.integer().transform_async(stringify)
                    .pipe(z.string().min(2).refine_async(always)),
                }),
                [
                    {"n": None, "c": 1, "p": 10},
                    {"n": None, "c": 0, "p": 10},
                    {"n": "x", "o": None, "p": 10},
                    MISSING,
                ],
            ),
            (
                "callbacks",
                z.object({
                    "a": z.integer().default_factory(lambda: 4),
                    "b": z.integer().positive().catch(9),
                    "c": z.integer().transform(lambda value: value + 1),
                    "d": z.preprocess(str, z.string().min(2)),
                }),
                z.object({
                    "a": z.integer().default_factory_async(default_factory),
                    "b": z.integer().positive().catch_async(catch_factory),
                    "c": z.integer().transform_async(
                        lambda value: resolved(value + 1),
                    ),
                    "d": z.preprocess_async(preprocess, z.string().min(2)),
                }),
                [
                    {"b": 1, "c": 2, "d": 42},
                    {"b": 0, "c": 2, "d": 42},
                    {"d": 4},
                    MISSING,
                ],
            ),
            (
                "super refine",
                z.integer().super_refine(sync_super_issues),
                z.integer().super_refine_async(super_issues),
                [1, MISSING],
            ),
            (
                "coerce and stringbool",
                z.object({
                    "number": z.coerce.integer().refine(sync_always),
                    "flag": z.string().stringbool().refine(sync_always),
                }),
                z.object({
                    "number": z.coerce.integer().refine_async(always),
                    "flag": z.string().stringbool().refine_async(always),
                }),
                [
                    {"number": "2", "flag": "true"},
                    {"number": "x", "flag": "yes"},
                    {"number": 2.0, "flag": True},
                    MISSING,
                ],
            ),
            (
                "lazy",
                z.lazy(lambda: z.object({
                    "value": z.integer().refine(sync_always),
                })),
                z.lazy(lambda: z.object({
                    "value": z.integer().refine_async(always),
                })),
                [{"value": 1}, {"value": "x"}, {}, MISSING],
            ),
            (
                "transform error",
                z.integer().transform(sync_explode),
                z.integer().transform_async(explode),
                [1, "x", MISSING],
            ),
        ]

        for name, sync_schema, async_schema, values in pairs:
            for value in values:
                with self.subTest(name=name, value=value):
                    try:
                        sync_result = sync_schema.safe_parse(value)
                        sync_error = None
                    except Exception as error:
                        sync_result = None
                        sync_error = error
                    try:
                        async_result = await async_schema.safe_parse_async(value)
                        async_error = None
                    except Exception as error:
                        async_result = None
                        async_error = error

                    if sync_error is not None or async_error is not None:
                        self.assertIsNotNone(sync_error)
                        self.assertIsNotNone(async_error)
                        self.assertIs(type(sync_error), type(async_error))
                        self.assertEqual(str(sync_error), str(async_error))
                        continue

                    if isinstance(sync_result, Failure):
                        self.assertIsInstance(async_result, Failure)
                        self.assertEqual(
                            async_result.error.to_dict(),
                            sync_result.error.to_dict(),
                        )
                    else:
                        self.assertIsInstance(sync_result, Success)
                        self.assertIsInstance(async_result, Success)
                        self.assertEqual(async_result.data, sync_result.data)

    async def test_async_codec_decode_and_encode(self):
        decode_calls = []
        encode_calls = []

        async def decode(value):
            decode_calls.append(value)
            await asyncio.sleep(0)
            return int(value)

        async def encode(value):
            encode_calls.append(value)
            await asyncio.sleep(0)
            return str(value)

        codec = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=encode,
        )
        self.assertEqual(await codec.decode_async("3"), 3)
        self.assertEqual(await codec.parse_async("4"), 4)
        self.assertEqual(await codec.encode_async(5), "5")
        self.assertEqual(decode_calls, ["3", "4"])
        self.assertEqual(encode_calls, [5])

        # Input schema failure skips decode; output schema failure is reported.
        self.assertIsInstance(await codec.safe_decode_async(1), Failure)
        self.assertEqual(decode_calls, ["3", "4"])

        async def bad_decode(value):
            return "not an integer"

        bad_output = z.codec_async(
            z.string(), z.integer(), decode=bad_decode, encode=encode,
        )
        output_failure = await bad_output.safe_decode_async("x")
        self.assertIsInstance(output_failure, Failure)
        self.assertEqual(output_failure.error.issues[0].code, "invalid_type")

        async def reject(value):
            raise ValidationError([])

        rejected = z.codec_async(
            z.string(), z.integer(), decode=reject, encode=encode,
        )
        rejected_failure = await rejected.safe_decode_async("x")
        self.assertIsInstance(rejected_failure, Failure)
        self.assertEqual(rejected_failure.error.issues[0].path, ())

        with self.assertRaisesRegex(TypeError, "parse_async"):
            codec.decode("1")
        with self.assertRaisesRegex(TypeError, "parse_async"):
            codec.encode(1)

        async def generator(value):
            yield value

        invalid_builders = [
            lambda: z.codec_async(
                "not a schema", z.integer(), decode=decode, encode=encode,
            ),
            lambda: z.codec_async(
                z.string(), z.integer(), decode=1, encode=encode,
            ),
            lambda: z.codec_async(
                z.string(), z.integer(), decode=decode, encode=1,
            ),
            lambda: z.codec_async(
                z.string(), z.integer(), decode=generator, encode=encode,
            ),
            lambda: z.codec_async(
                z.string(), z.integer(), decode=decode, encode=generator,
            ),
        ]
        for build in invalid_builders:
            with self.subTest(build=build), self.assertRaises(TypeError):
                build()

        def returns_value(value):
            return value

        non_awaitable = z.codec_async(
            z.string(), z.integer(), decode=returns_value, encode=encode,
        )
        with self.assertRaisesRegex(TypeError, "awaitable"):
            await non_awaitable.decode_async("1")

    async def test_async_codec_composition_and_wrappers(self):
        async def decode(value):
            try:
                return int(value)
            except ValueError:
                raise ValidationError([
                    Issue("custom", "not an integer", ()),
                ]) from None

        async def encode(value):
            return str(value)

        codec = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=encode,
        )
        self.assertEqual(codec.input_schema.parse("1"), "1")
        self.assertEqual(codec.output_schema.parse(1), 1)

        object_schema = z.object({"value": codec, "name": z.string()})
        self.assertEqual(
            await object_schema.parse_async({"value": "2", "name": "x"}),
            {"value": 2, "name": "x"},
        )
        self.assertEqual(
            await object_schema.encode_async({"value": 2, "name": "x"}),
            {"value": "2", "name": "x"},
        )

        optional = codec.optional()
        self.assertEqual(await optional.decode_async(MISSING), MISSING)
        self.assertEqual(await optional.encode_async(MISSING), MISSING)
        self.assertEqual(await optional.decode_async("3"), 3)
        self.assertEqual(await optional.encode_async(3), "3")

        nullable = codec.nullable()
        self.assertIsNone(await nullable.decode_async(None))
        self.assertIsNone(await nullable.encode_async(None))

        record_codec = codec.record()
        self.assertEqual(await record_codec.decode_async({"a": "4"}), {"a": 4})
        self.assertEqual(
            await record_codec.encode_async({"a": 4}), {"a": "4"},
        )

        array_codec = z.codecs.array(codec)
        self.assertEqual(await array_codec.decode_async(["5", "6"]), [5, 6])
        self.assertEqual(await array_codec.encode_async([5, 6]), ["5", "6"])

        union_codec = z.codecs.union(codec, z.string())
        self.assertEqual(await union_codec.decode_async("7"), 7)
        self.assertEqual(await union_codec.decode_async("text"), "text")
        self.assertEqual(await union_codec.encode_async(7), "7")
        self.assertEqual(
            await union_codec.encode_async("text"), "text",
        )

        tuple_codec = z.codecs.tuple(codec, z.string())
        self.assertEqual(
            await tuple_codec.decode_async(["8", "x"]), (8, "x"),
        )
        self.assertEqual(
            await tuple_codec.encode_async((8, "x")), ("8", "x"),
        )

    async def test_async_encoding_and_reversibility(self):
        async def decode(value):
            return int(value)

        async def encode(value):
            return str(value)

        codec = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=encode,
        )
        self.assertEqual(await codec.encode_async(9), "9")
        self.assertIsInstance(await codec.safe_encode_async("bad"), Failure)

        sync_codec = z.codec(
            z.string(), z.integer(), decode=int, encode=str,
        )
        self.assertEqual(await sync_codec.decode_async("10"), 10)
        self.assertEqual(await sync_codec.encode_async(10), "10")

        plain = z.object({"value": z.integer()})
        self.assertEqual(
            await plain.encode_async({"value": 10}), {"value": 10},
        )

        async def wrong_encode(value):
            return value

        bad_encoder = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=wrong_encode,
        )
        bad_failure = await bad_encoder.safe_encode_async(1)
        self.assertIsInstance(bad_failure, Failure)
        self.assertEqual(bad_failure.error.issues[0].code, "invalid_type")

        async def reject_encode(value):
            raise ValidationError([])

        rejected_encoder = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=reject_encode,
        )
        rejected_failure = await rejected_encoder.safe_encode_async(1)
        self.assertIsInstance(rejected_failure, Failure)
        self.assertEqual(rejected_failure.error.issues[0].code, "custom")

        async def explode(value):
            raise RuntimeError("encode callback failed")

        exploding = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=explode,
        )
        with self.assertRaisesRegex(RuntimeError, "encode callback failed"):
            await exploding.encode_async(1)

        with self.assertRaises(TypeError):
            await z.string().refine_async(lambda value: True).encode_async("x")
        with self.assertRaises(TypeError):
            await z.string().transform(str.upper).encode_async("x")

    async def test_async_dataclass_codec(self):
        @dataclass(frozen=True)
        class User:
            name: str
            value: int

        async def decode(value):
            if value == "bad":
                raise ValidationError([])
            return int(value)

        async def encode(value):
            return str(value)

        field_codec = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=encode,
        )
        user_codec = z.codecs.dataclass(User, fields={
            "name": z.string(),
            "value": field_codec,
        })
        encoded = {"name": "x", "value": "3"}
        user = User("x", 3)

        self.assertEqual(await user_codec.decode_async(encoded), user)
        self.assertEqual(await user_codec.encode_async(user), encoded)
        self.assertEqual(await user_codec.parse_async(encoded), user)
        output_schema = z.to_json_schema(user_codec, io="output")
        self.assertEqual(
            output_schema["properties"]["value"], {"type": "integer"},
        )
        self.assertEqual(
            z.to_json_schema(user_codec.optional(), io="output"),
            output_schema,
        )
        self.assertEqual(
            z.to_json_schema(z.codecs.array(user_codec), io="output"),
            {"type": "array", "items": output_schema},
        )

        failure = await user_codec.safe_decode_async({
            "name": "x", "value": "bad",
        })
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("value",))

        with self.assertRaisesRegex(TypeError, "parse_async"):
            user_codec.decode(encoded)
        with self.assertRaisesRegex(TypeError, "parse_async"):
            user_codec.encode(user)

        @dataclass(frozen=True)
        class Outer:
            user: User

        outer_codec = z.codecs.dataclass(Outer, fields={"user": user_codec})
        outer = Outer(user)
        self.assertEqual(
            await outer_codec.decode_async({"user": encoded}), outer,
        )
        self.assertEqual(
            await outer_codec.encode_async(outer), {"user": encoded},
        )

        async def nonempty(value):
            return bool(value)

        @dataclass(frozen=True)
        class PlainAsync:
            name: str

        plain_codec = z.codecs.dataclass(PlainAsync, fields={
            "name": z.string().refine_async(nonempty),
        })
        plain = PlainAsync("x")
        self.assertEqual(
            await plain_codec.decode_async({"name": "x"}), plain,
        )
        self.assertEqual(
            await plain_codec.encode_async(plain), {"name": "x"},
        )

        @dataclass(frozen=True)
        class Defaulted:
            value: int = 0

        defaulted = z.codecs.dataclass(Defaulted, fields={
            "value": field_codec,
        })
        self.assertEqual(
            await defaulted.decode_async({}), Defaulted(0),
        )
        self.assertEqual(
            await defaulted.encode_async(Defaulted(0)), {"value": "0"},
        )

    async def test_async_json_and_validation_helpers(self):
        async def nonempty(value):
            await asyncio.sleep(0)
            return bool(value)

        schema = z.object({
            "id": z.integer(),
            "name": z.string().refine_async(nonempty),
        })
        raw = b'{"id":1,"name":"Alex"}'
        expected = {"id": 1, "name": "Alex"}
        self.assertEqual(await schema.parse_json_async(raw), expected)
        self.assertEqual(await schema.parse_json_async(raw.decode()), expected)
        self.assertEqual(await schema.parse_json_async(bytearray(raw)), expected)

        failure = await schema.safe_parse_json_async(
            b'{"id":1,"name":""}',
        )
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("name",))

        invalid_json = await schema.safe_parse_json_async(b'{"id":')
        self.assertIsInstance(invalid_json, Failure)
        self.assertEqual(invalid_json.error.issues[0].code, "invalid_format")
        self.assertIn("position", invalid_json.error.issues[0].params)
        with self.assertRaises(ValidationError):
            await schema.parse_json_async(b'{"id":')
        with self.assertRaises(TypeError):
            await schema.safe_parse_json_async(1)  # type: ignore[arg-type]

        async def explode(value):
            raise RuntimeError("json callback failed")

        exploding = z.object({
            "name": z.string().refine_async(explode),
        })
        with self.assertRaisesRegex(RuntimeError, "json callback failed"):
            await exploding.parse_json_async(b'{"name":"Alex"}')

        # Synchronous schemas keep the native JSON fast path.
        self.assertEqual(await z.integer().parse_json_async(b"1"), 1)
        self.assertEqual(
            await z.integer().parse_json_async(bytearray(b"1")), 1,
        )
        self.assertIsNone(await z.none().parse_json_async(b"null"))
        self.assertEqual(
            await z.string().transform(str.upper).parse_json_async(b'"a"'),
            "A",
        )

        self.assertTrue(await z.integer().positive().validate_async(1))
        self.assertFalse(await z.integer().positive().validate_async(0))
        self.assertTrue(await schema.validate_async(expected))
        self.assertFalse(
            await schema.validate_async({"id": 1, "name": ""}),
        )

        async def rejecting(value):
            return False

        async def failing(value):
            raise RuntimeError("validate callback failed")

        self.assertFalse(
            await z.integer().refine_async(rejecting).validate_async(1),
        )
        with self.assertRaisesRegex(RuntimeError, "validate callback failed"):
            await z.integer().refine_async(failing).validate_async(1)

    async def test_async_refine_runs_once_and_preserves_order(self):
        values = []

        async def positive(value):
            values.append(value)
            await asyncio.sleep(0)
            return value > 0

        schema = z.integer().refine_async(positive)
        self.assertEqual(await schema.parse_async(2), 2)
        failure = await schema.safe_parse_async(0)
        self.assertIsInstance(failure, Failure)
        self.assertEqual(values, [2, 0])
        self.assertEqual(failure.error.issues[0].code, "custom")
        self.assertEqual(failure.error.issues[0].path, ())

        custom = z.integer().refine_async(positive, "must be positive", path=("n",))
        custom_failure = await custom.safe_parse_async(0)
        self.assertIsInstance(custom_failure, Failure)
        issue = custom_failure.error.issues[0]
        self.assertEqual((issue.message, issue.path), ("must be positive", ("n",)))

    async def test_async_transform(self):
        async def stringify(value):
            await asyncio.sleep(0)
            return str(value)

        schema = z.integer().transform_async(stringify)
        self.assertEqual(await schema.parse_async(4), "4")
        self.assertIsInstance(await schema.safe_parse_async("bad"), Failure)

    async def test_ordinary_callable_returning_awaitable(self):
        def positive(value):
            return asyncio.sleep(0, result=value > 0)

        schema = z.integer().refine_async(positive)
        self.assertEqual(await schema.parse_async(1), 1)
        self.assertIsInstance(await schema.safe_parse_async(0), Failure)

    async def test_async_callback_validation_errors(self):
        async def reject(value):
            raise ValidationError([Issue("custom", "nope", ("inner",))])

        schema = z.integer().refine_async(reject)
        failure = await schema.safe_parse_async(1)
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("inner",))

        async def empty(value):
            raise ValidationError([])

        empty_failure = await z.integer().refine_async(empty).safe_parse_async(1)
        self.assertIsInstance(empty_failure, Failure)
        self.assertEqual(empty_failure.error.issues[0].code, "custom")
        self.assertEqual(empty_failure.error.issues[0].message, "Invalid value")

        nested_empty = await z.object({
            "value": z.integer().refine_async(empty),
        }).safe_parse_async({"value": 1})
        self.assertIsInstance(nested_empty, Failure)
        self.assertEqual(nested_empty.error.issues[0].path, ("value",))
        self.assertEqual(nested_empty.error.issues[0].message, "Invalid value")

        async def explode(value):
            raise RuntimeError("callback failed")

        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            await z.integer().transform_async(explode).parse_async(1)

    async def test_async_super_refine(self):
        async def none(value):
            return None

        schema = z.integer().super_refine_async(none)
        self.assertEqual(await schema.parse_async(1), 1)

        async def empty(value):
            return []

        self.assertEqual(await z.integer().super_refine_async(empty).parse_async(1), 1)

        async def issue(value):
            return Issue("custom", "bad", ("inner",), params={"source": "async"})

        failure = await z.integer().super_refine_async(issue).safe_parse_async(1)
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            (failure.error.issues[0].path, failure.error.issues[0].message),
            (("inner",), "bad"),
        )

        async def multiple(value):
            return [
                Issue("custom", "first", ("a",)),
                Issue("custom", "second", ("b",)),
            ]

        multiple_failure = await (
            z.integer().super_refine_async(multiple).safe_parse_async(1)
        )
        self.assertIsInstance(multiple_failure, Failure)
        self.assertEqual(
            [issue.path for issue in multiple_failure.error.issues],
            [("a",), ("b",)],
        )

        async def invalid(value):
            return [True]

        with self.assertRaisesRegex(TypeError, "non-Issue"):
            await z.integer().super_refine_async(invalid).parse_async(1)

        async def raised(value):
            raise ValidationError([Issue("custom", "raised", ("inner",))])

        raised_failure = await (
            z.integer().super_refine_async(raised).safe_parse_async(1)
        )
        self.assertIsInstance(raised_failure, Failure)
        self.assertEqual(raised_failure.error.issues[0].path, ("inner",))

        with self.assertRaisesRegex(TypeError, "parse_async"):
            schema.parse(1)

        async def generator(value):
            yield None

        with self.assertRaisesRegex(TypeError, "async generator"):
            z.integer().super_refine_async(generator)

    async def test_child_validation_happens_before_await(self):
        called = False

        async def callback(value):
            nonlocal called
            called = True
            return True

        schema = z.integer().refine_async(callback)
        failure = await schema.safe_parse_async("bad")
        self.assertIsInstance(failure, Failure)
        self.assertFalse(called)

    async def test_sync_parse_rejects_async_schema(self):
        async def callback(value):
            return True

        schema = z.integer().refine_async(callback)
        with self.assertRaisesRegex(TypeError, "parse_async"):
            schema.parse(1)
        with self.assertRaisesRegex(TypeError, "parse_async"):
            schema.safe_parse(1)

    async def test_async_generators_and_non_awaitables_are_rejected(self):
        async def generator(value):
            yield True

        with self.assertRaisesRegex(TypeError, "async generator"):
            z.integer().refine_async(generator)

        def returns_generator(value):
            return generator(value)

        with self.assertRaisesRegex(TypeError, "async generators"):
            await z.integer().refine_async(returns_generator).parse_async(1)

        def returns_value(value):
            return True

        with self.assertRaisesRegex(TypeError, "awaitable"):
            await z.integer().refine_async(returns_value).parse_async(1)

    async def test_unreached_async_nodes_do_not_reject_sync_parse(self):
        async def callback(value):
            return True

        bypassed = z.union(z.string(), z.string().refine_async(callback))
        self.assertEqual(bypassed.parse("x"), "x")
        self.assertEqual(await bypassed.parse_async("x"), "x")

        reached = z.union(z.integer(), z.string().refine_async(callback))
        with self.assertRaisesRegex(TypeError, "parse_async"):
            reached.parse("x")

    async def test_object_array_and_tuple_composition(self):
        async def nonempty(value):
            await asyncio.sleep(0)
            return bool(value)

        schema = z.object({
            "id": z.integer().positive(),
            "name": z.string().refine_async(nonempty),
            "tags": z.string().refine_async(nonempty).array(),
        })
        result = await schema.parse_async({
            "id": 1, "name": "Alex", "tags": ["admin"],
        })
        self.assertEqual(
            result, {"id": 1, "name": "Alex", "tags": ["admin"]},
        )
        self.assertEqual(list(result), ["id", "name", "tags"])

        failure = await schema.safe_parse_async({
            "id": 0, "name": "", "tags": ["ok", ""],
        })
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [
                (issue.code, issue.path)
                for issue in failure.error.issues
            ],
            [
                ("too_small", ("id",)),
                ("custom", ("name",)),
                ("custom", ("tags", 1)),
            ],
        )

        record = z.record(z.string().refine_async(nonempty), z.integer())
        self.assertEqual(await record.parse_async({"a": 1}), {"a": 1})
        record_failure = await record.safe_parse_async({"": 1, "b": 2})
        self.assertIsInstance(record_failure, Failure)
        self.assertEqual(record_failure.error.issues[0].path, ())

        rest = z.tuple(z.string()).rest(z.integer().refine_async(nonempty))
        self.assertEqual(await rest.parse_async(["a", 1, 2]), ("a", 1, 2))
        rest_failure = await rest.safe_parse_async(["a", 1, 0])
        self.assertIsInstance(rest_failure, Failure)
        self.assertEqual(rest_failure.error.issues[0].path, (2,))

    async def test_object_defaults_transforms_and_catchall(self):
        calls = []

        async def positive(value):
            calls.append(value)
            return value > 0

        async def stringify(value):
            return str(value)

        schema = z.object({
            "a": z.integer().transform_async(stringify),
            "b": z.integer().refine_async(positive).optional(),
            "c": z.integer().refine_async(positive).default(1),
        }).catchall(z.integer().refine_async(positive))

        result = await schema.parse_async({"a": 1, "extra": 2})
        self.assertEqual(result, {"a": "1", "c": 1, "extra": 2})
        self.assertEqual(list(result), ["a", "c", "extra"])
        self.assertEqual(calls, [1, 2])

        failure = await schema.safe_parse_async({"a": 1, "b": 0})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("b",))

        extra_failure = await schema.safe_parse_async({"a": 1, "extra": 0})
        self.assertIsInstance(extra_failure, Failure)
        self.assertEqual(extra_failure.error.issues[0].path, ("extra",))

    async def test_union_composition(self):
        calls = []

        async def second(value):
            calls.append(value)
            return bool(value)

        schema = z.union(z.integer(), z.string().refine_async(second))
        self.assertEqual(await schema.parse_async(1), 1)
        self.assertEqual(calls, [])
        self.assertEqual(await schema.parse_async("x"), "x")
        self.assertEqual(calls, ["x"])

        failure = await schema.safe_parse_async("")
        self.assertIsInstance(failure, Failure)
        issue = failure.error.issues[0]
        self.assertEqual(issue.code, "invalid_union")
        self.assertEqual(len(issue.branches), 2)
        self.assertEqual(issue.branches[0][0].code, "invalid_type")
        self.assertEqual(issue.branches[1][0].code, "custom")

        async def first(value):
            calls.append(value)
            return bool(value)

        calls.clear()
        first_success = z.union(
            z.string().refine_async(first),
            z.string().refine_async(second),
        )
        self.assertEqual(await first_success.parse_async("x"), "x")
        self.assertEqual(calls, ["x"])

        async def explode(value):
            raise RuntimeError("union failed")

        calls.clear()
        programming = z.union(
            z.string().transform_async(explode),
            z.string().refine_async(second),
        )
        with self.assertRaisesRegex(RuntimeError, "union failed"):
            await programming.parse_async("x")
        self.assertEqual(calls, [])

    async def test_intersection_composition(self):
        async def positive(value):
            return value > 0

        async def nonempty(value):
            return bool(value)

        schema = z.intersection(
            z.object({"a": z.integer().refine_async(positive)}),
            z.object({"b": z.string().refine_async(nonempty)}),
        )
        self.assertEqual(
            await schema.parse_async({"a": 1, "b": "x"}),
            {"a": 1, "b": "x"},
        )

        failure = await schema.safe_parse_async({"a": 0, "b": ""})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [issue.path for issue in failure.error.issues],
            [("a",), ("b",)],
        )

        left_error = RuntimeError("left failed")
        right_calls = []

        async def left_boom(value):
            raise left_error

        async def right_ok(value):
            right_calls.append(value)
            return True

        programming = z.intersection(
            z.object({"a": z.integer().transform_async(left_boom)}),
            z.object({"b": z.string().refine_async(right_ok)}),
        )
        with self.assertRaises(RuntimeError) as caught:
            await programming.parse_async({"a": 1, "b": "x"})
        self.assertIs(caught.exception, left_error)
        self.assertEqual(right_calls, ["x"])

        class Control(BaseException):
            pass

        control = Control("stop")
        right_calls.clear()

        async def left_stop(value):
            raise control

        stopping = z.intersection(
            z.object({"a": z.integer().transform_async(left_stop)}),
            z.object({"b": z.string().refine_async(right_ok)}),
        )
        with self.assertRaises(Control) as caught:
            await stopping.parse_async({"a": 1, "b": "x"})
        self.assertIs(caught.exception, control)
        self.assertEqual(right_calls, [])

        async def left_text(value):
            return "left"

        async def right_text(value):
            return "right"

        non_objects = z.intersection(
            z.object({"a": z.integer()}).transform_async(left_text),
            z.object({"b": z.integer()}).transform_async(right_text),
        )
        non_object_failure = await non_objects.safe_parse_async({
            "a": 1, "b": 2,
        })
        self.assertIsInstance(non_object_failure, Failure)
        self.assertEqual(
            non_object_failure.error.issues[0].code,
            "invalid_intersection_types",
        )

    async def test_intersection_outcome_matrix(self):
        calls = []
        errors = {
            "left": RuntimeError("left failed"),
            "right": RuntimeError("right failed"),
        }

        def callback_for(side, outcome):
            async def callback(value):
                calls.append(f"{side} {outcome}")
                if outcome == "success":
                    return True
                if outcome == "invalid":
                    return False
                raise errors[side]

            return callback

        def schema_for(side, outcome):
            callback = callback_for(side, outcome)
            if outcome == "error":
                field = z.integer().transform_async(callback)
            else:
                message = f"{side} invalid"
                field = z.integer().refine_async(callback, message)
            return z.object({side: field})

        for left_kind in ("success", "invalid", "error"):
            for right_kind in ("success", "invalid", "error"):
                with self.subTest(left=left_kind, right=right_kind):
                    calls.clear()
                    schema = z.intersection(
                        schema_for("left", left_kind),
                        schema_for("right", right_kind),
                    )
                    expected_calls = [
                        f"left {left_kind}",
                        f"right {right_kind}",
                    ]
                    if left_kind == "error" or right_kind == "error":
                        expected = errors[
                            "left" if left_kind == "error" else "right"
                        ]
                        with self.assertRaises(RuntimeError) as caught:
                            await schema.parse_async({"left": 1, "right": 2})
                        self.assertIs(caught.exception, expected)
                        self.assertEqual(calls, expected_calls)
                        continue

                    result = await schema.safe_parse_async({
                        "left": 1,
                        "right": 2,
                    })
                    self.assertEqual(calls, expected_calls)
                    if left_kind == "success" and right_kind == "success":
                        self.assertIsInstance(result, Success)
                        self.assertEqual(
                            result.data,
                            {"left": 1, "right": 2},
                        )
                        continue
                    self.assertIsInstance(result, Failure)
                    expected_issues = []
                    if left_kind == "invalid":
                        expected_issues.append(("left invalid", ("left",)))
                    if right_kind == "invalid":
                        expected_issues.append(("right invalid", ("right",)))
                    self.assertEqual(
                        [
                            (issue.message, issue.path)
                            for issue in result.error.issues
                        ],
                        expected_issues,
                    )

        class Control(BaseException):
            pass

        calls.clear()
        control = Control("right control")
        left = z.object({
            "left": z.integer().refine_async(
                callback_for("left", "success"),
            ),
        })

        async def stop(value):
            calls.append("right control")
            raise control

        right = z.object({"right": z.integer().refine_async(stop)})
        with self.assertRaises(Control) as caught:
            await z.intersection(left, right).parse_async({
                "left": 1,
                "right": 2,
            })
        self.assertIs(caught.exception, control)
        self.assertEqual(calls, ["left success", "right control"])

    async def test_pipe_catch_lazy_and_discriminated_composition(self):
        async def nonempty(value):
            return bool(value)

        async def stringify(value):
            return str(value)

        async def positive(value):
            return value > 0

        piped = z.integer().transform_async(stringify).pipe(z.string().min(2))
        self.assertEqual(await piped.parse_async(10), "10")
        self.assertIsInstance(await piped.safe_parse_async(1), Failure)

        async_in_pipe = z.integer().transform(str).pipe(
            z.string().refine_async(nonempty),
        )
        self.assertEqual(await async_in_pipe.parse_async(0), "0")

        caught = z.integer().refine_async(positive).catch(5)
        self.assertEqual(await caught.parse_async(0), 5)

        async def explode(value):
            raise RuntimeError("not caught")

        not_caught = z.integer().transform_async(explode).catch(5)
        with self.assertRaisesRegex(RuntimeError, "not caught"):
            await not_caught.parse_async(1)

        factory_calls = []

        def factory():
            factory_calls.append(1)
            return z.object({"v": z.integer().refine_async(positive)})

        lazy = z.lazy(factory)
        self.assertEqual(await lazy.parse_async({"v": 1}), {"v": 1})
        self.assertEqual(len(factory_calls), 1)
        lazy_failure = await lazy.safe_parse_async({"v": 0})
        self.assertIsInstance(lazy_failure, Failure)
        self.assertEqual(lazy_failure.error.issues[0].path, ("v",))

        calls = []

        async def mark(label):
            async def callback(value):
                calls.append(label)
                return True
            return callback

        discriminated = z.discriminated_union("kind", {
            "a": z.object({
                "kind": z.literal("a"),
                "value": z.integer().refine_async(await mark("a")),
            }),
            "b": z.object({
                "kind": z.literal("b"),
                "value": z.integer().refine_async(await mark("b")),
            }),
        })
        self.assertEqual(
            await discriminated.parse_async({"kind": "a", "value": 1}),
            {"kind": "a", "value": 1},
        )
        self.assertEqual(calls, ["a"])

    async def test_async_refine_truthiness(self):
        values = [False, 0, "", None]
        for result in values:
            async def callback(parsed, result=result):
                return result
            failure = await z.integer().refine_async(callback).safe_parse_async(1)
            self.assertIsInstance(failure, Failure)
            self.assertEqual(failure.error.issues[0].code, "custom")

        marker = object()

        async def truthy(parsed):
            return marker

        self.assertEqual(
            await z.integer().refine_async(truthy).parse_async(1), 1,
        )

        class Unbool:
            def __bool__(self):
                raise RuntimeError("truthiness failed")

        async def unboolean(parsed):
            return Unbool()

        with self.assertRaisesRegex(RuntimeError, "truthiness failed"):
            await z.integer().refine_async(unboolean).parse_async(1)

    async def test_default_factory_and_catch_factory_composition(self):
        async def positive(value):
            return value > 0

        factory_calls = []

        def factory():
            factory_calls.append(1)
            return 2

        defaulted = z.object({
            "value": z.integer().refine_async(positive).default_factory(factory),
        })
        self.assertEqual(await defaulted.parse_async({}), {"value": 2})
        self.assertEqual(await defaulted.parse_async({"value": 1}), {"value": 1})
        self.assertEqual(len(factory_calls), 1)

        catch_calls = []

        def fallback():
            catch_calls.append(1)
            return 9

        caught = z.integer().refine_async(positive).catch(fallback)
        self.assertEqual(await caught.parse_async(0), 9)
        self.assertEqual(await caught.parse_async(1), 1)
        self.assertEqual(len(catch_calls), 1)

        async def explode(value):
            raise RuntimeError("not caught")

        programming = z.integer().transform_async(explode).catch(fallback)
        with self.assertRaisesRegex(RuntimeError, "not caught"):
            await programming.parse_async(1)
        self.assertEqual(len(catch_calls), 1)

    async def test_async_preprocess_and_overwrite(self):
        preprocess_calls = []

        async def to_text(value):
            preprocess_calls.append(value)
            await asyncio.sleep(0)
            return str(value)

        schema = z.preprocess_async(to_text, z.string().min(2))
        self.assertEqual(await schema.parse_async(42), "42")
        failure = await schema.safe_parse_async(4)
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].code, "too_small")
        self.assertEqual(preprocess_calls, [42, 4])

        async def reject(value):
            raise ValidationError([])

        caught = z.preprocess_async(reject, z.integer()).catch(7)
        self.assertEqual(await caught.parse_async(1), 7)

        async def nonempty(value):
            return bool(value)

        async_child = z.preprocess_async(
            to_text, z.string().refine_async(nonempty),
        )
        self.assertEqual(await async_child.parse_async(42), "42")

        async def identity(value):
            return value

        missing_default = z.preprocess_async(
            identity, z.integer().default(3),
        )
        self.assertEqual(await missing_default.parse_async(MISSING), 3)

        overwrite_calls = []

        async def bump(value):
            overwrite_calls.append(value)
            await asyncio.sleep(0)
            return value + 1

        overwritten = z.integer().overwrite_async(bump)
        self.assertEqual(await overwritten.parse_async(1), 2)
        self.assertEqual(overwrite_calls, [1])

        self.assertIsInstance(await overwritten.safe_parse_async("bad"), Failure)
        self.assertEqual(overwrite_calls, [1])

        async def positive(value):
            return value > 0

        async_overwrite = z.integer().refine_async(positive).overwrite_async(bump)
        self.assertEqual(await async_overwrite.parse_async(1), 2)
        self.assertEqual(overwrite_calls, [1, 1])

        async def explode(value):
            raise RuntimeError("overwrite failed")

        with self.assertRaisesRegex(RuntimeError, "overwrite failed"):
            await z.integer().overwrite_async(explode).parse_async(1)

        async def reject_overwrite(value):
            raise ValidationError([])

        caught_overwrite = z.integer().overwrite_async(reject_overwrite).catch(7)
        self.assertEqual(await caught_overwrite.parse_async(1), 7)

        async def generator(value):
            yield value

        async def zero_generator():
            yield None

        invalid_builders = [
            lambda: z.preprocess_async(1, z.string()),
            lambda: z.preprocess_async(generator, z.string()),
            lambda: z.integer().overwrite_async(1),
            lambda: z.integer().overwrite_async(generator),
            lambda: z.integer().default_factory_async(1),
            lambda: z.integer().default_factory_async(zero_generator),
            lambda: z.integer().catch_async(1),
            lambda: z.integer().catch_async(zero_generator),
        ]
        for build in invalid_builders:
            with self.subTest(build=build), self.assertRaises(TypeError):
                build()

        nested = z.object({
            "p": z.preprocess_async(to_text, z.string().min(2)),
            "o": z.integer().overwrite_async(bump),
        })
        self.assertEqual(
            await nested.parse_async({"p": 50, "o": 1}),
            {"p": "50", "o": 2},
        )

        before = len(preprocess_calls)
        with self.assertRaisesRegex(TypeError, "parse_async"):
            schema.parse(42)
        self.assertEqual(len(preprocess_calls), before)

    async def test_async_callback_families_require_awaitables(self):
        def returns_value(value=None):
            return 1

        async def generator():
            yield 1

        def returns_generator(value=None):
            return generator()

        cases = [
            lambda: z.preprocess_async(returns_value, z.integer()),
            lambda: z.integer().overwrite_async(returns_value),
            lambda: z.integer().default_factory_async(returns_value),
            lambda: z.integer().catch_async(returns_value),
        ]
        for build in cases:
            schema = build()
            with (
                self.subTest(schema=schema),
                self.assertRaisesRegex(TypeError, "awaitable"),
            ):
                if schema._node.kind == "async_default_factory":
                    await schema.parse_async()
                elif schema._node.kind == "async_catch":
                    await schema.parse_async("bad")
                else:
                    await schema.parse_async(1)

        generator_cases = [
            lambda: z.preprocess_async(returns_generator, z.integer()),
            lambda: z.integer().overwrite_async(returns_generator),
            lambda: z.integer().default_factory_async(
                lambda: returns_generator(),
            ),
            lambda: z.integer().catch_async(lambda: returns_generator()),
        ]
        for build in generator_cases:
            schema = build()
            with (
                self.subTest(schema=schema),
                self.assertRaisesRegex(TypeError, "async generators"),
            ):
                if schema._node.kind == "async_default_factory":
                    await schema.parse_async()
                elif schema._node.kind == "async_catch":
                    await schema.parse_async("bad")
                else:
                    await schema.parse_async(1)

    async def test_async_default_factory(self):
        factory_calls = []

        async def factory():
            factory_calls.append(1)
            await asyncio.sleep(0)
            return 2

        schema = z.integer().positive().default_factory_async(factory)
        self.assertEqual(await schema.parse_async(), 2)
        self.assertEqual(await schema.parse_async(3), 3)
        self.assertEqual(len(factory_calls), 1)

        optional = z.integer().default_factory_async(factory).optional()
        self.assertEqual(await optional.parse_async(MISSING), MISSING)
        self.assertEqual(len(factory_calls), 1)

        async def invalid_default():
            return 0

        invalid = z.integer().positive().default_factory_async(invalid_default)
        failure = await invalid.safe_parse_async()
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ())

        async def failing_factory():
            raise RuntimeError("factory failed")

        with self.assertRaisesRegex(RuntimeError, "factory failed"):
            await z.integer().default_factory_async(failing_factory).parse_async()

        async def reject_factory():
            raise ValidationError([])

        caught_factory = z.integer().default_factory_async(reject_factory).catch(7)
        self.assertEqual(await caught_factory.parse_async(), 7)

        self.assertEqual(
            await z.object({
                "value": z.integer().default_factory_async(factory),
            }).parse_async({}),
            {"value": 2},
        )
        self.assertEqual(len(factory_calls), 2)

        async def positive(value):
            return value > 0

        async def async_factory():
            return 4

        async_child = (
            z.integer().refine_async(positive)
            .default_factory_async(async_factory)
        )
        self.assertEqual(await async_child.parse_async(), 4)

        with self.assertRaisesRegex(TypeError, "parse_async"):
            schema.parse(3)

    async def test_async_catch_fallback(self):
        fallback_calls = []

        async def fallback():
            fallback_calls.append(1)
            await asyncio.sleep(0)
            return 9

        schema = z.integer().positive().catch_async(fallback)
        self.assertEqual(await schema.parse_async(1), 1)
        self.assertEqual(fallback_calls, [])
        self.assertEqual(await schema.parse_async(0), 9)
        self.assertEqual(fallback_calls, [1])
        self.assertEqual(await schema.parse_async("bad"), 9)
        self.assertEqual(fallback_calls, [1, 1])
        self.assertEqual(await schema.parse_async(), 9)
        self.assertEqual(fallback_calls, [1, 1, 1])

        async def explode(value):
            raise RuntimeError("catch programming error")

        before = len(fallback_calls)
        with self.assertRaisesRegex(RuntimeError, "catch programming error"):
            await z.integer().transform_async(explode).catch_async(fallback).parse_async(1)
        self.assertEqual(len(fallback_calls), before)

        async def failing_fallback():
            raise RuntimeError("fallback failed")

        with self.assertRaisesRegex(RuntimeError, "fallback failed"):
            await z.integer().catch_async(failing_fallback).parse_async("bad")

        async def wrong_type():
            return "fallback"

        self.assertEqual(
            await z.integer().catch_async(wrong_type).parse_async("bad"),
            "fallback",
        )

        async def issue_fallback():
            raise ValidationError([
                Issue("custom", "inner", ("x",)),
            ])

        async def outer_fallback():
            return 11

        nested = (
            z.integer().positive()
            .catch_async(issue_fallback)
            .catch_async(outer_fallback)
        )
        self.assertEqual(await nested.parse_async(0), 11)

        bypassed = z.integer().catch_async(fallback).optional()
        self.assertEqual(await bypassed.parse_async(MISSING), MISSING)
        self.assertEqual(len(fallback_calls), 3)

        with self.assertRaisesRegex(TypeError, "parse_async"):
            schema.parse(1)
        self.assertEqual(len(fallback_calls), 3)

    async def test_async_super_refine_paths_in_containers(self):
        async def returned(value):
            return Issue("custom", "returned", ("inner",))

        schema = z.object({
            "a": z.integer().super_refine_async(returned),
            "b": z.integer().positive(),
        })
        failure = await schema.safe_parse_async({"a": 1, "b": -1})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [(issue.path, issue.message) for issue in failure.error.issues],
            [(("a", "inner"), "returned"), (("b",), "Must be positive")],
        )

        async def raised(value):
            raise ValidationError([Issue("custom", "raised", ("inner",))])

        raised_failure = await z.object({
            "a": z.integer().super_refine_async(raised),
        }).safe_parse_async({"a": 1})
        self.assertIsInstance(raised_failure, Failure)
        self.assertEqual(raised_failure.error.issues[0].path, ("inner",))

        async def empty(value):
            return []

        self.assertEqual(
            await z.object({
                "a": z.integer().super_refine_async(empty),
            }).parse_async({"a": 1}),
            {"a": 1},
        )

    async def test_snapshots_survive_suspension(self):
        object_input = {"a": 1, "b": 2, "extra": 3}

        async def mutate_object(value):
            object_input.clear()
            object_input.update({"a": 9, "b": 9, "extra": 9, "added": 9})
            return True

        object_schema = z.object({
            "a": z.integer().refine_async(mutate_object),
            "b": z.integer(),
        }).passthrough()
        self.assertEqual(
            await object_schema.parse_async(object_input),
            {"a": 1, "b": 2, "extra": 3},
        )

        array_input = [1, 2]

        async def mutate_array(value):
            array_input.append(3)
            array_input[0] = 9
            return True

        array_schema = z.array(z.unknown().refine_async(mutate_array))
        self.assertEqual(await array_schema.parse_async(array_input), [1, 2])

        record_input = {"a": 1}

        async def mutate_record(value):
            record_input["a"] = 9
            record_input["b"] = 9
            return True

        record_schema = z.record(z.unknown().refine_async(mutate_record))
        self.assertEqual(await record_schema.parse_async(record_input), {"a": 1})

        no_extras = {"a": 1}

        async def add_extra(value):
            no_extras["added"] = 9
            return True

        no_extra_schema = z.object({
            "a": z.integer().refine_async(add_extra),
        }).passthrough()
        self.assertEqual(await no_extra_schema.parse_async(no_extras), {"a": 1})

    async def test_lazy_cache_is_session_scoped(self):
        factory_calls = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def callback(value):
            if value == 1:
                entered.set()
                await release.wait()
            return True

        def factory():
            factory_calls.append(1)
            return z.object({"v": z.integer().refine_async(callback)})

        lazy = z.lazy(factory)
        schema = z.object({"a": lazy, "b": lazy})

        first = asyncio.create_task(
            schema.parse_async({"a": {"v": 1}, "b": {"v": 2}}),
        )
        await entered.wait()
        second = asyncio.create_task(
            schema.parse_async({"a": {"v": 3}, "b": {"v": 4}}),
        )
        for _ in range(10):
            if len(factory_calls) == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(factory_calls), 2)
        release.set()
        self.assertEqual(
            await first, {"a": {"v": 1}, "b": {"v": 2}},
        )
        self.assertEqual(
            await second, {"a": {"v": 3}, "b": {"v": 4}},
        )

    async def test_nested_callbacks_are_sequential_and_reentrant(self):
        events = []

        async def first(value):
            events.append("first")
            await asyncio.sleep(0)
            return True

        def middle(value):
            events.append("middle")
            return True

        async def last(value):
            events.append("last")
            return True

        schema = z.object({
            "a": z.integer().refine_async(first),
            "b": z.integer().refine(middle),
            "c": z.integer().refine_async(last),
        })
        await schema.parse_async({"a": 1, "b": 2, "c": 3})
        self.assertEqual(events, ["first", "middle", "last"])

        async def reenter(value):
            if value == 0:
                return True
            return await checked.parse_async(0) == 0

        checked = z.integer().refine_async(reenter)
        self.assertEqual(await checked.parse_async(1), 1)

        nested = z.object({"value": z.integer().refine_async(first)})
        with self.assertRaisesRegex(TypeError, "parse_async"):
            nested.parse({"value": 1})
        self.assertEqual(
            await nested.optional().parse_async(MISSING), MISSING,
        )

    async def test_depth_matches_synchronous_composition(self):
        def synchronous_schema():
            recursive = None
            recursive = z.lazy(lambda: z.object({
                "value": z.integer().refine(lambda value: True),
                "child": recursive.optional(),
            }))
            return recursive

        async def asynchronous_schema():
            async def callback(value):
                return True

            recursive = None
            recursive = z.lazy(lambda: z.object({
                "value": z.integer().refine_async(callback),
                "child": recursive.optional(),
            }))
            return recursive

        def payload(depth):
            value = {"value": -1}
            for index in range(depth):
                value = {"value": index, "child": value}
            return value

        def sync_limit(schema):
            for depth in range(100):
                try:
                    schema.parse(payload(depth))
                except RecursionError:
                    return depth
            self.fail("synchronous schema did not hit the depth limit")

        async def async_limit(schema):
            for depth in range(100):
                try:
                    await schema.parse_async(payload(depth))
                except RecursionError:
                    return depth
            self.fail("asynchronous schema did not hit the depth limit")

        self.assertEqual(
            sync_limit(synchronous_schema()),
            await async_limit(await asynchronous_schema()),
        )

    async def test_async_intersection_merge_depth_is_bounded(self):
        schema = z.intersection(z.unknown(), z.unknown())
        nested = {}
        for _ in range(1000):
            nested = {"child": nested}
        with self.assertRaises(RecursionError):
            await schema.safe_parse_async(nested)
        self.assertEqual(
            await schema.parse_async({"a": {"b": 1}}), {"a": {"b": 1}},
        )

    async def test_session_gc_and_pending_coroutine_cleanup(self):
        cleanup_calls = []

        class Callback:
            def __init__(self):
                self.session = None

            async def __call__(self, value):
                return True

            def close(self):
                cleanup_calls.append("close")

        callback = Callback()
        schema = z.object({"value": z.integer().refine_async(callback)})
        session = AsyncSession(
            schema._node, {"value": 1}, MISSING, Issue, ValidationError,
        )
        callback.session = session
        done, payload = session.start()
        self.assertFalse(done)
        _token, awaitable = payload
        awaitable.close()
        reference = weakref.ref(session)
        del session, schema, callback
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(cleanup_calls, [])

        async def wait_forever(value):
            await asyncio.sleep(100)
            return True

        pending = z.integer().refine_async(wait_forever)
        pending_session = AsyncSession(
            pending._node, 1, MISSING, Issue, ValidationError,
        )
        done, payload = pending_session.start()
        self.assertFalse(done)
        _token, coroutine = payload
        self.assertIsNotNone(coroutine.cr_frame)
        pending_session.close()
        self.assertIsNone(coroutine.cr_frame)

    async def test_native_session_rejects_stale_tokens_and_closes(self):
        async def callback(value):
            return True

        schema = z.integer().refine_async(callback)
        session = AsyncSession(
            schema._node, 1, MISSING, Issue, ValidationError,
        )
        done, payload = session.start()
        self.assertFalse(done)
        token, awaitable = payload
        awaitable.close()
        with self.assertRaisesRegex(RuntimeError, "Stale"):
            session.resume_value(token + 1, True)
        done, value = session.resume_value(token, True)
        self.assertTrue(done)
        self.assertEqual(value, 1)
        session.close()
        session.close()
        with self.assertRaisesRegex(RuntimeError, "not awaiting"):
            session.resume_value(token, True)

    async def test_reentrant_resume_does_not_consume_pending_state(self):
        class Callback:
            def __init__(self):
                self.session = None
                self.errors = []

            def __call__(self, value):
                try:
                    self.session.resume_value(0, True)
                except RuntimeError as error:
                    self.errors.append(error)

                async def result():
                    return True

                return result()

        callback = Callback()
        schema = z.integer().refine_async(callback)
        session = AsyncSession(
            schema._node, 1, MISSING, Issue, ValidationError,
        )
        callback.session = session
        done, payload = session.start()
        self.assertFalse(done)
        self.assertTrue(callback.errors)
        self.assertIsInstance(callback.errors[0], RuntimeError)
        token, awaitable = payload
        awaitable.close()
        done, value = session.resume_value(token, True)
        self.assertTrue(done)
        self.assertEqual(value, 1)
        session.close()

    async def test_tokens_are_unique_and_thread_affine(self):
        async def callback(value):
            return True

        schema = z.integer().refine_async(callback)

        def start_session():
            session = AsyncSession(
                schema._node, 1, MISSING, Issue, ValidationError,
            )
            done, payload = session.start()
            self.assertFalse(done)
            return session, payload

        first_session, (first_token, first_awaitable) = start_session()
        second_session, (second_token, second_awaitable) = start_session()
        first_awaitable.close()
        second_awaitable.close()
        try:
            self.assertNotEqual(first_token, second_token)
        finally:
            first_session.close()
            second_session.close()

        session, (token, awaitable) = start_session()
        awaitable.close()
        errors = []

        def resume_elsewhere():
            try:
                session.resume_value(token, True)
            except RuntimeError as error:
                errors.append(error)

        worker = threading.Thread(target=resume_elsewhere)
        worker.start()
        worker.join()
        self.assertTrue(errors)
        self.assertIn("different thread", str(errors[0]))
        done, value = session.resume_value(token, True)
        self.assertTrue(done)
        self.assertEqual(value, 1)
        session.close()

    async def test_control_exception_identity(self):
        class Control(BaseException):
            pass

        original = Control("stop")

        async def stop(value):
            raise original

        schema = z.integer().transform_async(stop)
        with self.assertRaises(Control) as caught:
            await schema.parse_async(1)
        self.assertIs(caught.exception, original)
        with self.assertRaises(Control):
            await schema.safe_parse_async(1)

    async def test_many_concurrent_parses_isolate_sessions(self):
        factory_calls = []

        async def nonempty(value):
            await asyncio.sleep(0)
            return bool(value)

        def factory():
            factory_calls.append(1)
            return z.object({"name": z.string().refine_async(nonempty)})

        schema = z.object({
            "id": z.integer().refine_async(nonempty),
            "item": z.lazy(factory),
        })
        payloads = [
            {"id": index, "item": {"name": f"name-{index}"}}
            for index in range(1, 65)
        ]
        results = await asyncio.gather(
            *(schema.parse_async(payload) for payload in payloads),
        )
        self.assertEqual(results, payloads)
        self.assertEqual(len(factory_calls), len(payloads))

        failures = await asyncio.gather(*(
            schema.safe_parse_async({"id": 0, "item": {"name": "x"}})
            for _ in range(32)
        ))
        for failure in failures:
            self.assertIsInstance(failure, Failure)
            self.assertEqual(
                [(issue.code, issue.path) for issue in failure.error.issues],
                [("custom", ("id",))],
            )

    async def test_cancellation_at_nested_await_points_stops_work(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()
        later = []
        fallback_calls = []

        async def wait(value):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def sibling(value):
            later.append(value)
            return True

        async def fallback():
            fallback_calls.append(1)
            return 9

        schema = z.object({
            "first": z.integer().refine_async(wait),
            "second": z.integer().refine_async(sibling),
        }).catch_async(fallback)
        task = asyncio.create_task(schema.parse_async({
            "first": 1,
            "second": 2,
        }))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cleaned.wait(), timeout=1)
        self.assertEqual(later, [])
        self.assertEqual(fallback_calls, [])

        timeout_started = asyncio.Event()
        timeout_cleaned = asyncio.Event()

        async def timeout_wait(value):
            timeout_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                timeout_cleaned.set()

        timed = z.object({
            "value": z.integer().refine_async(timeout_wait),
        })
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                timed.parse_async({"value": 1}),
                timeout=0.01,
            )
        await asyncio.wait_for(timeout_cleaned.wait(), timeout=1)

    async def test_async_codec_brand(self):
        UserId = NewType("UserId", int)

        async def decode(value):
            return int(value)

        async def encode(value):
            return str(value)

        codec = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=encode,
        ).brand(UserId)
        self.assertEqual(await codec.decode_async("3"), 3)
        self.assertEqual(await codec.encode_async(3), "3")

    async def test_async_codec_cancellation_cleans_decode(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()
        encode_calls = []

        async def decode(value):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def encode(value):
            encode_calls.append(value)
            return str(value)

        codec = z.codec_async(
            z.string(), z.integer(), decode=decode, encode=encode,
        )
        task = asyncio.create_task(codec.decode_async("1"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cleaned.wait(), timeout=1)
        self.assertEqual(encode_calls, [])

    async def test_cancellation_propagates_and_cleans_up(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def wait(value):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        schema = z.integer().refine_async(wait)
        task = asyncio.create_task(schema.parse_async(1))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cleaned.wait(), timeout=1)


if __name__ == "__main__":
    unittest.main()
