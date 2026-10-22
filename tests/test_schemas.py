import copy
import dataclasses
import datetime as dt
import decimal
import enum
import inspect
import json
import math
import pathlib
import pickle
import re
import unittest
import weakref
from typing import (
    Annotated,
    Any,
    Generic,
    Literal,
    NewType,
    NotRequired,
    Required,
    TypedDict,
    TypeVar,
    get_type_hints,
)

from zodish import (
    MISSING,
    Failure,
    Issue,
    Schema,
    Success,
    UnrepresentableError,
    ValidatedModel,
    ValidationError,
    to_json_schema,
    z,
)


@dataclasses.dataclass
class _RecursiveNode:
    value: int
    children: list[_RecursiveNode] = dataclasses.field(default_factory=list)


class SchemaTests(unittest.TestCase):
    def test_container_schemas_are_hashable(self):
        schema = z.object({"name": z.string()})
        self.assertIsInstance(hash(schema), int)
        described = schema.describe("User")
        self.assertEqual(schema, described)
        self.assertEqual(hash(schema), hash(described))
        self.assertIsInstance(
            hash(z.discriminated_union("kind", {"a": z.string()})), int,
        )

    def test_primitives_are_strict(self):
        cases = [
            (z.string(), "a", [1, True, None]),
            (z.integer(), 42, [42.0, True, "42"]),
            (z.number(), 42.5, [True, "42", math.nan, math.inf, -math.inf]),
            (z.boolean(), True, [1, "true", None]),
            (z.none(), None, [0, False]),
        ]
        for schema, good, bad_values in cases:
            with self.subTest(schema=schema, good=good):
                self.assertEqual(schema.parse(good), good)
                for bad in bad_values:
                    self.assertIsInstance(schema.safe_parse(bad), Failure)
                self.assertIsInstance(schema.safe_parse(), Failure)
        self.assertEqual(z.number().parse(10**1000), 10**1000)

    def test_safe_parse_discriminates_success_and_failure(self):
        good = z.string().safe_parse("hello")
        bad = z.string().safe_parse(7)
        self.assertIsInstance(good, Success)
        self.assertTrue(good.success)
        self.assertEqual(good.data, "hello")
        self.assertFalse(bad.success)
        self.assertIsInstance(bad.error, ValidationError)
        self.assertEqual(bad.error.issues[0].code, "invalid_type")
        self.assertIs(bad.error.issues, bad.error.issues)

    def test_parse_json(self):
        schema = z.object({
            "id": z.integer().positive(),
            "name": z.string().min(1).max(64),
            "tags": z.string().array(),
        })
        expected = {"id": 1, "name": "Alex", "tags": ["admin"]}
        raw = b'{"id":1,"name":"Alex","tags":["admin"],"extra":true}'
        self.assertEqual(schema.parse_json(raw), expected)
        self.assertEqual(schema.parse_json(raw.decode()), expected)
        self.assertEqual(schema.parse_json(bytearray(raw)), expected)
        self.assertTrue(schema.validate_json(raw))
        self.assertIsInstance(schema.safe_parse_json(b"not json"), Failure)

        invalid = schema.safe_parse_json(b'{"id":0,"name":"","tags":[]}')
        self.assertIsInstance(invalid, Failure)
        self.assertEqual(
            [(issue.code, issue.path) for issue in invalid.error.issues],
            [("too_small", ("id",)), ("too_small", ("name",))],
        )
        with self.assertRaises(ValidationError) as caught:
            schema.parse_json(b'{"id":')
        issue = caught.exception.issues[0]
        self.assertEqual(issue.code, "invalid_format")
        self.assertEqual(issue.params["format"], "json")
        self.assertEqual(issue.params["position"], 6)

        self.assertEqual(z.boolean().parse_json(b"true"), True)
        self.assertIsNone(z.none().parse_json(b"null"))
        self.assertEqual(z.integer().parse_json(str(10**100).encode()), 10**100)
        self.assertEqual(
            z.object({"x": z.integer().default(1)}).parse_json(b"{}"),
            {"x": 1},
        )
        self.assertEqual(z.integer().nullable().parse_json(b"null"), None)
        self.assertEqual(z.integer().optional().nonoptional().parse_json(b"1"), 1)
        self.assertEqual(z.string().regex("^a+$").parse_json(b'"aaa"'), "aaa")
        self.assertIsInstance(
            z.string().regex("^a+$").safe_parse_json(b'"b"'), Failure,
        )
        self.assertEqual(z.string().transform(str.upper).parse_json(b'"a"'), "A")

        self.assertEqual(z.integer().min(1).max(3).parse_json(b"2"), 2)
        self.assertEqual(z.integer().gt(1).lt(3).parse_json(b"2"), 2)
        huge_bound = 10**100
        self.assertEqual(
            z.integer().min(huge_bound).parse_json(str(huge_bound + 1).encode()),
            huge_bound + 1,
        )
        self.assertEqual(z.integer().multiple_of(3).parse_json(b"9"), 9)
        self.assertIsInstance(z.integer().multiple_of(3).safe_parse_json(b"8"), Failure)
        self.assertEqual(z.literal("a").parse_json(b'"a"'), "a")
        self.assertEqual(z.literal(1).parse_json(b"1"), 1)
        self.assertEqual(z.literal(None).parse_json(b"null"), None)
        self.assertEqual(z.enum(["a", "b"]).parse_json(b'"b"'), "b")
        self.assertIsInstance(z.literal(1).safe_parse_json(b"true"), Failure)
        self.assertEqual(z.enum([1.5]).parse_json(b"1.5"), 1.5)
        self.assertEqual(
            z.object({
                "x": z.integer(),
                "note": z.string().optional(),
            }).parse_json(b'{"x":1}'),
            {"x": 1},
        )
        self.assertEqual(
            z.object({
                "x": z.integer(),
                "note": z.string().optional(),
            }).parse_json(b'{"x":1,"extra":2}'),
            {"x": 1},
        )
        self.assertIsInstance(
            z.object({
                "x": z.integer(),
                "note": z.string().optional(),
            }).safe_parse_json(b'{"x":1,"note":null}'),
            Failure,
        )
        strict = z.object({"x": z.integer()}).strict()
        self.assertEqual(strict.parse_json(b'{"x":1}'), {"x": 1})
        strict_bad = strict.safe_parse_json(b'{"x":1,"extra":2}')
        self.assertIsInstance(strict_bad, Failure)
        self.assertEqual(strict_bad.error.issues[0].path, ("extra",))
        with self.assertRaises(TypeError):
            schema.parse_json(1)  # type: ignore[arg-type]

    def test_string_chain_is_ordered_and_immutable(self):
        base = z.string()
        trimmed = base.trim().min(2).max(4).lower()
        self.assertEqual(trimmed.parse(" AB "), "ab")
        self.assertEqual(base.parse(" AB "), " AB ")
        self.assertIsInstance(trimmed.safe_parse(" a "), Failure)
        self.assertEqual(z.string().upper().parse("ab"), "AB")
        self.assertIsInstance(z.string().regex(r"^a+$").safe_parse("b"), Failure)
        self.assertEqual(z.string().email().parse("alex@example.com"), "alex@example.com")
        for bad in ("alex", "a@b", "a @b.com", "a@b.com\n"):
            self.assertIsInstance(z.string().email().safe_parse(bad), Failure)

    def test_number_constraints(self):
        schema = z.number().min(1).max(10).int()
        self.assertEqual(schema.parse(3.0), 3.0)
        for bad in (0, 11, 1.5):
            self.assertIsInstance(schema.safe_parse(bad), Failure)
        self.assertIsInstance(z.integer().positive().safe_parse(0), Failure)
        self.assertEqual(z.integer().nonnegative().parse(0), 0)

        bounded = z.integer().gt(1).gte(2).lt(10).lte(9)
        for good in (2, 9):
            self.assertEqual(bounded.parse(good), good)
        for bad in (1, 10):
            self.assertIsInstance(bounded.safe_parse(bad), Failure)
        self.assertEqual(z.number().gt(0).lt(1).parse(0.5), 0.5)
        self.assertEqual(z.number().gte(0).lte(1).parse(0), 0)

        multiples = z.integer().multiple_of(3)
        for good in (-6, 0, 9):
            self.assertEqual(multiples.parse(good), good)
        for bad in (1, 10):
            self.assertIsInstance(multiples.safe_parse(bad), Failure)
        self.assertEqual(z.number().multiple_of(0.1).parse(0.3), 0.3)
        self.assertIsInstance(z.number().multiple_of(0.1).safe_parse(0.35), Failure)
        huge = 10**1000
        self.assertEqual(z.integer().multiple_of(2.0).parse(huge), huge)
        self.assertIsInstance(z.integer().multiple_of(3).safe_parse(huge + 1), Failure)

        at_i64_limit = z.integer().multiple_of(float(2**63))
        self.assertEqual(at_i64_limit.parse(2**63), 2**63)
        self.assertEqual(at_i64_limit.parse(2 * 2**63), 2 * 2**63)
        for bad in (2**63 - 1, 2**63 + 1024, 3 * 2**63 + 1):
            self.assertIsInstance(at_i64_limit.safe_parse(bad), Failure)
        float_divisor = int(1e300)
        beyond_i64 = z.integer().multiple_of(1e300)
        self.assertEqual(beyond_i64.parse(2 * float_divisor), 2 * float_divisor)
        self.assertIsInstance(
            beyond_i64.safe_parse(2 * float_divisor + 1), Failure,
        )

    def test_validation_errors_and_issues_pickle(self):
        error = z.string().safe_parse(1).error
        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual(
            [(issue.code, issue.message) for issue in restored.issues],
            [(issue.code, issue.message) for issue in error.issues],
        )
        self.assertEqual(error.args, ("Validation failed",))

        issue = Issue("custom", "x", params={"values": [1]})
        self.assertEqual(pickle.loads(pickle.dumps(issue)), issue)
        self.assertEqual(copy.deepcopy(issue), issue)
        self.assertEqual(copy.deepcopy(error).issues, error.issues)

    def test_issue_hashing_uses_core_fields(self):
        issue = Issue("custom", "x", params={"values": [1]})
        same = Issue("custom", "x", params={"values": [1]})
        self.assertEqual(issue, same)
        self.assertEqual(hash(issue), hash(same))
        self.assertIsInstance(hash(issue), int)

    def test_issue_path_and_branches_are_tuples(self):
        issue = Issue("custom", "x", path=["a", 1])
        self.assertEqual(issue.path, ("a", 1))
        self.assertEqual(issue, Issue("custom", "x", path=("a", 1)))
        nested = Issue("custom", "y")
        parent = Issue("custom", "x", branches=[[nested]])
        self.assertEqual(parent.branches, ((nested,),))

    def test_builders_reject_non_schema_arguments(self):
        async def passthrough(value):
            return value

        cases = (
            ("union", lambda: z.union(z.string(), 1)),
            ("or_", lambda: z.string().or_(1)),
            ("pipe", lambda: z.string().pipe(1)),
            ("array", lambda: z.array(1)),
            ("tuple", lambda: z.tuple(z.string(), 1)),
            ("preprocess", lambda: z.preprocess(str, 1)),
            ("preprocess_async", lambda: z.preprocess_async(passthrough, 1)),
        )
        for name, build in cases:
            with self.subTest(builder=name), self.assertRaises(TypeError):
                build()

    def test_structured_issues_and_params(self):
        cases = [
            (z.string().min(3), "ab", "too_small",
             {"minimum": 3, "inclusive": True, "origin": "string"}),
            (z.string().max(3), "abcd", "too_big",
             {"maximum": 3, "inclusive": True, "origin": "string"}),
            (z.integer().min(18), 12, "too_small",
             {"minimum": 18, "inclusive": True, "origin": "number"}),
            (z.integer().max(18), 19, "too_big",
             {"maximum": 18, "inclusive": True, "origin": "number"}),
            (z.integer().gt(18), 18, "too_small",
             {"minimum": 18, "inclusive": False, "origin": "number"}),
            (z.integer().lt(18), 18, "too_big",
             {"maximum": 18, "inclusive": False, "origin": "number"}),
            (z.integer().multiple_of(3), 10, "not_multiple_of",
             {"divisor": 3, "origin": "number"}),
            (z.number().positive(), 0, "too_small",
             {"minimum": 0, "inclusive": False, "origin": "number"}),
            (z.number().int(), 1.5, "invalid_type", {"expected": "integer"}),
            (z.string().email(), "bad", "invalid_format", {"format": "email"}),
            (z.string().starts_with("ab"), "xab", "invalid_format",
             {"format": "starts_with", "prefix": "ab"}),
            (z.string().ends_with("bc"), "abx", "invalid_format",
             {"format": "ends_with", "suffix": "bc"}),
            (z.string().includes("bc"), "axc", "invalid_format",
             {"format": "includes", "substring": "bc"}),
            (z.string().ipv6(), "127.0.0.1", "invalid_format", {"format": "ipv6"}),
            (z.string().regex(r"^a+$"), "b", "invalid_format", {"format": "regex"}),
            (z.literal("a"), "b", "invalid_value", {"values": ("a",)}),
            (z.enum(["a", "b"]), "c", "invalid_value", {"values": ("a", "b")}),
            (z.integer().array().min(2), [1], "too_small",
             {"minimum": 2, "inclusive": True, "origin": "array"}),
            (z.tuple(z.integer()), [], "invalid_length", {"expected": 1}),
            (z.object({}), [], "invalid_type", {"expected": "dict with string keys"}),
            (z.record(z.integer()), {1: 1}, "invalid_key", {"expected": "string"}),
        ]
        for schema, value, code, params in cases:
            with self.subTest(code=code):
                result = schema.safe_parse(value)
                self.assertIsInstance(result, Failure)
                issue = result.error.issues[0]
                self.assertEqual(issue.code, code)
                self.assertEqual(issue.params, params)
                json.dumps(issue.to_dict())
        result = z.string().safe_parse()
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.issues[0].code, "required")

    def test_message_overrides(self):
        cases = [
            (z.string().min(3, message="Too short"), "ab", "Too short"),
            (z.string().max(3, message="Too long"), "abcd", "Too long"),
            (z.string().email(message="Bad email"), "x", "Bad email"),
            (z.string().regex(r"^a+$", "Only a"), "b", "Only a"),
            (z.string().starts_with("a", message="Prefix"), "b", "Prefix"),
            (z.string().ends_with("a", message="Suffix"), "b", "Suffix"),
            (z.string().includes("a", message="Substring"), "b", "Substring"),
            (z.string().ipv6(message="Bad IPv6"), "x", "Bad IPv6"),
            (z.integer().min(5, message="Too small"), 4, "Too small"),
            (z.integer().max(5, message="Too big"), 6, "Too big"),
            (z.integer().gt(5, message="Greater"), 5, "Greater"),
            (z.integer().lt(5, message="Less"), 5, "Less"),
            (z.integer().multiple_of(2, message="Even only"), 3, "Even only"),
            (z.integer().positive(message="Positive only"), 0, "Positive only"),
            (z.integer().nonnegative(message="Nonnegative only"), -1, "Nonnegative only"),
            (z.number().int(message="Integer only"), 1.5, "Integer only"),
            (z.integer().array().min(2, message="Need two"), [1], "Need two"),
            (z.integer().array().max(1, message="Too many"), [1, 2], "Too many"),
            (z.integer().array().nonempty(message="Empty"), [], "Empty"),
        ]
        for schema, value, message in cases:
            with self.subTest(message=message):
                result = schema.safe_parse(value)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].message, message)

    def test_enum_reports_one_invalid_value_issue(self):
        result = z.enum(["a", "b"]).safe_parse("c")
        self.assertIsInstance(result, Failure)
        self.assertEqual(len(result.error.issues), 1)
        issue = result.error.issues[0]
        self.assertEqual(issue.code, "invalid_value")
        self.assertEqual(issue.params["values"], ("a", "b"))
        self.assertEqual(issue.message, "Expected one of: 'a', 'b'")
        for schema, value in (
            (z.enum([True]), 1),
            (z.enum([1]), True),
            (z.enum([1]), 1.0),
        ):
            self.assertIsInstance(schema.safe_parse(value), Failure)

    def test_string_formats(self):
        cases = (
            (z.string().url(), "https://example.com/path?q=1", "uri"),
            (
                z.string().uuid(),
                "123e4567-e89b-12d3-a456-426614174000",
                "uuid",
            ),
            (z.string().date(), "2026-10-20", "date"),
            (z.string().time(), "12:30:00", "time"),
            (z.string().datetime(), "2026-10-20T12:30:00Z", "date-time"),
            (z.string().ipv4(), "192.168.0.1", "ipv4"),
            (z.string().ipv6(), "2001:db8::1", "ipv6"),
            (z.string().ipv6(), "::ffff:192.0.2.1", "ipv6"),
            (z.string().hostname(), "example.com", "hostname"),
        )
        for schema, value, format_name in cases:
            with self.subTest(format=format_name):
                self.assertEqual(schema.parse(value), value)
                self.assertEqual(
                    schema.to_json_schema(),
                    {"type": "string", "format": format_name},
                )
        invalid = (
            (z.string().url(), "example.com"),
            (z.string().uuid(), "123e4567"),
            (z.string().date(), "20-10-2026"),
            (z.string().time(), "1230"),
            (z.string().datetime(), "2026-10-20"),
            (z.string().ipv4(), "999.1.1.1"),
            (z.string().ipv6(), "192.168.0.1"),
            (z.string().ipv6(), "gggg::1"),
            (z.string().ipv6(), "[::1]"),
            (z.string().hostname(), "-bad.com"),
        )
        for schema, value in invalid:
            with self.subTest(value=value):
                result = schema.safe_parse(value)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].code, "invalid_format")
        self.assertEqual(
            z.string().url(message="Bad URL").safe_parse("x").error.issues[0].message,
            "Bad URL",
        )

    def test_additional_string_formats(self):
        cases = (
            (z.string().ulid(), "01ARZ3NDEKTSV4RRFFQ69G5FAV", "ulid"),
            (z.string().cuid(), "cjld2cjxh0000qzrmn831i7rn", "cuid"),
            (z.string().cuid2(), "tz4a98xxat96iws9zmbrgj3a", "cuid2"),
            (z.string().nanoid(), "V1StGXR8_Z5jdHi6B-myT", "nanoid"),
            (
                z.string().jwt(),
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
                "jwt",
            ),
            (z.string().e164(), "+14155552671", "e164"),
        )
        for schema, value, format_name in cases:
            with self.subTest(format=format_name):
                self.assertEqual(schema.parse(value), value)
                self.assertEqual(
                    schema.to_json_schema(),
                    {"type": "string", "format": format_name},
                )

        invalid = (
            (z.string().ulid(), "01ARZ3NDEKTSV4RRFFQ69G5FA"),
            (z.string().ulid(), "81ARZ3NDEKTSV4RRFFQ69G5FAV"),
            (z.string().cuid(), "not-a-cuid"),
            (z.string().cuid2(), "1notacuid2"),
            (z.string().nanoid(), "short"),
            (z.string().jwt(), "only.two"),
            (z.string().e164(), "14155552671"),
            (z.string().e164(), "+0123"),
        )
        for schema, value in invalid:
            with self.subTest(value=value):
                result = schema.safe_parse(value)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].code, "invalid_format")

        for schema, value in (
            (z.string().base64(), "aGVsbG8="),
            (z.string().base64url(), "aGVsbG8"),
        ):
            with self.subTest(value=value):
                self.assertEqual(schema.parse(value), value)
                self.assertEqual(
                    schema.to_json_schema(),
                    {"type": "string", "pattern": schema._node.args[0].pattern},
                )
        for schema, value in (
            (z.string().base64(), "not base64!"),
            (z.string().base64url(), "a+b/"),
        ):
            with self.subTest(value=value):
                self.assertIsInstance(schema.safe_parse(value), Failure)

    def test_set_and_map(self):
        values = z.set(z.integer().positive())
        self.assertEqual(values.parse({1, 2}), {1, 2})
        self.assertEqual(values.unwrap().parse(3), 3)

        wrong_type = values.safe_parse([1, 2])
        self.assertIsInstance(wrong_type, Failure)
        self.assertEqual(wrong_type.error.issues[0].code, "invalid_type")
        self.assertEqual(wrong_type.error.issues[0].params["expected"], "set")

        bad_item = values.safe_parse({0})
        self.assertIsInstance(bad_item, Failure)
        self.assertEqual(bad_item.error.issues[0].path, (0,))

        missing = values.safe_parse()
        self.assertIsInstance(missing, Failure)
        self.assertEqual(missing.error.issues[0].code, "required")

        collapsed = z.set(z.integer().transform(lambda value: value % 2))
        self.assertEqual(collapsed.parse({1, 3, 4}), {0, 1})
        self.assertEqual(
            z.object({"values": values}).parse({"values": {1, 2}}),
            {"values": {1, 2}},
        )
        with self.assertRaises(TypeError):
            z.set(z.integer().transform(lambda value: [value])).parse({1})

        mutable = {1, 2}

        def mutate(value):
            mutable.clear()
            return True

        self.assertEqual(
            z.set(z.integer().refine(mutate)).parse(mutable), {1, 2},
        )

        mapping = z.map(z.string().min(1), z.integer())
        self.assertEqual(mapping.parse({"a": 1}), {"a": 1})
        self.assertEqual(mapping.unwrap().parse(2), 2)
        bad_key = mapping.safe_parse({"": 1})
        self.assertIsInstance(bad_key, Failure)
        self.assertEqual(bad_key.error.issues[0].code, "too_small")
        bad_value = mapping.safe_parse({"a": "x"})
        self.assertIsInstance(bad_value, Failure)
        self.assertEqual(bad_value.error.issues[0].path, ("a",))
        self.assertIsInstance(mapping.safe_parse([("a", 1)]), Failure)
        self.assertIsInstance(mapping.safe_parse(), Failure)

        with self.assertRaises(TypeError):
            z.set("bad")
        with self.assertRaises(TypeError):
            z.map("bad", z.integer())
        with self.assertRaises(TypeError):
            z.map(z.string(), "bad")

        self.assertEqual(
            z.set(z.integer()).min(1).max(3).to_json_schema(),
            {
                "type": "array",
                "items": {"type": "integer"},
                "uniqueItems": True,
                "minItems": 1,
                "maxItems": 3,
            },
        )
        self.assertEqual(
            z.map(z.string(), z.integer()).to_json_schema(),
            {
                "type": "object",
                "propertyNames": {"type": "string"},
                "additionalProperties": {"type": "integer"},
            },
        )

        encoded_set = z.set(z.integer()).encode({1, 2})
        self.assertIsInstance(encoded_set, list)
        self.assertEqual(set(encoded_set), {1, 2})
        self.assertEqual(
            z.map(z.string(), z.integer()).encode({"a": 1}), {"a": 1},
        )
        codec = z.codec(z.string(), z.integer(), decode=int, encode=str)
        self.assertEqual(
            set(z.set(codec).encode({1, 2})), {"1", "2"},
        )
        self.assertEqual(
            z.map(z.string(), codec).encode({"a": 1}), {"a": "1"},
        )
        with self.assertRaises(TypeError):
            z.set(z.integer().transform(lambda value: value + 1)).encode({1})
        with self.assertRaises(TypeError):
            z.map(
                z.string(), z.integer().transform(lambda value: value + 1),
            ).encode({"a": 1})

    def test_set_and_bytes_lengths(self):
        values = z.set(z.integer()).min(2).max(3)
        self.assertEqual(values.parse({1, 2}), {1, 2})
        too_small = values.safe_parse({1})
        self.assertIsInstance(too_small, Failure)
        self.assertEqual(too_small.error.issues[0].code, "too_small")
        self.assertEqual(too_small.error.issues[0].params, {
            "minimum": 2, "inclusive": True, "origin": "set",
        })
        too_big = values.safe_parse({1, 2, 3, 4})
        self.assertIsInstance(too_big, Failure)
        self.assertEqual(too_big.error.issues[0].code, "too_big")
        self.assertEqual(too_big.error.issues[0].params, {
            "maximum": 3, "inclusive": True, "origin": "set",
        })
        self.assertEqual(z.set(z.string()).length(2).parse({"a", "b"}), {"a", "b"})
        self.assertIsInstance(z.set(z.string()).length(2).safe_parse({"a"}), Failure)
        with self.assertRaises(ValueError):
            z.set(z.integer()).min(-1)

        data = z.bytes().min(2).max(3)
        self.assertEqual(data.parse(b"ab"), b"ab")
        too_small = data.safe_parse(b"a")
        self.assertIsInstance(too_small, Failure)
        self.assertEqual(too_small.error.issues[0].params, {
            "minimum": 2, "inclusive": True, "origin": "bytes",
        })
        too_big = data.safe_parse(b"abcd")
        self.assertIsInstance(too_big, Failure)
        self.assertEqual(too_big.error.issues[0].params, {
            "maximum": 3, "inclusive": True, "origin": "bytes",
        })
        exact = z.bytes().length(2)
        self.assertEqual(exact.parse(b"ab"), b"ab")
        self.assertEqual(exact.encode(b"ab"), b"ab")
        with self.assertRaises(ValueError):
            z.bytes().length(1.5)  # type: ignore[arg-type]

    def test_wire_names(self):
        schema = z.object({
            "user_name": z.string().min(1).wire("userName"),
            "created_at": z.codecs.iso_datetime().wire("createdAt"),
        }).strict()
        value = schema.parse({
            "userName": "Alex", "createdAt": "2026-10-21T12:00:00",
        })
        self.assertEqual(value, {
            "user_name": "Alex",
            "created_at": dt.datetime(2026, 10, 21, 12, 0),
        })
        self.assertEqual(schema.encode(value), {
            "userName": "Alex", "createdAt": "2026-10-21T12:00:00",
        })
        self.assertEqual(
            schema.parse_json('{"userName": "Alex", "createdAt": "2026-10-21T12:00:00"}'),
            value,
        )
        wrong = schema.safe_parse({
            "user_name": "Alex", "createdAt": "2026-10-21T12:00:00",
        })
        self.assertIsInstance(wrong, Failure)
        self.assertIn(
            "unrecognized_key", [issue.code for issue in wrong.error.issues],
        )
        path_failure = z.object({
            "user_name": z.string().min(3).wire("userName"),
        }).safe_parse({"userName": "x"})
        self.assertIsInstance(path_failure, Failure)
        self.assertEqual(path_failure.error.issues[0].path, ("userName",))

        stripped = z.object({"name": z.string().wire("displayName")})
        self.assertEqual(stripped.parse({"displayName": "Alex", "name": "x"}), {
            "name": "Alex",
        })
        passthrough = stripped.passthrough()
        self.assertEqual(
            passthrough.parse({"displayName": "Alex", "name": "shadow"}),
            {"name": "Alex"},
        )
        self.assertEqual(
            passthrough.parse({"displayName": "Alex", "extra": 1}),
            {"name": "Alex", "extra": 1},
        )
        catchall = z.object({
            "name": z.string().wire("displayName"),
        }).catchall(z.integer())
        self.assertEqual(
            catchall.parse({"displayName": "Alex", "extra": 1}),
            {"name": "Alex", "extra": 1},
        )
        self.assertEqual(
            catchall.parse({"displayName": "Alex", "name": 2}),
            {"name": "Alex"},
        )

        optional = z.object({
            "name": z.string().wire("displayName").optional(),
        })
        self.assertEqual(optional.parse({}), {})
        self.assertEqual(optional.parse({"displayName": "Alex"}), {"name": "Alex"})
        self.assertEqual(
            z.object({
                "name": z.string().optional().wire("displayName"),
            }).parse({"displayName": "Alex"}),
            {"name": "Alex"},
        )
        partial = z.object({
            "name": z.string().min(2).wire("displayName"),
        }).partial()
        self.assertEqual(partial.parse({}), {})
        self.assertEqual(partial.parse({"displayName": "Alex"}), {"name": "Alex"})
        self.assertEqual(partial.keyof().parse("name"), "name")
        self.assertIsInstance(partial.keyof().safe_parse("displayName"), Failure)

        with self.assertRaises(TypeError):
            z.string().wire(1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            z.string().wire("a").wire("b")
        with self.assertRaises(ValueError):
            z.object({
                "a": z.string().wire("x"), "b": z.string().wire("x"),
            })
        with self.assertRaises(ValueError):
            z.object({"a": z.string().wire("b"), "b": z.string()})
        with self.assertRaises(ValueError):
            z.discriminated_union("kind", {
                "a": z.object({"kind": z.string().wire("type")}),
            })

        codec = z.codec(z.string().min(1), z.integer(), decode=int, encode=str)
        wired_codec = z.object({"count": codec.wire("countValue")})
        self.assertEqual(wired_codec.parse({"countValue": "3"}), {"count": 3})
        self.assertEqual(wired_codec.encode({"count": 3}), {"countValue": "3"})

        plain = z.object({
            "user_name": z.string().wire("userName"),
            "age": z.integer().wire("userAge"),
        })
        self.assertEqual(plain.to_json_schema(io="input"), {
            "type": "object",
            "properties": {
                "userName": {"type": "string"},
                "userAge": {"type": "integer"},
            },
            "required": ["userName", "userAge"],
        })
        self.assertEqual(plain.to_json_schema(io="output"), {
            "type": "object",
            "properties": {
                "user_name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["user_name", "age"],
            "additionalProperties": False,
        })

        class User(TypedDict):
            user_name: str

        typed = z.typed_object(User, {
            "user_name": z.string().optional().wire("userName"),
        })
        required = typed.safe_parse({})
        self.assertIsInstance(required, Failure)
        self.assertEqual(required.error.issues[0].code, "required")
        self.assertEqual(required.error.issues[0].path, ("user_name",))

    def test_typed_object_and_brand(self):
        class User(TypedDict):
            name: str
            age: int
            nickname: NotRequired[str]

        schema = z.typed_object(User, {
            "name": z.string().min(1),
            "age": z.integer().gte(0),
        })
        self.assertIsInstance(schema, z.TypedObjectSchema)
        self.assertEqual(
            schema.parse({"name": "Alex", "age": 30}),
            {"name": "Alex", "age": 30},
        )
        self.assertEqual(
            schema.safe_parse({"name": "", "age": 30}).error.issues[0].code,
            "too_small",
        )
        self.assertEqual(
            to_json_schema(schema),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "age": {"type": "integer", "minimum": 0},
                },
                "required": ["name", "age"],
                "additionalProperties": False,
            },
        )

        strict = schema.strict()
        self.assertIsInstance(strict, z.TypedObjectSchema)
        self.assertIsInstance(
            strict.safe_parse({"name": "Alex", "age": 30, "extra": 1}),
            Failure,
        )
        self.assertIsInstance(schema.partial(), z.ObjectSchema)
        self.assertEqual(schema.required().parse({"name": "Alex", "age": 30}), {
            "name": "Alex", "age": 30,
        })

        class Partial(TypedDict, total=False):
            name: str

        self.assertEqual(z.typed_object(Partial, {}).parse({}), {})

        class Mixed(TypedDict, total=False):
            id: Required[int]
            name: str

        mixed = z.typed_object(Mixed, {"id": z.integer()})
        self.assertEqual(mixed.parse({"id": 1}), {"id": 1})

        with self.assertRaises(TypeError):
            z.typed_object(dict, {})
        with self.assertRaises(ValueError):
            z.typed_object(User, {"name": z.string()})
        with self.assertRaises(ValueError):
            z.typed_object(User, {
                "name": z.string(),
                "age": z.integer(),
                "extra": z.string(),
            })
        with self.assertRaises(TypeError):
            z.typed_object(User, {"name": "bad", "age": z.integer()})

        UserId = NewType("UserId", int)
        base = z.integer().positive()
        branded = base.brand(UserId)
        self.assertEqual(branded.parse(3), 3)
        self.assertIsInstance(branded.safe_parse(0), Failure)
        self.assertIs(branded.unwrap(), base)
        with self.assertRaises(TypeError):
            z.integer().brand("bad")
        with self.assertRaises(TypeError):
            z.integer().brand(lambda value: value)

        codec = z.codec(z.string(), z.integer(), decode=int, encode=str)
        branded_codec = codec.brand(UserId)
        self.assertEqual(branded_codec.decode("3"), 3)
        self.assertEqual(branded_codec.encode(3), "3")

    def test_typed_object_required_output(self):
        class User(TypedDict, total=False):
            age: Required[int]
            nickname: str

        field = z.integer().optional()
        schema = z.typed_object(User, {
            "age": field, "nickname": z.string().optional(),
        }).describe("User")
        for value in ({}, {"age": MISSING}):
            failure = schema.safe_parse(value)
            self.assertIsInstance(failure, Failure)
            self.assertEqual(
                [(issue.code, issue.path) for issue in failure.error.issues],
                [("required", ("age",))],
            )
        self.assertEqual(schema.parse({"age": 1}), {"age": 1})
        self.assertEqual(schema.parse({"age": 1, "nickname": "x"}), {
            "age": 1, "nickname": "x",
        })
        self.assertIsInstance(schema.safe_parse({"age": None}), Failure)
        self.assertIs(field.parse(), MISSING)
        self.assertEqual(z.object({"age": field}).parse({}), {})
        self.assertEqual(schema.partial().parse({}), {})
        self.assertEqual(schema.extend({"age": field}).parse({}), {})
        for rebuilt in (
            schema.strict(), schema.strip(), schema.passthrough(), schema.catchall(z.integer()),
        ):
            with self.subTest(policy=rebuilt.unknown_keys):
                self.assertIsInstance(rebuilt, z.TypedObjectSchema)
                self.assertEqual(rebuilt.metadata, {"description": "User"})
                self.assertIsInstance(rebuilt.safe_parse({}), Failure)
                self.assertEqual(rebuilt.parse({"age": 1}), {"age": 1})

        defaults = z.typed_object(User, {"age": z.integer().default(7)})
        self.assertEqual(defaults.parse({}), {"age": 7})
        self.assertEqual(defaults.strict().parse({}), {"age": 7})
        factory_calls = []
        factory = z.typed_object(User, {
            "age": z.integer().default_factory(lambda: factory_calls.append(1) or 8),
        })
        self.assertEqual(factory.parse({}), {"age": 8})
        self.assertEqual(factory_calls, [1])
        self.assertEqual(factory.parse({"age": 9}), {"age": 9})
        self.assertEqual(factory_calls, [1])
        self.assertEqual(
            z.typed_object(User, {"age": z.integer().nullable()}).parse({"age": None}),
            {"age": None},
        )
        transformed = z.typed_object(User, {
            "age": z.integer().transform(lambda _: MISSING),
        })
        self.assertIsInstance(transformed.safe_parse({"age": 1}), Failure)
        nested = z.object({"user": schema, "label": z.string()})
        failure = nested.safe_parse({"user": {}, "label": 1})
        self.assertEqual(
            [(issue.code, issue.path) for issue in failure.error.issues],
            [("required", ("user", "age")), ("invalid_type", ("label",))],
        )
        json.dumps(failure.error.to_dict())
        for data in ('{}', '{"age": 1}', '{"age": null}'):
            parsed_json = schema.safe_parse_json(data)
            parsed_python = schema.safe_parse(json.loads(data))
            if isinstance(parsed_python, Failure):
                self.assertEqual(parsed_json.error.to_dict(), parsed_python.error.to_dict())
            else:
                self.assertEqual(parsed_json.data, parsed_python.data)

    def test_typed_object_postponed_key_qualifiers(self):
        namespace = {"__name__": __name__}
        exec(
            "from __future__ import annotations\n"
            "from typing import Annotated, NotRequired, Required, TypedDict\n"
            "class Mixed(TypedDict, total=False):\n"
            "    age: Required[int]\n"
            "    nullable: Annotated[Required[int | None], 'metadata']\n"
            "    nickname: NotRequired[str]\n"
            "    unresolved: MissingValueType\n"
            "class Total(TypedDict):\n"
            "    age: int\n"
            "    nickname: NotRequired[str]\n",
            namespace,
        )
        schema = z.typed_object(namespace["Mixed"], {
            "age": z.integer().optional(),
            "nullable": z.integer().nullable().optional(),
        })
        failure = schema.safe_parse({})
        self.assertEqual([issue.path for issue in failure.error.issues], [
            ("age",), ("nullable",),
        ])
        self.assertEqual(schema.parse({"age": 1, "nullable": None}), {
            "age": 1, "nullable": None,
        })
        self.assertEqual(
            z.typed_object(namespace["Total"], {"age": z.integer()}).parse({"age": 1}),
            {"age": 1},
        )
        with self.assertRaisesRegex(ValueError, "missing required keys"):
            z.typed_object(namespace["Mixed"], {"nullable": z.integer().nullable()})

    def test_typed_object_required_output_json_schema(self):
        class Count(TypedDict):
            value: int

        cases = (
            (z.integer().optional(), False),
            (z.integer().optional().nullable(), False),
            (z.integer().default(1), True),
            (z.integer().optional().default(1), True),
            (z.integer().default(1).optional(), False),
            (z.union(z.integer().optional(), z.integer().default(1)), False),
            (z.union(z.integer().default(1), z.integer().optional()), True),
        )
        for field, accepts_missing in cases:
            with self.subTest(field=field):
                schema = z.typed_object(Count, {"value": field})
                result = schema.safe_parse({})
                if accepts_missing:
                    self.assertEqual(result.data, {"value": 1})
                else:
                    self.assertIsInstance(result, Failure)
                for target in ("draft-2020-12", "openapi-3.0"):
                    output = schema.to_json_schema(io="output", target=target)
                    self.assertEqual(output["required"], ["value"])
                    input_schema = schema.to_json_schema(io="input", target=target)
                    self.assertEqual(
                        input_schema.get("required", []),
                        [] if accepts_missing else ["value"],
                    )
        field = z.integer().min(0).default(5).describe("Count")
        schema = z.typed_object(Count, {"value": field})
        self.assertEqual(schema.to_json_schema()["properties"]["value"], {
            "type": "integer", "minimum": 0, "default": 5, "description": "Count",
        })
        codec = z.codec(z.string(), z.integer().min(0), decode=int, encode=str)
        schema = z.typed_object(Count, {"value": codec})
        self.assertEqual(schema.parse({"value": "2"}), {"value": 2})
        self.assertEqual(schema.to_json_schema(io="input")["properties"]["value"], {
            "type": "string",
        })
        self.assertEqual(schema.to_json_schema(io="output")["properties"]["value"], {
            "type": "integer", "minimum": 0,
        })

    def test_string_predicates(self):
        schema = z.string().starts_with("ab").ends_with("yz").includes("cd")
        self.assertEqual(schema.parse("abcdyz"), "abcdyz")
        for value in ("xabcdyz", "abcdy", "abyz", ""):
            with self.subTest(value=value):
                self.assertIsInstance(schema.safe_parse(value), Failure)
        self.assertEqual(z.string().trim().starts_with("a").parse(" a "), "a")
        self.assertEqual(z.string().upper().ends_with("B").parse("ab"), "AB")
        self.assertEqual(z.string().includes("").parse("anything"), "anything")
        self.assertEqual(z.string().starts_with("😀").parse("😀x"), "😀x")
        self.assertEqual(z.string().includes("é").parse("xéy"), "xéy")
        result = z.string().starts_with("ab").safe_parse("xab")
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.issues[0].message, "Must start with 'ab'")

    def test_config_formats(self):
        duration = z.string().duration()
        for value in ("PT15M", "P1D", "P2W", "-PT0.5S", "P1DT2H3M4.5S"):
            self.assertEqual(duration.parse(value), value)
        for value in ("P", "P1Y", "P1M", "PT", "15M", "P1.5D", "P1WT1H"):
            result = duration.safe_parse(value)
            self.assertIsInstance(result, Failure)
            self.assertEqual(result.error.issues[0].code, "invalid_format")
            self.assertEqual(result.error.issues[0].params["format"], "duration")

        http_url = z.string().http_url()
        for value in ("http://example.com", "HTTPS://EXAMPLE.COM/x"):
            self.assertEqual(http_url.parse(value), value)
        for value in ("ftp://example.com", "http://", "example.com", "http://a b"):
            result = http_url.safe_parse(value)
            self.assertIsInstance(result, Failure)
            self.assertEqual(result.error.issues[0].params["format"], "http_url")

        hex_schema = z.string().hex()
        for value in ("deadBEEF", "0", "abc"):
            self.assertEqual(hex_schema.parse(value), value)
        for value in ("", "xyz", "12 34", "0x12"):
            result = hex_schema.safe_parse(value)
            self.assertIsInstance(result, Failure)
            self.assertEqual(result.error.issues[0].params["format"], "hex")

        cidrv4 = z.string().cidrv4()
        for value in ("10.0.0.0/8", "0.0.0.0/0", "192.168.1.1/32"):
            self.assertEqual(cidrv4.parse(value), value)
        for value in (
            "10.0.0.0", "10.0.0.0/33", "10.0.0.01/8",
            "10.0.0.0/08", "10.0.0.0/8/9",
        ):
            result = cidrv4.safe_parse(value)
            self.assertIsInstance(result, Failure)
            self.assertEqual(result.error.issues[0].code, "invalid_format")
            self.assertEqual(result.error.issues[0].params["format"], "cidrv4")

        cidrv6 = z.string().cidrv6()
        for value in ("2001:db8::/32", "::/0", "::1/128", "::ffff:192.0.2.0/120"):
            self.assertEqual(cidrv6.parse(value), value)
        for value in ("2001:db8::", "::/129", "gg::/1", "::/0128"):
            result = cidrv6.safe_parse(value)
            self.assertIsInstance(result, Failure)
            self.assertEqual(result.error.issues[0].params["format"], "cidrv6")

        self.assertEqual(
            z.string().cidrv4(message="Bad CIDR").safe_parse("x")
            .error.issues[0].message,
            "Bad CIDR",
        )
        self.assertEqual(cidrv4.encode("10.0.0.0/8"), "10.0.0.0/8")
        self.assertEqual(
            cidrv4.to_json_schema(), {"type": "string", "format": "cidrv4"},
        )
        self.assertEqual(
            cidrv6.to_json_schema(), {"type": "string", "format": "cidrv6"},
        )
        for schema in (duration, http_url, hex_schema):
            self.assertIn("pattern", schema.to_json_schema())

    def test_python_scalars(self):
        day = dt.date(2026, 10, 20)
        moment = dt.datetime(2026, 10, 20, 12, 30)
        clock = dt.time(12, 30)
        delta = dt.timedelta(days=1)
        amount = decimal.Decimal("1.50")
        self.assertEqual(z.date().parse(day), day)
        self.assertIsInstance(z.date().safe_parse(moment), Failure)
        self.assertIsInstance(z.date().safe_parse("2026-10-20"), Failure)
        self.assertEqual(z.datetime().parse(moment), moment)
        self.assertIsInstance(z.datetime().safe_parse(day), Failure)
        self.assertEqual(z.time().parse(clock), clock)
        self.assertIsInstance(z.time().safe_parse(moment), Failure)
        self.assertEqual(z.timedelta().parse(delta), delta)
        self.assertEqual(z.decimal().parse(amount), amount)
        self.assertIsInstance(z.decimal().safe_parse(1.5), Failure)
        self.assertEqual(z.bytes().parse(b"abc"), b"abc")
        self.assertIsInstance(z.bytes().safe_parse(bytearray(b"abc")), Failure)
        path = pathlib.Path("/tmp/example")
        self.assertEqual(z.path().parse(path), path)
        self.assertEqual(
            z.path().parse(pathlib.PurePath("x")), pathlib.PurePath("x"),
        )
        self.assertIsInstance(z.path().safe_parse("/tmp/example"), Failure)

        payload = {"a": [1, True, None, 1.5, "x"]}
        self.assertEqual(z.json().parse(payload), payload)
        for bad in ({1: "x"}, {1, 2}, float("nan"), object()):
            self.assertIsInstance(z.json().safe_parse(bad), Failure)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(RecursionError):
            z.json().safe_parse(cyclic)

        class Color(enum.Enum):
            RED = "red"
            BLUE = "blue"

        self.assertIs(z.native_enum(Color).parse(Color.RED), Color.RED)
        self.assertIsInstance(z.native_enum(Color).safe_parse("red"), Failure)
        self.assertEqual(
            z.native_enum(Color).to_json_schema(),
            {"enum": ["red", "blue"]},
        )
        with self.assertRaises(TypeError):
            z.native_enum("not an enum")
        with self.assertRaises(ValueError):
            class Empty(enum.Enum):
                pass

            z.native_enum(Empty)

    def test_coercion(self):
        self.assertEqual(z.coerce.string().parse("x"), "x")
        self.assertEqual(z.coerce.string().parse(3), "3")
        self.assertEqual(z.coerce.string().parse(3.5), "3.5")
        for bad in (True, None, [1]):
            self.assertIsInstance(z.coerce.string().safe_parse(bad), Failure)

        self.assertEqual(z.coerce.number().parse("3"), 3)
        self.assertEqual(z.coerce.number().parse("3.5"), 3.5)
        self.assertEqual(z.coerce.number().parse(" 42 "), 42)
        self.assertEqual(z.coerce.number().parse(3), 3)
        self.assertEqual(z.coerce.number().parse(3.5), 3.5)
        huge = "1" + "0" * 1000
        self.assertEqual(z.coerce.number().parse(huge), int(huge))
        for bad in (True, None, "x", "nan", "inf", ""):
            self.assertIsInstance(z.coerce.number().safe_parse(bad), Failure)

        self.assertEqual(z.coerce.integer().parse("3"), 3)
        self.assertEqual(z.coerce.integer().parse(3), 3)
        self.assertEqual(z.coerce.integer().parse(3.0), 3)
        for bad in ("3.5", 3.5, True, None):
            self.assertIsInstance(z.coerce.integer().safe_parse(bad), Failure)

        for value, expected in (
            (True, True),
            (False, False),
            (1, True),
            (0, False),
            ("true", True),
            ("FALSE", False),
            (" yes ", True),
        ):
            with self.subTest(value=value):
                self.assertIs(z.coerce.boolean().parse(value), expected)
        for bad in (2, None, "maybe"):
            self.assertIsInstance(z.coerce.boolean().safe_parse(bad), Failure)

        self.assertIsInstance(z.coerce.number().safe_parse(), Failure)
        self.assertEqual(z.coerce.number().min(5).parse("10"), 10)
        self.assertIsInstance(z.coerce.number().min(5).safe_parse("3"), Failure)
        self.assertEqual(z.coerce.number().default(5).parse(), 5)
        self.assertEqual(
            z.coerce.number().meta({"title": "Number"}).metadata["title"],
            "Number",
        )

    def test_stringbool(self):
        schema = z.string().stringbool()
        self.assertIs(schema.parse("true"), True)
        self.assertIs(schema.parse("FALSE"), False)
        self.assertIs(schema.parse("1"), True)
        self.assertIs(schema.parse("0"), False)
        self.assertIs(z.string().trim().stringbool().parse(" yes "), True)
        for bad in ("maybe", "2", ""):
            self.assertIsInstance(schema.safe_parse(bad), Failure)
        self.assertIsInstance(schema.safe_parse(), Failure)
        result = schema.safe_parse("maybe")
        self.assertEqual(result.error.issues[0].code, "invalid_value")
        self.assertEqual(
            result.error.issues[0].params["values"],
            ("true", "1", "yes", "on", "false", "0", "no", "off"),
        )

        custom = z.string().stringbool(
            truthy=("Y",), falsy=("N",), case_sensitive=True,
        )
        self.assertIs(custom.parse("Y"), True)
        self.assertIs(custom.parse("N"), False)
        self.assertIsInstance(custom.safe_parse("y"), Failure)
        insensitive = z.string().stringbool(truthy=("Y",), falsy=("N",))
        self.assertIs(insensitive.parse("y"), True)
        self.assertEqual(
            z.string().stringbool(message="Not a boolean").safe_parse("x").error
            .issues[0].message,
            "Not a boolean",
        )
        with self.assertRaises(ValueError):
            z.string().stringbool(truthy=("a",), falsy=("A",))
        with self.assertRaises(ValueError):
            z.string().stringbool(truthy=(), falsy=("no",))
        with self.assertRaises(TypeError):
            z.string().stringbool(truthy=(1,))
        with self.assertRaises(TypeError):
            z.string().stringbool(case_sensitive=1)

    def test_codec_decoding_and_encoding(self):
        integer_text = z.codec(
            z.string().regex(r"^\d+$"),
            z.integer().positive(),
            decode=int,
            encode=str,
        )
        self.assertIsInstance(integer_text, z.CodecSchema)
        self.assertIsInstance(integer_text.input_schema, z.StringSchema)
        self.assertIsInstance(integer_text.output_schema, z.NumberSchema)
        self.assertEqual(integer_text.parse("42"), 42)
        self.assertEqual(integer_text.decode("42"), 42)
        self.assertEqual(integer_text.encode(42), "42")
        self.assertEqual(integer_text.safe_decode("42").data, 42)
        self.assertEqual(integer_text.safe_encode(42).data, "42")

        input_bad = integer_text.safe_decode("x")
        self.assertIsInstance(input_bad, Failure)
        self.assertEqual(input_bad.error.issues[0].code, "invalid_format")
        self.assertEqual(input_bad.error.issues[0].path, ())

        output_bad = z.codec(
            z.string(), z.integer(), decode=lambda value: value, encode=str,
        ).safe_decode("42")
        self.assertIsInstance(output_bad, Failure)
        self.assertEqual(output_bad.error.issues[0].code, "invalid_type")
        self.assertEqual(output_bad.error.issues[0].params, {"expected": "integer"})

        encoded_input_bad = integer_text.safe_encode("42")
        self.assertIsInstance(encoded_input_bad, Failure)
        self.assertEqual(encoded_input_bad.error.issues[0].code, "invalid_type")

        encoded_output_bad = z.codec(
            z.string().starts_with("n:"),
            z.integer(),
            decode=lambda value: int(value[2:]),
            encode=lambda value: str(value),
        ).safe_encode(1)
        self.assertIsInstance(encoded_output_bad, Failure)
        self.assertEqual(encoded_output_bad.error.issues[0].code, "invalid_format")
        self.assertEqual(
            encoded_output_bad.error.issues[0].params["format"], "starts_with",
        )

        required = integer_text.safe_decode()
        self.assertIsInstance(required, Failure)
        self.assertEqual(required.error.issues[0].code, "required")
        with self.assertRaises(TypeError):
            z.codec("not a schema", z.integer(), decode=int, encode=str)
        with self.assertRaises(TypeError):
            z.codec(z.string(), z.integer(), decode=1, encode=str)
        with self.assertRaises(TypeError):
            z.codec(z.string(), z.integer(), decode=int, encode=1)
        with self.assertRaises(ZeroDivisionError):
            z.codec(
                z.string(),
                z.integer(),
                decode=lambda value: 1 / 0,
                encode=str,
            ).safe_decode("x")
        with self.assertRaises(ZeroDivisionError):
            z.codec(
                z.string(),
                z.integer(),
                decode=int,
                encode=lambda value: 1 / 0,
            ).safe_encode(1)

    def test_codec_nesting_and_reverse_encoding(self):
        integer_text = z.codec(
            z.string().regex(r"^\d+$"), z.integer(), decode=int, encode=str,
        )
        schema = z.object({"count": integer_text, "label": z.string()})
        decoded = schema.parse({"count": "3", "label": "x"})
        self.assertEqual(decoded, {"count": 3, "label": "x"})
        self.assertEqual(
            schema.encode(decoded), {"count": "3", "label": "x"},
        )
        bad = schema.safe_encode({"count": "3", "label": "x"})
        self.assertIsInstance(bad, Failure)
        self.assertEqual(bad.error.issues[0].path, ("count",))
        bad_decode = schema.safe_parse({"count": "x", "label": "x"})
        self.assertIsInstance(bad_decode, Failure)
        self.assertEqual(bad_decode.error.issues[0].path, ("count",))
        aggregated = z.object({"a": integer_text, "b": integer_text}).safe_encode(
            {"a": "x", "b": "y"},
        )
        self.assertIsInstance(aggregated, Failure)
        self.assertEqual(
            [issue.path for issue in aggregated.error.issues],
            [("a",), ("b",)],
        )

        optional = z.object({"count": integer_text.optional()})
        self.assertEqual(optional.parse({}), {})
        self.assertEqual(optional.encode({}), {})
        self.assertEqual(optional.encode({"count": 3}), {"count": "3"})
        self.assertEqual(
            z.object({"count": integer_text}).partial().encode({}), {},
        )
        self.assertEqual(
            z.object({"count": integer_text}).required().encode({"count": 3}),
            {"count": "3"},
        )
        self.assertIs(integer_text.optional().encode(MISSING), MISSING)
        self.assertEqual(integer_text.optional().encode(3), "3")
        self.assertIsNone(integer_text.nullable().encode(None))
        self.assertIs(integer_text.nullish().encode(MISSING), MISSING)
        self.assertIsNone(integer_text.nullish().encode(None))

        self.assertEqual(z.array(integer_text).min(2).parse(["1", "2"]), [1, 2])
        self.assertEqual(z.array(integer_text).min(2).encode([1, 2]), ["1", "2"])
        self.assertIsInstance(
            z.array(integer_text).min(2).safe_encode([1]), Failure,
        )
        self.assertEqual(
            z.tuple(integer_text, z.string()).encode((1, "x")), ("1", "x"),
        )
        self.assertEqual(z.record(integer_text).encode({"a": 1}), {"a": "1"})
        keyed = z.record(
            z.codec(z.string(), z.integer(), decode=int, encode=str),
            z.string(),
        )
        self.assertEqual(keyed.encode({1: "x"}), {"1": "x"})
        self.assertEqual(z.union(integer_text, z.string()).encode(1), "1")
        self.assertEqual(z.union(integer_text, z.string()).encode("x"), "x")
        event = z.discriminated_union("kind", {
            "set": z.object({"kind": z.literal("set"), "value": integer_text}),
        })
        self.assertEqual(
            event.encode({"kind": "set", "value": 1}),
            {"kind": "set", "value": "1"},
        )
        both = z.intersection(
            z.object({"n": integer_text}), z.object({"x": z.integer()}),
        )
        self.assertEqual(both.encode({"n": 1, "x": 2}), {"n": "1", "x": 2})
        self.assertEqual(z.object({"a": z.integer()}).encode({"a": 1}), {"a": 1})
        self.assertEqual(z.string().trim().encode("x"), "x")
        self.assertEqual(
            integer_text.meta({"title": "Count"}).encode(1), "1",
        )

        for schema, value in (
            (z.object({"x": z.string().transform(len)}), {"x": "a"}),
            (z.object({"x": z.string().default("x")}), {}),
            (z.object({"x": z.string().catch("x")}), {"x": "x"}),
            (z.array(z.string().transform(len)), ["a"]),
            (z.string().refine(lambda value: True), "x"),
            (z.string().super_refine(lambda value: None), "x"),
            (z.string().overwrite(str.upper), "x"),
            (z.coerce.number(), 1),
            (z.string().stringbool(), "true"),
            (z.preprocess(str, z.string()), "x"),
            (z.string().pipe(z.string()), "x"),
            (z.lazy(lambda: z.string()), "x"),
        ):
            with self.subTest(schema=schema), self.assertRaises(TypeError):
                schema.encode(value)

    def test_builtin_codecs(self):
        iso_date = z.codecs.iso_date()
        self.assertEqual(iso_date.decode("2026-10-20"), dt.date(2026, 10, 20))
        self.assertEqual(iso_date.encode(dt.date(2026, 10, 20)), "2026-10-20")
        for bad in ("2026-02-31", "20-10-2026", ""):
            with self.subTest(value=bad):
                result = iso_date.safe_decode(bad)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].code, "invalid_format")
                self.assertEqual(result.error.issues[0].params["format"], "date")
        self.assertEqual(
            z.codecs.iso_date(message="Bad date").safe_decode("x")
            .error.issues[0].message,
            "Bad date",
        )

        iso_time = z.codecs.iso_time()
        self.assertEqual(iso_time.decode("12:30"), dt.time(12, 30))
        self.assertEqual(iso_time.encode(dt.time(12, 30)), "12:30:00")
        self.assertEqual(
            iso_time.decode("12:30:00Z"),
            dt.time(12, 30, tzinfo=dt.UTC),
        )
        self.assertEqual(
            iso_time.encode(dt.time(12, 30, tzinfo=dt.UTC)),
            "12:30:00+00:00",
        )
        for bad in ("25:00:00", "12:60:00", "12"):
            with self.subTest(value=bad):
                result = iso_time.safe_decode(bad)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].params["format"], "time")

        iso_datetime = z.codecs.iso_datetime()
        self.assertEqual(
            iso_datetime.decode("2026-10-20T12:30:00Z"),
            dt.datetime(2026, 10, 20, 12, 30, tzinfo=dt.UTC),
        )
        self.assertEqual(
            iso_datetime.decode("2026-10-20 12:30:00"),
            dt.datetime(2026, 10, 20, 12, 30),
        )
        self.assertEqual(
            iso_datetime.encode(
                dt.datetime(2026, 10, 20, 12, 30, tzinfo=dt.UTC),
            ),
            "2026-10-20T12:30:00+00:00",
        )
        for bad in ("2026-02-31T12:00:00", "2026-10-20"):
            with self.subTest(value=bad):
                result = iso_datetime.safe_decode(bad)
                self.assertIsInstance(result, Failure)
                self.assertEqual(
                    result.error.issues[0].params["format"], "date-time",
                )

        decimal_codec = z.codecs.decimal()
        self.assertEqual(
            decimal_codec.decode("1.50"), decimal.Decimal("1.50"),
        )
        self.assertEqual(decimal_codec.encode(decimal.Decimal("1.50")), "1.50")
        self.assertEqual(
            decimal_codec.decode("1e3"), decimal.Decimal("1E+3"),
        )
        for bad in ("NaN", "Infinity", "1_000", "1.2.3", ""):
            with self.subTest(value=bad):
                result = decimal_codec.safe_decode(bad)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].params["format"], "decimal")

        json_text = z.codecs.json_text()
        self.assertEqual(
            json_text.decode('{"a": [1, true, null]}'),
            {"a": [1, True, None]},
        )
        self.assertEqual(
            json_text.encode({"a": [1, True, None]}),
            '{"a": [1, true, null]}',
        )
        invalid_json = json_text.safe_decode('{"a":')
        self.assertIsInstance(invalid_json, Failure)
        self.assertEqual(invalid_json.error.issues[0].code, "invalid_format")
        self.assertEqual(
            invalid_json.error.issues[0].params,
            {"format": "json", "position": 5},
        )
        self.assertEqual(
            z.codecs.json_text(message="Bad JSON").safe_decode("{")
            .error.issues[0].message,
            "Bad JSON",
        )
        nested_json = z.object({"data": json_text}).safe_parse({"data": '{"a":'})
        self.assertIsInstance(nested_json, Failure)
        self.assertEqual(nested_json.error.issues[0].path, ("data",))
        self.assertEqual(
            nested_json.error.issues[0].params["format"], "json",
        )
        non_finite = json_text.safe_decode("NaN")
        self.assertIsInstance(non_finite, Failure)
        self.assertEqual(non_finite.error.issues[0].code, "invalid_type")

        path_codec = z.codecs.path()
        path = pathlib.Path("/tmp/x")
        self.assertEqual(path_codec.decode("/tmp/x"), path)
        self.assertEqual(path_codec.encode(path), str(path))
        windows_path = pathlib.PureWindowsPath("/tmp/x")
        self.assertEqual(path_codec.encode(windows_path), str(windows_path))
        self.assertIsInstance(path_codec.safe_decode(1), Failure)

        base64_codec = z.codecs.bytes_base64()
        self.assertEqual(base64_codec.decode("aGVsbG8="), b"hello")
        self.assertEqual(base64_codec.decode(""), b"")
        self.assertEqual(base64_codec.encode(b"hello"), "aGVsbG8=")
        for bad in ("not base64!", "aGVsbG8", "===="):
            with self.subTest(value=bad):
                result = base64_codec.safe_decode(bad)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].params["format"], "base64")
        self.assertEqual(
            z.codecs.bytes_base64(message="Bad base64").safe_decode("!")
            .error.issues[0].message,
            "Bad base64",
        )
        urlsafe = z.codecs.bytes_base64(urlsafe=True)
        self.assertEqual(urlsafe.decode("-_8="), b"\xfb\xff")
        self.assertEqual(urlsafe.encode(b"\xfb\xff"), "-_8=")
        self.assertIsInstance(urlsafe.safe_decode("+/8="), Failure)
        self.assertEqual(
            urlsafe.safe_decode("!").error.issues[0].params["format"],
            "base64url",
        )
        with self.assertRaises(TypeError):
            z.codecs.bytes_base64(urlsafe=1)

        record = z.object({
            "created": iso_datetime,
            "amount": decimal_codec,
            "payload": z.codecs.bytes_base64(),
        })
        decoded = record.parse({
            "created": "2026-10-20T12:30:00Z",
            "amount": "1.50",
            "payload": "aGVsbG8=",
        })
        self.assertEqual(decoded["amount"], decimal.Decimal("1.50"))
        self.assertEqual(decoded["payload"], b"hello")
        self.assertEqual(
            record.encode(decoded),
            {
                "created": "2026-10-20T12:30:00+00:00",
                "amount": "1.50",
                "payload": "aGVsbG8=",
            },
        )

    def test_iso_duration_codec(self):
        duration = z.codecs.iso_duration()
        cases = {
            "PT15M": dt.timedelta(minutes=15),
            "P2W": dt.timedelta(weeks=2),
            "-PT0.5S": dt.timedelta(seconds=-0.5),
            "P1DT2H3M4.5S": dt.timedelta(
                days=1, hours=2, minutes=3, seconds=4.5,
            ),
        }
        for text, expected in cases.items():
            with self.subTest(value=text):
                self.assertEqual(duration.decode(text), expected)
                encoded = duration.encode(expected)
                self.assertEqual(duration.decode(encoded), expected)
        self.assertEqual(duration.encode(dt.timedelta()), "PT0S")
        self.assertEqual(duration.encode(dt.timedelta(days=1)), "P1D")

        for bad in ("P1Y", "P1M", "P", "PT", "15M"):
            with self.subTest(value=bad):
                result = duration.safe_decode(bad)
                self.assertIsInstance(result, Failure)
                self.assertEqual(result.error.issues[0].code, "invalid_format")
                self.assertEqual(
                    result.error.issues[0].params["format"], "duration",
                )
        self.assertEqual(
            z.codecs.iso_duration(message="Bad duration").safe_decode("x")
            .error.issues[0].message,
            "Bad duration",
        )
        self.assertEqual(
            duration.to_json_schema(io="input"),
            z.string().duration().to_json_schema(io="input"),
        )
        with self.assertRaises(UnrepresentableError):
            duration.to_json_schema(io="output")

    def test_dataclass_codec(self):
        @dataclasses.dataclass(frozen=True)
        class User:
            name: str
            created: dt.datetime
            score: int

        user_codec = z.codecs.dataclass(User, fields={
            "name": z.string().min(1),
            "created": z.codecs.iso_datetime(),
            "score": z.integer().nonnegative(),
        })
        encoded = {
            "name": "Alex",
            "created": "2026-10-20T12:30:00Z",
            "score": 3,
        }
        user = user_codec.decode(encoded)
        self.assertEqual(
            user,
            User("Alex", dt.datetime(2026, 10, 20, 12, 30, tzinfo=dt.UTC), 3),
        )
        self.assertEqual(
            user_codec.encode(user),
            {
                "name": "Alex",
                "created": "2026-10-20T12:30:00+00:00",
                "score": 3,
            },
        )
        self.assertEqual(user_codec.parse(encoded), user)
        self.assertEqual(user_codec.decode({**encoded, "extra": 1}), user)

        missing = user_codec.safe_parse({
            "name": "Alex", "created": "2026-10-20T12:30:00Z",
        })
        self.assertIsInstance(missing, Failure)
        self.assertEqual(missing.error.issues[0].path, ("score",))
        self.assertEqual(missing.error.issues[0].code, "required")

        bad_syntax = user_codec.safe_parse({
            "name": "Alex", "created": "not a date", "score": 3,
        })
        self.assertIsInstance(bad_syntax, Failure)
        self.assertEqual(bad_syntax.error.issues[0].path, ("created",))
        self.assertEqual(bad_syntax.error.issues[0].code, "invalid_format")

        bad_calendar = user_codec.safe_parse({
            "name": "Alex", "created": "2026-02-31T12:00:00", "score": 3,
        })
        self.assertIsInstance(bad_calendar, Failure)
        self.assertEqual(bad_calendar.error.issues[0].path, ("created",))
        self.assertEqual(
            bad_calendar.error.issues[0].params["format"], "date-time",
        )

        @dataclasses.dataclass(frozen=True)
        class Pair:
            a: dt.date
            b: dt.date

        pair_codec = z.codecs.dataclass(Pair, fields={
            "a": z.codecs.iso_date(),
            "b": z.codecs.iso_date(),
        })
        pair_error = pair_codec.safe_parse({
            "a": "2026-02-31", "b": "2026-04-31",
        })
        self.assertIsInstance(pair_error, Failure)
        self.assertEqual(
            [issue.path for issue in pair_error.error.issues],
            [("a",), ("b",)],
        )

        @dataclasses.dataclass
        class Count:
            n: int

        count_codec = z.codecs.dataclass(Count, fields={
            "n": z.codec(z.string(), z.integer(), decode=int, encode=str),
        })
        self.assertEqual(count_codec.encode(Count(3)), {"n": "3"})
        bad_encode = count_codec.safe_encode(Count("x"))
        self.assertIsInstance(bad_encode, Failure)
        self.assertEqual(bad_encode.error.issues[0].path, ("n",))
        self.assertEqual(bad_encode.error.issues[0].code, "invalid_type")

        @dataclasses.dataclass
        class Profile:
            user: User
            label: str

        profile_codec = z.codecs.dataclass(Profile, fields={
            "user": user_codec,
            "label": z.string(),
        })
        profile = profile_codec.parse({"user": encoded, "label": "x"})
        self.assertEqual(profile, Profile(user, "x"))
        self.assertEqual(
            profile_codec.encode(profile),
            {"user": user_codec.encode(user), "label": "x"},
        )

        @dataclasses.dataclass
        class WithInitFalse:
            value: int
            derived: int = dataclasses.field(init=False, default=2)

        init_false = z.codecs.dataclass(WithInitFalse, fields={
            "value": z.integer(),
        })
        self.assertEqual(init_false.decode({"value": 1}), WithInitFalse(1))
        self.assertEqual(init_false.encode(WithInitFalse(1)), {"value": 1})

        @dataclasses.dataclass
        class Empty:
            pass

        empty = z.codecs.dataclass(Empty, fields={})
        self.assertEqual(empty.decode({}), Empty())
        self.assertEqual(empty.encode(Empty()), {})

        @dataclasses.dataclass
        class Settings:
            name: str
            retries: int = 3

        settings_codec = z.codecs.dataclass(Settings, fields={
            "name": z.string(),
            "retries": z.integer(),
        })
        self.assertEqual(settings_codec.parse({"name": "x"}), Settings("x"))
        self.assertEqual(
            settings_codec.parse({"name": "x", "retries": 5}),
            Settings("x", 5),
        )
        self.assertEqual(
            settings_codec.encode(Settings("x")),
            {"name": "x", "retries": 3},
        )
        self.assertEqual(
            settings_codec.to_json_schema(io="input"),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "retries": {"type": "integer"},
                },
                "required": ["name"],
            },
        )
        self.assertEqual(
            settings_codec.to_json_schema(io="output"),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "retries": {"type": "integer"},
                },
                "required": ["name", "retries"],
                "additionalProperties": False,
            },
        )

        @dataclasses.dataclass
        class Tags:
            tags: list[str] = dataclasses.field(default_factory=list)

        tags_codec = z.codecs.dataclass(Tags, fields={
            "tags": z.string().array(),
        })
        self.assertEqual(tags_codec.parse({}), Tags([]))
        self.assertEqual(tags_codec.encode(Tags()), {"tags": []})

        @dataclasses.dataclass
        class Event:
            day: dt.date = dt.date(2026, 1, 1)

        event_codec = z.codecs.dataclass(Event, fields={
            "day": z.codecs.iso_date(),
        })
        self.assertEqual(event_codec.parse({}), Event())
        self.assertEqual(event_codec.encode(Event()), {"day": "2026-01-01"})
        self.assertNotIn("required", event_codec.to_json_schema(io="input"))
        with self.assertRaises(UnrepresentableError):
            event_codec.to_json_schema(io="output")

        @dataclasses.dataclass
        class OptionalField:
            value: str

        with self.assertRaises(ValueError):
            z.codecs.dataclass(OptionalField, fields={
                "value": z.string().optional(),
            })

        @dataclasses.dataclass
        class OptionalDefault:
            value: str | None = None

        optional_default = z.codecs.dataclass(OptionalDefault, fields={
            "value": z.string().optional(),
        })
        self.assertEqual(optional_default.parse({}), OptionalDefault())
        self.assertEqual(
            optional_default.parse({"value": "x"}), OptionalDefault("x"),
        )
        self.assertNotIn(
            "required", optional_default.to_json_schema(io="input"),
        )

        @dataclasses.dataclass
        class SchemaDefault:
            value: str

        schema_default = z.codecs.dataclass(SchemaDefault, fields={
            "value": z.string().default("x"),
        })
        self.assertEqual(schema_default.parse({}), SchemaDefault("x"))

        @dataclasses.dataclass
        class BothDefaults:
            value: int = 3

        both_defaults = z.codecs.dataclass(BothDefaults, fields={
            "value": z.integer().default(7),
        })
        self.assertEqual(both_defaults.parse({}), BothDefaults(3))
        self.assertEqual(
            both_defaults.to_json_schema(io="input"),
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
            },
        )

        with self.assertRaises(ValueError):
            z.codecs.dataclass(User, fields={
                "name": z.string(),
                "created": z.codecs.iso_datetime(),
                "score": z.integer(),
                "extra": z.string(),
            })
        with self.assertRaises(TypeError):
            z.codecs.dataclass(int, fields={})
        with self.assertRaises(TypeError):
            z.codecs.dataclass(User, fields={
                "name": "bad",
                "created": z.codecs.iso_datetime(),
                "score": z.integer(),
            })

        @dataclasses.dataclass
        class WithInitVar:
            value: int
            extra: dataclasses.InitVar[int]

        with self.assertRaises(TypeError):
            z.codecs.dataclass(WithInitVar, fields={"value": z.integer()})

        self.assertEqual(
            user_codec.to_json_schema(io="input"),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "created": {"type": "string", "format": "date-time"},
                    "score": {"type": "integer", "minimum": 0},
                },
                "required": ["name", "created", "score"],
            },
        )
        with self.assertRaises(UnrepresentableError):
            user_codec.to_json_schema(io="output")

        @dataclasses.dataclass
        class Numeric:
            n: int
            label: str

        numeric_codec = z.codecs.dataclass(Numeric, fields={
            "n": z.integer(),
            "label": z.string(),
        })
        self.assertEqual(
            numeric_codec.to_json_schema(io="input"),
            {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "label": {"type": "string"},
                },
                "required": ["n", "label"],
            },
        )
        self.assertEqual(
            numeric_codec.to_json_schema(io="output"),
            {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "label": {"type": "string"},
                },
                "required": ["n", "label"],
                "additionalProperties": False,
            },
        )

        @dataclasses.dataclass
        class Wrapper:
            item: Numeric

        wrapper_codec = z.codecs.dataclass(Wrapper, fields={
            "item": numeric_codec,
        })
        self.assertEqual(
            wrapper_codec.to_json_schema(io="output"),
            {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "object",
                        "properties": {
                            "n": {"type": "integer"},
                            "label": {"type": "string"},
                        },
                        "required": ["n", "label"],
                        "additionalProperties": False,
                    },
                },
                "required": ["item"],
                "additionalProperties": False,
            },
        )

    def test_dataclass_annotated_schemas(self):
        name_schema = z.string().trim().min(1)

        @dataclasses.dataclass(frozen=True)
        class User:
            name: Annotated[str, "label", name_schema]
            age: Annotated[int, z.integer().min(0)]
            nickname: Annotated[str | None, z.string().nullable()] = None
            count: Annotated[int, z.integer().default(9)] = 3
            ignored: Annotated[int, {"description": "metadata"}] = 1

        codec = z.codecs.dataclass(User)
        user = codec.parse({"name": " Alex ", "age": 0})
        self.assertEqual(user, User("Alex", 0))
        self.assertEqual(codec.encode(user), {
            "name": "Alex", "age": 0, "nickname": None,
            "count": 3, "ignored": 1,
        })
        # Construction remains ordinary dataclass construction.
        self.assertEqual(User("", -1).age, -1)
        failure = codec.safe_parse({"name": "  ", "age": -1})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [issue.path for issue in failure.error.issues],
            [("name",), ("age",)],
        )
        self.assertEqual(
            [issue.code for issue in failure.error.issues],
            ["too_small", "too_small"],
        )
        self.assertIsInstance(codec.safe_parse({"name": "x"}), Failure)
        self.assertIsInstance(codec.safe_parse({"name": "x", "age": None}), Failure)
        self.assertTrue(name_schema.validate("x"))
        self.assertEqual(
            codec.to_json_schema(
                io="input", unrepresentable="any",
            )["properties"]["age"],
            {"type": "integer", "minimum": 0},
        )
        overridden = z.codecs.dataclass(User, fields={"age": z.integer().min(-5)})
        self.assertEqual(overridden.parse({"name": "x", "age": -1}).age, -1)

        @dataclasses.dataclass
        class MissingDefault:
            value: Annotated[int, z.integer().optional()]

        with self.assertRaisesRegex(ValueError, "no default"):
            z.codecs.dataclass(MissingDefault)

    def test_dataclass_type_aliases_and_inline_codecs(self):
        count_codec = z.codec(
            z.string(), z.integer().min(0), decode=int, encode=str,
        )
        type Count = Annotated[int, count_codec]
        type CountAlias = Annotated[Count, "count label"]
        type Counts = list[CountAlias]
        type Key = str
        type MaybeCount = Count | None

        @dataclasses.dataclass(frozen=True)
        class Inner:
            value: Count

        type InnerAlias = Inner

        @dataclasses.dataclass(frozen=True)
        class Payload:
            counts: Counts
            mapping: dict[Key, Count]
            pair: tuple[Count, str]
            inner: InnerAlias
            maybe: MaybeCount = None

        codec = z.codecs.dataclass(Payload)
        encoded = {
            "counts": ["1", "2"], "mapping": {"a": "3"},
            "pair": ("4", "x"), "inner": {"value": "5"}, "maybe": "6",
        }
        value = Payload([1, 2], {"a": 3}, (4, "x"), Inner(5), 6)
        self.assertEqual(codec.parse(encoded), value)
        self.assertEqual(codec.encode(value), encoded)
        self.assertIsNone(codec.parse({**encoded, "maybe": None}).maybe)
        without_maybe = {k: v for k, v in encoded.items() if k != "maybe"}
        self.assertIsNone(codec.parse(without_maybe).maybe)
        self.assertEqual(
            codec.to_json_schema(io="input")["properties"]["counts"]["items"],
            {"type": "string"},
        )
        self.assertEqual(
            codec.to_json_schema(io="output")["properties"]["counts"]["items"],
            {"type": "integer", "minimum": 0},
        )
        failure = codec.safe_parse({
            **encoded, "counts": ["-1"], "mapping": {"a": "-2"},
        })
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [issue.path for issue in failure.error.issues],
            [("counts", 0), ("mapping", "a")],
        )
        failure = codec.safe_encode(dataclasses.replace(value, counts=[-1]))
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("counts", 0))

    def test_dataclass_generic_aliases(self):
        type Age = Annotated[int, z.integer().min(0)]
        type Items[T] = list[T]
        type Matrix[T] = Items[Items[T]]
        type Pair[L, R] = tuple[L, R]
        type Swapped[A, B] = Pair[B, A]
        type Record[K, V] = dict[K, V]
        type Maybe[T] = T | None
        type Labeled[T] = Annotated[T, "label"]
        type Constrained[T] = Annotated[T, z.integer().max(100)]

        @dataclasses.dataclass(frozen=True)
        class Batch:
            values: Items[Age]
            nested: Items[Items[Age]]
            matrix: Matrix[Age]
            pair: Swapped[str, int]
            mapping: Record[str, Age]
            labeled: Labeled[Age]
            constrained: Constrained[int]
            maybe: Maybe[Age] = None

        data = {
            "values": [0, 1], "nested": [[2]], "matrix": [[3]],
            "pair": (4, "x"), "mapping": {"a": 5},
            "labeled": 6, "constrained": 7,
        }
        codec = z.codecs.dataclass(Batch)
        value = Batch([0, 1], [[2]], [[3]], (4, "x"), {"a": 5}, 6, 7)
        self.assertEqual(codec.parse(data), value)
        self.assertEqual(codec.encode(value), {**data, "maybe": None})
        self.assertEqual(codec.parse({**data, "maybe": 8}).maybe, 8)
        self.assertIsNone(codec.parse({**data, "maybe": None}).maybe)
        failure = codec.safe_parse({**data, "nested": [[-1]], "labeled": -2})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(
            [(issue.code, issue.path) for issue in failure.error.issues],
            [("too_small", ("nested", 0, 0)), ("too_small", ("labeled",))],
        )
        json.dumps(failure.error.to_dict())
        for invalid in (None, [True], [-1]):
            with self.subTest(invalid=invalid):
                self.assertIsInstance(codec.safe_parse({**data, "values": invalid}), Failure)
        without_values = {k: v for k, v in data.items() if k != "values"}
        self.assertIsInstance(codec.safe_parse(without_values), Failure)
        self.assertIsInstance(codec.safe_parse({**data, "constrained": 101}), Failure)
        self.assertEqual(
            codec.to_json_schema(io="input")["properties"]["values"]["items"],
            {"type": "integer", "minimum": 0},
        )
        overridden = z.codecs.dataclass(Batch, fields={"values": z.array(z.string())})
        self.assertEqual(overridden.parse({**data, "values": ["x"]}).values, ["x"])

    def test_dataclass_generic_alias_defaults(self):
        type Age = Annotated[int, z.integer().min(0)]
        type Items[T = Age] = list[T]
        type Pair[T, U = T] = tuple[T, U]
        type Bundle[T = Age, U = Items[T], V = Pair[T, U]] = tuple[T, U, V]

        @dataclasses.dataclass(frozen=True)
        class Defaults:
            ages: Items
            names: Items[str]
            pair: Pair[Age]
            bundle: Bundle
            override: Bundle[str, int, bool]

        codec = z.codecs.dataclass(Defaults)
        data = {
            "ages": [1], "names": ["x"], "pair": (2, 3),
            "bundle": (4, [5], (6, [7])), "override": ("x", 8, True),
        }
        value = Defaults([1], ["x"], (2, 3), (4, [5], (6, [7])), ("x", 8, True))
        self.assertEqual(codec.parse(data), value)
        self.assertEqual(codec.encode(value), data)
        failure = codec.safe_parse({**data, "bundle": (4, [-5], (6, [7]))})
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("bundle", 1, 0))

    def test_dataclass_generic_alias_codecs(self):
        calls = []

        def decode(value):
            calls.append(("decode", value))
            return int(value)

        def encode(value):
            calls.append(("encode", value))
            return str(value)

        type Count = Annotated[int, z.codec(
            z.string(), z.integer().min(0), decode=decode, encode=encode,
        )]
        type Items[T] = list[T]
        type Maybe[T] = T | None
        type Choice[T] = T | bool

        @dataclasses.dataclass(frozen=True)
        class Inner:
            count: Count

        @dataclasses.dataclass(frozen=True)
        class Payload:
            values: Items[Items[Count]]
            children: Items[Inner]
            maybe: Maybe[Count]
            choice: Choice[Count]

        codec = z.codecs.dataclass(Payload)
        data = {
            "values": [["1"]], "children": [{"count": "2"}],
            "maybe": "3", "choice": True,
        }
        value = Payload([[1]], [Inner(2)], 3, True)
        self.assertEqual(codec.parse(data), value)
        self.assertEqual(calls, [("decode", "1"), ("decode", "2"), ("decode", "3")])
        self.assertEqual(codec.encode(value), data)
        self.assertEqual(calls[-3:], [("encode", 1), ("encode", 2), ("encode", 3)])
        self.assertIsNone(codec.parse({**data, "maybe": None}).maybe)
        self.assertEqual(codec.parse({**data, "choice": "4"}).choice, 4)
        for io, expected in (
            ("input", {"type": "string"}),
            ("output", {"type": "integer", "minimum": 0}),
        ):
            self.assertEqual(
                codec.to_json_schema(io=io)["properties"]["values"]["items"]["items"],
                expected,
            )
        failure = codec.safe_encode(dataclasses.replace(value, values=[[-1]]))
        self.assertIsInstance(failure, Failure)
        self.assertEqual(failure.error.issues[0].path, ("values", 0, 0))

    def test_dataclass_annotated_configuration_errors(self):
        first = z.integer().min(0)
        second = z.integer().max(10)
        type Count = Annotated[int, first]
        type Recursive = list[Recursive]
        type Mutual = list[Other]
        type Other = Mutual | None
        type Unresolved = MissingAliasName  # noqa: F821 - lazy unresolved alias
        type Generic[T] = list[T]
        type Two[T, U] = tuple[T, U]
        type Variadic[*Ts] = tuple[*Ts]
        type Parameters[**P] = int
        type GenericRecursive[T] = list[GenericRecursive[T]]
        type Growing[T] = list[Growing[list[T]]]
        type GenericMutual[T] = list[GenericOther[T]]
        type GenericOther[T] = GenericMutual[T] | None
        type Constrained[T] = Annotated[T, second]
        type BadDefault[T = MissingDefaultName] = list[T]  # noqa: F821

        cases = (
            (Annotated[int, first, second], "multiple Annotated schemas"),
            (Annotated[Annotated[int, first], second], "multiple Annotated schemas"),
            (Annotated[Count, second], "multiple Annotated schemas"),
            (Recursive, "Recursive type aliases"),
            (Mutual, "Recursive type aliases"),
            (Unresolved, "Cannot resolve type alias"),
            (Generic, "Missing type argument"),
            (Generic[()], "Missing type argument"),
            (Two[int], "Missing type argument"),
            (Generic[int, str], "Too many type arguments"),
            (Generic[Generic.__type_params__[0]], "Unbound type parameter"),
            (Variadic[int, str], "Variadic type alias parameters"),
            (Parameters[[int]], "Variadic type alias parameters"),
            (GenericRecursive[int], "Recursive type aliases"),
            (Growing[int], "Recursive type aliases"),
            (GenericMutual[int], "Recursive type aliases"),
            (Constrained[Count], "multiple Annotated schemas"),
            (BadDefault, "Cannot resolve type alias"),
        )
        for annotation, message in cases:
            with self.subTest(annotation=annotation):
                cls = dataclasses.make_dataclass("Example", [("value", annotation)])
                with self.assertRaisesRegex(TypeError, message):
                    z.codecs.dataclass(cls)
                # An explicit definition bypasses annotation inference entirely.
                codec = z.codecs.dataclass(cls, fields={"value": z.integer()})
                self.assertEqual(codec.parse({"value": 1}), cls(1))

    def test_dataclass_annotation_inference(self):
        class Color(enum.Enum):
            RED = "red"
            BLUE = "blue"

        @dataclasses.dataclass(frozen=True)
        class Profile:
            name: str
            age: int
            score: float
            active: bool
            created: dt.datetime
            birthday: dt.date
            at: dt.time
            delta: dt.timedelta
            amount: decimal.Decimal
            path: pathlib.Path
            color: Color
            tags: list[str]
            counts: dict[str, int]
            pair: tuple[str, int]
            choice: int | str
            nickname: str | None = None
            grade: Literal["a", "b"] = "a"
            annotated: Annotated[int, "meta"] = 1

        codec = z.codecs.dataclass(Profile)
        value = codec.parse({
            "name": "Alex",
            "age": 30,
            "score": 1.5,
            "active": True,
            "created": dt.datetime(2026, 10, 20, 12, 30),
            "birthday": dt.date(1990, 1, 1),
            "at": dt.time(12, 30),
            "delta": dt.timedelta(days=1),
            "amount": decimal.Decimal("1.5"),
            "path": pathlib.Path("/tmp/x"),
            "color": Color.RED,
            "tags": ["a"],
            "counts": {"x": 1},
            "pair": ("a", 1),
            "choice": 1,
        })
        self.assertEqual(value.nickname, None)
        self.assertEqual(value.grade, "a")
        self.assertEqual(value.annotated, 1)
        self.assertEqual(value.pair, ("a", 1))
        self.assertEqual(value.color, Color.RED)
        encoded = codec.encode(value)
        self.assertEqual(encoded["pair"], ("a", 1))
        self.assertIsInstance(encoded["color"], Color)
        self.assertEqual(codec.parse({**encoded, "choice": "x"}).choice, "x")

        @dataclasses.dataclass
        class Event:
            name: str
            created: dt.datetime

        event = z.codecs.dataclass(Event, fields={
            "created": z.codecs.iso_datetime(),
        })
        parsed = event.parse({
            "name": "launch", "created": "2026-10-20T12:30:00Z",
        })
        self.assertEqual(
            event.encode(parsed),
            {
                "name": "launch",
                "created": "2026-10-20T12:30:00+00:00",
            },
        )

        @dataclasses.dataclass
        class Simple:
            name: str
            count: int = 1

        simple = z.codecs.dataclass(Simple)
        self.assertEqual(
            simple.to_json_schema(io="input"),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["name"],
            },
        )
        self.assertEqual(
            simple.to_json_schema(io="output"),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["name", "count"],
                "additionalProperties": False,
            },
        )

        @dataclasses.dataclass
        class Inner:
            value: int

        inner_codec = z.codecs.dataclass(Inner)
        self.assertEqual(inner_codec.parse({"value": 1}), Inner(1))

        @dataclasses.dataclass
        class Outer:
            inner: Inner

        inferred_outer = z.codecs.dataclass(Outer)
        self.assertEqual(
            inferred_outer.parse({"inner": {"value": 2}}), Outer(Inner(2)),
        )
        self.assertEqual(
            inferred_outer.encode(Outer(Inner(2))),
            {"inner": {"value": 2}},
        )
        explicit_outer = z.codecs.dataclass(Outer, fields={
            "inner": inner_codec,
        })
        self.assertEqual(
            explicit_outer.parse({"inner": {"value": 1}}), Outer(Inner(1)),
        )

        @dataclasses.dataclass
        class Container:
            items: list[Inner]
            mapping: dict[str, Inner]
            maybe: Inner | None = None
            either: Inner | str = "x"
            pair: tuple[Inner, int] = (Inner(0), 0)

        container = z.codecs.dataclass(Container)
        value = container.parse({
            "items": [{"value": 1}, {"value": 2}],
            "mapping": {"a": {"value": 3}},
            "maybe": {"value": 4},
            "either": {"value": 5},
            "pair": [{"value": 6}, 7],
        })
        self.assertEqual(
            container.encode(value),
            {
                "items": [{"value": 1}, {"value": 2}],
                "mapping": {"a": {"value": 3}},
                "maybe": {"value": 4},
                "either": {"value": 5},
                "pair": ({"value": 6}, 7),
            },
        )
        self.assertEqual(
            container.parse({
                "items": [], "mapping": {}, "either": "plain",
            }).either,
            "plain",
        )
        container_output = container.to_json_schema(io="output")
        self.assertEqual(
            container_output["properties"]["items"]["items"]["properties"]["value"],
            {"type": "integer"},
        )
        self.assertIs(
            container_output["properties"]["items"]["items"][
                "additionalProperties"
            ],
            False,
        )
        self.assertEqual(
            container_output["properties"]["maybe"]["anyOf"][1],
            {"type": "null"},
        )

        with self.assertRaises(TypeError):
            z.codecs.dataclass(_RecursiveNode)

        @dataclasses.dataclass
        class IntKeys:
            values: dict[int, str]

        @dataclasses.dataclass
        class VariableTuple:
            values: tuple[int, ...]

        @dataclasses.dataclass
        class Unsupported:
            values: set[str]

        for unsupported in (IntKeys, VariableTuple, Unsupported):
            with self.subTest(dataclass=unsupported), self.assertRaises(TypeError):
                z.codecs.dataclass(unsupported)

    def test_codec_combinators(self):
        int_text = z.codec(
            z.string().regex(r"^\d+$"),
            z.integer(),
            decode=int,
            encode=str,
        )
        array = z.codecs.array(int_text)
        self.assertEqual(array.parse(["1", "2"]), [1, 2])
        self.assertEqual(array.encode([1, 2]), ["1", "2"])
        self.assertEqual(
            array.to_json_schema(io="input")["items"],
            {"type": "string", "pattern": "^\\d+$"},
        )
        self.assertEqual(
            array.to_json_schema(io="output")["items"],
            {"type": "integer"},
        )

        record = int_text.record()
        self.assertEqual(record.parse({"a": "1"}), {"a": 1})
        self.assertEqual(record.encode({"a": 1}), {"a": "1"})
        self.assertEqual(
            record.to_json_schema(io="output")["additionalProperties"],
            {"type": "integer"},
        )

        optional = int_text.optional()
        self.assertIs(optional.parse(), MISSING)
        self.assertEqual(optional.parse("1"), 1)
        self.assertIs(optional.encode(MISSING), MISSING)
        self.assertEqual(optional.encode(1), "1")
        self.assertEqual(
            optional.to_json_schema(io="output"),
            {"type": "integer"},
        )

        nonoptional = optional.nonoptional()
        self.assertIsInstance(nonoptional.safe_parse(), Failure)
        self.assertEqual(nonoptional.parse("1"), 1)

        nullable = int_text.nullable()
        self.assertIsNone(nullable.parse(None))
        self.assertEqual(nullable.parse("1"), 1)
        self.assertIsNone(nullable.encode(None))
        self.assertEqual(nullable.encode(1), "1")

        nullish = int_text.nullish()
        self.assertIsNone(nullish.parse(None))
        self.assertIs(nullish.parse(), MISSING)

        union = z.codecs.union(int_text, z.string())
        self.assertEqual(union.parse("1"), 1)
        self.assertEqual(union.parse("plain"), "plain")
        self.assertEqual(union.encode(1), "1")
        self.assertEqual(union.encode("plain"), "plain")
        self.assertEqual(
            union.to_json_schema(io="output"),
            {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        )

        tuple_codec = z.codecs.tuple(int_text, z.string())
        self.assertEqual(tuple_codec.parse(["1", "plain"]), (1, "plain"))
        self.assertEqual(tuple_codec.encode((1, "plain")), ("1", "plain"))
        self.assertEqual(
            tuple_codec.to_json_schema(io="input")["prefixItems"],
            [
                {"type": "string", "pattern": "^\\d+$"},
                {"type": "string"},
            ],
        )

        aggregated = z.codecs.array(z.codecs.iso_date()).safe_parse([
            "2026-02-31", "2026-04-31",
        ])
        self.assertIsInstance(aggregated, Failure)
        self.assertEqual(
            [issue.path for issue in aggregated.error.issues],
            [(0,), (1,)],
        )

        with self.assertRaises(TypeError):
            z.codecs.array("not a codec")
        with self.assertRaises(TypeError):
            z.codecs.union("not a schema")
        with self.assertRaises(TypeError):
            z.codecs.tuple("not a schema")

    def test_codec_combinator_metadata(self):
        codec = z.codecs.iso_date().meta({"title": "Date"})
        self.assertEqual(codec.metadata, {"title": "Date"})

        # Collection and composition constructors start empty, matching the
        # plain array/record/union/tuple builders.
        self.assertEqual(z.codecs.array(codec).metadata, {})
        self.assertEqual(codec.record().metadata, {})
        self.assertEqual(z.codecs.union(codec, z.string()).metadata, {})
        self.assertEqual(z.codecs.tuple(codec, z.string()).metadata, {})

        # Wrappers describe the same value and preserve metadata.
        for wrapped in (
            codec.optional(),
            codec.nonoptional(),
            codec.nullable(),
            codec.nullish(),
        ):
            self.assertEqual(wrapped.metadata, {"title": "Date"})

        self.assertEqual(
            z.codecs.array(codec).meta({"title": "Dates"}).metadata,
            {"title": "Dates"},
        )
        self.assertEqual(
            codec.record().meta({"title": "By name"}).metadata,
            {"title": "By name"},
        )

    def test_codec_composition_runs_callbacks_once(self):
        input_calls: list[str] = []
        output_calls: list[str] = []
        decode_calls: list[str] = []
        encode_calls: list[str] = []

        def input_transform(value: str) -> str:
            input_calls.append(value)
            return value + "!"

        def output_transform(value: str) -> str:
            output_calls.append(value)
            return value + "?"

        codec = z.codec(
            z.string().transform(input_transform),
            z.string().transform(output_transform),
            decode=lambda value: (decode_calls.append(value), value)[1],
            encode=lambda value: (encode_calls.append(value), value)[1],
        )

        def reset() -> None:
            for calls in (input_calls, output_calls, decode_calls, encode_calls):
                calls.clear()

        decoded = [
            ("direct", codec, "a", "a!?"),
            ("optional", codec.optional(), "a", "a!?"),
            ("array", z.codecs.array(codec), ["a"], ["a!?"]),
            ("nested", z.codecs.array(codec.optional()), ["a"], ["a!?"]),
            ("record", codec.record(), {"a": "a"}, {"a": "a!?"}),
            ("union", z.codecs.union(codec, z.integer()), "a", "a!?"),
            ("tuple", z.codecs.tuple(codec, z.integer()), ["a", 1], ("a!?", 1)),
            ("nullable", codec.nullable(), "a", "a!?"),
            ("nonoptional", codec.nonoptional(), "a", "a!?"),
        ]
        for name, schema, value, expected in decoded:
            with self.subTest(direction="decode", case=name):
                reset()
                self.assertEqual(schema.parse(value), expected)
                self.assertEqual(input_calls, ["a"])
                self.assertEqual(decode_calls, ["a!"])
                self.assertEqual(output_calls, ["a!"])
                self.assertEqual(encode_calls, [])

        encoded = [
            ("direct", codec, "v", "v?!"),
            ("optional", codec.optional(), "v", "v?!"),
            ("array", z.codecs.array(codec), ["v"], ["v?!"]),
            ("nested", z.codecs.array(codec.optional()), ["v"], ["v?!"]),
            ("record", codec.record(), {"a": "v"}, {"a": "v?!"}),
            ("union", z.codecs.union(codec, z.integer()), "v", "v?!"),
            ("tuple", z.codecs.tuple(codec, z.integer()), ("v", 1), ("v?!", 1)),
            ("nullable", codec.nullable(), "v", "v?!"),
            ("nonoptional", codec.nonoptional(), "v", "v?!"),
        ]
        for name, schema, value, expected in encoded:
            with self.subTest(direction="encode", case=name):
                reset()
                self.assertEqual(schema.encode(value), expected)
                self.assertEqual(output_calls, ["v"])
                self.assertEqual(encode_calls, ["v?"])
                self.assertEqual(input_calls, ["v?"])
                self.assertEqual(decode_calls, [])

        @dataclasses.dataclass
        class Item:
            name: str

        item_codec = z.codecs.dataclass(Item, fields={"name": codec})
        reset()
        self.assertEqual(item_codec.parse({"name": "a"}), Item("a!?"))
        self.assertEqual(input_calls, ["a"])
        self.assertEqual(decode_calls, ["a!"])
        self.assertEqual(output_calls, ["a!"])
        reset()
        self.assertEqual(item_codec.encode(Item("v")), {"name": "v?!"})
        self.assertEqual(output_calls, ["v"])
        self.assertEqual(encode_calls, ["v?"])
        self.assertEqual(input_calls, ["v?"])

    def test_discriminated_union(self):
        schema = z.discriminated_union("kind", {
            "created": z.object({"kind": z.literal("created"), "id": z.integer()}),
            "deleted": z.object({
                "kind": z.literal("deleted"),
                "id": z.integer(),
                "reason": z.string(),
            }),
        })
        self.assertEqual(
            schema.parse({"kind": "created", "id": 1}),
            {"kind": "created", "id": 1},
        )
        self.assertEqual(
            schema.parse({"kind": "deleted", "id": 2, "reason": "gone"}),
            {"kind": "deleted", "id": 2, "reason": "gone"},
        )

        unknown = schema.safe_parse({"kind": "unknown", "id": 3})
        self.assertIsInstance(unknown, Failure)
        issue = unknown.error.issues[0]
        self.assertEqual(issue.code, "invalid_value")
        self.assertEqual(issue.path, ("kind",))
        self.assertEqual(issue.params, {"values": ("created", "deleted")})

        missing = schema.safe_parse({"id": 4})
        self.assertIsInstance(missing, Failure)
        self.assertEqual(missing.error.issues[0].code, "required")
        self.assertEqual(missing.error.issues[0].path, ("kind",))

        non_dict = schema.safe_parse([])
        self.assertIsInstance(non_dict, Failure)
        self.assertEqual(non_dict.error.issues[0].code, "invalid_type")
        self.assertEqual(
            non_dict.error.issues[0].params, {"expected": "dict with string keys"},
        )

        bad_branch = schema.safe_parse({"kind": "created", "id": "bad"})
        self.assertIsInstance(bad_branch, Failure)
        self.assertEqual(bad_branch.error.issues[0].path, ("id",))

        nested = z.object({"event": schema}).safe_parse({
            "event": {"kind": "deleted", "id": 2},
        })
        self.assertIsInstance(nested, Failure)
        self.assertEqual(nested.error.issues[0].path, ("event", "reason"))

        self.assertIsInstance(schema.safe_parse({"kind": True, "id": 1}), Failure)
        single = z.discriminated_union("kind", {
            "a": z.object({"kind": z.literal("a")}),
        })
        self.assertEqual(single.parse({"kind": "a"}), {"kind": "a"})
        stripped = z.discriminated_union("kind", {
            "a": z.object({"id": z.integer()}),
        })
        self.assertEqual(stripped.parse({"kind": "a", "id": 1}), {"id": 1})

        for fn in (
            lambda: z.discriminated_union(1, {"a": z.object({})}),
            lambda: z.discriminated_union("kind", {}),
            lambda: z.discriminated_union("kind", {1: z.object({})}),
            lambda: z.discriminated_union("kind", {"a": "not a schema"}),
        ):
            with self.subTest(fn=fn), self.assertRaises((TypeError, ValueError)):
                fn()

    def test_fallbacks_nonoptional_and_overwrite(self):
        self.assertEqual(z.integer().catch(0).parse("bad"), 0)
        self.assertEqual(z.integer().catch(0).parse(1), 1)
        self.assertEqual(z.string().catch("x").parse(), "x")
        self.assertEqual(z.integer().catch(lambda: 5).parse("bad"), 5)
        self.assertEqual(
            z.integer().refine(lambda value: value > 0, "positive").catch(0).parse(-1),
            0,
        )
        self.assertEqual(
            z.object({"x": z.integer().catch(0)}).parse({}), {"x": 0},
        )
        fallback = []
        schema = z.array(z.integer()).catch(fallback)
        first = schema.parse("bad")
        first.append(1)
        self.assertEqual(fallback, [])
        self.assertEqual(schema.parse("bad"), [])
        with self.assertRaises(ZeroDivisionError):
            z.integer().transform(lambda value: 1 / 0).catch(0).safe_parse(1)

        self.assertIsInstance(
            z.string().optional().nonoptional().safe_parse(), Failure,
        )
        self.assertEqual(z.string().optional().nonoptional().parse("x"), "x")
        self.assertEqual(z.string().overwrite(str.upper).parse("ab"), "AB")
        with self.assertRaises(ZeroDivisionError):
            z.integer().overwrite(lambda value: 1 / 0).safe_parse(1)

    def test_object_required_and_keyof(self):
        base = z.object({
            "a": z.string().optional(),
            "b": z.string().default("x"),
        }).meta({"title": "Record"})
        required = base.required()
        self.assertEqual(required.metadata["title"], "Record")
        self.assertIsInstance(required.safe_parse({}), Failure)
        self.assertEqual(
            required.parse({"a": "a", "b": "b"}), {"a": "a", "b": "b"},
        )
        self.assertEqual(base.required().partial().parse({}), {})

        keyof = z.object({"a": z.integer(), "b": z.string()}).keyof()
        self.assertEqual(keyof.parse("a"), "a")
        bad = keyof.safe_parse("c")
        self.assertIsInstance(bad, Failure)
        self.assertEqual(bad.error.issues[0].code, "invalid_value")
        self.assertEqual(bad.error.issues[0].params, {"values": ("a", "b")})
        with self.assertRaises(ValueError):
            z.object({}).keyof()

    def test_exact_length(self):
        self.assertEqual(z.string().length(2).parse("ab"), "ab")
        for bad in ("a", "abc"):
            self.assertIsInstance(z.string().length(2).safe_parse(bad), Failure)
        self.assertEqual(z.array(z.integer()).length(1).parse([1]), [1])
        for bad in ([], [1, 2]):
            self.assertIsInstance(
                z.array(z.integer()).length(1).safe_parse(bad), Failure,
            )
        result = z.string().length(2, message="Exactly two").safe_parse("a")
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.issues[0].message, "Exactly two")
        with self.assertRaises(ValueError):
            z.string().length(-1)

    def test_unwrap_and_validate(self):
        base = z.string()
        self.assertIs(base.optional().unwrap(), base)
        self.assertIs(base.nullable().unwrap(), base)
        self.assertIs(base.default("x").unwrap(), base)
        self.assertIs(base.nonoptional().unwrap(), base)
        self.assertIs(base.catch("x").unwrap(), base)
        self.assertIs(base.overwrite(str.upper).unwrap(), base)
        self.assertIs(base.transform(str.upper).unwrap(), base)
        self.assertIs(base.array().unwrap(), base)
        self.assertIs(z.record(base).unwrap(), base)
        with self.assertRaises(TypeError):
            base.unwrap()
        self.assertTrue(z.integer().validate(1))
        self.assertFalse(z.integer().validate("x"))
        self.assertFalse(z.integer().validate())
        with self.assertRaises(ZeroDivisionError):
            z.integer().transform(lambda value: 1 / 0).validate(1)

    def test_intersection(self):
        left = z.object({"a": z.string()})
        right = z.object({"b": z.integer()})
        schema = z.intersection(left, right)
        self.assertEqual(
            schema.parse({"a": "x", "b": 1}), {"a": "x", "b": 1},
        )
        both = schema.safe_parse({"a": 1, "b": "x"})
        self.assertIsInstance(both, Failure)
        self.assertEqual([i.path for i in both.error.issues], [("a",), ("b",)])
        one = schema.safe_parse({"a": "x", "b": "x"})
        self.assertIsInstance(one, Failure)
        self.assertEqual([i.path for i in one.error.issues], [("b",)])
        missing = schema.safe_parse()
        self.assertIsInstance(missing, Failure)
        self.assertEqual(missing.error.issues[0].code, "required")
        self.assertIs(schema.optional().parse(), MISSING)

        nested = z.intersection(
            z.object({"profile": z.object({"a": z.string()})}),
            z.object({"profile": z.object({"b": z.integer()})}),
        )
        self.assertEqual(
            nested.parse({"profile": {"a": "x", "b": 1}}),
            {"profile": {"a": "x", "b": 1}},
        )
        precedence = z.intersection(
            z.object({"x": z.string().transform(lambda value: "left")}),
            z.object({"x": z.string().transform(lambda value: "right")}),
        )
        self.assertEqual(precedence.parse({"x": "value"}), {"x": "right"})

        non_dict = z.intersection(z.string(), z.string()).safe_parse("x")
        self.assertIsInstance(non_dict, Failure)
        self.assertEqual(
            non_dict.error.issues[0].code, "invalid_intersection_types",
        )

        self.assertEqual(
            z.object({"a": z.string()}).and_(
                z.object({"b": z.integer()}),
            ).parse({"a": "x", "b": 1}),
            {"a": "x", "b": 1},
        )
        self.assertIs(schema.left, left)
        self.assertIs(schema.right, right)
        decorated = schema.meta({"title": "Both"})
        self.assertEqual(decorated.metadata["title"], "Both")
        self.assertIs(decorated.left, left)
        with self.assertRaises(TypeError):
            z.intersection("left", "right")
        with self.assertRaises(TypeError):
            left.and_("bad")

    def test_error_helpers(self):
        schema = z.object({
            "name": z.string().min(3),
            "tags": z.string().array(),
            "profile": z.object({"age": z.integer().positive()}),
        })
        result = schema.safe_parse({
            "name": "x", "tags": ["ok", 1], "profile": {"age": 0},
        })
        self.assertIsInstance(result, Failure)
        error = result.error
        self.assertEqual(error.flatten(), {
            "form_errors": [],
            "field_errors": {
                "name": ["Must contain at least 3 characters"],
                "tags": ["Expected string"],
                "profile": ["Must be positive"],
            },
        })
        tree = error.treeify()
        self.assertEqual(
            tree["properties"]["name"]["errors"],
            ["Must contain at least 3 characters"],
        )
        self.assertEqual(
            tree["properties"]["tags"]["items"][1]["errors"],
            ["Expected string"],
        )
        self.assertEqual(
            tree["properties"]["profile"]["properties"]["age"]["errors"],
            ["Must be positive"],
        )
        pretty = error.prettify().splitlines()
        self.assertIn("name: Must contain at least 3 characters", pretty)
        self.assertIn("tags.1: Expected string", pretty)
        self.assertIn("profile.age: Must be positive", pretty)
        with self.assertRaises(TypeError):
            error.issues[0].params["minimum"] = 0

    def test_parse_error_map(self):
        calls: list[str] = []

        def explain(issue):
            calls.append(issue.message)
            if issue.code == "too_small" and issue.path == ("age",):
                return f"Age must be at least {issue.params['minimum']}."
            return issue.message

        schema = z.object({
            "age": z.integer().min(18),
            "name": z.string().min(1),
        })
        result = schema.safe_parse({"age": 12, "name": ""}, error=explain)
        self.assertIsInstance(result, Failure)
        self.assertEqual(calls, [])

        self.assertEqual(
            [issue.message for issue in result.error.issues],
            ["Age must be at least 18.", "Must contain at least 1 characters"],
        )
        self.assertEqual(
            calls, ["Must be at least 18", "Must contain at least 1 characters"],
        )
        self.assertEqual(
            result.error.flatten()["field_errors"]["age"],
            ["Age must be at least 18."],
        )
        self.assertEqual(
            result.error.to_dict()["issues"][0]["message"],
            "Age must be at least 18.",
        )
        self.assertIn(
            "name: Must contain at least 1 characters",
            result.error.prettify(),
        )
        self.assertEqual(len(calls), 2)

        raised_calls: list[str] = []

        def raised(issue):
            raised_calls.append(issue.code)
            return "mapped"

        with self.assertRaises(ValidationError) as caught:
            z.string().parse(1, error=raised)
        self.assertEqual(raised_calls, [])
        self.assertEqual(str(caught.exception), "<root>: mapped")
        self.assertEqual(raised_calls, ["invalid_type"])

        json_calls: list[str] = []

        def json_error(issue):
            json_calls.append(issue.code)
            return "Bad JSON"

        json_result = z.string().safe_parse_json("not json", error=json_error)
        self.assertIsInstance(json_result, Failure)
        self.assertEqual(json_calls, [])
        self.assertEqual(json_result.error.issues[0].code, "invalid_format")
        self.assertEqual(json_result.error.issues[0].message, "Bad JSON")
        self.assertEqual(json_calls, ["invalid_format"])

        codec = z.codec(
            z.string().regex(r"^\d+$"), z.integer(), decode=int, encode=str,
        )
        codec_result = codec.safe_decode("x", error=lambda issue: "digits only")
        self.assertEqual(codec_result.error.issues[0].message, "digits only")

        with self.assertRaises(TypeError):
            z.string().safe_parse("x", error=1)  # type: ignore[arg-type]

    def test_parse_error_map_failures(self):
        non_string = z.string().safe_parse(
            1, error=lambda issue: 1,  # type: ignore[arg-type,return-value]
        )
        self.assertIsInstance(non_string, Failure)
        with self.assertRaises(TypeError):
            _ = non_string.error.issues

        def broken(issue):
            raise RuntimeError(issue.code)

        failed = z.string().safe_parse(1, error=broken)
        self.assertIsInstance(failed, Failure)
        with self.assertRaises(RuntimeError):
            _ = failed.error.issues

        def branch_message(issue):
            return f"mapped:{issue.code}"

        union = z.union(z.string().min(5), z.integer().min(10))
        union_result = union.safe_parse(3, error=branch_message)
        self.assertIsInstance(union_result, Failure)
        issue = union_result.error.issues[0]
        self.assertEqual(issue.message, "mapped:invalid_union")
        self.assertEqual(
            {branch.message for items in issue.branches for branch in items},
            {"mapped:invalid_type", "mapped:too_small"},
        )

    def test_metadata_is_immutable_and_propagates(self):
        base = z.string()
        name = base.meta({"title": "Name", "examples": ["Alex"]})
        self.assertEqual(name.metadata, {"title": "Name", "examples": ["Alex"]})
        self.assertEqual(z.string().metadata, {})
        self.assertEqual(name, base)
        self.assertEqual(hash(name), hash(base))
        with self.assertRaises(TypeError):
            name.metadata["title"] = "Other"

        described = name.describe("Display name")
        self.assertEqual(described.metadata["title"], "Name")
        self.assertEqual(described.metadata["description"], "Display name")
        merged = name.meta({"title": "Full name", "deprecated": False})
        self.assertEqual(merged.metadata["title"], "Full name")
        self.assertEqual(merged.metadata["examples"], ["Alex"])
        self.assertFalse(merged.metadata["deprecated"])

        for schema in (
            name.trim().min(1),
            name.refine(lambda value: bool(value)),
            name.super_refine(lambda value: None),
            name.transform(str.upper),
            name.overwrite(str.upper),
            name.pipe(z.string()),
            name.optional(),
            name.nonoptional(),
            name.nullable(),
            name.nullish(),
            name.default("x"),
            name.default_factory(lambda: "x"),
            name.catch("x"),
            name.length(1),
            z.preprocess(str, name),
        ):
            with self.subTest(schema=schema):
                self.assertEqual(schema.metadata["title"], "Name")

        tags = z.string().array().meta({"title": "Tags"})
        self.assertEqual(tags.min(1).metadata["title"], "Tags")
        age = z.integer().meta({"title": "Age"})
        self.assertEqual(age.positive().metadata["title"], "Age")

        obj = z.object({"name": name}).meta({"title": "Person"})
        self.assertEqual(z.object({"name": name}).metadata, {})
        for schema in (
            obj.strict(),
            obj.strip(),
            obj.passthrough(),
            obj.extend({"age": z.integer()}),
            obj.pick("name"),
            obj.omit("name"),
            obj.partial(),
        ):
            with self.subTest(schema=schema):
                self.assertEqual(schema.metadata["title"], "Person")

        self.assertEqual(name.array().metadata, {})
        self.assertEqual(name.or_(z.integer()).metadata, {})

        for fn in (
            lambda: z.string().meta("bad"),
            lambda: z.string().meta({1: "x"}),
            lambda: z.string().describe(1),
        ):
            with self.subTest(fn=fn), self.assertRaises(TypeError):
                fn()

    def test_child_schema_references_are_retained(self):
        item = z.string().meta({"title": "Tag"})
        array = z.array(item)
        self.assertIs(array.item, item)
        self.assertEqual(array.item.metadata["title"], "Tag")
        self.assertEqual(array.metadata, {})
        self.assertIs(array.min(1).item, item)

        record = z.record(item)
        self.assertIs(record.value_schema, item)
        self.assertEqual(record.value_schema.metadata["title"], "Tag")

        tuple_schema = z.tuple(item, z.integer())
        self.assertIs(tuple_schema.items[0], item)
        self.assertEqual(tuple_schema.items[0].metadata["title"], "Tag")

        union_schema = z.union(item, z.integer())
        self.assertIs(union_schema.options[0], item)
        self.assertEqual(union_schema.options[0].metadata["title"], "Tag")
        self.assertIs(item.or_(z.integer()).options[0], item)

        option = z.object({"kind": z.literal("a")}).meta({"title": "A"})
        discriminated = z.discriminated_union("kind", {"a": option})
        self.assertIs(discriminated.options["a"], option)
        self.assertEqual(discriminated.options["a"].metadata["title"], "A")
        self.assertEqual(discriminated.metadata, {})
        self.assertEqual(discriminated.key, "kind")
        with self.assertRaises(TypeError):
            discriminated.options["b"] = option

        decorated = z.array(item).meta({"title": "Tags"})
        self.assertIs(decorated.item, item)
        self.assertEqual(decorated.metadata["title"], "Tags")
        decorated_union = z.union(item, z.integer()).meta({"title": "Choice"})
        self.assertIs(decorated_union.options[0], item)
        decorated_discriminated = discriminated.meta({"title": "Event"})
        self.assertIs(decorated_discriminated.options["a"], option)

    def test_optional_is_not_nullable(self):
        optional = z.string().optional()
        self.assertIs(optional.parse(), MISSING)
        self.assertIsInstance(optional.safe_parse(None), Failure)
        nullable = z.string().nullable()
        self.assertIsNone(nullable.parse(None))
        self.assertIsInstance(nullable.safe_parse(), Failure)
        self.assertIs(z.string().nullish().parse(), MISSING)
        self.assertIsNone(z.string().nullish().parse(None))

    def test_defaults_are_validated_fresh_and_missing_only(self):
        default = ["a"]
        schema = z.string().array().default(default)
        first = schema.parse()
        first.append("b")
        default.append("c")
        self.assertEqual(schema.parse(), ["a"])
        self.assertEqual(default, ["a", "c"])
        self.assertIsInstance(schema.safe_parse(None), Failure)
        self.assertIsInstance(z.integer().positive().default(-1).safe_parse(), Failure)
        self.assertEqual(z.string().trim().default(" x ").parse(), "x")
        calls = []
        factory_schema = z.integer().default_factory(lambda: len(calls.append(1) or calls))
        self.assertEqual(factory_schema.parse(100), 100)
        self.assertEqual(calls, [])
        self.assertEqual(factory_schema.parse(), 1)

    def test_defaults_are_inputs_for_transformed_schemas(self):
        schema = z.string().transform(len)
        self.assertEqual(schema.default("abc").parse(), 3)
        self.assertIsInstance(schema.default(0).safe_parse(), Failure)
        self.assertEqual(schema.default_factory(lambda: "abcd").parse(), 4)
        self.assertIsInstance(schema.default_factory(lambda: 0).safe_parse(), Failure)

    def test_object_policies_and_omitted_properties(self):
        schema = z.object({
            "name": z.string(), "note": z.string().optional(),
            "role": z.enum(["user", "admin"]).default("user"),
        })
        self.assertEqual(
            schema.parse({"name": "Alex"}),
            {"name": "Alex", "role": "user"},
        )
        self.assertEqual(
            schema.parse({"name": "Alex", "extra": 1}),
            {"name": "Alex", "role": "user"},
        )
        self.assertEqual(schema.passthrough().parse({"name": "Alex", "extra": 1})["extra"], 1)
        bad = schema.strict().safe_parse({"name": "Alex", "extra": 1})
        self.assertEqual(bad.error.issues[0].path, ("extra",))
        self.assertEqual(bad.error.issues[0].code, "unrecognized_key")
        self.assertEqual(bad.error.issues[0].params, {"keys": ("extra",)})
        self.assertIsInstance(schema.safe_parse({"name": "Alex", "note": None}), Failure)
        self.assertIsInstance(z.object({}).safe_parse({1: "a"}), Failure)
        self.assertEqual(
            schema.strict().strip().parse({"name": "Alex", "x": 1}),
            {"name": "Alex", "role": "user"},
        )
        replaced = z.object({"a": z.integer(), "b": z.integer()}).strict()
        result = replaced.safe_parse({"a": 1, "c": 2})
        self.assertEqual(
            [(issue.code, issue.path) for issue in result.error.issues],
            [("required", ("b",)), ("unrecognized_key", ("c",))],
        )
        wide = z.object({f"k{index}": z.integer() for index in range(6)})
        self.assertEqual(
            wide.parse({f"k{index}": index for index in range(6)}),
            {f"k{index}": index for index in range(6)},
        )
        self.assertIsInstance(wide.safe_parse({"k0": 1}), Failure)

    def test_nested_issues_accumulate_and_serialize(self):
        schema = z.object({"users": z.object({
            "name": z.string().min(1), "age": z.integer().positive(),
        }).array()})
        result = schema.safe_parse({"users": [{"name": "", "age": -1}, {}]})
        self.assertEqual([i.path for i in result.error.issues], [
            ("users", 0, "name"), ("users", 0, "age"),
            ("users", 1, "name"), ("users", 1, "age"),
        ])
        payload = json.loads(json.dumps(result.error.to_dict()))
        self.assertEqual(payload["issues"][0]["path"], ["users", 0, "name"])
        self.assertIn("users.0.name", str(result.error))

    def test_object_structural_methods_do_not_mutate(self):
        shape = {"a": z.string()}
        base = z.object(shape).strict()
        shape["b"] = z.integer()
        self.assertEqual(base.parse({"a": "a"}), {"a": "a"})
        with self.assertRaises(TypeError):
            base.shape["b"] = z.integer()
        extended = base.extend({"b": z.integer()})
        self.assertEqual(extended.parse({"a": "a", "b": 2}), {"a": "a", "b": 2})
        self.assertEqual(extended.pick("b").parse({"b": 2}), {"b": 2})
        self.assertEqual(extended.omit("b").parse({"a": "a"}), {"a": "a"})
        self.assertEqual(extended.partial().parse({}), {})
        self.assertEqual(extended.partial("a").parse({"b": 2}), {"b": 2})
        self.assertIsInstance(extended.partial("a").safe_parse({}), Failure)
        self.assertIsInstance(extended.partial().safe_parse({"b": None}), Failure)
        with self.assertRaises(KeyError):
            base.pick("missing")
        with self.assertRaises(KeyError):
            extended.partial("missing")
        self.assertIsInstance(base.safe_parse({"a": "a", "b": 2}), Failure)

    def test_collections(self):
        self.assertEqual(z.integer().array().nonempty().max(2).parse([1]), [1])
        self.assertIsInstance(z.integer().array().min(1).safe_parse([]), Failure)
        self.assertIsInstance(z.integer().array().safe_parse((1,)), Failure)
        self.assertEqual(z.tuple(z.string(), z.integer()).parse(["x", 1]), ("x", 1))
        self.assertIsInstance(z.tuple(z.string()).safe_parse([]), Failure)
        bad = z.tuple(z.string(), z.integer()).safe_parse([1, "x"])
        self.assertEqual([i.path for i in bad.error.issues], [(0,), (1,)])
        self.assertEqual(z.record(z.integer()).parse({"a": 1}), {"a": 1})
        self.assertIsInstance(z.record(z.integer()).safe_parse({1: 1}), Failure)
        bad = z.record(z.integer()).safe_parse({"a": "a", "b": "b"})
        self.assertEqual([i.path for i in bad.error.issues], [("a",), ("b",)])

    def test_object_catchall(self):
        schema = z.object({"a": z.string()}).catchall(z.integer())
        self.assertEqual(
            schema.parse({"a": "x", "extra": 1}),
            {"a": "x", "extra": 1},
        )
        bad = schema.safe_parse({"a": "x", "extra": "bad"})
        self.assertIsInstance(bad, Failure)
        self.assertEqual(bad.error.issues[0].code, "invalid_type")
        self.assertEqual(bad.error.issues[0].path, ("extra",))
        invalid_key = schema.safe_parse({"a": "x", 1: 2})
        self.assertIsInstance(invalid_key, Failure)
        self.assertEqual(invalid_key.error.issues[0].code, "invalid_key")

        self.assertIsInstance(
            schema.strict().safe_parse({"a": "x", "extra": 1}), Failure,
        )
        self.assertEqual(
            schema.passthrough().parse({"a": "x", "extra": "unchanged"})["extra"],
            "unchanged",
        )
        self.assertNotIn("extra", schema.strip().parse({"a": "x", "extra": 1}))
        self.assertEqual(
            schema.extend({"b": z.string()}).parse({"a": "x", "b": "y", "extra": 2}),
            {"a": "x", "b": "y", "extra": 2},
        )
        self.assertEqual(schema.partial().parse({"extra": 2}), {"extra": 2})

        decorated = schema.meta({"title": "Catchall"})
        self.assertEqual(decorated.metadata["title"], "Catchall")
        self.assertEqual(
            decorated.required().parse({"a": "x", "b": 1}), {"a": "x", "b": 1},
        )

        with self.assertRaises(TypeError):
            schema.catchall("bad")
        with self.assertRaises(ValueError):
            z.object({}, unknown_keys="catchall")
        with self.assertRaises(ValueError):
            z.object({}, unknown_keys="strict", catchall=z.integer())
        with self.assertRaises(TypeError):
            z.object({}, catchall="bad")

    def test_record_key_schemas(self):
        schema = z.record(z.string().min(1), z.integer())
        self.assertEqual(schema.parse({"a": 1}), {"a": 1})
        bad_key = schema.safe_parse({"": 1})
        self.assertIsInstance(bad_key, Failure)
        self.assertEqual(bad_key.error.issues[0].code, "too_small")
        bad_value = schema.safe_parse({"a": "x"})
        self.assertIsInstance(bad_value, Failure)
        self.assertEqual(bad_value.error.issues[0].path, ("a",))

        ints = z.record(z.integer(), z.string())
        self.assertEqual(ints.parse({1: "a"}), {1: "a"})
        self.assertIsInstance(ints.safe_parse({"1": "a"}), Failure)

        transformed = z.record(z.string().transform(str.upper), z.integer())
        self.assertEqual(transformed.parse({"a": 1}), {"A": 1})
        transformed_bad = transformed.safe_parse({"a": "x"})
        self.assertIsInstance(transformed_bad, Failure)
        self.assertEqual(transformed_bad.error.issues[0].path, ("A",))

        values = z.record(z.enum(["a", "b"]), z.integer())
        self.assertEqual(values.parse({"a": 1, "b": 2}), {"a": 1, "b": 2})
        self.assertIsInstance(values.safe_parse({"c": 1}), Failure)

        legacy = z.record(z.integer())
        legacy_bad = legacy.safe_parse({1: 1})
        self.assertIsInstance(legacy_bad, Failure)
        self.assertEqual(legacy_bad.error.issues[0].code, "invalid_key")

        with self.assertRaises(TypeError):
            z.record("bad", z.integer())
        with self.assertRaises(TypeError):
            z.record(z.string(), "bad")

    def test_tuple_rest(self):
        schema = z.tuple(z.string()).rest(z.integer())
        self.assertEqual(schema.parse(["a", 1, 2]), ("a", 1, 2))
        self.assertEqual(schema.parse(("a",)), ("a",))
        short = schema.safe_parse([])
        self.assertIsInstance(short, Failure)
        self.assertEqual(short.error.issues[0].code, "too_small")
        bad = schema.safe_parse(["a", "b"])
        self.assertIsInstance(bad, Failure)
        self.assertEqual(bad.error.issues[0].path, (1,))

        fixed = z.tuple(z.string())
        self.assertEqual(fixed.parse(["a"]), ("a",))
        self.assertIsInstance(fixed.safe_parse(["a", "b"]), Failure)
        self.assertEqual(z.tuple().rest(z.integer()).parse([1, 2]), (1, 2))

        decorated = schema.meta({"title": "Tuple"})
        self.assertEqual(decorated.metadata["title"], "Tuple")
        with self.assertRaises(TypeError):
            schema.rest("bad")

    def test_literal_enum_union(self):
        self.assertIsInstance(z.literal(1).safe_parse(True), Failure)
        self.assertIsInstance(z.literal(1).safe_parse(1.0), Failure)
        self.assertEqual(z.literal(None).parse(None), None)
        self.assertEqual(z.enum(["a", "b"]).parse("b"), "b")
        self.assertEqual(z.string().or_(z.integer()).parse(1), 1)
        schema = z.object({"value": z.union(z.string(), z.integer())})
        bad = schema.safe_parse({"value": None})
        issue = bad.error.issues[0]
        self.assertEqual(issue.code, "invalid_union")
        self.assertEqual(len(issue.branches), 2)
        self.assertEqual(issue.branches[0][0].path, ("value",))
        json.dumps(bad.error.to_dict())

    def test_transform_refine_pipe_and_preprocess(self):
        schema = z.string().trim().transform(len).pipe(z.integer().min(2))
        self.assertEqual(schema.parse(" abc "), 3)
        self.assertIsInstance(schema.safe_parse("a"), Failure)
        self.assertEqual(z.preprocess(str, z.string()).parse(42), "42")
        pair = z.object({"a": z.integer(), "b": z.integer()}).refine(
            lambda v: v["a"] < v["b"], "a must be less than b",
        )
        self.assertIsInstance(pair.safe_parse({"a": 3, "b": 2}), Failure)
        self.assertIs(type(pair), Schema)
        with self.assertRaises(ValueError):
            z.string().transform(int).safe_parse("oops")
        with self.assertRaises(ZeroDivisionError):
            z.integer().refine(lambda v: 1 / v > 0).safe_parse(0)

    def test_typed_output_and_instance(self):
        @dataclasses.dataclass
        class User:
            name: str
        schema = z.object({"name": z.string()}).transform(lambda v: User(**v))
        user = schema.parse({"name": "Alex"})
        self.assertEqual(user, User("Alex"))
        self.assertIs(z.instance_of(User).parse(user), user)
        self.assertIsInstance(z.instance_of(User).safe_parse({}), Failure)

    def test_recursive_schema(self):
        node: Schema[dict[str, Any]] = z.lazy(lambda: z.object({
            "name": z.string(), "children": node.array().default([]),
        }))
        self.assertEqual(node.parse({"name": "a", "children": [{"name": "b"}]}), {
            "name": "a", "children": [{"name": "b", "children": []}],
        })

    def test_unknown_required_and_mutable_copy(self):
        self.assertIsNone(z.unknown().parse(None))
        self.assertIsInstance(z.object({"x": z.unknown()}).safe_parse({}), Failure)
        raw = {"items": [1, 2]}
        parsed = z.object({"items": z.integer().array()}).parse(raw)
        parsed["items"].append(3)
        self.assertEqual(raw["items"], [1, 2])
        raw = {"extra": {"a": []}}
        parsed = z.object({}).passthrough().parse(raw)
        parsed["extra"]["a"].append(1)
        self.assertEqual(raw, {"extra": {"a": []}})

    def test_invalid_schema_configuration(self):
        for fn in (
            lambda: z.union(), lambda: z.enum([]), lambda: z.literal([]),
            lambda: z.literal(math.nan), lambda: z.string().min(-1),
            lambda: z.string().max(True), lambda: z.integer().min(math.inf),
            lambda: z.integer().gt(True), lambda: z.integer().lt(math.inf),
            lambda: z.integer().multiple_of(0),
            lambda: z.integer().multiple_of(-1),
            lambda: z.integer().multiple_of(math.inf),
            lambda: z.integer().multiple_of(True),
            lambda: z.string().starts_with(1),
            lambda: z.string().ends_with(None),
            lambda: z.string().includes(b"x"),
            lambda: z.string().ipv6(message=1),
            lambda: z.object({}, unknown_keys="invalid"),
            lambda: z.object({"x": "not a schema"}),
            lambda: z.enum([object()]),
            lambda: z.string().min(1, message=1),
            lambda: z.string().refine(lambda value: True, message=1),
            lambda: z.string().refine(lambda value: True, path="bad"),
            lambda: z.string().refine(lambda value: True, path=("a", True)),
        ):
            with self.subTest(fn=fn), self.assertRaises((TypeError, ValueError)):
                fn()

    def test_validated_dataclass_constructor_accepts_and_normalizes(self):
        @z.dataclass
        class User:
            name: Annotated[str, z.string().trim().min(1)]
            age: Annotated[int, z.integer().min(0)]
            nickname: str | None = None

        user = User(name=" Alex ", age=30)
        self.assertEqual(user.name, "Alex")
        self.assertEqual(user, User(name="Alex", age=30, nickname=None))
        self.assertTrue(dataclasses.is_dataclass(User))
        signature = inspect.signature(User)
        self.assertEqual(
            tuple(signature.parameters), ("name", "age", "nickname"),
        )
        self.assertIsNone(signature.parameters["nickname"].default)
        self.assertIsNotNone(User.__init__.__wrapped__)

    def test_validated_dataclass_constructor_aggregates_issues(self):
        @z.dataclass
        class User:
            name: Annotated[str, z.string().min(1)]
            age: Annotated[int, z.integer().min(0)]
            nickname: str | None = None

        with self.assertRaises(ValidationError) as caught:
            User(name="", age=-1, nickname=1)
        self.assertEqual(
            [(issue.code, issue.path) for issue in caught.exception.issues],
            [
                ("too_small", ("name",)),
                ("too_small", ("age",)),
                ("invalid_type", ("nickname",)),
            ],
        )

    def test_validated_dataclass_defaults_are_validated(self):
        @z.dataclass
        class Defaults:
            name: Annotated[str, z.string().min(3)] = "valid"
            age: Annotated[int, z.integer().min(0)] = 1

        self.assertEqual(Defaults().name, "valid")
        with self.assertRaises(ValidationError) as caught:
            Defaults(name="x", age=-1)
        self.assertEqual(
            [(issue.code, issue.path) for issue in caught.exception.issues],
            [("too_small", ("name",)), ("too_small", ("age",))],
        )

        @z.dataclass
        class BadDefault:
            value: Annotated[int, z.integer().min(0)] = -1

        with self.assertRaises(ValidationError) as caught:
            BadDefault()
        self.assertEqual(caught.exception.issues[0].path, ("value",))

        @z.dataclass
        class BadDefaults:
            name: Annotated[str, z.string().min(3)] = "x"
            age: Annotated[int, z.integer().min(0)] = -1

        with self.assertRaises(ValidationError) as caught:
            BadDefaults()
        self.assertEqual(
            [(issue.code, issue.path) for issue in caught.exception.issues],
            [("too_small", ("name",)), ("too_small", ("age",))],
        )

    def test_validated_dataclass_default_factory_called_once(self):
        calls: list[int] = []

        @z.dataclass
        class Item:
            tags: list[str] = dataclasses.field(
                default_factory=lambda: calls.append(1) or [],
            )
            count: Annotated[int, z.integer().min(0)] = 1

        first = Item()
        second = Item()
        Item(tags=["x"])
        self.assertEqual(calls, [1, 1])
        self.assertIsNot(first.tags, second.tags)

    def test_validated_dataclass_strict_types(self):
        @z.dataclass
        class User:
            age: Annotated[int, z.integer()]
            nickname: str | None = None

        for value in ("30", True, 1.0):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                User(age=value)
        self.assertIsNone(User(age=30, nickname=None).nickname)
        with self.assertRaises(ValidationError):
            User(age=30, nickname=1)

    def test_validated_dataclass_operation_order(self):
        calls: list[str] = []

        def upper(value: str) -> str:
            calls.append(value)
            return value.upper()

        @z.dataclass
        class Normalized:
            name: Annotated[
                str, z.string().trim().min(2).overwrite(upper)
            ]

        self.assertEqual(Normalized(name=" ab ").name, "AB")
        self.assertEqual(calls, ["ab"])
        with self.assertRaises(ValidationError):
            Normalized(name=" a ")
        self.assertEqual(calls, ["ab"])

    def test_validated_dataclass_uses_one_native_evaluation(self):
        calls: list[int] = []

        @z.dataclass
        class One:
            value: Annotated[int, z.integer().overwrite(
                lambda value: calls.append(value) or value + 1,
            )]

        self.assertEqual(One(value=1).value, 2)
        self.assertEqual(calls, [1])

    def test_validated_dataclass_explicit_output_guard(self):
        with self.assertRaises(TypeError):
            @z.dataclass
            class StaticMismatch:
                value: Annotated[int, z.string()]

        @z.dataclass
        class DynamicMismatch:
            value: Annotated[str, z.unknown()]

        with self.assertRaises(TypeError) as caught:
            DynamicMismatch(value=1)
        self.assertIn("produced int", str(caught.exception))

        @z.dataclass
        class Items:
            values: Annotated[list[int], z.array(z.string())]

        with self.assertRaises(TypeError):
            Items(values=["x"])

        @z.dataclass
        class TupleValue:
            value: Annotated[tuple[int, str], z.unknown()]

        self.assertEqual(TupleValue(value=(1, "x")).value, (1, "x"))
        with self.assertRaises(TypeError):
            TupleValue(value=[1, "x"])

    def test_validated_dataclass_binding_errors(self):
        @z.dataclass
        class Simple:
            value: int

        for call in (
            lambda: Simple(),
            lambda: Simple(1, 2),
            lambda: Simple(value=1, extra=2),
            lambda: Simple(value=MISSING),
        ):
            with self.subTest(call=call), self.assertRaises(TypeError):
                call()

    def test_validated_dataclass_rejects_unsupported_features(self):
        with self.assertRaises(ValueError):
            @z.dataclass
            class OptionalNoDefault:
                value: Annotated[int, z.integer().optional()]

        with self.assertRaises(TypeError):
            @z.dataclass
            class InitVarMissingPostInitParam:
                value: int
                extra: dataclasses.InitVar[int]

                def __post_init__(self) -> None: ...

        with self.assertRaises(TypeError):
            @z.dataclass
            class BadPostInit:
                value: int

                def __post_init__(self, extra: int) -> None: ...

        with self.assertRaises(TypeError):
            @z.dataclass
            class CustomInit:
                value: int

                def __init__(self, value: int) -> None:
                    self.value = value

        @dataclasses.dataclass
        class CustomInitBase:
            value: int

            def __init__(self, value: int) -> None:
                self.value = value

        with self.assertRaises(TypeError):
            @z.dataclass
            class CustomInitChild(CustomInitBase):
                extra: int

        @dataclasses.dataclass(init=False)
        class NoInitBase:
            value: int

        with self.assertRaises(TypeError):
            @z.dataclass
            class NoInitChild(NoInitBase):
                extra: int

        with self.assertRaises(TypeError):
            @z.dataclass(weakref_slot=True)
            class Weak:
                value: int

        with self.assertRaises(TypeError):
            @z.dataclass(init=False)
            class NoInit:
                value: int

        with self.assertRaises(TypeError):
            @z.dataclass
            class MissingDefault:
                value: int = MISSING

        with self.assertRaises(TypeError):
            @z.dataclass
            class Preprocessed:
                value: Annotated[int, z.preprocess(int, z.integer())]

        with self.assertRaises(TypeError):
            @z.dataclass
            class LazyField:
                value: Annotated[int, z.lazy(lambda: z.integer())]

        @z.dataclass
        class MissingFactoryResult:
            value: int = dataclasses.field(
                default_factory=lambda: MISSING,
            )

        with self.assertRaises(TypeError):
            MissingFactoryResult()
        with self.assertRaises(TypeError):
            z.codecs.dataclass(MissingFactoryResult).parse({})

        @z.dataclass
        class ValidatedBase:
            value: int

        class ValidatedChild(ValidatedBase):
            pass

        with self.assertRaises(TypeError):
            z.codecs.dataclass(ValidatedChild)

    def test_validated_dataclass_frozen_slots_and_kw_only(self):
        @z.dataclass(frozen=True, slots=True, weakref_slot=True)
        class Frozen:
            name: str

        instance = Frozen("Alex")
        self.assertEqual(instance.name, "Alex")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            instance.name = "Other"
        self.assertIs(weakref.ref(instance)(), instance)

        @z.dataclass(kw_only=True)
        class Keyword:
            name: str

        with self.assertRaises(TypeError):
            Keyword("Alex")
        self.assertEqual(Keyword(name="Alex").name, "Alex")

    def test_validated_dataclass_replace_and_wrapping(self):
        @z.dataclass
        class Replaceable:
            value: Annotated[int, z.integer().min(0)]

        instance = Replaceable(1)
        self.assertEqual(dataclasses.replace(instance, value=2).value, 2)
        with self.assertRaises(ValidationError):
            dataclasses.replace(instance, value=-1)

        @dataclasses.dataclass
        class Existing:
            name: str

        Wrapped = z.dataclass(Existing)
        self.assertEqual(Wrapped("Alex").name, "Alex")
        with self.assertRaises(ValidationError):
            Wrapped(name=1)
        with self.assertRaises(TypeError):
            z.dataclass(frozen=True)(Existing)

        @dataclasses.dataclass
        class Outer:
            name: str

        z.dataclass(Outer)
        dataclasses.dataclass(Outer)
        with self.assertRaises(ValidationError):
            Outer(name=1)

    def test_validated_dataclass_codec_transform_runs_once(self):
        calls: list[int] = []
        post: list[int] = []

        def bump(value: int) -> int:
            calls.append(value)
            return value + 1

        @z.dataclass
        class Counter:
            value: Annotated[int, z.integer().overwrite(bump)]

            def __post_init__(self) -> None:
                post.append(self.value)

        direct = Counter(value=1)
        self.assertEqual(direct.value, 2)
        self.assertEqual(calls, [1])
        self.assertEqual(post, [2])

        calls.clear()
        post.clear()
        decoded = z.codecs.dataclass(Counter).parse({"value": 1})
        self.assertEqual(decoded.value, 2)
        self.assertEqual(calls, [1])
        self.assertEqual(post, [2])

    def test_validated_dataclass_codec_validates_defaults(self):
        @z.dataclass
        class Defaulted:
            count: Annotated[int, z.integer().min(0)] = 1

        self.assertEqual(z.codecs.dataclass(Defaulted).parse({}).count, 1)

        @z.dataclass
        class BadDefault:
            count: Annotated[int, z.integer().min(0)] = -1

        with self.assertRaises(ValidationError) as caught:
            z.codecs.dataclass(BadDefault).parse({})
        self.assertEqual(
            [(issue.code, issue.path) for issue in caught.exception.issues],
            [("too_small", ("count",))],
        )

    def test_validated_dataclass_codec_guards_override_outputs(self):
        @z.dataclass
        class Override:
            value: Annotated[int, z.integer()]

        codec = z.codecs.dataclass(Override, fields={"value": z.string()})
        with self.assertRaises(TypeError) as caught:
            codec.parse({"value": "12"})
        self.assertIn("expected int", str(caught.exception))

        @z.dataclass
        class Inferred:
            value: int

        inferred = z.codecs.dataclass(Inferred, fields={"value": z.string()})
        with self.assertRaises(TypeError):
            inferred.parse({"value": "12"})

    def test_validated_dataclass_codec_boundary_and_aggregation(self):
        @z.dataclass
        class User:
            name: Annotated[str, z.string().min(1)]

        codec = z.codecs.dataclass(User)
        self.assertEqual(codec.parse({"name": "Alex"}).name, "Alex")
        self.assertEqual(codec.encode(User("Alex")), {"name": "Alex"})
        self.assertEqual(
            to_json_schema(codec, io="input")["type"], "object",
        )

        @z.dataclass
        class Multi:
            created: Annotated[dt.date, z.codecs.iso_date()]
            count: Annotated[int, z.integer().min(0)] = -1

        with self.assertRaises(ValidationError) as caught:
            z.codecs.dataclass(Multi).parse({"created": "bad"})
        self.assertEqual(
            [(issue.code, issue.path) for issue in caught.exception.issues],
            [("invalid_format", ("created",)), ("too_small", ("count",))],
        )

        @z.dataclass
        class DefaultedCodec:
            created: Annotated[
                dt.date, z.codecs.iso_date()
            ] = dt.date(2026, 1, 1)

        self.assertEqual(
            z.codecs.dataclass(DefaultedCodec).parse({}).created,
            dt.date(2026, 1, 1),
        )

    def test_validated_dataclass_nested(self):
        @z.dataclass
        class Inner:
            value: Annotated[int, z.integer().min(0)]

        @z.dataclass
        class Outer:
            inner: Inner
            label: str

        outer = Outer(inner=Inner(1), label="x")
        self.assertEqual(outer.inner.value, 1)
        with self.assertRaises(ValidationError):
            Outer(inner={"value": 1}, label="x")
        parsed = z.codecs.dataclass(Outer).parse({
            "inner": {"value": 2}, "label": "y",
        })
        self.assertEqual(parsed.inner.value, 2)

    def test_validated_dataclass_generic_alias_guards(self):
        type Items[T] = list[T]
        type Maybe[T] = T | None
        type Box[T = int] = list[T]

        @z.dataclass
        class Batch:
            values: Items[int]
            maybe: Maybe[int]
            box: Box

        batch = Batch(values=[1, 2], maybe=None, box=[3])
        self.assertEqual(batch.values, [1, 2])
        self.assertIsNone(batch.maybe)
        self.assertEqual(batch.box, [3])
        with self.assertRaises(ValidationError):
            Batch(values=["x"], maybe=1, box=[3])

    def test_validated_dataclass_nested_explicit_metadata(self):
        @z.dataclass
        class NestedExplicit:
            values: list[Annotated[int, z.string()]]

        with self.assertRaises(TypeError):
            NestedExplicit(values=["x"])
        with self.assertRaises(TypeError):
            z.codecs.dataclass(NestedExplicit).parse({"values": ["x"]})

    def test_validated_dataclass_rejects_nested_preprocess(self):
        with self.assertRaises(TypeError):
            @z.dataclass
            class NestedPreprocess:
                extra: Annotated[
                    dict[str, int],
                    z.object({}).catchall(
                        z.preprocess(int, z.integer()),
                    ),
                ]

    def test_validated_dataclass_wire_codec_uses_decoded_values(self):
        @z.dataclass
        class Wired:
            created: Annotated[
                dt.datetime, z.codecs.iso_datetime().wire("createdAt")
            ]

        value = Wired(created=dt.datetime(2026, 1, 1, 12, 30))
        self.assertEqual(value.created, dt.datetime(2026, 1, 1, 12, 30))
        with self.assertRaises(ValidationError):
            Wired(created="2026-01-01T12:30:00")
        decoded = z.codecs.dataclass(Wired).parse({
            "createdAt": "2026-01-01T12:30:00",
        })
        self.assertEqual(decoded.created, dt.datetime(2026, 1, 1, 12, 30))

    def test_validated_dataclass_rejects_custom_hooks(self):
        @dataclasses.dataclass
        class ExistingCustomInit:
            value: int

            def __init__(self, value: int) -> None:
                self.value = value * 10

        with self.assertRaises(TypeError):
            z.dataclass(ExistingCustomInit)

        with self.assertRaises(TypeError):
            @z.dataclass
            class CustomNew:
                value: int

                def __new__(cls, *args: Any, **kwargs: Any) -> CustomNew:
                    return super().__new__(cls)

        with self.assertRaises(TypeError):
            @z.dataclass
            class CustomSetAttr:
                value: int

                def __setattr__(self, name: str, value: Any) -> None:
                    object.__setattr__(self, name, value)

        class Meta(type):
            def __call__(cls, *args: Any, **kwargs: Any) -> Any:
                return super().__call__(*args, **kwargs)

        @z.dataclass
        class WithMeta(metaclass=Meta):
            value: int

        self.assertEqual(WithMeta(value=1).value, 1)

    def test_validated_dataclass_inherited_fieldless_base(self):
        @dataclasses.dataclass
        class EmptyBase:
            pass

        @z.dataclass
        class Child(EmptyBase):
            value: int

        self.assertEqual(Child(value=1).value, 1)

    def test_validated_dataclass_primitive_subclass_guard(self):
        class MyInt(int):
            pass

        @z.dataclass
        class Guarded:
            value: Annotated[MyInt, z.unknown()]

        self.assertEqual(Guarded(value=MyInt(3)).value, MyInt(3))
        with self.assertRaises(TypeError):
            Guarded(value=3)

        @z.dataclass
        class Explicit:
            value: Annotated[MyInt, z.instance_of(MyInt)]

        self.assertIsInstance(Explicit(value=MyInt(3)).value, MyInt)
        with self.assertRaises(ValidationError):
            Explicit(value=3)

        @z.dataclass
        class Inferred:
            value: MyInt

        self.assertIsInstance(Inferred(value=MyInt(3)).value, MyInt)
        with self.assertRaises(ValidationError):
            Inferred(value=3)

    def test_validated_dataclass_extended_guard_annotations(self):
        class Blob:
            pass

        class Data(TypedDict):
            x: int

        @z.dataclass
        class Guards:
            blob: Annotated[Blob, z.unknown()]
            tags: Annotated[set[int], z.unknown()]
            keys: Annotated[dict[int, str], z.unknown()]
            values: Annotated[tuple[int, ...], z.unknown()]
            data: Annotated[Data, z.unknown()]

        item = Guards(
            blob=Blob(), tags={1, 2}, keys={1: "a"},
            values=(1, 2), data={"x": 1},
        )
        self.assertIsInstance(item.blob, Blob)
        self.assertEqual(item.tags, {1, 2})
        self.assertEqual(item.keys, {1: "a"})
        self.assertEqual(item.values, (1, 2))
        self.assertEqual(item.data, {"x": 1})

        invalid_values = (
            {"blob": object()},
            {"tags": {1, "x"}},
            {"keys": {"x": "a"}},
            {"values": [1, 2]},
            {"values": (1, "x")},
            {"data": "x"},
        )
        for changes in invalid_values:
            arguments = {
                "blob": Blob(), "tags": {1, 2}, "keys": {1: "a"},
                "values": (1, 2), "data": {"x": 1},
            }
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                Guards(**arguments)

    def test_validated_dataclass_rejects_type_changing_schemas(self):
        for schema in (
            z.string().transform(int),
            z.coerce.integer(),
            z.string().stringbool(),
            z.string().pipe(z.integer()),
        ):
            with self.subTest(schema=schema), self.assertRaises(TypeError):
                @z.dataclass
                class Field:
                    value: Annotated[Any, schema]

    def test_validated_dataclass_inheritance(self):
        factory_calls: list[int] = []

        @dataclasses.dataclass
        class Base:
            x: Annotated[int, z.integer().min(0)]
            tags: list[str] = dataclasses.field(
                default_factory=lambda: factory_calls.append(1) or [],
            )

        @z.dataclass
        class Sub(Base):
            y: str = "a"

        sub = Sub(x=1)
        self.assertEqual((sub.x, sub.tags, sub.y), (1, [], "a"))
        self.assertEqual(factory_calls, [1])
        with self.assertRaises(ValidationError):
            Sub(x=-1)
        self.assertEqual(
            z.codecs.dataclass(Sub).parse({"x": 2}).x, 2,
        )
        self.assertEqual(
            z.codecs.dataclass(Sub).encode(sub),
            {"x": 1, "tags": [], "y": "a"},
        )

        @z.dataclass
        class ValidatedBase:
            x: Annotated[int, z.integer().min(0)]

        @z.dataclass
        class ValidatedSub(ValidatedBase):
            y: str = "b"

        self.assertEqual(ValidatedSub(x=1).y, "b")
        with self.assertRaises(ValidationError):
            ValidatedSub(x=-1)
        self.assertEqual(
            z.codecs.dataclass(ValidatedSub).parse(
                {"x": 3, "y": "c"},
            ).y,
            "c",
        )

        @z.dataclass
        class LevelOne:
            x: int

        @z.dataclass
        class LevelTwo(LevelOne):
            y: int = 0

        @z.dataclass
        class LevelThree(LevelTwo):
            z: int = 0

        self.assertEqual(LevelThree(x=1).z, 0)

        @dataclasses.dataclass
        class OverrideBase:
            x: int = 0

        @z.dataclass
        class OverrideSub(OverrideBase):
            x: Annotated[int, z.integer().min(5)] = 5

        self.assertEqual(OverrideSub().x, 5)
        with self.assertRaises(ValidationError):
            OverrideSub(x=1)

        post_calls: list[int] = []

        @dataclasses.dataclass
        class PostBase:
            x: int

            def __post_init__(self) -> None:
                post_calls.append(self.x)

        @z.dataclass
        class PostSub(PostBase):
            y: int = 0

        PostSub(x=2)
        self.assertEqual(post_calls, [2])
        post_calls.clear()
        z.codecs.dataclass(PostSub).parse({"x": 3})
        self.assertEqual(post_calls, [3])

        @dataclasses.dataclass
        class InitVarBase:
            x: int
            token: dataclasses.InitVar[str]
            seen: str = dataclasses.field(init=False, default="")

            def __post_init__(self, token: str) -> None:
                self.seen = token

        @z.dataclass
        class InitVarSub(InitVarBase):
            y: int = 0

        inherited = InitVarSub(x=1, token="t")
        self.assertEqual(inherited.seen, "t")
        self.assertEqual(
            z.codecs.dataclass(InitVarSub).parse(
                {"x": 2, "token": "u"},
            ).seen,
            "u",
        )

        @z.dataclass(frozen=True)
        class FrozenBase:
            x: int

        @z.dataclass(frozen=True, slots=True)
        class FrozenSub(FrozenBase):
            y: int = 0

        self.assertEqual(FrozenSub(x=1, y=2).y, 2)

        class Mixin:
            pass

        @z.dataclass
        class Mixed(Mixin):
            x: int

        self.assertEqual(Mixed(x=1).x, 1)

        type_var = TypeVar("T")

        @dataclasses.dataclass
        class GenericBase(Generic[type_var]):
            value: type_var

        with self.assertRaises(TypeError):
            @z.dataclass
            class GenericSub(GenericBase[int]):
                extra: int = 0

    def test_validated_dataclass_multiple_bases(self):
        @dataclasses.dataclass
        class LeftBase:
            left: Annotated[int, z.integer().min(0)]

        @dataclasses.dataclass
        class RightBase:
            right: Annotated[str, z.string().min(1)]

        @z.dataclass
        class Combined(LeftBase, RightBase):
            extra: Annotated[int, z.integer().min(0)] = 0

        combined = Combined(right="x", left=1, extra=2)
        self.assertEqual(combined.left, 1)
        self.assertEqual(combined.right, "x")
        self.assertEqual(combined.extra, 2)
        for kwargs in (
            {"left": -1, "right": "x"},
            {"left": 1, "right": ""},
            {"left": 1, "right": "x", "extra": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(
                ValidationError,
            ):
                Combined(**kwargs)

        codec = z.codecs.dataclass(Combined)
        decoded = codec.parse({"right": "x", "left": 1})
        self.assertEqual(decoded, Combined(right="x", left=1))
        self.assertEqual(
            codec.encode(decoded),
            {"right": "x", "left": 1, "extra": 0},
        )
        self.assertEqual(
            Combined.parse({"right": "x", "left": 1}), decoded,
        )
        schema = Combined.to_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertEqual(
            set(schema["properties"]), {"left", "right", "extra"},
        )

        @z.dataclass(validate_assignment=True)
        class ValidatedLeft:
            left: Annotated[int, z.integer().min(0)]

        @z.dataclass
        class ValidatedRight:
            right: Annotated[str, z.string().min(1)]

        @z.dataclass
        class ValidatedCombined(ValidatedLeft, ValidatedRight):
            extra: int = 0

        validated = ValidatedCombined(left=1, right="x")
        self.assertEqual(
            ValidatedCombined.parse({"left": 1, "right": "x"}),
            validated,
        )
        self.assertEqual(
            ValidatedCombined.encode(validated),
            {"right": "x", "left": 1, "extra": 0},
        )
        validated.left = 2
        with self.assertRaises(ValidationError):
            validated.left = -1
        with self.assertRaises(ValidationError):
            validated.right = ""
        self.assertEqual(validated.left, 2)
        self.assertEqual(validated.right, "x")

        @dataclasses.dataclass
        class Root:
            root: int

        @dataclasses.dataclass
        class LeftBranch(Root):
            left: int

        @dataclasses.dataclass
        class RightBranch(Root):
            right: int

        @z.dataclass
        class Diamond(LeftBranch, RightBranch):
            extra: int = 0

        diamond = Diamond(root=1, left=2, right=3)
        self.assertEqual(
            Diamond.parse({"root": 1, "left": 2, "right": 3}),
            diamond,
        )
        self.assertEqual(diamond.root, 1)

        @dataclasses.dataclass
        class ValueBase:
            value: int

        @dataclasses.dataclass
        class TokenBase:
            token: dataclasses.InitVar[str]
            seen: str = dataclasses.field(init=False, default="")

            def __post_init__(self, token: str) -> None:
                self.seen = token

        @z.dataclass
        class WithToken(ValueBase, TokenBase):
            extra: int = 0

        with_token = WithToken(value=1, token="t")
        self.assertEqual(with_token.seen, "t")
        parsed = WithToken.parse({"value": 2, "token": "u"})
        self.assertEqual(parsed.seen, "u")
        self.assertEqual(
            WithToken.encode(parsed),
            {"value": 2, "extra": 0},
        )

    def test_validated_dataclass_initvar(self):
        calls: list[str] = []

        @z.dataclass
        class User:
            name: Annotated[str, z.string().min(1)]
            token: dataclasses.InitVar[
                Annotated[str, z.string().min(1)]
            ]
            seen: str = dataclasses.field(init=False, default="")

            def __post_init__(self, token: str) -> None:
                calls.append(token)
                self.seen = token

        user = User(name="Alex", token="abc")
        self.assertEqual(user.seen, "abc")
        self.assertEqual(calls, ["abc"])
        self.assertFalse(hasattr(user, "token"))
        self.assertIn("token", inspect.signature(User).parameters)
        with self.assertRaises(ValidationError) as caught:
            User(name="", token="")
        self.assertEqual(
            [(issue.code, issue.path) for issue in caught.exception.issues],
            [("too_small", ("name",)), ("too_small", ("token",))],
        )
        codec = z.codecs.dataclass(User)
        decoded = codec.parse({"name": "Alex", "token": "xyz"})
        self.assertEqual(decoded.seen, "xyz")
        self.assertEqual(codec.encode(decoded), {"name": "Alex"})

        @z.dataclass
        class Defaulted:
            value: int
            token: dataclasses.InitVar[str] = "default"
            seen: str = dataclasses.field(init=False, default="")

            def __post_init__(self, token: str) -> None:
                self.seen = token

        self.assertEqual(Defaulted(value=1).seen, "default")
        self.assertEqual(
            z.codecs.dataclass(Defaulted).parse({"value": 1}).seen,
            "default",
        )

        @z.dataclass
        class NoPost:
            value: int
            token: dataclasses.InitVar[str]

        self.assertEqual(NoPost(value=1, token="ignored").value, 1)

        @z.dataclass
        class Coded:
            value: int
            token: dataclasses.InitVar[
                Annotated[dt.date, z.codecs.iso_date()]
            ]
            seen: dt.date = dataclasses.field(
                init=False, default=dt.date(2000, 1, 1),
            )

            def __post_init__(self, token: dt.date) -> None:
                self.seen = token

        self.assertEqual(
            Coded(value=1, token=dt.date(2026, 1, 1)).seen,
            dt.date(2026, 1, 1),
        )
        with self.assertRaises(ValidationError):
            Coded(value=1, token="2026-01-01")
        coded = z.codecs.dataclass(Coded).parse({
            "value": 1, "token": "2026-01-01",
        })
        self.assertEqual(coded.seen, dt.date(2026, 1, 1))

    def test_validated_dataclass_init_false_fields(self):
        factory_calls: list[int] = []

        @z.dataclass
        class Item:
            value: int
            doubled: Annotated[int, z.integer().min(0)] = dataclasses.field(
                init=False, default=0,
            )
            tags: list[str] = dataclasses.field(
                init=False,
                default_factory=lambda: factory_calls.append(1) or [],
            )

            def __post_init__(self) -> None:
                self.doubled = self.value * 2

        item = Item(value=2)
        self.assertEqual(item.doubled, 4)
        self.assertEqual(item.tags, [])
        self.assertEqual(factory_calls, [1])
        self.assertEqual(
            z.codecs.dataclass(Item).parse({"value": 3}).doubled, 6,
        )
        self.assertEqual(factory_calls, [1, 1])
        self.assertEqual(
            z.codecs.dataclass(Item).encode(item), {"value": 2},
        )

        @z.dataclass
        class BadDefault:
            value: int
            count: Annotated[int, z.integer().min(0)] = dataclasses.field(
                init=False, default=-1,
            )

        with self.assertRaises(ValidationError) as caught:
            BadDefault(value=1)
        self.assertEqual(caught.exception.issues[0].path, ("count",))
        with self.assertRaises(ValidationError):
            z.codecs.dataclass(BadDefault).parse({"value": 1})

        @z.dataclass
        class PostOnly:
            value: int
            result: int = dataclasses.field(init=False)

            def __post_init__(self) -> None:
                self.result = self.value + 1

        self.assertEqual(PostOnly(value=1).result, 2)
        self.assertEqual(
            z.codecs.dataclass(PostOnly).parse({"value": 1}).result, 2,
        )

    def test_validated_dataclass_assignment_validation(self):
        @z.dataclass(validate_assignment=True)
        class User:
            name: Annotated[str, z.string().trim().min(1)]
            age: Annotated[int, z.integer().min(0)]
            nickname: str | None = None

        user = User(" Alex ", 30)
        self.assertEqual(user.name, "Alex")
        user.name = " Bob "
        self.assertEqual(user.name, "Bob")
        user.age = 0
        with self.assertRaises(ValidationError) as caught:
            user.age = -1
        self.assertEqual(caught.exception.issues[0].path, ("age",))
        self.assertEqual(user.age, 0)
        with self.assertRaises(TypeError):
            user.age = MISSING
        self.assertEqual(user.age, 0)
        user.nickname = "Al"
        self.assertEqual(user.nickname, "Al")
        user.extra = "allowed"
        self.assertEqual(user.extra, "allowed")

        with self.assertRaises(ValidationError):
            user.name = "   "
        self.assertEqual(user.name, "Bob")
        self.assertEqual(
            dataclasses.replace(user, age=1).age, 1,
        )
        with self.assertRaises(ValidationError):
            dataclasses.replace(user, age=-1)

        validation_calls: list[int] = []

        @z.dataclass(validate_assignment=True)
        class Counted:
            value: Annotated[
                int,
                z.integer().overwrite(
                    lambda value: validation_calls.append(value) or value,
                ),
            ]

        Counted(1)
        self.assertEqual(validation_calls, [1])
        counted = Counted(1)
        validation_calls.clear()
        counted.value = 2
        self.assertEqual(validation_calls, [2])

        @z.dataclass(validate_assignment=True)
        class Items:
            values: Annotated[list[int], z.array(z.integer().min(0))]

        items = Items([1])
        with self.assertRaises(ValidationError) as caught:
            items.values = [1, -1]
        self.assertEqual(caught.exception.issues[0].path, ("values", 1))
        self.assertEqual(items.values, [1])

        @z.dataclass(validate_assignment=True)
        class Event:
            when: Annotated[dt.datetime, z.codecs.iso_datetime()]

        event = Event(dt.datetime(2026, 1, 1))
        event.when = dt.datetime(2026, 1, 2)
        self.assertEqual(event.when, dt.datetime(2026, 1, 2))
        with self.assertRaises(ValidationError):
            event.when = "2026-01-03T00:00:00"

        @z.dataclass(validate_assignment=True)
        class Guarded:
            value: Annotated[int, z.unknown()]

        guarded = Guarded(1)
        with self.assertRaises(TypeError):
            guarded.value = "wrong"
        self.assertEqual(guarded.value, 1)

        @z.dataclass
        class Plain:
            value: int

        plain = Plain(1)
        plain.value = "unvalidated"
        self.assertEqual(plain.value, "unvalidated")

        with self.assertRaises(TypeError):
            @z.dataclass(frozen=True, validate_assignment=True)
            class Frozen:
                value: int

        @z.dataclass(slots=True, validate_assignment=True)
        class Slotted:
            value: Annotated[int, z.integer().min(0)]

        slotted = Slotted(1)
        slotted.value = 2
        with self.assertRaises(ValidationError):
            slotted.value = -1
        self.assertEqual(slotted.value, 2)
        with self.assertRaises(AttributeError):
            slotted.extra = "not a slot"

        @z.dataclass(validate_assignment=True)
        class PostInit:
            value: int
            doubled: Annotated[int, z.integer().min(0)] = dataclasses.field(
                init=False, default=0,
            )
            result: Annotated[int, z.integer().min(0)] = dataclasses.field(
                init=False,
            )

            def __post_init__(self) -> None:
                self.doubled = self.value * 2
                self.result = self.value + 1

        post = PostInit(2)
        self.assertEqual(post.doubled, 4)
        self.assertEqual(post.result, 3)
        with self.assertRaises(ValidationError):
            post.doubled = -1
        with self.assertRaises(ValidationError):
            post.result = -1
        self.assertEqual(post.doubled, 4)
        self.assertEqual(post.result, 3)

        @z.dataclass(validate_assignment=True)
        class BadPostInit:
            value: int

            def __post_init__(self) -> None:
                self.value = "bad"

        with self.assertRaises(ValidationError):
            BadPostInit(1)

        @z.dataclass(validate_assignment=True)
        class Base:
            value: Annotated[int, z.integer().min(0)]

        @z.dataclass
        class Child(Base):
            other: Annotated[int, z.integer().min(0)] = 0

        child = Child(1)
        child.other = 2
        with self.assertRaises(ValidationError):
            child.value = -1
        with self.assertRaises(ValidationError):
            child.other = -1

        @z.dataclass(validate_assignment=False)
        class UnvalidatedChild(Base):
            other: int = 0

        unvalidated = UnvalidatedChild(1)
        unvalidated.value = -1
        unvalidated.other = "unvalidated"
        self.assertEqual(unvalidated.value, -1)
        self.assertEqual(unvalidated.other, "unvalidated")

        @dataclasses.dataclass
        class Existing:
            value: int

        Wrapped = z.dataclass(Existing, validate_assignment=True)
        wrapped = Wrapped(1)
        with self.assertRaises(ValidationError):
            wrapped.value = "wrong"
        self.assertEqual(wrapped.value, 1)

    def test_validated_dataclass_classmethods(self):
        @z.dataclass
        class User:
            name: Annotated[str, z.string().min(1)]
            age: int = 0

        self.assertEqual(User.parse({"name": "Alex"}), User("Alex"))
        self.assertIsInstance(User.safe_parse({"name": "Alex"}), Success)
        self.assertIsInstance(User.safe_parse({"name": ""}), Failure)
        self.assertTrue(User.validate({"name": "Alex"}))
        self.assertFalse(User.validate({"name": ""}))
        self.assertEqual(
            User.parse_json('{"name": "Alex"}'), User("Alex"),
        )
        self.assertTrue(
            User.safe_parse_json('{"name": "Alex"}').success,
        )
        self.assertTrue(User.validate_json(b'{"name": "Alex"}'))
        self.assertEqual(
            User.encode(User("Alex")), {"name": "Alex", "age": 0},
        )
        self.assertIsInstance(User.safe_encode(User("Alex")), Success)
        self.assertEqual(User.to_json_schema()["type"], "object")
        self.assertEqual(
            User.to_json_schema(
                target="openapi-3.0", io="input",
            )["type"],
            "object",
        )
        with self.assertRaises(ValidationError):
            User.parse({"name": ""})

        @z.dataclass
        class Model(ValidatedModel):
            name: str

        self.assertEqual(Model.parse({"name": "Alex"}), Model("Alex"))
        self.assertEqual(Model.encode(Model("Alex")), {"name": "Alex"})
        self.assertEqual(Model.to_json_schema()["type"], "object")

        @z.dataclass(frozen=True, slots=True)
        class SlottedModel(ValidatedModel):
            name: str

        self.assertFalse(hasattr(SlottedModel("Alex"), "__dict__"))
        self.assertEqual(
            SlottedModel.parse({"name": "Alex"}), SlottedModel("Alex"),
        )

        class Undecorated(ValidatedModel):
            pass

        with self.assertRaises(TypeError):
            Undecorated.parse({})

        @z.dataclass
        class Custom:
            name: str

            @classmethod
            def parse(cls, value):
                return "custom"

        self.assertEqual(Custom.parse({"name": "x"}), "custom")

        @z.dataclass
        class FieldCollision:
            parse: str

        self.assertEqual(FieldCollision(parse="x").parse, "x")

        @z.dataclass
        class WithInitVar:
            name: str
            token: dataclasses.InitVar[str]
            seen: str = dataclasses.field(init=False, default="")

            def __post_init__(self, token: str) -> None:
                self.seen = token

        parsed = WithInitVar.parse({"name": "Alex", "token": "t"})
        self.assertEqual(parsed.seen, "t")
        self.assertEqual(WithInitVar.encode(parsed), {"name": "Alex"})

    def test_public_annotations_resolve(self):
        self.assertIn("params", get_type_hints(z.Issue))
        self.assertIn("path", get_type_hints(z.Issue))
        self.assertIn("return", get_type_hints(z.tuple))


class JSONSchemaTests(unittest.TestCase):
    def test_primitives_and_constraints(self):
        self.assertEqual(
            to_json_schema(z.string().min(2).max(5)),
            {"type": "string", "minLength": 2, "maxLength": 5},
        )
        self.assertEqual(
            to_json_schema(z.string().regex(r"^a+$")),
            {"type": "string", "pattern": "^a+$"},
        )
        self.assertEqual(
            to_json_schema(z.string().email()),
            {"type": "string", "format": "email"},
        )
        self.assertEqual(
            to_json_schema(z.integer().min(1).max(10)),
            {"type": "integer", "minimum": 1, "maximum": 10},
        )
        self.assertEqual(
            to_json_schema(z.number().positive()),
            {"type": "number", "exclusiveMinimum": 0},
        )
        self.assertEqual(
            to_json_schema(z.number().gt(1).lt(10).multiple_of(3)),
            {
                "type": "number",
                "exclusiveMinimum": 1,
                "exclusiveMaximum": 10,
                "multipleOf": 3,
            },
        )
        self.assertEqual(
            to_json_schema(z.number().gte(1).lte(10)),
            {"type": "number", "minimum": 1, "maximum": 10},
        )
        self.assertEqual(
            to_json_schema(z.number().gt(1).lt(10), target="openapi-3.0"),
            {
                "type": "number",
                "minimum": 1,
                "exclusiveMinimum": True,
                "maximum": 10,
                "exclusiveMaximum": True,
            },
        )
        self.assertEqual(
            to_json_schema(z.number().positive().gt(-1)),
            {"type": "number", "exclusiveMinimum": 0},
        )
        self.assertEqual(
            to_json_schema(z.number().gt(5).min(10)),
            {"type": "number", "minimum": 10},
        )
        self.assertEqual(
            to_json_schema(z.number().min(10).gt(5)),
            {"type": "number", "minimum": 10},
        )
        self.assertEqual(
            to_json_schema(z.number().gt(5).gte(5)),
            {"type": "number", "exclusiveMinimum": 5},
        )
        self.assertEqual(
            to_json_schema(z.string().starts_with("a.b")),
            {"type": "string", "pattern": "^a\\.b"},
        )
        self.assertEqual(
            to_json_schema(z.string().ends_with("]")),
            {"type": "string", "pattern": "\\]$"},
        )
        self.assertEqual(
            to_json_schema(z.string().includes("a+b")),
            {"type": "string", "pattern": "a\\+b"},
        )
        combined = to_json_schema(z.string().starts_with("a").ends_with("z"))
        self.assertEqual(combined["type"], "string")
        self.assertEqual(
            {check["pattern"] for check in combined["allOf"]},
            {"^a", "z$"},
        )
        mixed = to_json_schema(z.string().regex("a").starts_with("b"))
        self.assertEqual(
            {check["pattern"] for check in mixed["allOf"]},
            {"a", "^b"},
        )
        self.assertEqual(
            to_json_schema(z.string().email().starts_with("a")),
            {"type": "string", "format": "email", "pattern": "^a"},
        )
        self.assertEqual(
            to_json_schema(z.string().ipv6()),
            {"type": "string", "format": "ipv6"},
        )
        multiple = to_json_schema(z.number().multiple_of(2).multiple_of(3))
        self.assertEqual(multiple["type"], "number")
        self.assertEqual(
            {check["multipleOf"] for check in multiple["allOf"]},
            {2, 3},
        )
        self.assertEqual(to_json_schema(z.number().int()), {"type": "integer"})
        self.assertEqual(
            to_json_schema(z.string().length(2)),
            {"type": "string", "minLength": 2, "maxLength": 2},
        )
        self.assertEqual(to_json_schema(z.boolean()), {"type": "boolean"})
        self.assertEqual(to_json_schema(z.none()), {"type": "null"})
        self.assertEqual(to_json_schema(z.unknown()), {})
        self.assertEqual(z.string().to_json_schema(), {"type": "string"})
        self.assertEqual(z.to_json_schema(z.string()), {"type": "string"})

    def test_literals_enums_and_openapi(self):
        self.assertEqual(to_json_schema(z.literal("a")), {"const": "a"})
        self.assertEqual(
            to_json_schema(z.literal("a"), target="openapi-3.0"),
            {"enum": ["a"]},
        )
        self.assertEqual(
            to_json_schema(z.enum(["a", "b"])),
            {"enum": ["a", "b"]},
        )
        self.assertEqual(
            to_json_schema(z.object({"a": z.integer(), "b": z.string()}).keyof()),
            {"enum": ["a", "b"]},
        )
        self.assertEqual(
            to_json_schema(z.number().positive(), target="openapi-3.0"),
            {"type": "number", "minimum": 0, "exclusiveMinimum": True},
        )
        self.assertEqual(
            to_json_schema(z.string().nullable(), target="openapi-3.0"),
            {"type": "string", "nullable": True},
        )
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.none(), target="openapi-3.0")
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.literal(None), target="openapi-3.0")
        self.assertEqual(
            to_json_schema(
                z.enum(["a", None]), target="openapi-3.0",
                unrepresentable="any",
            ),
            {},
        )

    def test_arrays_tuples_and_records(self):
        self.assertEqual(
            to_json_schema(z.array(z.string().min(1)).min(2).max(4)),
            {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 2,
                "maxItems": 4,
            },
        )
        self.assertEqual(
            to_json_schema(z.tuple(z.string(), z.integer())),
            {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "integer"}],
                "items": False,
                "minItems": 2,
                "maxItems": 2,
            },
        )
        with self.assertRaises(UnrepresentableError) as caught:
            to_json_schema(z.tuple(z.string()), target="openapi-3.0")
        self.assertEqual(caught.exception.path, ())
        self.assertEqual(
            to_json_schema(
                z.tuple(z.string()), target="openapi-3.0", unrepresentable="any",
            ),
            {},
        )
        self.assertEqual(
            to_json_schema(z.array(z.integer()).length(1)),
            {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 1,
                "maxItems": 1,
            },
        )
        self.assertEqual(
            to_json_schema(z.record(z.integer())),
            {
                "type": "object",
                "additionalProperties": {"type": "integer"},
            },
        )
        self.assertEqual(
            to_json_schema(z.map(z.string(), z.integer().describe("Count"))),
            {
                "type": "object",
                "propertyNames": {"type": "string"},
                "additionalProperties": {
                    "type": "integer", "description": "Count",
                },
            },
        )
        self.assertEqual(
            to_json_schema(z.record(z.integer().describe("Count"))),
            {
                "type": "object",
                "additionalProperties": {
                    "type": "integer", "description": "Count",
                },
            },
        )
        self.assertEqual(
            to_json_schema(z.tuple(z.string()).rest(z.integer())),
            {
                "type": "array",
                "prefixItems": [{"type": "string"}],
                "items": {"type": "integer"},
                "minItems": 1,
            },
        )
        self.assertEqual(
            to_json_schema(z.record(z.string().min(1), z.integer())),
            {
                "type": "object",
                "propertyNames": {"type": "string", "minLength": 1},
                "additionalProperties": {"type": "integer"},
            },
        )
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.record(z.integer(), z.string()))
        self.assertEqual(
            to_json_schema(
                z.record(z.integer(), z.string()), unrepresentable="any",
            ),
            {},
        )
        with self.assertRaises(UnrepresentableError):
            to_json_schema(
                z.record(z.string(), z.integer()), target="openapi-3.0",
            )

    def test_objects_requiredness_and_additional_properties(self):
        schema = z.object({
            "name": z.string(),
            "age": z.integer().optional(),
            "role": z.string().default("user"),
        })
        properties = {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "role": {"type": "string", "default": "user"},
        }
        self.assertEqual(
            to_json_schema(schema),
            {
                "type": "object",
                "properties": properties,
                "required": ["name", "role"],
                "additionalProperties": False,
            },
        )
        self.assertEqual(
            to_json_schema(schema, io="input"),
            {
                "type": "object",
                "properties": properties,
                "required": ["name"],
            },
        )
        self.assertEqual(
            to_json_schema(z.object({"a": z.string()}).strict()),
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
        )
        self.assertEqual(
            to_json_schema(z.object({"a": z.string()}).passthrough()),
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": True,
            },
        )
        self.assertEqual(
            to_json_schema(z.object({
                "x": z.string().optional(),
                "y": z.string().default("z"),
            }).required()),
            {
                "type": "object",
                "properties": {"x": {"type": "string"}, "y": {"type": "string"}},
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        )
        self.assertEqual(
            to_json_schema(z.object({"a": z.string()}).catchall(z.integer())),
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": {"type": "integer"},
            },
        )
        self.assertEqual(
            to_json_schema(z.object({
                "a": z.string(), "b": z.string(),
            }).partial("a")),
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["b"],
                "additionalProperties": False,
            },
        )

    def test_unions_and_discriminated_unions(self):
        self.assertEqual(
            to_json_schema(z.union(z.string(), z.integer())),
            {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        )
        schema = z.discriminated_union("kind", {
            "a": z.object({"kind": z.literal("a"), "id": z.integer()}),
            "b": z.object({"kind": z.literal("b")}),
        })
        exported = to_json_schema(schema)
        self.assertEqual(len(exported["oneOf"]), 2)
        self.assertEqual(
            exported["oneOf"][0]["properties"]["kind"], {"const": "a"},
        )
        self.assertEqual(
            exported["oneOf"][1]["properties"]["kind"], {"const": "b"},
        )
        openapi = to_json_schema(schema, target="openapi-3.0")
        self.assertEqual(openapi["discriminator"], {"propertyName": "kind"})
        self.assertEqual(
            openapi["oneOf"][0]["properties"]["kind"], {"enum": ["a"]},
        )

    def test_intersections(self):
        schema = z.intersection(
            z.object({"a": z.string()}),
            z.object({"b": z.integer()}),
        )
        exported = to_json_schema(schema)
        self.assertEqual(len(exported["allOf"]), 2)
        self.assertNotIn("additionalProperties", exported["allOf"][0])
        self.assertNotIn("additionalProperties", exported["allOf"][1])
        openapi = to_json_schema(schema, target="openapi-3.0")
        self.assertEqual(
            openapi["allOf"][0]["properties"]["a"], {"type": "string"},
        )
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.intersection(z.string(), z.string()))
        self.assertEqual(
            to_json_schema(
                z.intersection(z.string(), z.string()),
                unrepresentable="any",
            ),
            {},
        )
        strict = to_json_schema(z.intersection(
            z.object({"a": z.string()}).strict(),
            z.object({"b": z.integer()}).strict(),
        ))
        self.assertIs(strict["allOf"][0]["additionalProperties"], False)
        self.assertEqual(
            to_json_schema(schema.meta({"title": "Both"}))["title"], "Both",
        )

    def test_coercion_and_stringbool(self):
        self.assertEqual(
            to_json_schema(z.coerce.number().min(1)),
            {"type": "number", "minimum": 1},
        )
        self.assertEqual(
            to_json_schema(z.coerce.string().min(1)),
            {"type": "string", "minLength": 1},
        )
        self.assertEqual(
            to_json_schema(z.coerce.integer()),
            {"type": "integer"},
        )
        self.assertEqual(
            to_json_schema(z.coerce.boolean()),
            {"type": "boolean"},
        )
        schema = z.string().stringbool()
        self.assertEqual(to_json_schema(schema), {"type": "boolean"})
        self.assertEqual(
            to_json_schema(schema, io="input"),
            {
                "type": "string",
                "enum": [
                    "true", "1", "yes", "on",
                    "false", "0", "no", "off",
                ],
            },
        )

    def test_codecs(self):
        numeric = z.codec(
            z.string().regex(r"^\d+$"), z.integer(), decode=int, encode=str,
        )
        self.assertEqual(
            to_json_schema(numeric, io="input"),
            {"type": "string", "pattern": "^\\d+$"},
        )
        self.assertEqual(to_json_schema(numeric, io="output"), {"type": "integer"})
        schema = z.object({"n": numeric, "name": z.string()})
        self.assertEqual(
            schema.to_json_schema(io="input"),
            {
                "type": "object",
                "properties": {
                    "n": {"type": "string", "pattern": "^\\d+$"},
                    "name": {"type": "string"},
                },
                "required": ["n", "name"],
            },
        )
        self.assertEqual(
            schema.to_json_schema(io="output"),
            {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "name": {"type": "string"},
                },
                "required": ["n", "name"],
                "additionalProperties": False,
            },
        )
        optional = z.object({"n": numeric.optional()})
        self.assertEqual(
            optional.to_json_schema(io="input"),
            {
                "type": "object",
                "properties": {
                    "n": {"type": "string", "pattern": "^\\d+$"},
                },
            },
        )
        self.assertEqual(
            optional.to_json_schema(io="output"),
            {
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "additionalProperties": False,
            },
        )
        titled = numeric.meta({"title": "N"})
        self.assertEqual(
            to_json_schema(titled, io="input"),
            {"type": "string", "pattern": "^\\d+$", "title": "N"},
        )
        self.assertEqual(
            to_json_schema(titled, io="output"),
            {"type": "integer", "title": "N"},
        )
        self.assertEqual(
            to_json_schema(z.array(numeric), io="input")["items"],
            {"type": "string", "pattern": "^\\d+$"},
        )
        self.assertEqual(
            to_json_schema(z.array(numeric), io="output")["items"],
            {"type": "integer"},
        )

        optional_output = z.codec(
            z.string(),
            z.string().optional(),
            decode=lambda value: value,
            encode=lambda value: value or "",
        )
        optional_output_schema = z.object({"x": optional_output})
        self.assertEqual(
            optional_output_schema.to_json_schema(io="input")["required"],
            ["x"],
        )
        self.assertNotIn(
            "required", optional_output_schema.to_json_schema(io="output"),
        )
        optional_input = z.codec(
            z.string().optional(),
            z.integer(),
            decode=lambda value: len(value),
            encode=str,
        )
        optional_input_schema = z.object({"x": optional_input})
        self.assertNotIn(
            "required", optional_input_schema.to_json_schema(io="input"),
        )
        self.assertEqual(
            optional_input_schema.to_json_schema(io="output")["required"],
            ["x"],
        )

        datetime_codec = z.codec(
            z.string().datetime(),
            z.datetime(),
            decode=dt.datetime.fromisoformat,
            encode=dt.datetime.isoformat,
        )
        self.assertEqual(
            to_json_schema(datetime_codec, io="input"),
            {"type": "string", "format": "date-time"},
        )
        with self.assertRaises(UnrepresentableError):
            to_json_schema(datetime_codec, io="output")
        with self.assertRaises(UnrepresentableError):
            to_json_schema(
                z.object({"created": datetime_codec}), io="output",
            )

    def test_builtin_codecs(self):
        for codec, format_name in (
            (z.codecs.iso_date(), "date"),
            (z.codecs.iso_time(), "time"),
            (z.codecs.iso_datetime(), "date-time"),
        ):
            with self.subTest(format=format_name):
                self.assertEqual(
                    to_json_schema(codec, io="input"),
                    {"type": "string", "format": format_name},
                )
                with self.assertRaises(UnrepresentableError):
                    to_json_schema(codec, io="output")

        decimal_json = to_json_schema(z.codecs.decimal(), io="input")
        self.assertEqual(decimal_json["type"], "string")
        self.assertIn("pattern", decimal_json)
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.codecs.decimal(), io="output")

        self.assertEqual(
            to_json_schema(z.codecs.json_text(), io="input"),
            {"type": "string"},
        )
        self.assertEqual(to_json_schema(z.codecs.json_text(), io="output"), {})

        self.assertEqual(
            to_json_schema(z.codecs.path(), io="input"),
            {"type": "string"},
        )
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.codecs.path(), io="output")

        base64_json = to_json_schema(z.codecs.bytes_base64(), io="input")
        self.assertEqual(base64_json["type"], "string")
        self.assertIn("pattern", base64_json)
        urlsafe_json = to_json_schema(
            z.codecs.bytes_base64(urlsafe=True), io="input",
        )
        self.assertIn("pattern", urlsafe_json)
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.codecs.bytes_base64(), io="output")

    def test_python_scalars_and_native_enums(self):
        self.assertEqual(to_json_schema(z.json()), {})
        class Color(enum.Enum):
            RED = "red"
            BLUE = "blue"

        self.assertEqual(
            to_json_schema(z.native_enum(Color)),
            {"enum": ["red", "blue"]},
        )
        for schema in (
            z.date(),
            z.datetime(),
            z.time(),
            z.timedelta(),
            z.decimal(),
            z.bytes(),
            z.path(),
        ):
            with self.subTest(schema=schema):
                with self.assertRaises(UnrepresentableError):
                    to_json_schema(schema)
                self.assertEqual(
                    to_json_schema(schema, unrepresentable="any"), {},
                )

        class Odd(enum.Enum):
            VALUE = object()

        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.native_enum(Odd))

    def test_nullable_optional_and_defaults(self):
        self.assertEqual(
            to_json_schema(z.string().nullable()),
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
        )
        self.assertEqual(
            to_json_schema(z.string().nullish()),
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
        )
        self.assertEqual(
            to_json_schema(z.string().default("x")),
            {"type": "string", "default": "x"},
        )
        self.assertEqual(to_json_schema(z.string().optional()), {"type": "string"})
        self.assertEqual(
            to_json_schema(z.object({"x": z.string().nullish()})),
            {
                "type": "object",
                "properties": {
                    "x": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "additionalProperties": False,
            },
        )
        self.assertEqual(
            to_json_schema(z.tuple(z.integer()).default((1,))),
            {
                "type": "array",
                "prefixItems": [{"type": "integer"}],
                "items": False,
                "minItems": 1,
                "maxItems": 1,
                "default": [1],
            },
        )

    def test_metadata_is_merged(self):
        schema = z.object({
            "name": z.string().meta({"title": "Name", "examples": ["Alex"]}),
        }).meta({"title": "User", "description": "A user"})
        self.assertEqual(
            to_json_schema(schema),
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "title": "Name",
                        "examples": ["Alex"],
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
                "title": "User",
                "description": "A user",
            },
        )
        self.assertEqual(
            to_json_schema(z.array(z.string().meta({"title": "Tag"}))),
            {"type": "array", "items": {"type": "string", "title": "Tag"}},
        )
        self.assertEqual(
            to_json_schema(z.object({
                "x": z.integer().meta({"title": "X"}).optional(),
            })),
            {
                "type": "object",
                "properties": {"x": {"type": "integer", "title": "X"}},
                "additionalProperties": False,
            },
        )
        self.assertEqual(
            to_json_schema(z.tuple(z.string().meta({"title": "A"})))[
                "prefixItems"
            ][0]["title"],
            "A",
        )
        self.assertEqual(
            to_json_schema(z.union(
                z.string().meta({"title": "A"}), z.integer(),
            ))["anyOf"][0]["title"],
            "A",
        )
        self.assertEqual(
            to_json_schema(z.record(z.integer().meta({"title": "V"})))[
                "additionalProperties"
            ]["title"],
            "V",
        )
        self.assertEqual(
            to_json_schema(z.discriminated_union("kind", {
                "a": z.object({"kind": z.literal("a")}).meta({"title": "A"}),
            }))["oneOf"][0]["title"],
            "A",
        )
        self.assertEqual(
            to_json_schema(z.string().meta({"type": "integer"})),
            {"type": "integer"},
        )
        self.assertEqual(
            to_json_schema(
                z.string().meta({"examples": ["a", "b"]}),
                target="openapi-3.0",
            ),
            {"type": "string", "example": "a"},
        )
        self.assertEqual(
            to_json_schema(z.object({"a": z.string()}).catchall(
                z.integer().meta({"title": "Extra"}),
            ))["additionalProperties"]["title"],
            "Extra",
        )
        keyed = to_json_schema(z.record(
            z.string().meta({"title": "Key"}),
            z.integer().meta({"title": "Value"}),
        ))
        self.assertEqual(keyed["propertyNames"]["title"], "Key")
        self.assertEqual(keyed["additionalProperties"]["title"], "Value")
        self.assertEqual(
            to_json_schema(z.tuple(z.string()).rest(
                z.integer().meta({"title": "Rest"}),
            ))["items"]["title"],
            "Rest",
        )

    def test_async_nodes_follow_unsupported_and_missing_rules(self):
        async def callback(value):
            return True

        schema = z.object({
            "value": z.string().optional().refine_async(callback),
            "extra": z.string().optional().super_refine_async(callback),
        })
        with self.assertRaises(UnrepresentableError):
            to_json_schema(schema)
        exported = to_json_schema(schema, unrepresentable="any")
        self.assertEqual(exported["properties"]["value"], {})
        self.assertEqual(exported["properties"]["extra"], {})
        self.assertNotIn("required", exported)
        with self.assertRaises(TypeError):
            schema.encode({})

    def test_more_async_nodes_follow_unsupported_and_missing_rules(self):
        async def callback(value):
            return value

        async def factory():
            return "x"

        cases = (
            z.preprocess_async(callback, z.string()),
            z.string().overwrite_async(callback),
            z.string().default_factory_async(factory),
            z.string().catch_async(factory),
        )
        for schema in cases:
            with self.subTest(schema=schema):
                with self.assertRaises(UnrepresentableError):
                    to_json_schema(schema)
                self.assertEqual(
                    to_json_schema(schema, unrepresentable="any"), {},
                )
                with self.assertRaises(TypeError):
                    schema.encode("x")

        missing = z.object({
            "factory": z.string().default_factory_async(factory),
            "caught": z.string().catch_async(factory),
        })
        input_export = to_json_schema(
            missing, io="input", unrepresentable="any",
        )
        self.assertNotIn("required", input_export)
        output_export = to_json_schema(
            missing, io="output", unrepresentable="any",
        )
        self.assertEqual(
            set(output_export["required"]), {"factory", "caught"},
        )

    def test_recursive_lazy_refs(self):
        node = z.lazy(lambda: z.object({
            "name": z.string(),
            "children": node.array().default([]),
        }).meta({"id": "Node"}))
        expected_definition = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "children": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Node"},
                    "default": [],
                },
            },
            "required": ["name", "children"],
            "additionalProperties": False,
        }
        self.assertEqual(to_json_schema(node), {
            "$ref": "#/$defs/Node",
            "$defs": {"Node": expected_definition},
        })
        self.assertEqual(to_json_schema(node, target="openapi-3.0"), {
            "$ref": "#/components/schemas/Node",
            "components": {"schemas": {"Node": {
                **expected_definition,
                "properties": {
                    **expected_definition["properties"],
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Node"},
                        "default": [],
                    },
                },
            }}},
        })
        self.assertEqual(
            node.parse({"name": "root", "children": []}),
            {"name": "root", "children": []},
        )

        calls: list[int] = []

        def factory():
            calls.append(1)
            return z.object({
                "value": z.integer(),
                "next": chain.optional(),
            }).meta({"id": "Chain"})

        chain = z.lazy(factory)
        exported = to_json_schema(chain)
        self.assertEqual(calls, [1])
        self.assertEqual(exported["$ref"], "#/$defs/Chain")
        self.assertEqual(
            exported["$defs"]["Chain"]["properties"]["next"],
            {"$ref": "#/$defs/Chain"},
        )
        self.assertNotIn("id", exported["$defs"]["Chain"])

        leaf = z.lazy(lambda: z.string().meta({"id": "Leaf"}))
        self.assertEqual(to_json_schema(leaf), {
            "$ref": "#/$defs/Leaf",
            "$defs": {"Leaf": {"type": "string"}},
        })
        escaped = z.lazy(lambda: z.string()).meta({"id": "a/b"})
        escaped_export = to_json_schema(escaped)
        self.assertEqual(escaped_export["$ref"], "#/$defs/a~1b")
        self.assertIn("a/b", escaped_export["$defs"])

        self_ref = None
        self_ref = z.lazy(
            lambda: z.object({"next": self_ref.optional()}),
        ).meta({"id": "Self"})
        self_export = to_json_schema(self_ref)
        self.assertEqual(self_export["$ref"], "#/$defs/Self")
        self.assertEqual(
            self_export["$defs"]["Self"]["properties"]["next"],
            {"$ref": "#/$defs/Self"},
        )

        optional_lazy = z.lazy(
            lambda: z.string().optional(),
        ).meta({"id": "Maybe"})
        optional_export = to_json_schema(
            z.object({"value": optional_lazy}), io="output",
        )
        self.assertNotIn("required", optional_export)
        self.assertEqual(
            optional_export["properties"]["value"],
            {"$ref": "#/$defs/Maybe"},
        )

        described = z.object({"node": node.describe("Child node")})
        draft = to_json_schema(described, io="input")
        self.assertEqual(draft["properties"]["node"], {
            "$ref": "#/$defs/Node", "description": "Child node",
        })
        openapi = to_json_schema(described, io="input", target="openapi-3.0")
        self.assertEqual(openapi["properties"]["node"], {
            "allOf": [{"$ref": "#/components/schemas/Node"}],
            "description": "Child node",
        })

        without_id = z.lazy(lambda: z.string())
        with self.assertRaises(UnrepresentableError):
            to_json_schema(without_id)
        self.assertEqual(to_json_schema(without_id, unrepresentable="any"), {})

        duplicate_a = z.lazy(lambda: z.string()).meta({"id": "Same"})
        duplicate_b = z.lazy(lambda: z.integer()).meta({"id": "Same"})
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.object({
                "a": duplicate_a, "b": duplicate_b,
            }))

        def broken():
            raise RuntimeError("boom")

        with self.assertRaises(UnrepresentableError) as caught:
            to_json_schema(z.lazy(broken).meta({"id": "Broken"}))
        self.assertIn("lazy factory failed", str(caught.exception))

    def test_unrepresentable_nodes(self):
        cases = (
            z.string().transform(len),
            z.string().refine(lambda value: True),
            z.string().super_refine(lambda value: None),
            z.string().trim(),
            z.string().default_factory(lambda: "x"),
            z.instance_of(int),
            z.preprocess(str, z.string()),
            z.string().pipe(z.string()),
            z.lazy(lambda: z.string()),
            z.string().regex("a").regex("b"),
            z.string().regex(re.compile("a", re.IGNORECASE)),
            z.integer().catch(0),
            z.string().overwrite(str.upper),
            z.record(z.integer(), z.string()),
            z.record(z.string().transform(str.upper), z.integer()),
        )
        for schema in cases:
            with self.subTest(schema=schema):
                with self.assertRaises(UnrepresentableError):
                    to_json_schema(schema)
                self.assertEqual(
                    to_json_schema(schema, unrepresentable="any"), {},
                )

        nested = z.object({"value": z.string().transform(len)})
        with self.assertRaises(UnrepresentableError) as caught:
            to_json_schema(nested)
        self.assertEqual(caught.exception.path, ("properties", "value"))
        self.assertEqual(
            to_json_schema(nested, unrepresentable="any"),
            {
                "type": "object",
                "properties": {"value": {}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        with self.assertRaises(UnrepresentableError):
            to_json_schema(z.unknown().default(object()))
        self.assertEqual(
            to_json_schema(z.unknown().default(object()), unrepresentable="any"),
            {},
        )

    def test_invalid_arguments(self):
        with self.assertRaises(TypeError):
            to_json_schema("not a schema")
        for kwargs in (
            {"target": "draft-07"},
            {"io": "both"},
            {"unrepresentable": "ignore"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                to_json_schema(z.string(), **kwargs)


if __name__ == "__main__":
    unittest.main()
