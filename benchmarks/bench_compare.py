"""Compare validation libraries with pyperf; see benchmarks/README.md.

Optional dependencies are loaded at runtime so ordinary development and the
stdlib microbenchmark do not require the benchmark dependency group.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast, get_type_hints

from zodish import Schema, ValidationError, _native, z


type Parser = Callable[[Any], Any]
type IssuePath = tuple[str | int, ...]

LIBRARIES = ("zodish", "pydantic", "msgspec", "marshmallow")
USER = {"id": 1, "name": "Alex", "tags": ["admin", "staff"]}
TAGS = ("created", "updated", "deleted", "archived")


class Tree(TypedDict):
    value: int
    children: list[Tree]


class Created(TypedDict):
    kind: Literal["created"]
    value: int


class Updated(TypedDict):
    kind: Literal["updated"]
    value: int


class Deleted(TypedDict):
    kind: Literal["deleted"]
    value: int


class Archived(TypedDict):
    kind: Literal["archived"]
    value: int


def dependency(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        raise SystemExit(
            f"Missing benchmark dependency {name!r}; "
            "install with: python -m pip install --group benchmark"
        ) from error


def tree(depth: int) -> dict[str, Any]:
    return {
        "value": depth,
        "children": [tree(depth - 1), tree(depth - 1)] if depth else [],
    }


def positive(value: int) -> int:
    if value <= 0:
        raise ValueError("Must be positive")
    return value


@dataclass
class Adapter:
    library: str
    parsers: dict[str, Parser]
    error: type[Exception]
    paths: Callable[[Any], list[IssuePath]]
    make_user: Callable[[], Parser]
    accepts_tuple: bool = False
    aggregates: bool = True


def zodish_adapter() -> Adapter:
    def user_schema() -> Schema[Any, Any]:
        return z.object({
            "id": z.integer().positive(),
            "name": z.string().min(1).max(64),
            "tags": z.string().array(),
        })

    def make_user() -> Parser:
        return user_schema().parse

    user = make_user()
    json_user = user_schema()
    recursive: Schema[Any, Any] = z.lazy(lambda: node)
    node = z.object({"value": z.integer(), "children": recursive.array()})
    branches = {
        tag: z.object({"kind": z.literal(tag), "value": z.integer()})
        for tag in TAGS
    }
    return Adapter(
        "zodish",
        {
            "string": z.string().min(3).max(32).parse,
            "object": user,
            "array": z.integer().array().parse,
            "recursive": node.parse,
            "union": z.union(*branches.values()).parse,
            "discriminated": z.discriminated_union("kind", branches).parse,
            "callback": z.integer().refine(lambda value: value > 0).parse,
            "json": json_user.parse_json,
        },
        ValidationError,
        lambda error: [issue.path for issue in error.issues],
        make_user,
    )


def pydantic_adapter() -> Adapter:
    p = dependency("pydantic")

    class User(TypedDict):
        id: Annotated[int, p.Field(gt=0)]
        name: Annotated[str, p.Field(min_length=1, max_length=64)]
        tags: list[str]

    def parser(annotation: Any) -> Parser:
        adapter = p.TypeAdapter(annotation)
        return lambda data: adapter.validate_python(data, strict=True)

    def make_user() -> Parser:
        return parser(User)

    user_adapter = p.TypeAdapter(User)
    alternatives = Created | Updated | Deleted | Archived
    return Adapter(
        "pydantic",
        {
            "string": parser(Annotated[str, p.Field(min_length=3, max_length=32)]),
            "object": make_user(),
            "array": parser(list[int]),
            "recursive": parser(Tree),
            "union": parser(Annotated[alternatives, p.Field(union_mode="left_to_right")]),
            "discriminated": parser(Annotated[alternatives, p.Field(discriminator="kind")]),
            "callback": parser(Annotated[int, p.AfterValidator(positive)]),
            "json": lambda data: user_adapter.validate_json(data, strict=True),
        },
        p.ValidationError,
        lambda error: [tuple(issue["loc"]) for issue in error.errors()],
        make_user,
    )


def msgspec_adapter() -> Adapter:
    m = dependency("msgspec")

    class User(TypedDict):
        id: Annotated[int, m.Meta(gt=0)]
        name: Annotated[str, m.Meta(min_length=1, max_length=64)]
        tags: list[str]

    # msgspec resolves string annotations from the class's module, whereas m is
    # deliberately a local optional import. Resolve that namespace explicitly.
    User.__annotations__ = get_type_hints(User, localns={"m": m}, include_extras=True)

    def parser(annotation: Any) -> Parser:
        return lambda data: m.convert(data, type=annotation, strict=True)

    def paths(error: Any) -> list[IssuePath]:
        # Only used outside timing (except in the explicitly named formatting
        # case). msgspec exposes its first error's location in the message.
        text = str(error)
        if "missing required field `id`" in text:
            return [("id",)]
        for path, suffix in (
            (("tags", 1), "$.tags[1]"),
            (("id",), "$.id"),
            (("name",), "$.name"),
            (("tags",), "$.tags"),
        ):
            if text.endswith(f" - at `{suffix}`"):
                return [path]
        return [()]

    return Adapter(
        "msgspec",
        {
            "string": parser(Annotated[str, m.Meta(min_length=3, max_length=32)]),
            "object": parser(User),
            "array": parser(list[int]),
            "recursive": parser(Tree),
            "json": lambda data: m.json.decode(data, type=User, strict=True),
        },
        m.ValidationError,
        paths,
        lambda: parser(User),
        accepts_tuple=True,
        aggregates=False,
    )


def marshmallow_adapter() -> Adapter:
    m = dependency("marshmallow")

    def check_positive(value: int) -> None:
        if value <= 0:
            raise m.ValidationError("Must be positive")

    def make_user() -> Parser:
        schema = m.Schema.from_dict({
            "id": m.fields.Integer(
                required=True, strict=True, validate=m.validate.Range(min=1),
            ),
            "name": m.fields.String(
                required=True, validate=m.validate.Length(min=1, max=64),
            ),
            "tags": m.fields.List(m.fields.String(), required=True),
        })(unknown=m.EXCLUDE).load
        return cast(Parser, schema)

    def paths(error: Any) -> list[IssuePath]:
        result: list[IssuePath] = []

        def visit(messages: Any, path: IssuePath) -> None:
            if isinstance(messages, dict):
                for key, child in messages.items():
                    visit(child, (*path, key))
            else:
                result.extend(path for _ in messages)

        visit(error.messages, ())
        return result

    user = make_user()
    recursive: Any = m.Schema.from_dict({
        "value": m.fields.Integer(required=True, strict=True),
        "children": m.fields.List(m.fields.Nested(lambda: recursive), required=True),
    })(unknown=m.EXCLUDE)
    return Adapter(
        "marshmallow",
        {
            "string": m.fields.String(
                validate=m.validate.Length(min=3, max=32),
            ).deserialize,
            "object": user,
            "array": m.fields.List(m.fields.Integer(strict=True)).deserialize,
            "recursive": recursive.load,
            "callback": m.fields.Integer(
                strict=True, validate=check_positive,
            ).deserialize,
            "json": lambda data: user(json.loads(data)),
        },
        m.ValidationError,
        paths,
        make_user,
        accepts_tuple=True,
    )


def checked_failure(adapter: Adapter, parser: Parser, payload: Any) -> Exception:
    """The same exception-catching wrapper is used for every library."""
    try:
        parser(payload)
    except adapter.error as error:
        return error
    raise AssertionError(f"{adapter.library}: unexpectedly accepted {payload!r}")


@dataclass
class Case:
    adapter: Adapter
    name: str
    call: Callable[[], Any]
    check: Callable[[], None]
    issues: int = 0


def cases(adapter: Adapter) -> list[Case]:
    result: list[Case] = []
    specs: list[tuple[str, str, Any, Any]] = [
        ("string", "string_success", "hello", "hello"),
        ("object", "object_success", USER, USER),
        ("array", "array_1000", list(range(1000)), list(range(1000))),
        ("recursive", "recursive_tree", tree(4), tree(4)),
        ("callback", "refine_callback", 1, 1),
        ("json", "json_object", json.dumps(USER).encode(), USER),
    ]
    for kind in ("union", "discriminated"):
        for position, tag in (("first", TAGS[0]), ("last", TAGS[-1])):
            union_payload = {"kind": tag, "value": 1}
            specs.append((kind, f"{kind}_{position}", union_payload, union_payload))

    def success(
        parser: Parser, payload: Any, expected: Any,
    ) -> tuple[Callable[[], Any], Callable[[], None]]:
        def call() -> Any:
            return parser(payload)

        def check() -> None:
            before = deepcopy(payload)
            actual = call()
            if actual != expected or type(actual) is not type(expected):
                raise AssertionError(f"{adapter.library}: unexpected output {actual!r}")
            if payload != before:
                raise AssertionError(f"{adapter.library}: input mutated")

        return call, check

    for kind, name, payload, expected in specs:
        if kind in adapter.parsers:
            call, check = success(adapter.parsers[kind], payload, expected)
            result.append(Case(adapter, name, call, check))

    def failure(name: str, payload: Any, expected: list[IssuePath]) -> Case:
        def call() -> Exception:
            return checked_failure(adapter, adapter.parsers["object"], payload)

        def check() -> None:
            if adapter.paths(call()) != expected:
                raise AssertionError(f"{adapter.library}/{name}: wrong issue paths")

        return Case(adapter, name, call, check, len(expected))

    result.append(failure("object_failure_single", {**USER, "id": -1}, [("id",)]))
    paths: list[IssuePath] = (
        [("id",), ("name",), ("tags", 1)] if adapter.aggregates else [("id",)]
    )
    result.append(failure(
        "object_failure_multi",
        {"id": -1, "name": "", "tags": ["admin", 1]},
        paths,
    ))

    # Render an already-produced error, independently of validation and catching.
    error = checked_failure(adapter, adapter.parsers["object"], {**USER, "id": -1})
    result.append(Case(
        adapter, "error_string", lambda: str(error),
        lambda: require(bool(str(error)), "Empty formatted error"), 1,
    ))

    if adapter.library != "msgspec":
        # msgspec has no equivalent explicit validator compilation API.
        def check_factory() -> None:
            require(adapter.make_user()(USER) == USER, "Invalid newly built schema")

        result.append(Case(adapter, "schema_build", adapter.make_user, check_factory))
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def conformance(adapter: Adapter) -> None:
    """Check required fields, each constraint, coercion and unknown-key policy."""
    parse = adapter.parsers["object"]
    require(parse(USER) == USER, f"{adapter.library}: valid object")
    require(parse({**USER, "extra": 1}) == USER, f"{adapter.library}: unknown keys")
    for payload, expected_path in [
        ({**USER, "id": True}, ("id",)),
        ({**USER, "id": "1"}, ("id",)),
        ({**USER, "id": 0}, ("id",)),
        ({**USER, "name": ""}, ("name",)),
        ({**USER, "name": "x" * 65}, ("name",)),
        ({**USER, "name": None}, ("name",)),
        ({**USER, "tags": ["ok", 1]}, ("tags", 1)),
        ({"name": "Alex", "tags": []}, ("id",)),
    ]:
        paths = adapter.paths(checked_failure(adapter, parse, payload))
        require(expected_path in paths, f"{adapter.library}: wrong paths {paths!r}")
    tuple_input = {**USER, "tags": ("admin", "staff")}
    if adapter.accepts_tuple:
        require(parse(tuple_input) == USER, f"{adapter.library}: tuple conversion changed")
    else:
        checked_failure(adapter, parse, tuple_input)
    for kind, payload in [
        ("string", "hi"), ("string", "x" * 33), ("array", [True]),
        ("recursive", {"value": 1, "children": [{"value": "bad", "children": []}]}),
        ("union", {"kind": "unknown", "value": 1}),
        ("discriminated", {"kind": "unknown", "value": 1}),
        ("callback", 0), ("callback", True),
        ("json", json.dumps({**USER, "id": "1"}).encode()),
    ]:
        if kind in adapter.parsers:
            checked_failure(adapter, adapter.parsers[kind], payload)


def forward_args(command: list[str], args: argparse.Namespace) -> None:
    # pyperf launches fresh workers: custom selection must survive that boundary.
    command.extend(("--library", args.library, "--build-profile", args.build_profile))
    if args.scenario:
        command.extend(("--scenario", args.scenario))


def main() -> None:
    pyperf = dependency("pyperf")
    runner = pyperf.Runner(add_cmdline_args=forward_args)
    runner.argparser.add_argument("--library", choices=(*LIBRARIES, "all"), default="all")
    runner.argparser.add_argument("--scenario", help="scenario name substring")
    runner.argparser.add_argument("--check-only", action="store_true")
    runner.argparser.add_argument(
        "--build-profile", choices=("release", "debug", "unknown"), default="unknown",
        help="record the Zodish build profile; caller must verify it",
    )
    args = runner.parse_args()
    factories = (zodish_adapter, pydantic_adapter, msgspec_adapter, marshmallow_adapter)
    adapters = [
        factory() for library, factory in zip(LIBRARIES, factories, strict=True)
        if args.library in ("all", library)
    ]
    selected = []
    for adapter in adapters:
        conformance(adapter)
        for case in cases(adapter):
            if not args.scenario or args.scenario in case.name:
                case.check()
                selected.append(case)
    if not selected:
        runner.argparser.error("no matching scenarios")
    if args.check_only:
        for case in selected:
            print(f"OK {case.name}/{case.adapter.library} issues={case.issues}")
        return

    runner.metadata.update({
        "zodish_build_profile": args.build_profile,
        "zodish_native_sha256": hashlib.sha256(Path(_native.__file__).read_bytes()).hexdigest(),
        "zodish_python_sha256": hashlib.sha256(Path(z.__file__).read_bytes()).hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "gil_enabled": str(sys._is_gil_enabled()),
        "comparison_contract": "Python dict outputs; strict scalar types; strip unknown keys",
    })
    for name in ("zodish", "pyperf", *(a.library for a in adapters if a.library != "zodish")):
        runner.metadata[f"version_{name}"] = version(name)
    if any(a.library == "pydantic" for a in adapters):
        runner.metadata["version_pydantic_core"] = version("pydantic-core")
    for case in selected:
        runner.bench_func(
            f"{case.name}/{case.adapter.library}", case.call,
            metadata={
                "reported_issues": case.issues,
                "accepts_tuple_for_list": str(case.adapter.accepts_tuple),
                "library": case.adapter.library,
            },
        )


if __name__ == "__main__":
    main()
