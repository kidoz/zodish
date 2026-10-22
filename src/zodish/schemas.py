"""Typed Python schema builders backed by the native Rust evaluator."""

from __future__ import annotations

import base64
import binascii
import builtins
import copy as _copy
import datetime as _datetime
import decimal as _decimal
import inspect
import json as _json
import math
import pathlib as _pathlib
import re
import typing
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import wraps
from types import MappingProxyType
from typing import Any, Literal, Self, cast, overload

from ._async import drive
from ._native import AsyncSession, NativeSchema


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
_HTTP_URL_PATTERN = r"^[Hh][Tt][Tt][Pp][Ss]?://[^\s]+$"
_HEX_PATTERN = r"^[0-9a-fA-F]+$"
_DURATION_PATTERN = (
    r"^[+-]?P(?:\d+W|(?=\d|T)(?:\d+D)?"
    r"(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?)$"
)
_DURATION_PARTS = re.compile(
    r"^[+-]?P(?:(\d+)W|(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?)$"
)
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
    r"^(?:[A-Za-z0-9_-]{4})*"
    r"(?:[A-Za-z0-9_-]{2}==|[A-Za-z0-9_-]{3}=|[A-Za-z0-9_-]{2,3})?$"
)
_HOSTNAME_PATTERN = (
    r"^(?=.{1,253}$)"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(?:\.(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?))*$"
)
_ULID_PATTERN = r"(?i)^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_CUID_PATTERN = r"(?i)^c[^\s-]{8,}$"
_CUID2_PATTERN = r"^[a-z][a-z0-9]{1,31}$"
_NANOID_PATTERN = r"^[A-Za-z0-9_-]{21}$"
_JWT_PATTERN = r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"
_E164_PATTERN = r"^\+[1-9][0-9]{1,14}$"
_KNOWN_FORMATS = frozenset({
    "email", "uri", "uuid", "date", "time", "date-time", "ipv4", "ipv6",
    "hostname", "ulid", "cuid", "cuid2", "nanoid", "jwt", "e164",
})
_PATTERN_FORMATS = frozenset({
    "regex", "decimal", "base64", "base64url", "duration", "http_url", "hex",
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
        builtins.object.__setattr__(self, "path", builtins.tuple(self.path))
        builtins.object.__setattr__(
            self,
            "branches",
            builtins.tuple(
                builtins.tuple(branch) for branch in self.branches
            ),
        )
        builtins.object.__setattr__(
            self, "params", MappingProxyType(dict(self.params)),
        )

    def __hash__(self) -> int:
        return hash((self.code, self.message, self.path, self.branches))

    def __reduce__(
        self,
    ) -> builtins.tuple[type[Issue], builtins.tuple[Any, ...]]:
        return (
            type(self),
            (
                self.code,
                self.message,
                self.path,
                self.branches,
                dict(self.params),
            ),
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
        location = ".".join(builtins.map(str, path)) or "<root>"
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


type ErrorMap = Callable[[Issue], str]


def _check_error_map(error: ErrorMap | None) -> None:
    if error is not None and not callable(error):
        raise TypeError("error must be callable or None")


def _map_issue(issue: Issue, mapper: ErrorMap) -> Issue:
    message = mapper(issue)
    if not isinstance(message, str):
        raise TypeError("Error mapper must return a string")
    branches = builtins.tuple(
        builtins.tuple(_map_issue(branch, mapper) for branch in items)
        for items in issue.branches
    )
    return replace(issue, message=message, branches=branches)


_VALIDATION_ERROR_MESSAGE = "Validation failed"


class ValidationError(ValueError):
    _issues: builtins.tuple[Issue, ...] | None
    _raw_issues: builtins.tuple[builtins.tuple[Any, ...], ...] | None
    _bundle: Any = None
    _message: str | None = None
    _error_mapper: ErrorMap | None = None
    _mapped: bool = False

    def __init__(self, issues: Sequence[Issue]):
        self._issues = builtins.tuple(issues)
        self._raw_issues = None
        self._bundle = None
        self._error_mapper = None
        self._mapped = False
        super().__init__(_VALIDATION_ERROR_MESSAGE)

    def __reduce__(
        self,
    ) -> builtins.tuple[type[ValidationError], builtins.tuple[builtins.tuple[Issue, ...]]]:
        return (type(self), (self.issues,))

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
        error._error_mapper = None
        error._mapped = False
        ValueError.__init__(error, _VALIDATION_ERROR_MESSAGE)
        return error

    @classmethod
    def _from_bundle(cls, bundle: Any) -> Self:
        """Hold a GC-aware native bundle until issues are needed."""
        error = ValueError.__new__(cls)
        error._issues = None
        error._raw_issues = None
        error._bundle = bundle
        error._error_mapper = None
        error._mapped = False
        ValueError.__init__(error, _VALIDATION_ERROR_MESSAGE)
        return error

    def _set_error_mapper(self, mapper: ErrorMap | None) -> None:
        """Attach the per-parse mapper used when issues are materialized."""
        if mapper is None:
            return
        self._error_mapper = mapper
        self._message = None

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
            issues = builtins.tuple(builtins.map(_issue_from_raw, raw_issues))
            self._issues = issues
            self._raw_issues = None
        mapper = self._error_mapper
        if mapper is not None and not self._mapped:
            issues = builtins.tuple(
                _map_issue(issue, mapper) for issue in issues
            )
            self._issues = issues
            self._mapped = True
        return issues

    def __str__(self) -> str:
        cached = self._message
        if cached is None:
            if self._error_mapper is not None:
                cached = "; ".join(
                    f"{'.'.join(builtins.map(str, issue.path)) or '<root>'}: "
                    f"{issue.message}"
                    for issue in self.issues
                )
            else:
                raw_issues = self._raw_issues
                if raw_issues is None and self._bundle is not None:
                    raw_issues = self._bundle.to_raw()
                if raw_issues is None:
                    cached = "; ".join(
                        f"{'.'.join(builtins.map(str, issue.path)) or '<root>'}: "
                        f"{issue.message}"
                        for issue in self.issues
                    )
                else:
                    cached = "; ".join(
                        f"{'.'.join(builtins.map(str, raw[2])) or '<root>'}: {raw[1]}"
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
            f"{'.'.join(builtins.map(str, issue.path)) or '<root>'}: {issue.message}"
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


def _parse_iso_duration(value: str, message: str) -> _datetime.timedelta:
    if re.fullmatch(_DURATION_PATTERN, value) is None:
        raise _invalid_format(message, "duration")
    match = _DURATION_PARTS.fullmatch(value)
    if match is None:
        raise _invalid_format(message, "duration")
    weeks, days, hours, minutes, seconds = match.groups()
    sign = -1 if value.startswith("-") else 1
    try:
        if weeks is not None:
            return _datetime.timedelta(weeks=sign * int(weeks))
        return _datetime.timedelta(
            days=sign * int(days or 0),
            hours=sign * int(hours or 0),
            minutes=sign * int(minutes or 0),
            seconds=sign * float(seconds or 0),
        )
    except OverflowError:
        raise _invalid_format(message, "duration") from None


def _format_iso_duration(value: _datetime.timedelta) -> str:
    total = (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )
    sign = "-" if total < 0 else ""
    days, remainder = divmod(abs(total), 86_400_000_000)
    hours, remainder = divmod(remainder, 3_600_000_000)
    minutes, remainder = divmod(remainder, 60_000_000)
    seconds, microseconds = divmod(remainder, 1_000_000)
    time_part = ""
    if hours:
        time_part += f"{hours}H"
    if minutes:
        time_part += f"{minutes}M"
    if seconds or microseconds:
        if microseconds:
            fraction = f"{microseconds:06d}".rstrip("0")
            time_part += f"{seconds}.{fraction}S"
        else:
            time_part += f"{seconds}S"
    if not time_part:
        return f"{sign}P{days}D" if days else f"{sign}PT0S"
    date_part = f"P{days}D" if days else "P"
    return f"{sign}{date_part}T{time_part}"


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
                Out, self._node.parse(value, MISSING, Issue, ValidationError),
            )
        except ValidationError as failure:
            failure._set_error_mapper(error)
            raise

    def safe_parse(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors only; programming errors remain visible."""
        try:
            return Success(self.parse(value, error=error))
        except ValidationError as failure:
            return Failure(failure)

    async def parse_async(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Out:
        """Parse, awaiting async callback results when required."""
        _check_error_map(error)
        try:
            session = AsyncSession(
                self._node, value, MISSING, Issue, ValidationError,
            )
            return cast(Out, await drive(session))
        except ValidationError as failure:
            failure._set_error_mapper(error)
            raise

    async def safe_parse_async(
        self, value: In | Missing = MISSING, *, error: ErrorMap | None = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors from async parsing only."""
        try:
            return Success(await self.parse_async(value, error=error))
        except ValidationError as failure:
            return Failure(failure)

    def validate(self, value: In | Missing = MISSING) -> bool:
        """Return whether validation succeeds; callback errors still propagate."""
        return self.safe_parse(value).success

    async def validate_async(self, value: In | Missing = MISSING) -> bool:
        """Return whether async validation succeeds.

        Programming errors still propagate, matching ``validate``.
        """
        return (await self.safe_parse_async(value)).success

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
    ) -> Out:
        """Parse JSON text and validate the decoded value."""
        _check_error_map(error)
        try:
            fast, value = self._json_input(data)
        except ValidationError as failure:
            failure._set_error_mapper(error)
            raise
        return cast(Out, value) if fast else self.parse(value, error=error)

    async def parse_json_async(
        self,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
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
            else await self.parse_async(value, error=error)
        )

    async def safe_parse_json_async(
        self,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors from asynchronous JSON parsing only."""
        try:
            return Success(await self.parse_json_async(data, error=error))
        except ValidationError as failure:
            return Failure(failure)

    def safe_parse_json(
        self,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
    ) -> Success[Out] | Failure:
        """Catch validation errors from JSON parsing only."""
        try:
            return Success(self.parse_json(data, error=error))
        except ValidationError as failure:
            return Failure(failure)

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

    async def encode_async(self, value: Out) -> In:
        """Encode through the reverse graph, awaiting async callbacks."""
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
        return ArraySchema[Out, In](
            NativeSchema("array", (self._node,), ()),
            self,
        )

    def or_[U, OtherIn](
        self, other: Schema[U, OtherIn],
    ) -> UnionSchema[Out | U, In | OtherIn]:
        if not isinstance(other, Schema):
            raise TypeError("Union options must be schemas")
        return UnionSchema[Out | U, In | OtherIn](
            NativeSchema("union", (self._node, other._node), ()),
            (self, other),
        )

    def and_(self, other: Schema[Any, Any]) -> IntersectionSchema:
        return intersection(self, other)


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

    def http_url(self, *, message: str = "Invalid HTTP URL") -> Self:
        return self._regex(_HTTP_URL_PATTERN, message, "http_url")

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

    def cidrv4(self, *, message: str = "Invalid IPv4 CIDR") -> Self:
        return self._wrap("cidrv4", _message(message, "Invalid IPv4 CIDR"))

    def cidrv6(self, *, message: str = "Invalid IPv6 CIDR") -> Self:
        return self._wrap("cidrv6", _message(message, "Invalid IPv6 CIDR"))

    def hex(self, *, message: str = "Invalid hex string") -> Self:
        return self._regex(_HEX_PATTERN, message, "hex")

    def duration(self, *, message: str = "Invalid duration") -> Self:
        return self._regex(_DURATION_PATTERN, message, "duration")

    def hostname(self, *, message: str = "Invalid hostname") -> Self:
        return self._regex(_HOSTNAME_PATTERN, message, "hostname")

    def ulid(self, *, message: str = "Invalid ULID") -> Self:
        return self._regex(_ULID_PATTERN, message, "ulid")

    def cuid(self, *, message: str = "Invalid CUID") -> Self:
        return self._regex(_CUID_PATTERN, message, "cuid")

    def cuid2(self, *, message: str = "Invalid CUID2") -> Self:
        return self._regex(_CUID2_PATTERN, message, "cuid2")

    def nanoid(self, *, message: str = "Invalid Nano ID") -> Self:
        return self._regex(_NANOID_PATTERN, message, "nanoid")

    def jwt(self, *, message: str = "Invalid JWT") -> Self:
        return self._regex(_JWT_PATTERN, message, "jwt")

    def e164(self, *, message: str = "Invalid E.164 number") -> Self:
        return self._regex(_E164_PATTERN, message, "e164")

    def base64(self, *, message: str = "Invalid base64") -> Self:
        return self._regex(_BASE64_PATTERN, message, "base64")

    def base64url(self, *, message: str = "Invalid base64url") -> Self:
        return self._regex(_BASE64URL_PATTERN, message, "base64url")

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
            overlap = builtins.set(truthy_values) & builtins.set(falsy_values)
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


@dataclass(frozen=True)
class SetSchema[Out, In = Out](
    Schema[builtins.set[Out], builtins.set[In]],
):
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


@dataclass(frozen=True)
class BytesSchema(Schema[builtins.bytes, builtins.bytes]):
    def min(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self._wrap(
            "length_min",
            length,
            _message(
                message,
                f"Must contain at least {_bound_text(length)} bytes",
            ),
        )

    def max(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self._wrap(
            "length_max",
            length,
            _message(
                message,
                f"Must contain at most {_bound_text(length)} bytes",
            ),
        )

    def length(self, length: int, *, message: str | None = None) -> Self:
        _length_bound(length)
        return self.min(length, message=message).max(length, message=message)


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


def bytes() -> BytesSchema:
    return BytesSchema(
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
    if not isinstance(item, Schema):
        raise TypeError("Array item must be a schema")
    return ArraySchema[Out, In](
        NativeSchema("array", (item._node,), ()),
        item,
    )


def set[Out, In](item: Schema[Out, In]) -> SetSchema[Out, In]:
    if not isinstance(item, Schema):
        raise TypeError("Set item must be a schema")
    return SetSchema[Out, In](
        NativeSchema("set", (item._node,), ()),
        item,
    )


type UnknownKeys = Literal["strip", "strict", "passthrough"]


@dataclass(frozen=True)
class ObjectSchema(Schema[dict[str, Any], dict[str, Any]]):
    shape: Mapping[str, Schema[Any, Any]]
    unknown_keys: UnknownKeys | Literal["catchall"] = "strip"
    _catchall: Schema[Any, Any] | None = field(default=None, repr=False)

    def __hash__(self) -> int:
        return hash(self._node)

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
            selected = builtins.set(keys)
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
class TypedObjectSchema[Shape](Schema[Shape, Shape]):
    shape: Mapping[str, Schema[Any, Any]]
    unknown_keys: UnknownKeys | Literal["catchall"] = "strip"
    _catchall: Schema[Any, Any] | None = field(default=None, repr=False)

    def __hash__(self) -> int:
        return hash(self._node)

    def _base_rebuild(
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

    def _typed_rebuild(
        self,
        shape: Mapping[str, Schema[Any, Any]],
        unknown_keys: UnknownKeys | Literal["catchall"] | None = None,
        catchall: Schema[Any, Any] | builtins.object | None = _UNSET,
    ) -> TypedObjectSchema[Shape]:
        rebuilt = self._base_rebuild(shape, unknown_keys, catchall)
        return TypedObjectSchema[Shape](
            rebuilt._node,
            rebuilt.shape,
            rebuilt.unknown_keys,
            rebuilt._catchall,
            _meta=self._meta,
        )

    def strict(self) -> TypedObjectSchema[Shape]:
        return self._typed_rebuild(self.shape, "strict", catchall=None)

    def passthrough(self) -> TypedObjectSchema[Shape]:
        return self._typed_rebuild(self.shape, "passthrough", catchall=None)

    def strip(self) -> TypedObjectSchema[Shape]:
        return self._typed_rebuild(self.shape, "strip", catchall=None)

    def catchall(
        self, schema: Schema[Any, Any],
    ) -> TypedObjectSchema[Shape]:
        if not isinstance(schema, Schema):
            raise TypeError("Catchall must be a schema")
        return self._typed_rebuild(self.shape, "catchall", catchall=schema)

    def extend(self, shape: Mapping[str, Schema[Any, Any]]) -> ObjectSchema:
        return self._base_rebuild({**self.shape, **shape})

    def pick(self, *keys: str) -> ObjectSchema:
        _check_keys(self.shape, keys)
        return self._base_rebuild({key: self.shape[key] for key in keys})

    def omit(self, *keys: str) -> ObjectSchema:
        _check_keys(self.shape, keys)
        return self._base_rebuild({
            key: value for key, value in self.shape.items() if key not in keys
        })

    def partial(self, *keys: str) -> ObjectSchema:
        if keys:
            _check_keys(self.shape, keys)
            selected = builtins.set(keys)
            return self._base_rebuild({
                key: value.optional() if key in selected else value
                for key, value in self.shape.items()
            })
        return self._base_rebuild({
            key: value.optional() for key, value in self.shape.items()
        })

    def required(self) -> ObjectSchema:
        return self._base_rebuild({
            key: value.nonoptional() for key, value in self.shape.items()
        })

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
class MapSchema[Key, Out, In = Out](
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

    def __hash__(self) -> int:
        return hash(self._node)

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
    input_keys = builtins.tuple(
        key if (wire := _field_wire(child._node)) is None else wire
        for key, child in frozen.items()
    )
    if len(builtins.set(input_keys)) != len(input_keys):
        raise ValueError("Object wire names must be unique")

    node = NativeSchema(
        "object", builtins.tuple(child._node for child in frozen.values()),
        (
            builtins.tuple(frozen),
            policy,
            catchall._node if catchall is not None else None,
            input_keys,
        ),
    )
    return ObjectSchema(node, frozen, policy, catchall)


def typed_object[Shape](
    typ: type[Shape],
    shape: Mapping[str, Schema[Any, Any]],
    *,
    unknown_keys: UnknownKeys | Literal["catchall"] = "strip",
    catchall: Schema[Any, Any] | None = None,
) -> TypedObjectSchema[Shape]:
    """Build an object schema whose parsed result is statically typed."""
    from ._annotations import typed_dict_keys

    if not typing.is_typeddict(typ):
        raise TypeError("typed_object requires a TypedDict class")
    if not isinstance(shape, Mapping):
        raise TypeError("Object shape must be a mapping")
    keys = builtins.set(shape)
    if not all(isinstance(key, str) for key in keys):
        raise TypeError("Object shape keys must be strings")

    required, optional = typed_dict_keys(typ)
    missing = required - keys
    if missing:
        raise ValueError(
            "Typed object shape is missing required keys: "
            + ", ".join(sorted(missing))
        )
    unknown = keys - required - optional
    if unknown:
        raise ValueError(
            "Typed object shape has undeclared keys: "
            + ", ".join(sorted(unknown))
        )

    # Validate the configuration before wrapping required fields. The guard
    # checks results, so defaults run before missing output is rejected.
    base = object(
        shape,
        unknown_keys=unknown_keys,
        catchall=catchall,
    )
    guarded_shape = {
        key: Schema[Any, Any](
            NativeSchema("required_output", (schema._node,), ()),
            _inner=schema, _meta=schema._meta,
        ) if key in required else schema
        for key, schema in base.shape.items()
    }
    base = base._rebuild(guarded_shape)
    return TypedObjectSchema[Shape](
        base._node,
        base.shape,
        base.unknown_keys,
        base._catchall,
        _meta=base._meta,
    )


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
    if not all(isinstance(option, Schema) for option in options):
        raise TypeError("Union options must be schemas")

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
    for _, option in items:
        if not isinstance(option, ObjectSchema):
            continue
        field = option.shape.get(key)
        if field is not None and _field_wire(field._node) is not None:
            raise ValueError(
                "Discriminator fields cannot have a wire name",
            )
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


def map[Key, Out, In](
    key_schema: Schema[Key, Any],
    value_schema: Schema[Out, In],
    /,
) -> MapSchema[Key, Out, In]:
    if not isinstance(key_schema, Schema) or not isinstance(value_schema, Schema):
        raise TypeError("Map schemas must be Schema instances")
    return MapSchema[Key, Out, In](
        NativeSchema("record", (value_schema._node,), (key_schema._node,)),
        key_schema,
        value_schema,
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
    if not all(isinstance(item, Schema) for item in items):
        raise TypeError("Tuple items must be schemas")
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
    if not isinstance(schema, Schema):
        raise TypeError("Preprocess input must be a schema")
    fn = _sync_callback(fn, "preprocess callback")
    return Schema[Out, Any](
        NativeSchema("preprocess", (schema._node,), (fn,)),
        _inner=schema,
        _meta=schema._meta,
    )


def preprocess_async[Out, In](
    fn: Callable[[Any], Awaitable[Any]], schema: Schema[Out, In],
) -> Schema[Out, Any]:
    """Await input conversion before validating through ``schema``."""
    if not isinstance(schema, Schema):
        raise TypeError("Preprocess input must be a schema")
    fn = _async_callback(fn, "preprocess_async callback")
    return Schema[Out, Any](
        NativeSchema("async_preprocess", (schema._node,), (fn,)),
        _inner=schema,
        _meta=schema._meta,
    )


_CONSTRAINT_KINDS = frozenset({
    "length_min", "length_max", "min", "max", "exclusive_min", "exclusive_max",
    "multiple_of", "positive", "integral", "regex", "starts_with", "ends_with",
    "includes", "ipv6", "cidrv4", "cidrv6",
})

def to_json_schema(
    schema: Schema[Any, Any],
    *,
    target: JSONSchemaTarget = "draft-2020-12",
    io: JSONSchemaIO = "output",
    unrepresentable: UnrepresentablePolicy = "throw",
) -> JSONSchema:
    """Export a schema as a JSON Schema mapping.

    Refinements, transforms, preprocessors, pipes, and factories are not
    representable. Lazy schemas are exported as references when they declare an
    ``id``; resolving them may run each lazy factory once per export.
    ``unrepresentable="any"`` emits ``{}`` for unsupported nodes.
    """
    from ._json_schema import to_json_schema as _export_json_schema

    return _export_json_schema(
        schema,
        target=target,
        io=io,
        unrepresentable=unrepresentable,
    )


from . import _codecs as _codecs  # noqa: E402


AsyncCodecSchema = _codecs.AsyncCodecSchema
CodecSchema = _codecs.CodecSchema
codec = _codecs.codec
codec_async = _codecs.codec_async
codecs = _codecs._Codecs()
_encode_node_for = _codecs._encode_node_for
