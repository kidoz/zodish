"""Schema core, fluent wrappers, sentinels, and shared callbacks.

The builders and concrete schema classes live in ``zodish.schemas``. This
module imports them lazily from inside the few methods that construct them.
"""

from __future__ import annotations

import builtins
import copy as _copy
import datetime as _datetime
import decimal as _decimal
import inspect
import json as _json
import pathlib as _pathlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import wraps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from ._async import drive
from ._errors import (
    ErrorMap,
    Failure,
    Issue,
    Path,
    Success,
    ValidationError,
    _check_error_map,
)
from ._native import AsyncSession, NativeSchema


if TYPE_CHECKING:
    from .schemas import (
        ArraySchema,
        IntersectionSchema,
        UnionSchema,
    )


type JSONSchema = dict[str, Any]
type JSONSchemaTarget = Literal["draft-2020-12", "openapi-3.0"]
type JSONSchemaIO = Literal["input", "output"]
type UnrepresentablePolicy = Literal["throw", "any"]

_UNSET = builtins.object()
_SYNC_RESULT_TYPES = frozenset({
    bool, int, float, complex, str, builtins.bytes, bytearray, list, tuple,
    dict, set, frozenset, type(None), _datetime.date, _datetime.time,
    _datetime.datetime, _datetime.timedelta, _decimal.Decimal,
    _pathlib.PurePath,
})


class Missing(Enum):
    """Distinct from None: represents an absent object property."""

    VALUE = "MISSING"


MISSING = Missing.VALUE


