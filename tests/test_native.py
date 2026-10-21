"""Integration checks for behavior across the Python/Rust boundary."""

import gc
import importlib.machinery
import json
import math
import sys
import unittest
import weakref

from zodish import MISSING, Failure, Issue, ValidationError, _native, z


class NativeTests(unittest.TestCase):
    def test_native_issue_bundle_survives_gc_and_formats(self):
        failure = z.object({
            "name": z.string().min(2),
            "tags": z.string().array(),
        }).safe_parse({"name": "x", "tags": ["ok", 1]})
        self.assertIsInstance(failure, Failure)
        error = failure.error
        gc.collect()
        self.assertIn("name: Must contain at least 2 characters", str(error))
        self.assertEqual(
            [(issue.code, issue.path) for issue in error.issues],
            [("too_small", ("name",)), ("invalid_type", ("tags", 1))],
        )

    def test_extension_is_required_and_loaded(self):
        self.assertEqual(_native.BACKEND, "rust")
        self.assertTrue(any(
            _native.__file__.endswith(suffix)
            for suffix in importlib.machinery.EXTENSION_SUFFIXES
        ))
        self.assertIsInstance(z.string()._node, _native.NativeSchema)
        self.assertFalse(hasattr(z.string(), "_parser"))

    def test_native_nodes_expose_read_only_introspection(self):
        node = z.string().min(3)._node
        self.assertEqual(node.kind, "length_min")
        self.assertEqual(len(node.children), 1)
        self.assertEqual(node.children[0].kind, "string")
        self.assertEqual(node.args[0], 3)
        self.assertEqual(node.args[1], "Must contain at least 3 characters")
        self.assertIsInstance(node.children, tuple)
        self.assertIsInstance(node.args, tuple)
        with self.assertRaises(AttributeError):
            node.kind = "string"

        object_node = z.object({"name": z.string()})._node
        self.assertEqual(object_node.kind, "object")
        self.assertEqual(object_node.children[0].kind, "string")
        self.assertEqual(object_node.args[0], ("name",))
        self.assertEqual(object_node.args[1], "strip")

        union_node = z.discriminated_union("kind", {
            "a": z.object({"kind": z.literal("a")}),
        })._node
        self.assertEqual(union_node.kind, "discriminated_union")
        self.assertEqual(union_node.args[0], "kind")
        self.assertEqual(union_node.args[1], ("a",))

        self.assertEqual(z.number().gt(1)._node.kind, "exclusive_min")
        self.assertEqual(z.number().lt(1)._node.kind, "exclusive_max")
        self.assertEqual(z.number().multiple_of(2)._node.kind, "multiple_of")
        self.assertEqual(z.string().starts_with("a")._node.kind, "starts_with")
        self.assertEqual(z.string().ends_with("a")._node.kind, "ends_with")
        self.assertEqual(z.string().includes("a")._node.kind, "includes")
        self.assertEqual(z.string().ipv6()._node.kind, "ipv6")

    def test_optional_union_branches_handle_missing(self):
        self.assertIs(z.union(z.integer(), z.string().optional()).parse(), MISSING)
        self.assertEqual(z.union(z.integer(), z.string().default("x")).parse(), "x")
        self.assertEqual(z.object({"x": z.union(z.string().optional(), z.integer())}).parse({}), {})
        result = z.union(z.string(), z.integer()).safe_parse()
        self.assertEqual(result.error.issues[0].code, "invalid_union")
        self.assertEqual(result.error.issues[0].branches[0][0].code, "required")

    def test_python_unicode_and_regex_semantics(self):
        self.assertEqual(z.string().min(1).max(1).parse("😀"), "😀")
        self.assertEqual(z.string().min(2).max(2).parse("e\u0301"), "e\u0301")
        self.assertEqual(z.string().trim().upper().parse("\u2003straße\u2003"), "STRASSE")
        self.assertEqual(z.string().lower().parse("ΟΣ"), "ος")
        self.assertEqual(z.string().regex(r"^(\w+)\1$").parse("abab"), "abab")
        surrogate = "\ud800"
        self.assertEqual(z.string().max(1).parse(surrogate), surrogate)
        self.assertEqual(
            z.object({surrogate: z.string()}).parse({surrogate: "x"}),
            {surrogate: "x"},
        )
        self.assertEqual(
            z.string().starts_with(surrogate).parse(surrogate + "x"),
            surrogate + "x",
        )
        self.assertEqual(
            z.string().ends_with(surrogate).parse("x" + surrogate),
            "x" + surrogate,
        )
        self.assertEqual(
            z.string().includes(surrogate).parse("x" + surrogate + "y"),
            "x" + surrogate + "y",
        )

    def test_arbitrary_precision_bounds(self):
        huge = 10**1000
        self.assertEqual(z.integer().min(huge).parse(huge + 1), huge + 1)
        self.assertEqual(z.number().int().max(huge).parse(huge), huge)
        self.assertIsInstance(z.integer().min(huge).safe_parse(huge - 1), Failure)
        self.assertIsInstance(z.string().min(huge).safe_parse("x"), Failure)

    def test_string_operations_use_builtin_methods_for_subclasses(self):
        class Text(str):
            def strip(self):
                return 123

            def lower(self):
                return None

            def upper(self):
                raise AssertionError("Subclass method must not run")

            def startswith(self, prefix, *args):
                raise AssertionError("Subclass method must not run")

            def endswith(self, suffix, *args):
                raise AssertionError("Subclass method must not run")

            def find(self, substring, *args):
                raise AssertionError("Subclass method must not run")

        for schema, raw, expected in (
            (z.string().trim(), Text(" x "), "x"),
            (z.string().lower(), Text("ΟΣ"), "ος"),
            (z.string().upper(), Text("straße"), "STRASSE"),
            (z.string().trim().lower().min(1), Text(" X "), "x"),
            (z.string().starts_with("a"), Text("abc"), "abc"),
            (z.string().ends_with("c"), Text("abc"), "abc"),
            (z.string().includes("b"), Text("abc"), "abc"),
        ):
            with self.subTest(raw=raw, expected=expected):
                self.assertEqual(schema.parse(raw), expected)
                self.assertIsInstance(schema.safe_parse(123), Failure)
        self.assertIsInstance(z.string().trim().min(1).safe_parse(Text(" ")), Failure)
        self.assertIsInstance(z.string().starts_with("b").safe_parse(Text("abc")), Failure)

    def test_large_integer_diagnostics_respect_python_digit_limit(self):
        previous_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            huge = 10**5000
            for bound in (huge, -huge):
                with self.subTest(negative=bound < 0):
                    self.assertEqual(z.integer().min(bound).parse(bound), bound)
                    self.assertEqual(z.number().max(bound).parse(bound), bound)
                    self.assertEqual(z.literal(bound).parse(bound), bound)
                    self.assertEqual(z.integer().gt(bound).parse(bound + 1), bound + 1)
                    self.assertEqual(z.integer().lt(bound).parse(bound - 1), bound - 1)
                    cases = [
                        (z.integer().min(bound), bound - 1),
                        (z.number().max(bound), bound + 1),
                        (z.literal(bound), bound + 1),
                        (z.integer().gt(bound), bound),
                        (z.integer().lt(bound), bound),
                    ]
                    if bound > 0:
                        self.assertEqual(
                            z.integer().multiple_of(bound).parse(bound),
                            bound,
                        )
                        cases.append((z.integer().multiple_of(bound), 1))
                    for schema, invalid in cases:
                        result = schema.safe_parse(invalid)
                        self.assertIsInstance(result, Failure)
                        self.assertIn("integer with", result.error.issues[0].message)
                        json.dumps(result.error.to_dict())
            for schema, raw in (
                (z.string().min(huge), "x"),
                (z.integer().array().min(huge), [1]),
            ):
                self.assertIsInstance(schema.safe_parse(raw), Failure)
            self.assertEqual(z.string().max(huge).parse("x"), "x")
            self.assertEqual(z.integer().array().max(huge).parse([1]), [1])
            self.assertEqual(sys.get_int_max_str_digits(), 640)
        finally:
            sys.set_int_max_str_digits(previous_limit)

    def test_callback_errors_aggregate_and_other_exceptions_propagate(self):
        def reject(_):
            raise ValidationError([Issue("custom", "Rejected by callback", ("provided",))])

        schema = z.object({
            "a": z.string().transform(reject), "b": z.integer(),
        })
        result = schema.safe_parse({"a": "a", "b": "bad"})
        self.assertEqual([i.path for i in result.error.issues], [("provided",), ("b",)])

        def rethrow_deferred(_):
            try:
                z.string().min(1).parse("")
            except ValidationError as error:
                raise error

        nested = z.object({
            "a": z.string().transform(rethrow_deferred), "b": z.integer(),
        }).safe_parse({"a": "a", "b": "bad"})
        self.assertEqual([i.path for i in nested.error.issues], [(), ("b",)])

        with self.assertRaises(TypeError):
            z.string().transform(lambda _: 1 + "a").safe_parse("a")

        decoded = z.codec(
            z.string(), z.integer(), decode=reject, encode=str,
        ).safe_parse("1")
        self.assertIsInstance(decoded, Failure)
        self.assertEqual(decoded.error.issues[0].path, ("provided",))
        encoded = z.codec(
            z.string(), z.integer(), decode=int, encode=reject,
        ).safe_encode(1)
        self.assertIsInstance(encoded, Failure)
        self.assertEqual(encoded.error.issues[0].path, ("provided",))

    def test_async_callbacks_are_rejected(self):
        async def async_callback(value):
            return False

        async def async_generator(value):
            yield value

        for build in (
            lambda: z.string().refine(async_callback),
            lambda: z.string().transform(async_callback),
            lambda: z.preprocess(async_callback, z.string()),
            lambda: z.string().default_factory(async_callback),
            lambda: z.lazy(async_callback),
            lambda: z.string().super_refine(async_callback),
            lambda: z.string().overwrite(async_callback),
            lambda: z.string().catch(async_callback),
            lambda: z.string().refine(async_generator),
            lambda: z.codec(
                z.string(), z.integer(), decode=async_callback, encode=str,
            ),
            lambda: z.codec(
                z.string(), z.integer(), decode=int, encode=async_callback,
            ),
        ):
            with self.subTest(build=build), self.assertRaises(TypeError):
                build()

        def returning_coroutine(value):
            return async_callback(value)

        def returning_async_generator(value):
            return async_generator(value)

        for build in (
            lambda: z.string().refine(returning_coroutine),
            lambda: z.string().transform(returning_coroutine),
            lambda: z.preprocess(returning_coroutine, z.unknown()),
            lambda: z.string().super_refine(returning_coroutine),
            lambda: z.string().overwrite(returning_coroutine),
            lambda: z.integer().catch(returning_coroutine),
            lambda: z.string().refine(returning_async_generator),
            lambda: z.codec(
                z.string(), z.integer(),
                decode=returning_coroutine, encode=str,
            ).safe_parse("1"),
            lambda: z.codec(
                z.string(), z.integer(),
                decode=int, encode=returning_coroutine,
            ).safe_encode(1),
        ):
            with self.subTest(build=build), self.assertRaises(TypeError):
                build().safe_parse("x")

        with self.assertRaises(TypeError):
            z.string().default_factory(lambda: async_callback("x")).safe_parse()
        with self.assertRaises(TypeError):
            z.lazy(lambda: async_callback("x")).safe_parse("x")

        class AwaitableList(list):
            def __await__(self):
                yield None

        with self.assertRaises(TypeError):
            z.string().transform(lambda _: AwaitableList()).safe_parse("x")

        with self.assertRaisesRegex(TypeError, "must be synchronous"):
            z.string().refine(async_callback)
        with self.assertRaisesRegex(TypeError, "async validation is not supported"):
            z.string().refine(returning_coroutine).safe_parse("x")
        with self.assertRaisesRegex(TypeError, "must be synchronous"):
            z.codec(z.string(), z.integer(), decode=async_callback, encode=str)
        with self.assertRaisesRegex(TypeError, "async validation is not supported"):
            z.codec(
                z.string(), z.integer(), decode=returning_coroutine, encode=str,
            ).safe_parse("1")

    def test_refine_paths_are_relative_to_the_current_location(self):
        inner = z.object({"name": z.string()}).refine(
            lambda value: bool(value["name"]), "name required", path=("name",),
        )
        result = z.object({"profile": inner}).safe_parse({"profile": {"name": ""}})
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.issues[0].path, ("profile", "name"))
        self.assertEqual(result.error.issues[0].code, "custom")

    def test_super_refine_emits_multiple_relative_issues(self):
        schema = z.object({"a": z.integer(), "b": z.integer()}).super_refine(
            lambda value: [
                Issue("too_small", "a is small", ("a",), params={"minimum": 10}),
                Issue("too_big", "b is big", ("b",), params={"maximum": 1}),
                Issue("custom", "form"),
            ],
        )
        result = schema.safe_parse({"a": 1, "b": 2})
        self.assertIsInstance(result, Failure)
        self.assertEqual(
            [
                (issue.code, issue.message, issue.path, dict(issue.params))
                for issue in result.error.issues
            ],
            [
                ("too_small", "a is small", ("a",), {"minimum": 10}),
                ("too_big", "b is big", ("b",), {"maximum": 1}),
                ("custom", "form", (), {}),
            ],
        )

        nested = z.object({
            "profile": z.object({"name": z.string()}).super_refine(
                lambda value: Issue("custom", "bad name", ("name",)),
            ),
        })
        nested_result = nested.safe_parse({"profile": {"name": "x"}})
        self.assertIsInstance(nested_result, Failure)
        self.assertEqual(nested_result.error.issues[0].path, ("profile", "name"))

        self.assertEqual(z.integer().super_refine(lambda value: None).parse(1), 1)
        self.assertEqual(z.integer().super_refine(lambda value: []).parse(1), 1)
        tuple_result = z.integer().super_refine(
            lambda value: (Issue("custom", "tuple issue"),),
        ).safe_parse(1)
        self.assertIsInstance(tuple_result, Failure)
        self.assertEqual(tuple_result.error.issues[0].message, "tuple issue")
        with self.assertRaises(TypeError):
            z.integer().super_refine(lambda value: "bad").safe_parse(1)
        with self.assertRaises(TypeError):
            z.integer().super_refine(lambda value: ["bad"]).safe_parse(1)

        def reject(value):
            raise ValidationError([Issue("custom", "rejected", ("provided",))])

        rejected = z.integer().super_refine(reject).safe_parse(1)
        self.assertIsInstance(rejected, Failure)
        self.assertEqual(rejected.error.issues[0].path, ("provided",))

    def test_empty_callback_validation_errors_remain_failures(self):
        def reject(_):
            raise ValidationError([])

        rejected = z.integer().transform(reject)
        for schema, raw, paths in (
            (rejected, 1, [()]),
            (rejected.array(), [1, 2], [(0,), (1,)]),
            (z.tuple(rejected), [1], [(0,)]),
            (z.object({"x": rejected}), {"x": 1}, [("x",)]),
            (z.record(rejected), {"x": 1}, [("x",)]),
            (z.object({"x": rejected, "y": z.string()}),
             {"x": 1, "y": 2}, [("x",), ("y",)]),
        ):
            with self.subTest(paths=paths):
                result = schema.safe_parse(raw)
                self.assertIsInstance(result, Failure)
                self.assertEqual([i.path for i in result.error.issues], paths)
                self.assertEqual(result.error.issues[0].code, "custom")
                self.assertEqual(result.error.issues[0].message, "Invalid value")
                json.dumps(result.error.to_dict())
        self.assertEqual(z.union(rejected, z.integer()).parse(1), 1)
        result = z.union(rejected, z.string()).safe_parse(1)
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.issues[0].branches[0][0].code, "custom")

    def test_lazy_factory_is_resolved_once_per_parse(self):
        calls = 0

        def factory():
            nonlocal calls
            calls += 1
            return z.object({
                "value": z.integer(),
                "children": node.array(),
            })

        node = z.lazy(factory)
        payload = {
            "value": 1,
            "children": [{"value": 2, "children": []}],
        }
        self.assertEqual(node.parse(payload)["value"], 1)
        self.assertEqual(calls, 1)
        self.assertEqual(node.parse(payload)["value"], 1)
        self.assertEqual(calls, 2)

        schemas = iter([
            z.object({"value": z.integer()}),
            z.object({"value": z.string()}),
        ])
        dynamic = z.lazy(lambda: next(schemas))
        self.assertEqual(dynamic.parse({"value": 1}), {"value": 1})
        self.assertEqual(dynamic.parse({"value": "x"}), {"value": "x"})

    def test_native_recursion_limit_for_cycles(self):
        node = z.lazy(lambda: z.object({"child": node}))
        data = {}
        data["child"] = data
        with self.assertRaises(RecursionError):
            node.safe_parse(data)
        circular = z.lazy(lambda: circular)
        with self.assertRaises(RecursionError):
            circular.parse(1)

    def test_native_gc_traces_recursive_closures(self):
        def create_cycle():
            schema = z.lazy(lambda: schema)
            return weakref.ref(schema)

        ref = create_cycle()
        gc.collect()
        self.assertIsNone(ref())

    def test_callbacks_can_mutate_input_safely(self):
        data = {"a": 1, "b": 2}

        def mutate(value):
            data.clear()
            return value

        self.assertEqual(z.record(z.integer().transform(mutate)).parse(data), {"a": 1, "b": 2})

        data = {"a": "x", "b": 1}

        def mutate_object(value):
            data.clear()
            return value

        self.assertEqual(
            z.object({
                "a": z.string().transform(mutate_object),
                "b": z.integer(),
            }).parse(data),
            {"a": "x", "b": 1},
        )
        data = [1, 2]

        def mutate_list(value):
            data.append(3)
            return value

        self.assertEqual(z.integer().transform(mutate_list).array().parse(data), [1, 2])

    def test_invalid_native_nodes_raise_instead_of_panicking(self):
        for kind, children, args in (
            ("invalid", (), ()), ("array", (), ()), ("union", (), ()),
            ("integer", (), (1,)), ("object", (), (("x",), "strip")),
            ("discriminated_union", (), ()),
            ("discriminated_union", (z.string()._node,), ("kind", ("a", "b"))),
            ("catch", (), ()),
            ("nonoptional", (), ()),
            ("overwrite", (), ()),
            ("record", (), ()),
            ("tuple", (), ()),
            ("object", (), (("x",), "strip", None)),
            ("intersection", (), ()),
            ("intersection", (z.string()._node,), ()),
            ("exact_instance", (), ()),
            ("json", (), (1,)),
            ("native_enum", (), ()),
            ("coerce", (), ()),
            ("coerce", (z.string()._node,), ("bad",)),
            ("string_bool", (), ()),
            ("exclusive_min", (), ()),
            ("exclusive_max", (), ()),
            ("multiple_of", (), ()),
            ("starts_with", (), ()),
            ("ends_with", (), ()),
            ("includes", (), ()),
            ("ipv6", (), ()),
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                _native.NativeSchema(kind, children, args)

    def test_simple_array_fast_paths(self):
        data = ["a", "b"]
        parsed = z.string().array().parse(data)
        self.assertEqual(parsed, data)
        self.assertIsNot(parsed, data)
        string_failure = z.string().array().safe_parse(["a", 1])
        self.assertIsInstance(string_failure, Failure)
        self.assertEqual(string_failure.error.issues[0].path, (1,))

        self.assertEqual(z.number().array().parse([1, 1.5]), [1, 1.5])
        for bad in (math.nan, math.inf, -math.inf, True):
            result = z.number().array().safe_parse([1, bad])
            self.assertIsInstance(result, Failure)
            self.assertEqual(result.error.issues[0].path, (1,))

        self.assertEqual(z.boolean().array().parse([True, False]), [True, False])
        result = z.boolean().array().safe_parse([1])
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.issues[0].path, (0,))

        self.assertEqual(z.none().array().parse([None, None]), [None, None])
        result = z.none().array().safe_parse(["x"])
        self.assertIsInstance(result, Failure)
        self.assertEqual(result.error.issues[0].path, (0,))

        refs = [object(), object()]
        values = z.unknown().array().parse(refs)
        self.assertEqual(values, refs)
        self.assertIsNot(values, refs)
        self.assertIs(values[0], refs[0])
        self.assertIs(values[1], refs[1])

        for schema in (
            z.string().array(), z.number().array(), z.boolean().array(),
            z.none().array(), z.unknown().array(), z.integer().array(),
        ):
            self.assertEqual(schema.parse([]), [])

        class CustomList(list):
            def __iter__(self):
                return iter((10, 20))

        self.assertEqual(z.integer().array().parse(CustomList([1])), [10, 20])
        self.assertEqual(
            z.integer().transform(lambda value: value + 1).array().parse(
                [1, 2, 3, 4, 5],
            ),
            [2, 3, 4, 5, 6],
        )

    def test_tuple_subclass_iteration_cannot_overrun_children(self):
        class WeirdList(list):
            def __iter__(self):
                return iter([1, 2])

        self.assertIsInstance(z.tuple(z.integer()).safe_parse(WeirdList([1])), Failure)


class AsyncSessionProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_session_releases_references(self):
        class Payload:
            pass

        class Callback:
            async def __call__(self, value):
                return True

        callback = Callback()
        value = Payload()
        schema = z.instance_of(Payload).refine_async(callback)
        session = _native.AsyncSession(
            schema._node, value, MISSING, Issue, ValidationError,
        )
        refs = (
            weakref.ref(callback),
            weakref.ref(value),
            weakref.ref(session),
        )

        done, payload = session.start()
        self.assertFalse(done)
        token, awaitable = payload
        awaitable.close()
        done, output = session.resume_value(token, True)
        self.assertTrue(done)
        self.assertIs(output, value)

        del callback, value, schema, session
        del payload, token, awaitable, output, done
        gc.collect()
        self.assertEqual([reference() for reference in refs], [None, None, None])

    async def test_failed_session_releases_references(self):
        class Payload:
            pass

        class Callback:
            async def __call__(self, value):
                return False

        callback = Callback()
        value = Payload()
        schema = z.instance_of(Payload).refine_async(callback)
        session = _native.AsyncSession(
            schema._node, value, MISSING, Issue, ValidationError,
        )
        refs = (
            weakref.ref(callback),
            weakref.ref(value),
            weakref.ref(session),
        )

        done, payload = session.start()
        self.assertFalse(done)
        token, awaitable = payload
        awaitable.close()
        try:
            session.resume_value(token, False)
        except ValidationError:
            pass
        else:
            self.fail("expected callback validation to fail")

        del callback, value, schema, session
        del payload, token, awaitable, done
        gc.collect()
        self.assertEqual([reference() for reference in refs], [None, None, None])


if __name__ == "__main__":
    unittest.main()
