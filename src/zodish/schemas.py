"""Typed Python schema builders backed by the native Rust evaluator."""

from __future__ import annotations

import base64
import binascii
import builtins
import dataclasses
import datetime as _datetime
import decimal as _decimal
import inspect
import json as _json
import math
import pathlib as _pathlib
import re
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import wraps
from types import MappingProxyType
from typing import Any, Literal, Self, cast, overload

from ._native import NativeSchema


type Path = builtins.tuple[str | int, ...]
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
_OBJECT_NEW = builtins.object.__new__

_URL_PATTERN = r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+$"
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DATE_PATTERN = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])$"
)
_TIME_PATTERN = (
    r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:\.[0-9]+)?)?"
    r"(?:[Zz]|[+-](?:0[0-9]|1[0-9]|2[0-3]):[0-5][0-9])?$"
)
_DATETIME_PATTERN = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"[Tt ](?:[01][0-9]|2[0-3]):[0-5][0-9]"
    r"(?::[0-5][0-9](?:\.[0-9]+)?)?"
    r"(?:[Zz]|[+-](?:0[0-9]|1[0-9]|2[0-3]):[0-5][0-9])?$"
)
_IPV4_PATTERN = (
    r"^(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$"
)
_DECIMAL_PATTERN = (
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
_BASE64_PATTERN = (
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
)
_BASE64URL_PATTERN = (
    r"^(?:[A-Za-z0-9_-]{4})*(?:[A-Za-z0-9_-]{2}==|[A-Za-z0-9_-]{3}=)?$"
)
_HOSTNAME_PATTERN = (
    r"^(?=.{1,253}$)"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(?:\.(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?))*$"
)
_KNOWN_FORMATS = frozenset({
    "email", "uri", "uuid", "date", "time", "date-time", "ipv4", "ipv6", "hostname",
})
_PATTERN_FORMATS = frozenset({"regex", "decimal", "base64", "base64url"})


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


def _string_values(values: Sequence[str], role: str) -> builtins.tuple[str, ...]:
    result = builtins.tuple(values)
    if not result:
        raise ValueError(f"{role} values must not be empty")
    if not all(isinstance(value, str) for value in result):
        raise TypeError(f"{role} values must be strings")
    return result


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    path: Path = ()
    branches: builtins.tuple[builtins.tuple[Issue, ...], ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        builtins.object.__setattr__(
            self, "params", MappingProxyType(dict(self.params)),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.params)
        result.update({
            "code": self.code, "message": self.message, "path": list(self.path),
        })
        if self.branches:
            result["branches"] = [
                [issue.to_dict() for issue in branch] for branch in self.branches
            ]
        return result


class UnrepresentableError(ValueError):
    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        location = ".".join(map(str, path)) or "<root>"
        super().__init__(f"{location}: {reason}")


def _issue_from_raw(raw: builtins.tuple[Any, ...]) -> Issue:
    code, message, path, raw_branches, params = raw
    issue = _OBJECT_NEW(Issue)
    branches = (
        builtins.tuple(
            builtins.tuple(_issue_from_raw(item) for item in branch)
            for branch in raw_branches
        )
        if raw_branches
        else ()
    )
    if not isinstance(params, MappingProxyType):
        params = MappingProxyType(params)
    issue.__dict__.update(
        code=code,
        message=message,
        path=path,
        branches=branches,
        params=params,
    )
    return issue


class ValidationError(ValueError):
    _issues: builtins.tuple[Issue, ...] | None
    _raw_issues: builtins.tuple[builtins.tuple[Any, ...], ...] | None
    _bundle: Any = None
    _message: str | None = None

    def __init__(self, issues: Sequence[Issue]):
        self._issues = builtins.tuple(issues)
        self._raw_issues = None
        self._bundle = None
        super().__init__()

    @classmethod
    def _from_raw(
        cls,
        raw_issues: builtins.tuple[builtins.tuple[Any, ...], ...],
    ) -> Self:
        """Keep native issue data compact until ``issues`` is first read."""
        error = ValueError.__new__(cls)
        error._issues = None
        error._raw_issues = raw_issues
        error._bundle = None
        ValueError.__init__(error)
        return error

    @classmethod
    def _from_bundle(cls, bundle: Any) -> Self:
        """Hold a GC-aware native bundle until issues are needed."""
        error = ValueError.__new__(cls)
        error._issues = None
        error._raw_issues = None
        error._bundle = bundle
        ValueError.__init__(error)
        return error

    @property
    def issues(self) -> builtins.tuple[Issue, ...]:
        issues = self._issues
        if issues is None:
            bundle = self._bundle
            if bundle is not None:
                raw_issues = bundle.to_raw()
                self._bundle = None
            else:
                raw_issues = cast(
                    builtins.tuple[builtins.tuple[Any, ...], ...],
                    self._raw_issues,
                )
            issues = builtins.tuple(map(_issue_from_raw, raw_issues))
            self._issues = issues
            self._raw_issues = None
        return issues

    def __str__(self) -> str:
        cached = self._message
        if cached is None:
            raw_issues = self._raw_issues
            if raw_issues is None and self._bundle is not None:
                raw_issues = self._bundle.to_raw()
            if raw_issues is None:
                cached = "; ".join(
                    f"{'.'.join(map(str, issue.path)) or '<root>'}: {issue.message}"
                    for issue in self.issues
                )
            else:
                cached = "; ".join(
                    f"{'.'.join(map(str, raw[2])) or '<root>'}: {raw[1]}"
                    for raw in raw_issues
                )
            self._message = cached
        return cached

    def to_dict(self) -> dict[str, Any]:
        return {"issues": [issue.to_dict() for issue in self.issues]}

    def flatten(self) -> dict[str, Any]:
        form_errors: list[str] = []
        field_errors: dict[str, list[str]] = {}
        for issue in self.issues:
            if issue.path:
                field_errors.setdefault(str(issue.path[0]), []).append(issue.message)
            else:
                form_errors.append(issue.message)
        return {"form_errors": form_errors, "field_errors": field_errors}

    def treeify(self) -> dict[str, Any]:
        root: dict[str, Any] = {"errors": [], "properties": {}, "items": {}}
        for issue in self.issues:
            node = root
            for part in issue.path:
                branch = "items" if isinstance(part, int) else "properties"
                node = node[branch].setdefault(
                    part, {"errors": [], "properties": {}, "items": {}},
                )
            node["errors"].append(issue.message)
        return root

    def prettify(self) -> str:
        return "\n".join(
            f"{'.'.join(map(str, issue.path)) or '<root>'}: {issue.message}"
            for issue in self.issues
        )


@dataclass(frozen=True)
class Success[T]:
    data: T
    success: Literal[True] = field(default=True, init=False)


@dataclass(frozen=True)
class Failure:
    error: ValidationError
    success: Literal[False] = field(default=False, init=False)


def _invalid_format(
    message: str, format_name: str, **params: Any,
) -> ValidationError:
    return ValidationError([
        Issue(
            "invalid_format",
            message,
            params={"format": format_name, **params},
        ),
    ])


def _prefix_issue(issue: Issue, prefix: str | int) -> Issue:
    branches = builtins.tuple(
        builtins.tuple(_prefix_issue(branch, prefix) for branch in items)
        for items in issue.branches
    )
    return replace(
        issue,
        path=(prefix, *issue.path),
        branches=branches,
    )


def _parse_iso_date(value: str, message: str) -> _datetime.date:
    try:
        return _datetime.date.fromisoformat(value)
    except ValueError:
        raise _invalid_format(message, "date") from None


def _parse_iso_time(value: str, message: str) -> _datetime.time:
    try:
        return _datetime.time.fromisoformat(value)
    except ValueError:
        raise _invalid_format(message, "time") from None


def _parse_iso_datetime(value: str, message: str) -> _datetime.datetime:
    try:
        return _datetime.datetime.fromisoformat(value)
    except ValueError:
        raise _invalid_format(message, "date-time") from None


def _parse_decimal(value: str, message: str) -> _decimal.Decimal:
    try:
        return _decimal.Decimal(value)
    except (_decimal.InvalidOperation, ValueError):
        raise _invalid_format(message, "decimal") from None


def _parse_base64(
    value: str, urlsafe: bool, message: str,
) -> builtins.bytes:
    try:
        if urlsafe:
            return base64.urlsafe_b64decode(value)
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid_format(message, "base64") from None


def _encode_base64(value: builtins.bytes, urlsafe: bool) -> str:
    encoded = (
        base64.urlsafe_b64encode(value)
        if urlsafe
        else base64.b64encode(value)
    )
    return encoded.decode("ascii")


def _parse_path(value: str) -> _pathlib.PurePath:
    return _pathlib.Path(value)


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

    def parse(self, value: In | Missing = MISSING) -> Out:
        return cast(Out, self._node.parse(value, MISSING, Issue, ValidationError))

    def safe_parse(self, value: In | Missing = MISSING) -> Success[Out] | Failure:
        """Catch validation errors only; programming errors remain visible."""
        try:
            return Success(self.parse(value))
        except ValidationError as error:
            return Failure(error)

    def validate(self, value: In | Missing = MISSING) -> bool:
        """Return whether validation succeeds; callback errors still propagate."""
        return self.safe_parse(value).success

    def parse_json(self, data: str | builtins.bytes | bytearray) -> Out:
        """Parse JSON text and validate the decoded value."""
        if not isinstance(data, (str, builtins.bytes, bytearray)):
            raise TypeError("JSON input must be str, bytes, or bytearray")
        fast, value = self._node.parse_json(data)
        if fast:
            return cast(Out, value)
        try:
            decoded = _json.loads(data)
        except _json.JSONDecodeError as error:
            position = error.pos
        except UnicodeDecodeError as error:
            position = error.start
        else:
            return self.parse(decoded)
        raise ValidationError([Issue(
            "invalid_format",
            "Invalid JSON",
            (),
            params={"format": "json", "position": position},
        )]) from None

    def safe_parse_json(
        self, data: str | builtins.bytes | bytearray,
    ) -> Success[Out] | Failure:
        """Catch validation errors from JSON parsing only."""
        try:
            return Success(self.parse_json(data))
        except ValidationError as error:
            return Failure(error)

    def validate_json(self, data: str | builtins.bytes | bytearray) -> bool:
        """Return whether JSON text parses and validates."""
        return self.safe_parse_json(data).success

    def encode(self, value: Out) -> In:
        """Encode a decoded value when the schema is reversible.

        Transforms, refinements, coercions, defaults, catches, pipes,
        preprocessors, and lazy schemas have no reverse path and raise
        ``TypeError``.
        """
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

    def to_json_schema(
        self,
        *,
        target: JSONSchemaTarget = "draft-2020-12",
        io: JSONSchemaIO = "output",
        unrepresentable: UnrepresentablePolicy = "throw",
    ) -> JSONSchema:
        """Export this schema as a JSON Schema-compatible mapping."""
        return to_json_schema(
            self, target=target, io=io, unrepresentable=unrepresentable,
        )

    def _wrap(self, kind: str, *args: Any) -> Self:
        return replace(self, _node=NativeSchema(kind, (self._node,), args))

    def unwrap(self) -> Schema[Any, Any]:
        """Return the wrapped inner schema, when one is tracked."""
        if self._inner is not None:
            return self._inner
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

    def overwrite(self, fn: Callable[[Out], Out]) -> Schema[Out, In]:
        """Transform a parsed value without changing its static type."""
        fn = _sync_callback(fn, "overwrite callback")
        return Schema[Out, In](
            NativeSchema("overwrite", (self._node,), (fn,)),
            _inner=self,
            _meta=self._meta,
        )

    def pipe[U](self, other: Schema[U, Any]) -> Schema[U, In]:
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
        """Validate a fresh copy of the default when input is missing."""
        return Schema[Out, In](
            NativeSchema("default", (self._node,), (value,)),
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

    def catch[U](self, fallback: U | Callable[[], U]) -> Schema[Out | U, In]:
        """Return a fallback when validation fails; callback errors propagate."""
        if callable(fallback):
            fallback = _sync_callback(fallback, "catch fallback")
        return Schema[Out | U, In](
            NativeSchema("catch", (self._node,), (fallback,)),
            _inner=self,
            _meta=self._meta,
        )

    def array(self) -> ArraySchema[Out, In]:
        return ArraySchema[Out, In](
            NativeSchema("array", (self._node,), ()),
            self,
        )

    def or_[U, OtherIn](
        self, other: Schema[U, OtherIn],
    ) -> UnionSchema[Out | U, In | OtherIn]:
        return UnionSchema[Out | U, In | OtherIn](
            NativeSchema("union", (self._node, other._node), ()),
            (self, other),
        )

    def and_(self, other: Schema[Any, Any]) -> IntersectionSchema:
        return intersection(self, other)


@dataclass(frozen=True)
class CodecSchema[Out, In = Out](Schema[Out, In]):
    """Bidirectional conversion between an encoded and a decoded schema."""

    _encode_node: NativeSchema = field(repr=False, kw_only=True)
    _input_schema: Schema[In, Any] = field(
        repr=False, kw_only=True, compare=False,
    )
    _output_schema: Schema[Out, Any] = field(
        repr=False, kw_only=True, compare=False,
    )

    @property
    def input_schema(self) -> Schema[In, Any]:
        return self._input_schema

    @property
    def output_schema(self) -> Schema[Out, Any]:
        return self._output_schema

    def decode(self, value: In | Missing = MISSING) -> Out:
        """Decode an encoded value; equivalent to ``parse``."""
        return self.parse(value)

    def safe_decode(
        self, value: In | Missing = MISSING,
    ) -> Success[Out] | Failure:
        return self.safe_parse(value)

    def optional(self) -> CodecSchema[Out | Missing, In | Missing]:
        result = _optional_codec(self)
        return cast(
            CodecSchema[Out | Missing, In | Missing],
            replace(result, _meta=self._meta),
        )

    def nonoptional(self) -> CodecSchema[Out, In]:
        result = _nonoptional_codec(self)
        return cast(CodecSchema[Out, In], replace(result, _meta=self._meta))

    def nullable(self) -> CodecSchema[Out | None, In | None]:
        result = _nullable_codec(self)
        return cast(
            CodecSchema[Out | None, In | None],
            replace(result, _meta=self._meta),
        )

    def nullish(self) -> CodecSchema[Out | Missing | None, In | Missing | None]:
        return self.nullable().optional()

    def record(self) -> CodecSchema[dict[str, Out], dict[str, In]]:
        result = _record_codec(self)
        return cast(
            CodecSchema[dict[str, Out], dict[str, In]],
            replace(result, _meta=self._meta),
        )


@dataclass(frozen=True)
class _CompoundCodecSchema[Out, In = Out](CodecSchema[Out, In]):
    _json_output_schema: Schema[Any, Any] = field(
        repr=False, kw_only=True, compare=False,
    )


@dataclass(frozen=True)
class _JsonTextSchema(Schema[str, str]):
    """String schema that validates JSON syntax with path-aware issues."""


def _json_text_input(message: str) -> _JsonTextSchema:
    def check(value: str) -> Issue | None:
        try:
            _json.loads(value)
        except _json.JSONDecodeError as error:
            return Issue(
                "invalid_format",
                message,
                params={"format": "json", "position": error.pos},
            )
        return None

    return _JsonTextSchema(
        NativeSchema(
            "super_refine",
            (NativeSchema("string", (), ()),),
            (check,),
        ),
    )


@dataclass(frozen=True)
class StringSchema(Schema[str, str]):
    def min(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self._wrap(
            "length_min",
            length,
            _message(
                message,
                f"Must contain at least {_bound_text(length)} characters",
            ),
        )

    def max(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self._wrap(
            "length_max",
            length,
            _message(
                message,
                f"Must contain at most {_bound_text(length)} characters",
            ),
        )

    def length(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self.min(length, message=message).max(length, message=message)

    def regex(self, pattern: str | re.Pattern[str], message: str = "Invalid format") -> Self:
        return self._regex(pattern, message, "regex")

    def email(self, *, message: str = "Invalid email address") -> Self:
        """Basic ASCII-style address syntax, not deliverability validation."""
        return self._regex(
            r"^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+\Z", message, "email",
        )

    def url(self, *, message: str = "Invalid URL") -> Self:
        return self._regex(_URL_PATTERN, message, "uri")

    def uuid(self, *, message: str = "Invalid UUID") -> Self:
        return self._regex(_UUID_PATTERN, message, "uuid")

    def date(self, *, message: str = "Invalid date") -> Self:
        return self._regex(_DATE_PATTERN, message, "date")

    def time(self, *, message: str = "Invalid time") -> Self:
        return self._regex(_TIME_PATTERN, message, "time")

    def datetime(self, *, message: str = "Invalid datetime") -> Self:
        return self._regex(_DATETIME_PATTERN, message, "date-time")

    def ipv4(self, *, message: str = "Invalid IPv4 address") -> Self:
        return self._regex(_IPV4_PATTERN, message, "ipv4")

    def ipv6(self, *, message: str = "Invalid IPv6 address") -> Self:
        return self._wrap("ipv6", _message(message, "Invalid IPv6 address"))

    def hostname(self, *, message: str = "Invalid hostname") -> Self:
        return self._regex(_HOSTNAME_PATTERN, message, "hostname")

    def starts_with(self, prefix: str, *, message: str | None = None) -> Self:
        prefix = _string_argument(prefix, "Prefix")
        return self._wrap(
            "starts_with",
            prefix,
            _message(message, f"Must start with {prefix!r}"),
        )

    def ends_with(self, suffix: str, *, message: str | None = None) -> Self:
        suffix = _string_argument(suffix, "Suffix")
        return self._wrap(
            "ends_with",
            suffix,
            _message(message, f"Must end with {suffix!r}"),
        )

    def includes(self, substring: str, *, message: str | None = None) -> Self:
        substring = _string_argument(substring, "Substring")
        return self._wrap(
            "includes",
            substring,
            _message(message, f"Must include {substring!r}"),
        )

    def stringbool(
        self,
        *,
        truthy: Sequence[str] = ("true", "1", "yes", "on"),
        falsy: Sequence[str] = ("false", "0", "no", "off"),
        case_sensitive: bool = False,
        message: str = "Invalid boolean value",
    ) -> Schema[bool, str]:
        truthy_values = _string_values(truthy, "truthy")
        falsy_values = _string_values(falsy, "falsy")
        if not isinstance(case_sensitive, bool):
            raise TypeError("case_sensitive must be a boolean")
        if case_sensitive:
            overlap = set(truthy_values) & set(falsy_values)
        else:
            overlap = (
                {value.lower() for value in truthy_values}
                & {value.lower() for value in falsy_values}
            )
        if overlap:
            raise ValueError("truthy and falsy values must not overlap")
        return Schema[bool, str](
            NativeSchema(
                "string_bool",
                (self._node,),
                (
                    truthy_values,
                    falsy_values,
                    case_sensitive,
                    _message(message, "Invalid boolean value"),
                ),
            ),
            _inner=self,
            _meta=self._meta,
        )

    def _regex(
        self,
        pattern: str | re.Pattern[str],
        message: str,
        format_name: str,
    ) -> Self:
        compiled = re.compile(pattern)
        return self._wrap(
            "regex", compiled, _message(message, "Invalid format"), format_name,
        )

    def trim(self) -> Self:
        return self._wrap("trim")

    def lower(self) -> Self:
        return self._wrap("lower")

    def upper(self) -> Self:
        return self._wrap("upper")


@dataclass(frozen=True)
class NumberSchema[T: (int, float, int | float)](Schema[T, T]):
    def min(self, bound: int | float, *, message: str | None = None) -> Self:
        _numeric_bound(bound)
        return self._wrap(
            "min", bound, _message(message, f"Must be at least {_bound_text(bound)}"),
        )

    def max(self, bound: int | float, *, message: str | None = None) -> Self:
        _numeric_bound(bound)
        return self._wrap(
            "max", bound, _message(message, f"Must be at most {_bound_text(bound)}"),
        )

    def gt(self, bound: int | float, *, message: str | None = None) -> Self:
        _numeric_bound(bound)
        return self._wrap(
            "exclusive_min",
            bound,
            _message(message, f"Must be greater than {_bound_text(bound)}"),
        )

    def gte(self, bound: int | float, *, message: str | None = None) -> Self:
        return self.min(bound, message=message)

    def lt(self, bound: int | float, *, message: str | None = None) -> Self:
        _numeric_bound(bound)
        return self._wrap(
            "exclusive_max",
            bound,
            _message(message, f"Must be less than {_bound_text(bound)}"),
        )

    def lte(self, bound: int | float, *, message: str | None = None) -> Self:
        return self.max(bound, message=message)

    def multiple_of(self, divisor: int | float, *, message: str | None = None) -> Self:
        _numeric_bound(divisor)
        if divisor <= 0:
            raise ValueError("Divisor must be positive")
        return self._wrap(
            "multiple_of",
            divisor,
            _message(message, f"Must be a multiple of {_bound_text(divisor)}"),
        )

    def positive(self, *, message: str | None = None) -> Self:
        return self._wrap("positive", _message(message, "Must be positive"))

    def nonnegative(self, *, message: str | None = None) -> Self:
        return self.min(0, message=message)

    def int(self, *, message: str | None = None) -> Self:
        return self._wrap("integral", _message(message, "Must be an integer"))


@dataclass(frozen=True)
class ArraySchema[Out, In = Out](Schema[list[Out], list[In]]):
    item: Schema[Out, In]

    def unwrap(self) -> Schema[Out, In]:
        return self.item

    def min(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self._wrap(
            "length_min",
            length,
            _message(
                message,
                f"Must contain at least {_bound_text(length)} items",
            ),
        )

    def max(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self._wrap(
            "length_max",
            length,
            _message(
                message,
                f"Must contain at most {_bound_text(length)} items",
            ),
        )

    def length(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self.min(length, message=message).max(length, message=message)

    def nonempty(self, *, message: str | None = None) -> Self:
        return self.min(1, message=message)


def _bound_text(value: int | float) -> str:
    try:
        return str(value)
    except ValueError:
        if type(value) is int:
            return f"<integer with {value.bit_length()} bits>"
        raise


def _length_bound(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("Length must be a nonnegative integer")


def _numeric_bound(value: int | float) -> None:
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("Bound must be a finite number")


def string() -> StringSchema:
    return StringSchema(NativeSchema("string", (), ()))


def number() -> NumberSchema[int | float]:
    return NumberSchema(NativeSchema("number", (), ()))


def integer() -> NumberSchema[int]:
    return NumberSchema(NativeSchema("integer", (), ()))


def boolean() -> Schema[bool, bool]:
    return Schema[bool, bool](NativeSchema("boolean", (), ()))


class _Coerce:
    @staticmethod
    def string() -> StringSchema:
        base = string()
        return StringSchema(
            NativeSchema("coerce", (base._node,), ("string",)),
            _inner=base,
        )

    @staticmethod
    def number() -> NumberSchema[int | float]:
        base = number()
        return NumberSchema(
            NativeSchema("coerce", (base._node,), ("number",)),
            _inner=base,
        )

    @staticmethod
    def integer() -> NumberSchema[int]:
        base = integer()
        return NumberSchema(
            NativeSchema("coerce", (base._node,), ("integer",)),
            _inner=base,
        )

    @staticmethod
    def boolean() -> Schema[bool, bool]:
        base = boolean()
        return Schema[bool, bool](
            NativeSchema("coerce", (base._node,), ("boolean",)),
            _inner=base,
        )


coerce = _Coerce()


def unknown() -> Schema[Any, Any]:
    return Schema[Any, Any](NativeSchema("unknown", (), ()))


def none() -> Schema[None, None]:
    return Schema[None, None](NativeSchema("none", (), ()))


def date() -> Schema[_datetime.date, _datetime.date]:
    return Schema[_datetime.date, _datetime.date](
        NativeSchema("exact_instance", (), (_datetime.date,)),
    )


def datetime() -> Schema[_datetime.datetime, _datetime.datetime]:
    return Schema[_datetime.datetime, _datetime.datetime](
        NativeSchema("exact_instance", (), (_datetime.datetime,)),
    )


def time() -> Schema[_datetime.time, _datetime.time]:
    return Schema[_datetime.time, _datetime.time](
        NativeSchema("exact_instance", (), (_datetime.time,)),
    )


def timedelta() -> Schema[_datetime.timedelta, _datetime.timedelta]:
    return Schema[_datetime.timedelta, _datetime.timedelta](
        NativeSchema("exact_instance", (), (_datetime.timedelta,)),
    )


def decimal() -> Schema[_decimal.Decimal, _decimal.Decimal]:
    return Schema[_decimal.Decimal, _decimal.Decimal](
        NativeSchema("exact_instance", (), (_decimal.Decimal,)),
    )


def bytes() -> Schema[builtins.bytes, builtins.bytes]:
    return Schema[builtins.bytes, builtins.bytes](
        NativeSchema("exact_instance", (), (builtins.bytes,)),
    )


def path() -> Schema[_pathlib.PurePath, _pathlib.PurePath]:
    return Schema[_pathlib.PurePath, _pathlib.PurePath](
        NativeSchema("instance", (), (_pathlib.PurePath,)),
    )


def json() -> Schema[Any, Any]:
    return Schema[Any, Any](NativeSchema("json", (), ()))


def native_enum[E: Enum](enum_cls: type[E]) -> Schema[E, E]:
    if not isinstance(enum_cls, type) or not issubclass(enum_cls, Enum):
        raise TypeError("native_enum requires an Enum class")
    if not builtins.tuple(enum_cls):
        raise ValueError("Enum must contain at least one member")
    return Schema[E, E](NativeSchema("native_enum", (), (enum_cls,)))


def _validate_literal(value: Any) -> None:
    if type(value) not in (str, int, float, bool, type(None)):
        raise TypeError("Literal must be a string, number, boolean, or None")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Literal must be finite")


def literal[T](expected: T) -> Schema[T, T]:
    _validate_literal(expected)
    return Schema[T, T](NativeSchema("literal", (), (expected,)))


def enum[T](values: Sequence[T]) -> Schema[T, T]:
    choices = builtins.tuple(values)
    if not choices:
        raise ValueError("Enum must contain at least one value")
    for value in choices:
        _validate_literal(value)
    return Schema[T, T](NativeSchema("enum", (), (choices,)))


def instance_of[T](cls: type[T]) -> Schema[T, T]:
    return Schema[T, T](NativeSchema("instance", (), (cls,)))


def array[Out, In](item: Schema[Out, In]) -> ArraySchema[Out, In]:
    return ArraySchema[Out, In](
        NativeSchema("array", (item._node,), ()),
        item,
    )


type UnknownKeys = Literal["strip", "strict", "passthrough"]


@dataclass(frozen=True)
class ObjectSchema(Schema[dict[str, Any], dict[str, Any]]):
    shape: Mapping[str, Schema[Any, Any]]
    unknown_keys: UnknownKeys | Literal["catchall"] = "strip"
    _catchall: Schema[Any, Any] | None = field(default=None, repr=False)

    def _rebuild(
        self,
        shape: Mapping[str, Schema[Any, Any]],
        unknown_keys: UnknownKeys | Literal["catchall"] | None = None,
        catchall: Schema[Any, Any] | builtins.object | None = _UNSET,
    ) -> ObjectSchema:
        policy = self.unknown_keys if unknown_keys is None else unknown_keys
        effective_catchall = (
            self._catchall
            if catchall is _UNSET
            else cast(Schema[Any, Any] | None, catchall)
        )
        rebuilt = object(
            shape,
            unknown_keys=policy,
            catchall=effective_catchall,
        )
        return replace(rebuilt, _meta=self._meta)

    def strict(self) -> ObjectSchema:
        return self._rebuild(self.shape, "strict", catchall=None)

    def passthrough(self) -> ObjectSchema:
        return self._rebuild(self.shape, "passthrough", catchall=None)

    def strip(self) -> ObjectSchema:
        return self._rebuild(self.shape, "strip", catchall=None)

    def catchall(self, schema: Schema[Any, Any]) -> ObjectSchema:
        if not isinstance(schema, Schema):
            raise TypeError("Catchall must be a schema")
        return self._rebuild(self.shape, "catchall", catchall=schema)

    def extend(self, shape: Mapping[str, Schema[Any, Any]]) -> ObjectSchema:
        return self._rebuild({**self.shape, **shape})

    def pick(self, *keys: str) -> ObjectSchema:
        _check_keys(self.shape, keys)
        return self._rebuild({key: self.shape[key] for key in keys})

    def omit(self, *keys: str) -> ObjectSchema:
        _check_keys(self.shape, keys)
        return self._rebuild({k: v for k, v in self.shape.items() if k not in keys})

    def partial(self, *keys: str) -> ObjectSchema:
        if keys:
            _check_keys(self.shape, keys)
            selected = set(keys)
            return self._rebuild({
                key: value.optional() if key in selected else value
                for key, value in self.shape.items()
            })
        return self._rebuild({k: v.optional() for k, v in self.shape.items()})

    def required(self) -> ObjectSchema:
        return self._rebuild({k: v.nonoptional() for k, v in self.shape.items()})

    def keyof(self) -> Schema[str, str]:
        keys = builtins.tuple(self.shape)
        if not keys:
            raise ValueError("Cannot take keyof of an object with no keys")
        return enum(keys)


@dataclass(frozen=True)
class RecordSchema[Out, In = Out](Schema[dict[str, Out], dict[str, In]]):
    value_schema: Schema[Out, In]

    def unwrap(self) -> Schema[Out, In]:
        return self.value_schema


@dataclass(frozen=True)
class KeyedRecordSchema[Key, Out, In = Out](
    Schema[dict[Key, Out], dict[Key, In]],
):
    key_schema: Schema[Key, Any]
    value_schema: Schema[Out, In]

    def unwrap(self) -> Schema[Out, In]:
        return self.value_schema


@dataclass(frozen=True)
class TupleSchema[
    Out = builtins.tuple[Any, ...],
    In = list[Any] | builtins.tuple[Any, ...],
](Schema[Out, In]):
    items: builtins.tuple[Schema[Any, Any], ...]
    _rest: Schema[Any, Any] | None = field(default=None, repr=False)

    def rest(
        self, schema: Schema[Any, Any],
    ) -> TupleSchema[
        builtins.tuple[Any, ...],
        list[Any] | builtins.tuple[Any, ...],
    ]:
        if not isinstance(schema, Schema):
            raise TypeError("Tuple rest must be a schema")
        return TupleSchema[
            builtins.tuple[Any, ...],
            list[Any] | builtins.tuple[Any, ...],
        ](
            NativeSchema(
                "tuple",
                builtins.tuple(item._node for item in self.items),
                (schema._node,),
            ),
            self.items,
            schema,
            _meta=self._meta,
        )


@dataclass(frozen=True)
class UnionSchema[Out, In = Out](Schema[Out, In]):
    options: builtins.tuple[Schema[Any, Any], ...]


@dataclass(frozen=True)
class DiscriminatedUnionSchema(Schema[Any, Any]):
    key: str
    options: Mapping[str, Schema[Any, Any]]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.options, MappingProxyType):
            builtins.object.__setattr__(
                self, "options", MappingProxyType(dict(self.options)),
            )


@dataclass(frozen=True)
class IntersectionSchema(Schema[Any, Any]):
    left: Schema[Any, Any]
    right: Schema[Any, Any]


def _check_keys(shape: Mapping[str, Schema[Any, Any]], keys: Sequence[str]) -> None:
    for key in keys:
        if key not in shape:
            raise KeyError(key)


def object(
    shape: Mapping[str, Schema[Any, Any]],
    *,
    unknown_keys: UnknownKeys | Literal["catchall"] = "strip",
    catchall: Schema[Any, Any] | None = None,
) -> ObjectSchema:
    if catchall is not None:
        if not isinstance(catchall, Schema):
            raise TypeError("Catchall must be a schema")
        if unknown_keys not in ("strip", "catchall"):
            raise ValueError("Catchall conflicts with the unknown-key policy")
        policy: UnknownKeys | Literal["catchall"] = "catchall"
    else:
        if unknown_keys == "catchall":
            raise ValueError("Catchall schema is required")
        policy = unknown_keys
    if policy not in ("strip", "strict", "passthrough", "catchall"):
        raise ValueError("Unknown key policy")
    if not all(isinstance(k, str) and isinstance(v, Schema) for k, v in shape.items()):
        raise TypeError("Object shape must map strings to schemas")
    frozen = MappingProxyType(dict(shape))

    node = NativeSchema(
        "object", builtins.tuple(child._node for child in frozen.values()),
        (
            builtins.tuple(frozen),
            policy,
            catchall._node if catchall is not None else None,
        ),
    )
    return ObjectSchema(node, frozen, policy, catchall)


@overload
def union[T, TI](first: Schema[T, TI], /) -> UnionSchema[T, TI]: ...


@overload
def union[T, U, TI, UI](
    first: Schema[T, TI], second: Schema[U, UI], /
) -> UnionSchema[T | U, TI | UI]: ...


@overload
def union[T, U, V, TI, UI, VI](
    first: Schema[T, TI], second: Schema[U, UI], third: Schema[V, VI], /
) -> UnionSchema[T | U | V, TI | UI | VI]: ...


@overload
def union[T, U, V, W, TI, UI, VI, WI](
    first: Schema[T, TI], second: Schema[U, UI],
    third: Schema[V, VI], fourth: Schema[W, WI], /
) -> UnionSchema[T | U | V | W, TI | UI | VI | WI]: ...


@overload
def union(*options: Schema[Any, Any]) -> UnionSchema[Any, Any]: ...


def union(*options: Schema[Any, Any]) -> UnionSchema[Any, Any]:
    if not options:
        raise ValueError("Union requires at least one schema")

    return UnionSchema[Any, Any](
        NativeSchema("union", builtins.tuple(option._node for option in options), ()),
        options,
    )


def intersection(
    left: Schema[Any, Any], right: Schema[Any, Any], /
) -> IntersectionSchema:
    if not isinstance(left, Schema) or not isinstance(right, Schema):
        raise TypeError("Intersection operands must be schemas")
    return IntersectionSchema(
        NativeSchema("intersection", (left._node, right._node), ()),
        left,
        right,
    )


def discriminated_union(
    key: str,
    options: Mapping[str, Schema[Any, Any]],
) -> DiscriminatedUnionSchema:
    """Dispatch on a dictionary key; option values are branch schemas."""
    if not isinstance(key, str):
        raise TypeError("Discriminator key must be a string")
    items = builtins.tuple(options.items())
    if not items:
        raise ValueError("Discriminated union requires at least one option")
    if not all(
        isinstance(option_key, str) and isinstance(schema, Schema)
        for option_key, schema in items
    ):
        raise TypeError("Discriminated union options must map strings to schemas")
    return DiscriminatedUnionSchema(
        NativeSchema(
            "discriminated_union",
            builtins.tuple(schema._node for _, schema in items),
            (key, builtins.tuple(option_key for option_key, _ in items)),
        ),
        key,
        MappingProxyType(dict(items)),
    )


@overload
def record[Out, In](value_schema: Schema[Out, In], /) -> RecordSchema[Out, In]: ...


@overload
def record[Key, Out, In](
    key_schema: Schema[Key, Any], value_schema: Schema[Out, In], /
) -> KeyedRecordSchema[Key, Out, In]: ...


def record(
    first: Schema[Any, Any], second: Schema[Any, Any] | None = None,
) -> RecordSchema[Any, Any] | KeyedRecordSchema[Any, Any, Any]:
    if not isinstance(first, Schema):
        raise TypeError("Record schemas must be Schema instances")
    if second is None:
        return RecordSchema[Any, Any](
            NativeSchema("record", (first._node,), (None,)),
            first,
        )
    if not isinstance(second, Schema):
        raise TypeError("Record schemas must be Schema instances")
    return KeyedRecordSchema[Any, Any, Any](
        NativeSchema("record", (second._node,), (first._node,)),
        first,
        second,
    )


@overload
def tuple() -> TupleSchema[
    builtins.tuple[()], list[Any] | builtins.tuple[()],
]: ...


@overload
def tuple[T1, I1](
    first: Schema[T1, I1], /,
) -> TupleSchema[
    builtins.tuple[T1], list[I1] | builtins.tuple[I1],
]: ...


@overload
def tuple[T1, I1, T2, I2](
    first: Schema[T1, I1], second: Schema[T2, I2], /,
) -> TupleSchema[
    builtins.tuple[T1, T2],
    list[I1 | I2] | builtins.tuple[I1, I2],
]: ...


@overload
def tuple[T1, I1, T2, I2, T3, I3](
    first: Schema[T1, I1], second: Schema[T2, I2],
    third: Schema[T3, I3], /,
) -> TupleSchema[
    builtins.tuple[T1, T2, T3],
    list[I1 | I2 | I3] | builtins.tuple[I1, I2, I3],
]: ...


@overload
def tuple[T1, I1, T2, I2, T3, I3, T4, I4](
    first: Schema[T1, I1], second: Schema[T2, I2],
    third: Schema[T3, I3], fourth: Schema[T4, I4], /,
) -> TupleSchema[
    builtins.tuple[T1, T2, T3, T4],
    list[I1 | I2 | I3 | I4] | builtins.tuple[I1, I2, I3, I4],
]: ...


@overload
def tuple(
    *items: Schema[Any, Any],
) -> TupleSchema[
    builtins.tuple[Any, ...],
    list[Any] | builtins.tuple[Any, ...],
]: ...


def tuple(
    *items: Schema[Any, Any],
) -> TupleSchema[
    builtins.tuple[Any, ...],
    list[Any] | builtins.tuple[Any, ...],
]:
    return TupleSchema[
        builtins.tuple[Any, ...],
        list[Any] | builtins.tuple[Any, ...],
    ](
        NativeSchema("tuple", builtins.tuple(item._node for item in items), (None,)),
        items,
    )


def lazy[Out, In](factory: Callable[[], Schema[Out, In]]) -> Schema[Out, In]:
    """Resolve a recursive schema at parse time; input must be acyclic.

    Each lazy node resolves its factory once per parse and reuses the result for
    that parse; a later parse invokes the factory again.
    """
    factory = _sync_callback(factory, "lazy factory")
    return Schema[Out, In](NativeSchema("lazy", (), (factory,)))


def preprocess[Out, In](fn: Callable[[Any], Any], schema: Schema[Out, In]) -> Schema[Out, Any]:
    fn = _sync_callback(fn, "preprocess callback")
    return Schema[Out, Any](
        NativeSchema("preprocess", (schema._node,), (fn,)),
        _inner=schema,
        _meta=schema._meta,
    )


def codec[Out, In](
    input_schema: Schema[In, Any],
    output_schema: Schema[Out, Any],
    *,
    decode: Callable[[In], Out],
    encode: Callable[[Out], In],
) -> CodecSchema[Out, In]:
    """Build a bidirectional codec between encoded and decoded schemas.

    ``decode`` maps validated encoded input to decoded output; ``encode`` maps
    validated decoded input back to encoded output. Both directions validate
    their result through the corresponding schema.
    """
    if not isinstance(input_schema, Schema) or not isinstance(output_schema, Schema):
        raise TypeError("Codec input and output must be schemas")
    if not callable(decode):
        raise TypeError("decode must be callable")
    if not callable(encode):
        raise TypeError("encode must be callable")
    decode = _sync_callback(decode, "decode callback")
    encode = _sync_callback(encode, "encode callback")

    decode_node = NativeSchema(
        "pipe",
        (
            NativeSchema("transform", (input_schema._node,), (decode,)),
            output_schema._node,
        ),
        (),
    )
    encode_node = NativeSchema(
        "pipe",
        (
            NativeSchema("transform", (output_schema._node,), (encode,)),
            input_schema._node,
        ),
        (),
    )
    return CodecSchema[Out, In](
        decode_node,
        _encode_node=encode_node,
        _input_schema=input_schema,
        _output_schema=output_schema,
        _inner=output_schema,
    )


def _json_output_schema_for(schema: Schema[Any, Any]) -> Schema[Any, Any]:
    if isinstance(schema, _CompoundCodecSchema):
        return schema._json_output_schema
    if isinstance(schema, CodecSchema):
        return schema.output_schema
    return schema


def _compound_codec[Out, In](
    input_schema: Schema[In, Any],
    output_schema: Schema[Out, Any],
    json_output_schema: Schema[Any, Any],
    *,
    decode: Callable[[In], Out],
    encode: Callable[[Out], In],
) -> _CompoundCodecSchema[Out, In]:
    base = codec(input_schema, output_schema, decode=decode, encode=encode)
    return _CompoundCodecSchema[Out, In](
        base._node,
        _encode_node=base._encode_node,
        _input_schema=base._input_schema,
        _output_schema=base._output_schema,
        _json_output_schema=json_output_schema,
        _inner=base._inner,
        _meta=base._meta,
    )


def _identity_codec(schema: Schema[Any, Any]) -> CodecSchema[Any, Any]:
    return codec(
        schema,
        schema,
        decode=lambda value: value,
        encode=lambda value: value,
    )


def _array_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    def decode_values(values: list[Any]) -> list[Any]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, value in enumerate(values):
            try:
                result.append(inner.decode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    def encode_values(values: list[Any]) -> list[Any]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, value in enumerate(values):
            try:
                result.append(inner.encode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    return _compound_codec(
        array(inner.input_schema),
        array(inner.output_schema),
        array(_json_output_schema_for(inner)),
        decode=decode_values,
        encode=encode_values,
    )


def _record_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    def decode_values(values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        issues: list[Issue] = []
        for key, value in values.items():
            try:
                result[key] = inner.decode(value)
            except ValidationError as error:
                issues.extend(_prefix_issue(item, key) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    def encode_values(values: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        issues: list[Issue] = []
        for key, value in values.items():
            try:
                result[key] = inner.encode(value)
            except ValidationError as error:
                issues.extend(_prefix_issue(item, key) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return result

    return _compound_codec(
        record(inner.input_schema),
        record(inner.output_schema),
        record(_json_output_schema_for(inner)),
        decode=decode_values,
        encode=encode_values,
    )


def _optional_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    def decode_value(value: Any) -> Any:
        return MISSING if value is MISSING else inner.decode(value)

    def encode_value(value: Any) -> Any:
        return MISSING if value is MISSING else inner.encode(value)

    return _compound_codec(
        inner.input_schema.optional(),
        inner.output_schema.optional(),
        _json_output_schema_for(inner).optional(),
        decode=decode_value,
        encode=encode_value,
    )


def _nonoptional_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    return _compound_codec(
        inner.input_schema.nonoptional(),
        inner.output_schema.nonoptional(),
        _json_output_schema_for(inner).nonoptional(),
        decode=inner.decode,
        encode=inner.encode,
    )


def _nullable_codec(inner: CodecSchema[Any, Any]) -> CodecSchema[Any, Any]:
    def decode_value(value: Any) -> Any:
        return None if value is None else inner.decode(value)

    def encode_value(value: Any) -> Any:
        return None if value is None else inner.encode(value)

    return _compound_codec(
        inner.input_schema.nullable(),
        inner.output_schema.nullable(),
        _json_output_schema_for(inner).nullable(),
        decode=decode_value,
        encode=encode_value,
    )


def _union_codec(options: Sequence[Schema[Any, Any]]) -> CodecSchema[Any, Any]:
    if not options:
        raise ValueError("Union codec requires at least one schema")
    if len(options) == 1:
        option = options[0]
        return option if isinstance(option, CodecSchema) else _identity_codec(option)
    branches = [
        option if isinstance(option, CodecSchema) else _identity_codec(option)
        for option in options
    ]
    input_schema = union(*(branch.input_schema for branch in branches))
    output_schema = union(*(branch.output_schema for branch in branches))

    def decode_value(value: Any) -> Any:
        branch_issues: list[builtins.tuple[Issue, ...]] = []
        for branch in branches:
            try:
                return branch.decode(value)
            except ValidationError as error:
                branch_issues.append(error.issues)
        raise ValidationError([
            Issue(
                "invalid_union",
                "Invalid input",
                branches=builtins.tuple(branch_issues),
            ),
        ])

    def encode_value(value: Any) -> Any:
        branch_issues: list[builtins.tuple[Issue, ...]] = []
        for branch in branches:
            try:
                return branch.encode(value)
            except ValidationError as error:
                branch_issues.append(error.issues)
        raise ValidationError([
            Issue(
                "invalid_union",
                "Invalid input",
                branches=builtins.tuple(branch_issues),
            ),
        ])

    return _compound_codec(
        input_schema,
        output_schema,
        union(*(_json_output_schema_for(option) for option in options)),
        decode=decode_value,
        encode=encode_value,
    )


def _tuple_codec(options: Sequence[Schema[Any, Any]]) -> CodecSchema[Any, Any]:
    branches = [
        option if isinstance(option, CodecSchema) else _identity_codec(option)
        for option in options
    ]
    input_schema = tuple(*(branch.input_schema for branch in branches))
    output_schema = tuple(*(branch.output_schema for branch in branches))

    def decode_values(values: builtins.tuple[Any, ...]) -> builtins.tuple[Any, ...]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, (branch, value) in enumerate(
            zip(branches, values, strict=True),
        ):
            try:
                result.append(branch.decode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return builtins.tuple(result)

    def encode_values(values: builtins.tuple[Any, ...]) -> builtins.tuple[Any, ...]:
        result: list[Any] = []
        issues: list[Issue] = []
        for index, (branch, value) in enumerate(
            zip(branches, values, strict=True),
        ):
            try:
                result.append(branch.encode(value))
            except ValidationError as error:
                issues.extend(_prefix_issue(item, index) for item in error.issues)
        if issues:
            raise ValidationError(issues)
        return builtins.tuple(result)

    return _compound_codec(
        input_schema,
        output_schema,
        tuple(*(_json_output_schema_for(option) for option in options)),
        decode=decode_values,
        encode=encode_values,
    )


_decimal_output = decimal
_path_output = path


class _Codecs:
    @staticmethod
    def iso_date(
        *, message: str = "Invalid date",
    ) -> CodecSchema[_datetime.date, str]:
        validated = _message(message, "Invalid date")
        return codec(
            string().date(message=validated),
            date(),
            decode=lambda value: _parse_iso_date(value, validated),
            encode=lambda value: value.isoformat(),
        )

    @staticmethod
    def iso_time(
        *, message: str = "Invalid time",
    ) -> CodecSchema[_datetime.time, str]:
        validated = _message(message, "Invalid time")
        return codec(
            string().time(message=validated),
            time(),
            decode=lambda value: _parse_iso_time(value, validated),
            encode=lambda value: value.isoformat(),
        )

    @staticmethod
    def iso_datetime(
        *, message: str = "Invalid datetime",
    ) -> CodecSchema[_datetime.datetime, str]:
        validated = _message(message, "Invalid datetime")
        return codec(
            string().datetime(message=validated),
            datetime(),
            decode=lambda value: _parse_iso_datetime(value, validated),
            encode=lambda value: value.isoformat(),
        )

    @staticmethod
    def decimal(
        *, message: str = "Invalid decimal string",
    ) -> CodecSchema[_decimal.Decimal, str]:
        validated = _message(message, "Invalid decimal string")
        return codec(
            string()._regex(_DECIMAL_PATTERN, validated, "decimal"),
            _decimal_output(),
            decode=lambda value: _parse_decimal(value, validated),
            encode=lambda value: str(value),
        )

    @staticmethod
    def json_text(*, message: str = "Invalid JSON") -> CodecSchema[Any, str]:
        validated = _message(message, "Invalid JSON")
        return codec(
            _json_text_input(validated),
            json(),
            decode=lambda value: _json.loads(value),
            encode=lambda value: _json.dumps(value),
        )

    @staticmethod
    def path() -> CodecSchema[_pathlib.PurePath, str]:
        return codec(
            string(),
            _path_output(),
            decode=_parse_path,
            encode=lambda value: str(value),
        )

    @staticmethod
    def bytes_base64(
        *,
        urlsafe: bool = False,
        message: str = "Invalid base64 string",
    ) -> CodecSchema[builtins.bytes, str]:
        if not isinstance(urlsafe, bool):
            raise TypeError("urlsafe must be a boolean")
        validated = _message(message, "Invalid base64 string")
        pattern = _BASE64URL_PATTERN if urlsafe else _BASE64_PATTERN
        format_name = "base64url" if urlsafe else "base64"
        return codec(
            string()._regex(pattern, validated, format_name),
            bytes(),
            decode=lambda value: _parse_base64(value, urlsafe, validated),
            encode=lambda value: _encode_base64(value, urlsafe),
        )

    @staticmethod
    def array[Out, In](
        codec: CodecSchema[Out, In],
    ) -> CodecSchema[list[Out], list[In]]:
        if not isinstance(codec, CodecSchema):
            raise TypeError("array expects a codec")
        result = _array_codec(codec)
        return cast(
            CodecSchema[list[Out], list[In]],
            replace(result, _meta=codec._meta),
        )

    @staticmethod
    def union(
        *options: Schema[Any, Any],
    ) -> CodecSchema[Any, Any]:
        if not all(isinstance(item, Schema) for item in options):
            raise TypeError("union expects schemas")
        return _union_codec(options)

    @staticmethod
    def tuple(
        *options: Schema[Any, Any],
    ) -> CodecSchema[Any, Any]:
        if not all(isinstance(item, Schema) for item in options):
            raise TypeError("tuple expects schemas")
        return _tuple_codec(options)

    @staticmethod
    def dataclass[DataclassT](
        cls: type[DataclassT],
        *,
        fields: Mapping[str, Schema[Any, Any]] | None = None,
        _stack: builtins.tuple[type[Any], ...] = (),
    ) -> CodecSchema[DataclassT, dict[str, Any]]:
        """Build an encoded-dictionary ↔ dataclass codec.

        Missing field schemas are inferred from type annotations. Explicit
        entries override inference. Plain schemas act as validators; codec field
        schemas supply the encoded and decoded forms. Fields with ``init=False``
        are ignored.
        """
        if not isinstance(cls, type) or not dataclasses.is_dataclass(cls):
            raise TypeError("dataclass codec expects a dataclass type")
        if cls in _stack:
            raise TypeError(
                "Recursive dataclass annotations are not supported",
            )
        stack = (*_stack, cls)
        if fields is not None and not isinstance(fields, Mapping):
            raise TypeError("Dataclass fields must be a mapping")
        init_fields = [item for item in dataclasses.fields(cls) if item.init]
        names = builtins.tuple(item.name for item in init_fields)
        expected = set(names)
        parameters = {
            name
            for name, parameter in inspect.signature(cls).parameters.items()
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }
        unsupported = parameters - expected
        if unsupported:
            raise TypeError(
                "Unsupported dataclass init parameters: "
                + ", ".join(sorted(unsupported))
            )
        missing_parameters = expected - parameters
        if missing_parameters:
            raise TypeError(
                "Unsupported dataclass init fields: "
                + ", ".join(sorted(missing_parameters))
            )

        overrides = {} if fields is None else dict(fields)
        provided = set(overrides)
        unknown_fields = provided - expected
        if unknown_fields:
            raise ValueError(
                "Unknown dataclass field schemas: "
                + ", ".join(sorted(map(repr, unknown_fields)))
            )
        missing = expected - provided
        hints: dict[str, Any] = {}
        if missing:
            try:
                hints = typing.get_type_hints(cls)
            except (NameError, TypeError) as error:
                raise TypeError(
                    "Cannot resolve dataclass annotations",
                ) from error

        def infer(annotation: Any, name: str) -> Schema[Any, Any]:
            if annotation is Any or annotation is builtins.object:
                return unknown()
            if annotation is None or annotation is type(None):
                return none()
            origin = typing.get_origin(annotation)
            args = typing.get_args(annotation)
            if origin is typing.Annotated:
                return infer(args[0], name)
            if origin is typing.Literal:
                choices = builtins.tuple(args)
                return (
                    literal(choices[0]) if len(choices) == 1 else enum(choices)
                )
            if origin in (types.UnionType, typing.Union):
                non_none = [
                    item for item in args if item is not type(None)
                ]
                options = [infer(item, name) for item in non_none]
                if not options:
                    return none()
                if len(options) == 1:
                    combined: Schema[Any, Any] = options[0]
                elif any(isinstance(item, CodecSchema) for item in options):
                    combined = _union_codec(options)
                else:
                    combined = union(*options)
                if len(non_none) != len(args):
                    combined = (
                        _nullable_codec(combined)
                        if isinstance(combined, CodecSchema)
                        else combined.nullable()
                    )
                return combined
            if origin is list or origin is Sequence:
                item = infer(args[0], name) if args else unknown()
                return (
                    _array_codec(item)
                    if isinstance(item, CodecSchema)
                    else array(item)
                )
            if origin is dict or origin is Mapping:
                if not args:
                    return record(unknown())
                key_type, value_type = args[0], args[1]
                if key_type is not str:
                    raise TypeError(
                        f"Field {name!r} must use string dictionary keys",
                    )
                value = infer(value_type, name)
                return (
                    _record_codec(value)
                    if isinstance(value, CodecSchema)
                    else record(value)
                )
            if origin is builtins.tuple:
                if annotation is builtins.tuple:
                    raise TypeError(
                        f"Field {name!r} uses a bare tuple annotation; "
                        "use fixed-length tuple[...] syntax",
                    )
                if args and args[-1] is Ellipsis:
                    raise TypeError(
                        f"Field {name!r} uses a variable-length tuple; "
                        "use a fixed-length tuple[...] annotation",
                    )
                if not args:
                    return tuple()
                items = [infer(item, name) for item in args]
                return (
                    _tuple_codec(items)
                    if any(isinstance(item, CodecSchema) for item in items)
                    else tuple(*items)
                )
            if annotation is list or annotation is Sequence:
                return array(unknown())
            if annotation is dict or annotation is Mapping:
                return record(unknown())
            if annotation is builtins.tuple:
                raise TypeError(
                    f"Field {name!r} uses a bare tuple annotation; "
                    "use fixed-length tuple[...] syntax",
                )
            if not isinstance(annotation, type):
                supertype = getattr(annotation, "__supertype__", None)
                if supertype is not None:
                    return infer(supertype, name)
                raise TypeError(
                    f"Cannot infer a schema for field {name!r}: "
                    f"{annotation!r}",
                )
            if dataclasses.is_dataclass(annotation):
                return _Codecs.dataclass(annotation, _stack=stack)
            if issubclass(annotation, Enum):
                return native_enum(annotation)
            if issubclass(annotation, bool):
                return boolean()
            if issubclass(annotation, int):
                return integer()
            if issubclass(annotation, float):
                return number()
            if issubclass(annotation, str):
                return string()
            if issubclass(annotation, builtins.bytes):
                return bytes()
            if issubclass(annotation, _datetime.datetime):
                return datetime()
            if issubclass(annotation, _datetime.date):
                return date()
            if issubclass(annotation, _datetime.time):
                return time()
            if issubclass(annotation, _datetime.timedelta):
                return timedelta()
            if issubclass(annotation, _decimal.Decimal):
                return decimal()
            if issubclass(annotation, _pathlib.PurePath):
                return path()
            raise TypeError(
                f"Cannot infer a schema for field {name!r}: {annotation!r}",
            )

        schemas_by_name: dict[str, Schema[Any, Any]] = {}
        for name in names:
            if name in overrides:
                schema = overrides[name]
            else:
                annotation = hints.get(name, _UNSET)
                if annotation is _UNSET:
                    raise TypeError(
                        f"Cannot infer a schema for field {name!r}",
                    )
                schema = infer(annotation, name)
            if not isinstance(schema, Schema):
                raise TypeError(f"Field {name!r} must be a schema")
            schemas_by_name[name] = schema

        fields_by_name = {item.name: item for item in init_fields}
        has_default = {
            name: (
                fields_by_name[name].default is not dataclasses.MISSING
                or fields_by_name[name].default_factory is not dataclasses.MISSING
            )
            for name in names
        }
        def input_field_schema(name: str) -> Schema[Any, Any]:
            schema = schemas_by_name[name]
            base = (
                schema.input_schema
                if isinstance(schema, CodecSchema)
                else schema
            )
            if has_default[name]:
                return base.nonoptional().optional()
            if base._node.kind == "optional":
                raise ValueError(
                    f"Field {name!r} accepts missing input but the dataclass "
                    "field has no default"
                )
            return base

        encoded_shape = {
            name: input_field_schema(name) for name in names
        }
        input_schema = object(encoded_shape)
        output_schema: Schema[DataclassT, Any] = instance_of(cls)
        output_shape = {}
        for name, schema in schemas_by_name.items():
            field_output = _json_output_schema_for(schema)
            output_shape[name] = (
                field_output.nonoptional()
                if has_default[name]
                else field_output
            )
        output_json_schema = object(output_shape)

        def decode_value(data: dict[str, Any]) -> DataclassT:
            decoded: dict[str, Any] = {}
            issues: list[Issue] = []
            for name in names:
                schema = schemas_by_name[name]
                value = data.get(name, MISSING)
                if value is MISSING:
                    if not has_default[name]:
                        issues.append(Issue("required", "Required", (name,)))
                    continue
                if isinstance(schema, CodecSchema):
                    try:
                        value = schema.decode(value)
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                decoded[name] = value
            if issues:
                raise ValidationError(issues)
            return cls(**decoded)

        def encode_value(value: DataclassT) -> dict[str, Any]:
            encoded: dict[str, Any] = {}
            issues: list[Issue] = []
            for name in names:
                schema = schemas_by_name[name]
                field_value = getattr(value, name)
                if isinstance(schema, CodecSchema):
                    try:
                        field_value = schema.encode(field_value)
                    except ValidationError as error:
                        issues.extend(
                            _prefix_issue(item, name) for item in error.issues
                        )
                        continue
                encoded[name] = field_value
            if issues:
                raise ValidationError(issues)
            return encoded

        return _compound_codec(
            input_schema,
            output_schema,
            output_json_schema,
            decode=decode_value,
            encode=encode_value,
        )


codecs = _Codecs()


_CONSTRAINT_KINDS = frozenset({
    "length_min", "length_max", "min", "max", "exclusive_min", "exclusive_max",
    "multiple_of", "positive", "integral", "regex", "starts_with", "ends_with",
    "includes", "ipv6",
})

_IDENTITY_ENCODE_KINDS = frozenset({
    "string", "number", "integer", "boolean", "unknown", "none",
    "exact_instance", "json", "native_enum", "literal", "enum", "instance",
    "length_min", "length_max", "min", "max", "exclusive_min", "exclusive_max",
    "multiple_of", "positive", "integral", "regex", "starts_with", "ends_with",
    "includes", "ipv6", "trim", "lower", "upper",
})


def _array_encode_node(
    schema: ArraySchema[Any, Any], item: NativeSchema,
) -> NativeSchema:
    constraints: list[builtins.tuple[str, builtins.tuple[Any, ...]]] = []
    node = schema._node
    while node.kind in _CONSTRAINT_KINDS:
        constraints.append((node.kind, node.args))
        node = node.children[0]
    result = NativeSchema("array", (item,), ())
    for kind, args in reversed(constraints):
        result = NativeSchema(kind, (result,), args)
    return result


def _encode_node_for(schema: Schema[Any, Any]) -> NativeSchema | None:
    if isinstance(schema, CodecSchema):
        return schema._encode_node
    if isinstance(schema, ArraySchema):
        item_node = _encode_node_for(schema.item)
        if item_node is None:
            return None
        return _array_encode_node(schema, item_node)
    if isinstance(schema, ObjectSchema):
        property_nodes: list[NativeSchema] = []
        for prop in schema.shape.values():
            prop_node = _encode_node_for(prop)
            if prop_node is None:
                return None
            property_nodes.append(prop_node)
        catchall_node = None
        if schema._catchall is not None:
            catchall_node = _encode_node_for(schema._catchall)
            if catchall_node is None:
                return None
        return NativeSchema(
            "object",
            builtins.tuple(property_nodes),
            (builtins.tuple(schema.shape), schema.unknown_keys, catchall_node),
        )
    if isinstance(schema, KeyedRecordSchema):
        key_node = _encode_node_for(schema.key_schema)
        value_node = _encode_node_for(schema.value_schema)
        if key_node is None or value_node is None:
            return None
        return NativeSchema("record", (value_node,), (key_node,))
    if isinstance(schema, RecordSchema):
        value_node = _encode_node_for(schema.value_schema)
        if value_node is None:
            return None
        return NativeSchema("record", (value_node,), (None,))
    if isinstance(schema, TupleSchema):
        tuple_nodes: list[NativeSchema] = []
        for item_schema in schema.items:
            item_node = _encode_node_for(item_schema)
            if item_node is None:
                return None
            tuple_nodes.append(item_node)
        rest_node = None
        if schema._rest is not None:
            rest_node = _encode_node_for(schema._rest)
            if rest_node is None:
                return None
        return NativeSchema("tuple", builtins.tuple(tuple_nodes), (rest_node,))
    if isinstance(schema, UnionSchema):
        option_nodes: list[NativeSchema] = []
        for option in schema.options:
            option_node = _encode_node_for(option)
            if option_node is None:
                return None
            option_nodes.append(option_node)
        return NativeSchema("union", builtins.tuple(option_nodes), ())
    if isinstance(schema, DiscriminatedUnionSchema):
        entries = list(schema.options.items())
        branch_nodes: list[NativeSchema] = []
        for _, option in entries:
            branch_node = _encode_node_for(option)
            if branch_node is None:
                return None
            branch_nodes.append(branch_node)
        return NativeSchema(
            "discriminated_union",
            builtins.tuple(branch_nodes),
            (schema.key, builtins.tuple(label for label, _ in entries)),
        )
    if isinstance(schema, IntersectionSchema):
        left_node = _encode_node_for(schema.left)
        right_node = _encode_node_for(schema.right)
        if left_node is None or right_node is None:
            return None
        return NativeSchema("intersection", (left_node, right_node), ())

    node = schema._node
    kind = node.kind
    if kind in ("optional", "nullable", "nonoptional"):
        if schema._inner is None:
            return None
        wrapped_node = _encode_node_for(schema._inner)
        if wrapped_node is None:
            return None
        return NativeSchema(kind, (wrapped_node,), ())
    if kind in _IDENTITY_ENCODE_KINDS:
        return node
    return None


def to_json_schema(
    schema: Schema[Any, Any],
    *,
    target: JSONSchemaTarget = "draft-2020-12",
    io: JSONSchemaIO = "output",
    unrepresentable: UnrepresentablePolicy = "throw",
) -> JSONSchema:
    """Export a schema as a JSON Schema mapping.

    Refinements, transforms, preprocessors, pipes, factories, and lazy schemas
    are not representable. ``unrepresentable="any"`` emits ``{}`` for them.
    """
    if not isinstance(schema, Schema):
        raise TypeError("Expected a Schema")
    if target not in ("draft-2020-12", "openapi-3.0"):
        raise ValueError("Unknown JSON Schema target")
    if io not in ("input", "output"):
        raise ValueError("io must be 'input' or 'output'")
    if unrepresentable not in ("throw", "any"):
        raise ValueError("unrepresentable must be 'throw' or 'any'")
    return _export_schema(schema, (), target, io, unrepresentable, False)


def _unsupported(
    path: Path, reason: str, unrepresentable: UnrepresentablePolicy,
) -> JSONSchema:
    if unrepresentable == "any":
        return {}
    raise UnrepresentableError(path, reason)


def _inner_schema(schema: Schema[Any, Any], node: NativeSchema) -> Schema[Any, Any]:
    if schema._inner is not None:
        return schema._inner
    return Schema[Any, Any](node.children[0])


def _node_accepts_missing(node: NativeSchema, io: JSONSchemaIO) -> bool:
    kind = node.kind
    if kind == "optional":
        return True
    if kind == "nonoptional":
        return False
    if kind == "catch":
        if _node_accepts_missing(node.children[0], io):
            return True
        return io == "input"
    if kind in ("default", "default_factory"):
        return io == "input"
    if kind in _CONSTRAINT_KINDS or kind in ("nullable", "coerce", "string_bool"):
        return _node_accepts_missing(node.children[0], io)
    if kind == "union":
        return any(_node_accepts_missing(child, io) for child in node.children)
    if kind in ("pipe", "refine", "super_refine", "transform", "overwrite"):
        return _node_accepts_missing(node.children[0], io)
    return False


def _schema_accepts_missing(schema: Schema[Any, Any], io: JSONSchemaIO) -> bool:
    if isinstance(schema, _CompoundCodecSchema):
        source = (
            schema.input_schema
            if io == "input"
            else schema._json_output_schema
        )
        return _node_accepts_missing(source._node, io)
    if isinstance(schema, CodecSchema):
        source = schema.input_schema if io == "input" else schema.output_schema
        return _node_accepts_missing(source._node, io)
    return _node_accepts_missing(schema._node, io)


def _node_outputs_string(node: NativeSchema) -> bool:
    kind = node.kind
    if kind == "string":
        return True
    if kind == "literal":
        return isinstance(node.args[0], str)
    if kind == "enum":
        return all(isinstance(value, str) for value in node.args[0])
    if kind in _CONSTRAINT_KINDS or kind == "coerce":
        return _node_outputs_string(node.children[0])
    if kind == "union":
        return all(_node_outputs_string(child) for child in node.children)
    if kind in ("optional", "nonoptional", "default", "default_factory"):
        return _node_outputs_string(node.children[0])
    return False


def _node_outputs_dict(node: NativeSchema) -> bool:
    kind = node.kind
    if kind in ("object", "record", "discriminated_union"):
        return True
    if kind == "intersection":
        return all(_node_outputs_dict(child) for child in node.children)
    if kind == "union":
        return all(_node_outputs_dict(child) for child in node.children)
    if kind in ("optional", "nonoptional", "default", "default_factory", "coerce"):
        return _node_outputs_dict(node.children[0])
    return False


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite float")
        return value
    if isinstance(value, (list, builtins.tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Non-string mapping key")
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("Unsupported value")


def _export_schema(
    schema: Schema[Any, Any],
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    result = _export_node(
        schema, path, target, io, unrepresentable, relax_strip,
    )
    if schema.metadata:
        metadata = dict(schema.metadata)
        if target == "openapi-3.0" and "examples" in metadata:
            examples = metadata.pop("examples")
            if "example" not in metadata:
                if isinstance(examples, list):
                    if examples:
                        metadata["example"] = examples[0]
                else:
                    metadata["example"] = examples
        result = {**result, **metadata}
    return result


def _export_node(
    schema: Schema[Any, Any],
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if isinstance(schema, _JsonTextSchema):
        return {"type": "string"}
    if isinstance(schema, _CompoundCodecSchema):
        source = (
            schema.input_schema
            if io == "input"
            else schema._json_output_schema
        )
        return _export_schema(
            source, path, target, io, unrepresentable, relax_strip,
        )
    if isinstance(schema, CodecSchema):
        source = schema.input_schema if io == "input" else schema.output_schema
        return _export_schema(
            source, path, target, io, unrepresentable, relax_strip,
        )
    node = schema._node
    if node.kind in (
        "optional", "nonoptional", "nullable", "default", "default_factory",
    ):
        return _export_wrapper(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    constraints: list[NativeSchema] = []
    while True:
        if node.kind in _CONSTRAINT_KINDS:
            constraints.append(node)
            node = node.children[0]
            continue
        if node.kind == "coerce":
            node = node.children[0]
            continue
        break
    result = _export_base(
        schema, node, path, target, io, unrepresentable, relax_strip,
    )
    return _apply_constraints(
        result, constraints, node.kind, target, path, unrepresentable,
    )


def _export_wrapper(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    kind = node.kind
    child = _inner_schema(schema, node)
    if kind == "optional":
        return _export_schema(
            child, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "nonoptional":
        result = dict(
            _export_schema(
                child, path, target, io, unrepresentable, relax_strip,
            ),
        )
        result.pop("default", None)
        return result
    if kind == "nullable":
        exported = _export_schema(
            child, path, target, io, unrepresentable, relax_strip,
        )
        if target == "openapi-3.0":
            return {**exported, "nullable": True}
        return {"anyOf": [exported, {"type": "null"}]}
    if kind == "default_factory":
        return _unsupported(
            path, "default_factory is not representable", unrepresentable,
        )
    try:
        default = _json_value(node.args[0])
    except ValueError:
        return _unsupported(
            path, "default value is not JSON-compatible", unrepresentable,
        )
    result = dict(
        _export_schema(
            child, path, target, io, unrepresentable, relax_strip,
        ),
    )
    result["default"] = default
    return result


def _export_base(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    kind = node.kind
    if kind == "string":
        return {"type": "string"}
    if kind == "number":
        return {"type": "number"}
    if kind == "integer":
        return {"type": "integer"}
    if kind == "boolean":
        return {"type": "boolean"}
    if kind == "unknown":
        return {}
    if kind == "json":
        return {}
    if kind == "string_bool":
        if io == "input":
            truthy = list(node.args[0])
            falsy = list(node.args[1])
            return {"type": "string", "enum": truthy + falsy}
        return {"type": "boolean"}
    if kind == "exact_instance":
        return _unsupported(
            path,
            "Python-specific instance schemas are not representable",
            unrepresentable,
        )
    if kind == "native_enum":
        cls = node.args[0]
        try:
            values = [_json_value(member.value) for member in cls]
        except (TypeError, ValueError):
            return _unsupported(
                path,
                "native enum values are not JSON-compatible",
                unrepresentable,
            )
        if target == "openapi-3.0" and any(value is None for value in values):
            return _unsupported(
                path,
                "null enum values are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        return {"enum": values}
    if kind == "none":
        if target == "openapi-3.0":
            return _unsupported(
                path,
                "null-only schemas are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        return {"type": "null"}
    if kind == "literal":
        value = node.args[0]
        if target == "openapi-3.0":
            if value is None:
                return _unsupported(
                    path,
                    "null literals are not representable in OpenAPI 3.0",
                    unrepresentable,
                )
            return {"enum": [value]}
        return {"const": value}
    if kind == "enum":
        values = list(node.args[0])
        if target == "openapi-3.0" and any(value is None for value in values):
            return _unsupported(
                path,
                "null enum values are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        return {"enum": values}
    if kind == "array":
        item = (
            schema.item
            if isinstance(schema, ArraySchema)
            else Schema[Any, Any](node.children[0])
        )
        return {
            "type": "array",
            "items": _export_schema(
                item,
                (*path, "items"),
                target,
                io,
                unrepresentable,
                relax_strip,
            ),
        }
    if kind == "object":
        return _export_object(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "record":
        if isinstance(schema, KeyedRecordSchema):
            key_schema: Schema[Any, Any] | None = schema.key_schema
            value_schema = schema.value_schema
        elif isinstance(schema, RecordSchema):
            key_schema = None
            value_schema = schema.value_schema
        else:
            key_node = node.args[0]
            key_schema = (
                None if key_node is None else Schema[Any, Any](key_node)
            )
            value_schema = Schema[Any, Any](node.children[0])
        value_json = _export_schema(
            value_schema,
            (*path, "additionalProperties"),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
        if key_schema is None:
            return {"type": "object", "additionalProperties": value_json}
        if target == "openapi-3.0":
            return _unsupported(
                path,
                "keyed records are not representable in OpenAPI 3.0",
                unrepresentable,
            )
        if not _node_outputs_string(key_schema._node):
            return _unsupported(
                path,
                "record keys must produce strings for JSON Schema",
                unrepresentable,
            )
        return {
            "type": "object",
            "propertyNames": _export_schema(
                key_schema,
                (*path, "propertyNames"),
                target,
                io,
                unrepresentable,
                relax_strip,
            ),
            "additionalProperties": value_json,
        }
    if kind == "tuple":
        return _export_tuple(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "intersection":
        if isinstance(schema, IntersectionSchema):
            left, right = schema.left, schema.right
        else:
            left = Schema[Any, Any](node.children[0])
            right = Schema[Any, Any](node.children[1])
        if not _node_outputs_dict(left._node) or not _node_outputs_dict(
            right._node,
        ):
            return _unsupported(
                path,
                "intersection outputs must be dictionaries",
                unrepresentable,
            )
        return {
            "allOf": [
                _export_schema(
                    left,
                    (*path, "allOf", 0),
                    target,
                    io,
                    unrepresentable,
                    True,
                ),
                _export_schema(
                    right,
                    (*path, "allOf", 1),
                    target,
                    io,
                    unrepresentable,
                    True,
                ),
            ],
        }
    if kind == "union":
        return _export_union(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    if kind == "discriminated_union":
        return _export_discriminated_union(
            schema, node, path, target, io, unrepresentable, relax_strip,
        )
    return _unsupported(
        path, f"{kind} schemas are not representable", unrepresentable,
    )


def _export_object(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if isinstance(schema, ObjectSchema):
        shape = schema.shape
        policy = schema.unknown_keys
        catchall = schema._catchall
    else:
        shape = {
            key: Schema[Any, Any](child)
            for key, child in zip(node.args[0], node.children, strict=True)
        }
        policy = node.args[1]
        catchall = (
            None if node.args[2] is None else Schema[Any, Any](node.args[2])
        )
    properties = {
        key: _export_schema(
            prop,
            (*path, "properties", key),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
        for key, prop in shape.items()
    }
    result: JSONSchema = {"type": "object", "properties": properties}
    required = [
        key for key, prop in shape.items()
        if not _schema_accepts_missing(prop, io)
    ]
    if required:
        result["required"] = required
    if catchall is not None:
        result["additionalProperties"] = _export_schema(
            catchall,
            (*path, "additionalProperties"),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
    elif policy == "passthrough":
        result["additionalProperties"] = True
    elif policy == "strict" or (io == "output" and not relax_strip):
        result["additionalProperties"] = False
    return result


def _export_tuple(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if target == "openapi-3.0":
        return _unsupported(
            path,
            "tuples are not representable in OpenAPI 3.0",
            unrepresentable,
        )
    if isinstance(schema, TupleSchema):
        items = schema.items
        rest = schema._rest
    else:
        items = builtins.tuple(
            Schema[Any, Any](child) for child in node.children
        )
        rest_node = node.args[0]
        rest = None if rest_node is None else Schema[Any, Any](rest_node)
    prefix_items = [
        _export_schema(
            item,
            (*path, "prefixItems", index),
            target,
            io,
            unrepresentable,
            relax_strip,
        )
        for index, item in enumerate(items)
    ]
    if rest is not None:
        return {
            "type": "array",
            "prefixItems": prefix_items,
            "items": _export_schema(
                rest,
                (*path, "items"),
                target,
                io,
                unrepresentable,
                relax_strip,
            ),
            "minItems": len(items),
        }
    return {
        "type": "array",
        "prefixItems": prefix_items,
        "items": False,
        "minItems": len(items),
        "maxItems": len(items),
    }


def _export_union(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    options = (
        schema.options
        if isinstance(schema, UnionSchema)
        else builtins.tuple(Schema[Any, Any](child) for child in node.children)
    )
    return {
        "anyOf": [
            _export_schema(
                option,
                (*path, "anyOf", index),
                target,
                io,
                unrepresentable,
                relax_strip,
            )
            for index, option in enumerate(options)
        ],
    }


def _export_discriminated_union(
    schema: Schema[Any, Any],
    node: NativeSchema,
    path: Path,
    target: JSONSchemaTarget,
    io: JSONSchemaIO,
    unrepresentable: UnrepresentablePolicy,
    relax_strip: bool,
) -> JSONSchema:
    if isinstance(schema, DiscriminatedUnionSchema):
        key = schema.key
        options = list(schema.options.items())
    else:
        key = node.args[0]
        options = [
            (label, Schema[Any, Any](child))
            for label, child in zip(node.args[1], node.children, strict=True)
        ]
    result: JSONSchema = {
        "oneOf": [
            _export_schema(
                option,
                (*path, "oneOf", label),
                target,
                io,
                unrepresentable,
                relax_strip,
            )
            for label, option in options
        ],
    }
    if target == "openapi-3.0":
        result["discriminator"] = {"propertyName": key}
    return result


def _apply_constraints(
    result: JSONSchema,
    constraints: list[NativeSchema],
    base_kind: str,
    target: JSONSchemaTarget,
    path: Path,
    unrepresentable: UnrepresentablePolicy,
) -> JSONSchema:
    lower: builtins.tuple[int | float, bool] | None = None
    upper: builtins.tuple[int | float, bool] | None = None
    multiple_values: list[int | float] = []
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    integral = False
    regexes: list[NativeSchema] = []
    patterns: list[str] = []
    format_name: str | None = None
    extra_checks: list[JSONSchema] = []

    def stricter_lower(bound: int | float, inclusive: bool) -> None:
        nonlocal lower
        if (
            lower is None
            or bound > lower[0]
            or (bound == lower[0] and not inclusive)
        ):
            lower = (bound, inclusive)

    def stricter_upper(bound: int | float, inclusive: bool) -> None:
        nonlocal upper
        if (
            upper is None
            or bound < upper[0]
            or (bound == upper[0] and not inclusive)
        ):
            upper = (bound, inclusive)

    for node in constraints:
        kind = node.kind
        if kind in ("min", "max", "exclusive_min", "exclusive_max"):
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            bound = node.args[0]
            inclusive = kind in ("min", "max")
            if kind in ("min", "exclusive_min"):
                stricter_lower(bound, inclusive)
            else:
                stricter_upper(bound, inclusive)
        elif kind == "positive":
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            stricter_lower(0, False)
        elif kind == "multiple_of":
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            multiple_values.append(node.args[0])
        elif kind in ("length_min", "length_max"):
            if base_kind not in ("string", "array"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            bound = node.args[0]
            if kind == "length_min":
                if base_kind == "string":
                    min_length = bound if min_length is None else max(min_length, bound)
                else:
                    min_items = bound if min_items is None else max(min_items, bound)
            elif base_kind == "string":
                max_length = bound if max_length is None else min(max_length, bound)
            else:
                max_items = bound if max_items is None else min(max_items, bound)
        elif kind == "integral":
            if base_kind not in ("number", "integer"):
                return _unsupported(
                    path, f"{kind} is not representable for {base_kind}",
                    unrepresentable,
                )
            integral = True
        elif kind == "regex":
            if base_kind != "string":
                return _unsupported(
                    path, "regex is only representable for strings",
                    unrepresentable,
                )
            regexes.append(node)
        elif kind in ("starts_with", "ends_with", "includes"):
            if base_kind != "string":
                return _unsupported(
                    path, f"{kind} is only representable for strings",
                    unrepresentable,
                )
            needle = node.args[0]
            if not isinstance(needle, str):
                return _unsupported(
                    path, f"{kind} is not representable", unrepresentable,
                )
            escaped = re.escape(needle)
            if kind == "starts_with":
                patterns.append("^" + escaped)
            elif kind == "ends_with":
                patterns.append(escaped + "$")
            else:
                patterns.append(escaped)
        elif kind == "ipv6":
            if base_kind != "string":
                return _unsupported(
                    path, "ipv6 is only representable for strings",
                    unrepresentable,
                )
            if format_name not in (None, "ipv6"):
                return _unsupported(
                    path, "multiple formats are not representable",
                    unrepresentable,
                )
            format_name = "ipv6"
        else:
            return _unsupported(
                path, f"{kind} is not representable", unrepresentable,
            )

    if len(regexes) > 1:
        return _unsupported(
            path, "multiple regex constraints are not representable",
            unrepresentable,
        )
    if regexes:
        regex_node = regexes[0]
        compiled = regex_node.args[0]
        regex_format = regex_node.args[2]
        if not isinstance(compiled, re.Pattern):
            return _unsupported(
                path, "regex is not representable", unrepresentable,
            )
        if regex_format in _KNOWN_FORMATS:
            if format_name not in (None, regex_format):
                return _unsupported(
                    path, "multiple formats are not representable",
                    unrepresentable,
                )
            format_name = regex_format
        elif regex_format in _PATTERN_FORMATS:
            if not isinstance(compiled.pattern, str):
                return _unsupported(
                    path, "byte regexes are not representable",
                    unrepresentable,
                )
            if compiled.flags & ~re.UNICODE:
                return _unsupported(
                    path, "regex flags are not representable",
                    unrepresentable,
                )
            patterns.append(compiled.pattern)
        else:
            return _unsupported(
                path, "regex format is not representable", unrepresentable,
            )

    if base_kind in ("number", "integer"):
        if integral:
            result["type"] = "integer"
        if lower is not None:
            bound, inclusive = lower
            if target == "openapi-3.0":
                result["minimum"] = bound
                if not inclusive:
                    result["exclusiveMinimum"] = True
            elif inclusive:
                result["minimum"] = bound
            else:
                result["exclusiveMinimum"] = bound
        if upper is not None:
            bound, inclusive = upper
            if target == "openapi-3.0":
                result["maximum"] = bound
                if not inclusive:
                    result["exclusiveMaximum"] = True
            elif inclusive:
                result["maximum"] = bound
            else:
                result["exclusiveMaximum"] = bound
        if len(multiple_values) == 1:
            result["multipleOf"] = multiple_values[0]
        elif multiple_values:
            extra_checks.extend({"multipleOf": value} for value in multiple_values)
    elif base_kind == "string":
        if min_length is not None:
            result["minLength"] = min_length
        if max_length is not None:
            result["maxLength"] = max_length
        if format_name is not None:
            result["format"] = format_name
        if len(patterns) == 1:
            result["pattern"] = patterns[0]
        elif patterns:
            extra_checks.extend({"pattern": pattern} for pattern in patterns)
    elif base_kind == "array":
        if min_items is not None:
            result["minItems"] = min_items
        if max_items is not None:
            result["maxItems"] = max_items
    elif constraints:
        return _unsupported(
            path, f"constraints on {base_kind} are not representable",
            unrepresentable,
        )

    if extra_checks:
        existing = result.get("allOf")
        if isinstance(existing, list):
            result["allOf"] = [*existing, *extra_checks]
        else:
            result["allOf"] = extra_checks
    return result