def _sync_callback[F: Callable[..., Any]](fn: F, role: str) -> F:
    """Reject async callbacks; the evaluator is synchronous."""
    if inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
        raise TypeError(f"{role} must be synchronous")

    @wraps(fn)
    def checked(*args: Any) -> Any:
        result = fn(*args)
        if type(result) in _SYNC_RESULT_TYPES:
            return result
        if inspect.isawaitable(result) or inspect.isasyncgen(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError(
                f"{role} returned an asynchronous result; "
                "async validation is not supported",
            )
        return result

    return cast(F, checked)


def _async_callback[F: Callable[..., Any]](fn: F, role: str) -> F:
    """Validate an asynchronous callback without wrapping its result."""
    if not callable(fn):
        raise TypeError(f"{role} must be callable")
    if inspect.isasyncgenfunction(fn):
        raise TypeError(f"{role} must not be an async generator")
    return fn


def _message(value: str | None, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TypeError("Message must be a string")
    return value


def _relative_path(path: Sequence[str | int]) -> Path:
    if isinstance(path, (str, builtins.bytes)):
        raise TypeError("Refinement path must be a sequence of strings and integers")
    result = builtins.tuple(path)
    if not all(type(part) in (str, int) for part in result):
        raise TypeError("Refinement path must contain strings and integers")
    return result


def _string_argument(value: str, role: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{role} must be a string")
    return value


def _brand_argument(newtype: Any) -> None:
    if not callable(newtype) or getattr(newtype, "__supertype__", _UNSET) is _UNSET:
        raise TypeError("Brand must be a typing.NewType")


_WIRE_WRAPPER_KINDS = frozenset({
    "optional", "nonoptional", "nullable", "required_output", "default",
    "default_factory", "refine", "super_refine", "transform", "overwrite",
    "coerce", "string_bool", "preprocess", "catch",
    "length_min", "length_max", "min", "max", "exclusive_min", "exclusive_max",
    "multiple_of", "positive", "integral", "regex", "starts_with", "ends_with",
    "includes", "ipv6", "cidrv4", "cidrv6", "trim", "lower", "upper",
    "async_refine", "async_super_refine", "async_transform", "async_overwrite",
    "async_preprocess", "async_default_factory", "async_catch",
    "context_refine", "context_super_refine", "context_transform",
    "async_context_refine", "async_context_super_refine", "async_context_transform",
})


def _field_wire(node: NativeSchema) -> str | None:
    """Return the wire name carried by a field schema, if one is set."""
    while True:
        kind = node.kind
        if kind == "wire":
            name = node.args[0]
            return name if isinstance(name, str) else None
        if kind == "pipe":
            node = node.children[0]
            continue
        if kind in _WIRE_WRAPPER_KINDS and len(node.children) == 1:
            node = node.children[0]
            continue
        return None


@dataclass(frozen=True)
class Schema[Out, In = Out]:
    """Immutable schema; ``Out`` is the parsed type and ``In`` the input type."""

    _node: NativeSchema = field(repr=False)
    _inner: Schema[Any, Any] | None = field(
        default=None, repr=False, kw_only=True, compare=False,
    )
    _meta: Mapping[str, Any] = field(
        default_factory=dict, repr=False, kw_only=True, compare=False,
    )

    def __post_init__(self) -> None:
        builtins.object.__setattr__(
            self, "_meta", MappingProxyType(dict(self._meta)),
        )

    @property
    def metadata(self) -> Mapping[str, Any]:
        """Read-only documentation metadata attached to this schema."""
        return self._meta

    def meta(self, metadata: Mapping[str, Any]) -> Self:
        """Return a schema with metadata merged into the current metadata."""
        if not isinstance(metadata, Mapping):
            raise TypeError("Metadata must be a mapping")
        if not all(isinstance(key, str) for key in metadata):
            raise TypeError("Metadata keys must be strings")
        return replace(self, _meta={**self._meta, **metadata})

    def describe(self, description: str) -> Self:
        """Return a schema with its ``description`` metadata replaced."""
        if not isinstance(description, str):
            raise TypeError("Description must be a string")
        return self.meta({"description": description})

    def brand[Brand](self, newtype: Callable[[Out], Brand]) -> Schema[Brand, In]:
        """Apply a typing-only ``NewType`` brand to parsed output."""
        _brand_argument(newtype)
        return Schema[Brand, In](self._node, _inner=self, _meta=self._meta)

    def wire(self, name: str) -> Schema[Out, In]:
        """Use ``name`` for this field's input key in an object shape."""
        if not isinstance(name, str):
            raise TypeError("Wire name must be a string")
        if _field_wire(self._node) is not None:
            raise ValueError("Schema already has a wire name")
        return Schema[Out, In](
            NativeSchema("wire", (self._node,), (name,)),
            _inner=self,
            _meta=self._meta,
        )

    def parse(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Out:
        """Validate a value; raise ``ValidationError`` on failure.

        ``error`` rewrites issue messages when the error's issues are read.
        """
        _check_error_map(error)
        try:
            return cast(
                Out, self._node.parse(value, MISSING, Issue, ValidationError, context),
            )
        except ValidationError as failure:
            failure._set_error_mapper(error)
            raise

    def safe_parse(
        self,
        value: In | Missing = MISSING,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors only; programming errors remain visible."""
        try:
            return Success(self.parse(value, error=error, context=context))
        except ValidationError as failure:
            return Failure(failure)

    async def parse_async(
        self,
        value: In | Missing = MISSING,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Out:
        """Parse, awaiting async callback results when required."""
        _check_error_map(error)
        try:
            session = AsyncSession(
                self._node, value, MISSING, Issue, ValidationError, context,
            )
            return cast(Out, await drive(session))
        except ValidationError as failure:
            failure._set_error_mapper(error)
            raise

    async def safe_parse_async(
        self,
        value: In | Missing = MISSING,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors from async parsing only."""
        try:
            return Success(await self.parse_async(value, error=error, context=context))
        except ValidationError as failure:
            return Failure(failure)

    def validate(self, value: In | Missing = MISSING, *, context: Any = None) -> bool:
        """Return whether validation succeeds; callback errors still propagate."""
        return self.safe_parse(value, context=context).success

    async def validate_async(
        self, value: In | Missing = MISSING, *, context: Any = None,
    ) -> bool:
        """Return whether async validation succeeds.

        Programming errors still propagate, matching ``validate``.
        """
        return (await self.safe_parse_async(value, context=context)).success

    def _json_input(
        self, data: str | builtins.bytes | bytearray,
    ) -> builtins.tuple[bool, Any]:
        """Return ``(native_fast, value)`` for validated JSON input.

        ``native_fast`` is true when the native decoder fully validated and
        parsed the input. Otherwise ``value`` is the decoded JSON value for the
        caller's normal or async evaluator.
        """
        if not isinstance(data, (str, builtins.bytes, bytearray)):
            raise TypeError("JSON input must be str, bytes, or bytearray")
        fast, value = self._node.parse_json(data)
        if fast:
            return True, value
        try:
            return False, _json.loads(data)
        except _json.JSONDecodeError as error:
            position = error.pos
        except UnicodeDecodeError as error:
            position = error.start
        raise ValidationError([Issue(
            "invalid_format",
            "Invalid JSON",
            (),
            params={"format": "json", "position": position},
        )]) from None

    def parse_json(
        self,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Out:
        """Parse JSON text and validate the decoded value."""
        _check_error_map(error)
        try:
            fast, value = self._json_input(data)
        except ValidationError as failure:
            failure._set_error_mapper(error)
            raise
        return (
            cast(Out, value) if fast
            else self.parse(value, error=error, context=context)
        )

    async def parse_json_async(
        self,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Out:
        """Parse JSON text and await the normal asynchronous evaluator."""
        _check_error_map(error)
        try:
            fast, value = self._json_input(data)
        except ValidationError as failure:
            failure._set_error_mapper(error)
            raise
        return (
            cast(Out, value) if fast
            else await self.parse_async(value, error=error, context=context)
        )

    async def safe_parse_json_async(
        self,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors from asynchronous JSON parsing only."""
        try:
            return Success(await self.parse_json_async(data, error=error, context=context))
        except ValidationError as failure:
            return Failure(failure)

    def safe_parse_json(
        self,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors from JSON parsing only."""
        try:
            return Success(self.parse_json(data, error=error, context=context))
        except ValidationError as failure:
            return Failure(failure)

    def validate_json(
        self, data: str | builtins.bytes | bytearray, *, context: Any = None,
    ) -> bool:
        """Return whether JSON text parses and validates."""
        return self.safe_parse_json(data, context=context).success

    def encode(self, value: Out) -> In:
        """Encode a decoded value when the schema is reversible.

        Transforms, refinements, coercions, defaults, catches, pipes,
        preprocessors, and lazy schemas have no reverse path and raise
        ``TypeError``.
        """
        from ._codecs import _encode_node_for

        node = _encode_node_for(self)
        if node is None:
            raise TypeError("Schema has no reversible encoder")
        return cast(In, node.parse(value, MISSING, Issue, ValidationError))

    def safe_encode(self, value: Out) -> Success[In] | Failure:
        """Catch validation errors only; programming errors remain visible."""
        try:
            return Success(self.encode(value))
        except ValidationError as error:
            return Failure(error)

    async def encode_async(self, value: Out) -> In:
        """Encode through the reverse graph, awaiting async callbacks."""
        from ._codecs import _encode_node_for

        node = _encode_node_for(self)
        if node is None:
            raise TypeError("Schema has no reversible encoder")
        session = AsyncSession(node, value, MISSING, Issue, ValidationError)
        return cast(In, await drive(session))

    async def safe_encode_async(self, value: Out) -> Success[In] | Failure:
        """Catch validation errors from async encoding only."""
        try:
            return Success(await self.encode_async(value))
        except ValidationError as error:
            return Failure(error)

    def to_json_schema(
        self,
        *,
        target: JSONSchemaTarget = "draft-2020-12",
        io: JSONSchemaIO = "output",
        unrepresentable: UnrepresentablePolicy = "throw",
    ) -> JSONSchema:
        """Export this schema as a JSON Schema-compatible mapping."""
        from ._json_schema import to_json_schema

        return to_json_schema(
            self, target=target, io=io, unrepresentable=unrepresentable,
        )

    def _wrap(self, kind: str, *args: Any) -> Self:
        return replace(self, _node=NativeSchema(kind, (self._node,), args))

    def unwrap(self) -> Schema[Any, Any]:
        """Return the wrapped inner schema, when one is tracked."""
        if self._inner is not None:
            return self._inner
        from .schemas import ArraySchema, RecordSchema

        if isinstance(self, ArraySchema):
            return self.item
        if isinstance(self, RecordSchema):
            return self.value_schema
        raise TypeError("Schema has no inner schema to unwrap")

    def refine(
        self,
        predicate: Callable[[Out], bool],
        message: str = "Invalid value",
        *,
        path: Sequence[str | int] = (),
    ) -> Schema[Out, In]:
        """Reject a parsed value; ``path`` is relative to the current location."""
        predicate = _sync_callback(predicate, "refine callback")
        return Schema[Out, In](
            NativeSchema(
                "refine",
                (self._node,),
                (predicate, _message(message, "Invalid value"), _relative_path(path)),
            ),
            _inner=self,
            _meta=self._meta,
        )

    def refine_async(
        self,
        predicate: Callable[[Out], Awaitable[bool]],
        message: str = "Invalid value",
        *,
        path: Sequence[str | int] = (),
    ) -> Schema[Out, In]:
        """Asynchronously reject a parsed value; requires ``parse_async``."""
        if not callable(predicate):
            raise TypeError("refine_async callback must be callable")
        if inspect.isasyncgenfunction(predicate):
            raise TypeError("refine_async callback must not be an async generator")
        return Schema[Out, In](
            NativeSchema(
                "async_refine",
                (self._node,),
                (predicate, _message(message, "Invalid value"), _relative_path(path)),
            ),
            _inner=self,
            _meta=self._meta,
        )

    def super_refine_async(
        self,
        fn: Callable[[Out], Awaitable[
            Issue | list[Issue] | builtins.tuple[Issue, ...] | None
        ]],
    ) -> Schema[Out, In]:
        """Asynchronously emit issues relative to the current location."""
        if not callable(fn):
            raise TypeError("super_refine_async callback must be callable")
        if inspect.isasyncgenfunction(fn):
            raise TypeError(
                "super_refine_async callback must not be an async generator",
            )
        return Schema[Out, In](
            NativeSchema("async_super_refine", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def super_refine(
        self,
        fn: Callable[[Out], Issue | list[Issue] | builtins.tuple[Issue, ...] | None],
    ) -> Schema[Out, In]:
        """Emit issues relative to the current location, or return ``None``."""
        fn = _sync_callback(fn, "super_refine callback")
        return Schema[Out, In](
            NativeSchema("super_refine", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def transform[U](self, fn: Callable[[Out], U]) -> Schema[U, In]:
        fn = _sync_callback(fn, "transform callback")
        return Schema[U, In](
            NativeSchema("transform", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def transform_async[U](self, fn: Callable[[Out], Awaitable[U]]) -> Schema[U, In]:
        """Asynchronously transform parsed output; requires ``parse_async``."""
        if not callable(fn):
            raise TypeError("transform_async callback must be callable")
        if inspect.isasyncgenfunction(fn):
            raise TypeError("transform_async callback must not be an async generator")
        return Schema[U, In](
            NativeSchema("async_transform", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def refine_with_context(
        self,
        predicate: Callable[[Out, Any], bool],
        message: str = "Invalid value",
        *,
        path: Sequence[str | int] = (),
    ) -> Schema[Out, In]:
        """Reject a parsed value using the current parse context."""
        predicate = _sync_callback(predicate, "context refine callback")
        return Schema[Out, In](
            NativeSchema(
                "context_refine",
                (self._node,),
                (predicate, _message(message, "Invalid value"), _relative_path(path)),
            ),
            _inner=self,
            _meta=self._meta,
        )

    def refine_with_context_async(
        self,
        predicate: Callable[[Out, Any], Awaitable[bool]],
        message: str = "Invalid value",
        *,
        path: Sequence[str | int] = (),
    ) -> Schema[Out, In]:
        """Asynchronously reject a value using the current parse context."""
        predicate = _async_callback(predicate, "async context refine callback")
        return Schema[Out, In](
            NativeSchema(
                "async_context_refine",
                (self._node,),
                (predicate, _message(message, "Invalid value"), _relative_path(path)),
            ),
            _inner=self,
            _meta=self._meta,
        )

    def super_refine_with_context(
        self,
        fn: Callable[[Out, Any], Issue | list[Issue] | builtins.tuple[Issue, ...] | None],
    ) -> Schema[Out, In]:
        """Emit issues using the current parse context."""
        fn = _sync_callback(fn, "context super_refine callback")
        return Schema[Out, In](
            NativeSchema("context_super_refine", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def super_refine_with_context_async(
        self,
        fn: Callable[[Out, Any], Awaitable[
            Issue | list[Issue] | builtins.tuple[Issue, ...] | None
        ]],
    ) -> Schema[Out, In]:
        """Asynchronously emit issues using the current parse context."""
        fn = _async_callback(fn, "async context super_refine callback")
        return Schema[Out, In](
            NativeSchema("async_context_super_refine", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def transform_with_context[U](
        self, fn: Callable[[Out, Any], U],
    ) -> Schema[U, In]:
        """Transform parsed output using the current parse context."""
        fn = _sync_callback(fn, "context transform callback")
        return Schema[U, In](
            NativeSchema("context_transform", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def transform_with_context_async[U](
        self, fn: Callable[[Out, Any], Awaitable[U]],
    ) -> Schema[U, In]:
        """Asynchronously transform output using the current parse context."""
        fn = _async_callback(fn, "async context transform callback")
        return Schema[U, In](
            NativeSchema("async_context_transform", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def overwrite(self, fn: Callable[[Out], Out]) -> Schema[Out, In]:
        """Transform a parsed value without changing its static type."""
        fn = _sync_callback(fn, "overwrite callback")
        return Schema[Out, In](
            NativeSchema("overwrite", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def overwrite_async(
        self, fn: Callable[[Out], Awaitable[Out]],
    ) -> Schema[Out, In]:
        """Await a parsed-value change without changing its static type."""
        fn = _async_callback(fn, "overwrite_async callback")
        return Schema[Out, In](
            NativeSchema("async_overwrite", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def pipe[U](self, other: Schema[U, Any]) -> Schema[U, In]:
        if not isinstance(other, Schema):
            raise TypeError("Pipe operand must be a schema")
        return Schema[U, In](
            NativeSchema("pipe", (self._node, other._node), ()),
            _inner=self,
            _meta=self._meta,
        )

    def optional(self) -> Schema[Out | Missing, In | Missing]:
        return Schema[Out | Missing, In | Missing](
            NativeSchema("optional", (self._node,), ()),
            _inner=self,
            _meta=self._meta,
        )

    def nonoptional(self) -> Schema[Out, In]:
        """Reject missing input before delegating to the wrapped schema."""
        return Schema[Out, In](
            NativeSchema("nonoptional", (self._node,), ()),
            _inner=self,
            _meta=self._meta,
        )

    def nullable(self) -> Schema[Out | None, In | None]:
        return Schema[Out | None, In | None](
            NativeSchema("nullable", (self._node,), ()),
            _inner=self,
            _meta=self._meta,
        )

    def nullish(self) -> Schema[Out | Missing | None, In | Missing | None]:
        return self.nullable().optional()

    def default(self, value: In) -> Schema[Out, In]:
        """Snapshot ``value`` and validate a fresh copy for missing input."""
        return Schema[Out, In](
            NativeSchema("default", (self._node,), (_copy.deepcopy(value),)),
            _inner=self,
            _meta=self._meta,
        )

    def default_factory(self, factory: Callable[[], In]) -> Schema[Out, In]:
        factory = _sync_callback(factory, "default_factory callback")
        return Schema[Out, In](
            NativeSchema("default_factory", (self._node,), (factory,)),
            _inner=self,
            _meta=self._meta,
        )

    def default_factory_async(
        self, factory: Callable[[], Awaitable[In]],
    ) -> Schema[Out, In]:
        """Await a factory for missing input, then validate its result."""
        factory = _async_callback(factory, "default_factory_async callback")
        return Schema[Out, In](
            NativeSchema("async_default_factory", (self._node,), (factory,)),
            _inner=self,
            _meta=self._meta,
        )

    def catch[U](self, fallback: U | Callable[[], U]) -> Schema[Out | U, In]:
        """Return a fallback when validation fails; callback errors propagate.

        A callable fallback is invoked with no arguments.
        """
        if callable(fallback):
            fallback = _sync_callback(fallback, "catch fallback")
        return Schema[Out | U, In](
            NativeSchema("catch", (self._node,), (fallback,)),
            _inner=self,
            _meta=self._meta,
        )

    def catch_async[U](
        self, fallback: Callable[[], Awaitable[U]],
    ) -> Schema[Out | U, In]:
        """Await a fallback factory when validation fails."""
        fallback = _async_callback(fallback, "catch_async fallback")
        return Schema[Out | U, In](
            NativeSchema("async_catch", (self._node,), (fallback,)),
            _inner=self,
            _meta=self._meta,
        )

    def array(self) -> ArraySchema[Out, In]:
        from .schemas import ArraySchema

        return ArraySchema[Out, In](
            NativeSchema("array", (self._node,), ()),
            self,
        )

    def or_[U, OtherIn](
        self, other: Schema[U, OtherIn],
    ) -> UnionSchema[Out | U, In | OtherIn]:
        from .schemas import UnionSchema

        if not isinstance(other, Schema):
            raise TypeError("Union options must be schemas")
        return UnionSchema[Out | U, In | OtherIn](
            NativeSchema("union", (self._node, other._node), ()),
            (self, other),
        )

    def and_(self, other: Schema[Any, Any]) -> IntersectionSchema:
        from .schemas import intersection

        return intersection(self, other)

# Keep the public module paths stable after the private-module split.
Schema.__module__ = "zodish.schemas"
Missing.__module__ = "zodish.schemas"
