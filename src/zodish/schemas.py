"""Typed Python schema builders backed by the native Rust evaluator."""

from __future__ import annotations

import base64
import binascii
import builtins
import copy
import dataclasses
import datetime as _datetime
import decimal as _decimal
import keyword
import math
import pathlib as _pathlib
import re
import sys
import typing
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass as _dataclass
from dataclasses import field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Self, cast, overload

from ._core import (
    _UNSET,
    JSONSchema,
    JSONSchemaIO,
    JSONSchemaTarget,
    UnrepresentablePolicy,
    _async_callback,
    _field_wire,
    _message,
    _string_argument,
    _sync_callback,
)
from ._core import (
    MISSING as MISSING,
)
from ._core import (
    Missing as Missing,
)
from ._core import (
    Schema as Schema,
)
from ._errors import (
    ErrorMap as ErrorMap,
)
from ._errors import (
    Failure as Failure,
)
from ._errors import (
    Issue as Issue,
)
from ._errors import (
    Path as Path,
)
from ._errors import (
    Success as Success,
)
from ._errors import (
    UnrepresentableError as UnrepresentableError,
)
from ._errors import (
    ValidationError as ValidationError,
)
from ._errors import (
    _invalid_format,
)
from ._native import NativeSchema


# Unlike $, this end assertion rejects a final newline in Python and JSON
# Schema's regex dialect alike. Custom .regex() retains re.search semantics.
_URL_PATTERN = r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+(?![\s\S])"
_HTTP_URL_PATTERN = r"^[Hh][Tt][Tt][Pp][Ss]?://[^\s]+(?![\s\S])"
_HEX_PATTERN = r"^[0-9a-fA-F]+(?![\s\S])"
_DURATION_PATTERN = (
    r"^[+-]?P(?:\d+W|(?=\d|T)(?:\d+D)?"
    r"(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?)(?![\s\S])"
)
_DURATION_PARTS = re.compile(
    r"^[+-]?P(?:(\d+)W|(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?)$"
)
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![\s\S])"
)
_DATE_PATTERN = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])(?![\s\S])"
)
_TIME_PATTERN = (
    r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9](?:\.[0-9]+)?)?"
    r"(?:[Zz]|[+-](?:0[0-9]|1[0-9]|2[0-3]):[0-5][0-9])?(?![\s\S])"
)
_DATETIME_PATTERN = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"[Tt ](?:[01][0-9]|2[0-3]):[0-5][0-9]"
    r"(?::[0-5][0-9](?:\.[0-9]+)?)?"
    r"(?:[Zz]|[+-](?:0[0-9]|1[0-9]|2[0-3]):[0-5][0-9])?(?![\s\S])"
)
_IPV4_PATTERN = (
    r"^(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(?![\s\S])"
)
_DECIMAL_PATTERN = (
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?(?![\s\S])"
)
_BASE64_PATTERN = (
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?(?![\s\S])"
)
_BASE64URL_PATTERN = (
    r"^(?:[A-Za-z0-9_-]{4})*"
    r"(?:[A-Za-z0-9_-]{2}==|[A-Za-z0-9_-]{3}=|[A-Za-z0-9_-]{2,3})?(?![\s\S])"
)
_HOSTNAME_PATTERN = (
    r"^(?=.{1,253}$)"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(?:\.(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?))*(?![\s\S])"
)
_ULID_PATTERN = r"(?i)^[0-7][0-9A-HJKMNP-TV-Z]{25}(?![\s\S])"
_CUID_PATTERN = r"(?i)^c[^\s-]{8,}(?![\s\S])"
_CUID2_PATTERN = r"^[a-z][a-z0-9]{1,31}(?![\s\S])"
_NANOID_PATTERN = r"^[A-Za-z0-9_-]{21}(?![\s\S])"
_JWT_PATTERN = r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![\s\S])"
_E164_PATTERN = r"^\+[1-9][0-9]{1,14}(?![\s\S])"
_KNOWN_FORMATS = frozenset({
    "email", "uri", "uuid", "date", "time", "date-time", "ipv4", "ipv6",
    "hostname", "ulid", "cuid", "cuid2", "nanoid", "jwt", "e164",
})
_PATTERN_FORMATS = frozenset({
    "regex", "decimal", "base64", "base64url", "duration", "http_url", "hex",
})


def _string_values(values: Sequence[str], role: str) -> builtins.tuple[str, ...]:
    result = builtins.tuple(values)
    if not result:
        raise ValueError(f"{role} values must not be empty")
    if not all(isinstance(value, str) for value in result):
        raise TypeError(f"{role} values must be strings")
    return result


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
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
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


@_dataclass(frozen=True)
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
        """Match a Python regular expression.

        Patterns run through Python's ``re`` engine and can backtrack
        catastrophically on crafted input. Do not use untrusted patterns;
        prefer the built-in format helpers for untrusted data.
        """
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


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
class RecordSchema[Out, In = Out](Schema[dict[str, Out], dict[str, In]]):
    value_schema: Schema[Out, In]

    def unwrap(self) -> Schema[Out, In]:
        return self.value_schema


@_dataclass(frozen=True)
class KeyedRecordSchema[Key, Out, In = Out](
    Schema[dict[Key, Out], dict[Key, In]],
):
    key_schema: Schema[Key, Any]
    value_schema: Schema[Out, In]

    def unwrap(self) -> Schema[Out, In]:
        return self.value_schema


@_dataclass(frozen=True)
class MapSchema[Key, Out, In = Out](
    Schema[dict[Key, Out], dict[Key, In]],
):
    key_schema: Schema[Key, Any]
    value_schema: Schema[Out, In]

    def unwrap(self) -> Schema[Out, In]:
        return self.value_schema


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
class UnionSchema[Out, In = Out](Schema[Out, In]):
    options: builtins.tuple[Schema[Any, Any], ...]


@_dataclass(frozen=True)
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


@_dataclass(frozen=True)
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
    """Try options in order and return the first success.

    Every failing branch is evaluated, so deeply nested unions can grow
    exponentially. Prefer ``discriminated_union`` when branches share a
    literal discriminator.
    """
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
    """Dispatch on a dictionary key; only the selected branch is evaluated."""
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


def _validated_model_codec(cls: type[Any]) -> Any:
    from ._validated_dataclasses import codec_for

    return codec_for(cls)


class ValidatedModel:
    """Typed class-level entry points for ``@z.dataclass`` classes.

    Inherit from this mixin to expose the classmethods to type checkers.
    Decorated classes also receive the same methods at runtime.
    """

    __slots__ = ()

    @classmethod
    def parse(
        cls,
        value: dict[str, Any] | Missing = MISSING,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Self:
        return cast(
            Self, _validated_model_codec(cls).parse(value, error=error, context=context),
        )

    @classmethod
    def safe_parse(
        cls,
        value: dict[str, Any] | Missing = MISSING,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Self] | Failure:
        return cast(
            Success[Self] | Failure,
            _validated_model_codec(cls).safe_parse(value, error=error, context=context),
        )

    @classmethod
    def validate(
        cls, value: dict[str, Any] | Missing = MISSING, *, context: Any = None,
    ) -> bool:
        return cast(bool, _validated_model_codec(cls).validate(value, context=context))

    @classmethod
    async def acreate(cls, *args: Any, **kwargs: Any) -> Self:
        from ._validated_dataclasses import _acreate

        return cast(Self, await _acreate(cls, *args, **kwargs))

    @classmethod
    def parse_json(
        cls,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Self:
        return cast(
            Self,
            _validated_model_codec(cls).parse_json(data, error=error, context=context),
        )

    @classmethod
    def safe_parse_json(
        cls,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Self] | Failure:
        return cast(
            Success[Self] | Failure,
            _validated_model_codec(cls).safe_parse_json(
                data, error=error, context=context,
            ),
        )

    @classmethod
    def validate_json(
        cls, data: str | builtins.bytes | bytearray, *, context: Any = None,
    ) -> bool:
        return cast(
            bool, _validated_model_codec(cls).validate_json(data, context=context),
        )

    @classmethod
    async def parse_async(
        cls,
        value: dict[str, Any] | Missing = MISSING,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Self:
        return cast(
            Self,
            await _validated_model_codec(cls).parse_async(
                value, error=error, context=context,
            ),
        )

    @classmethod
    async def safe_parse_async(
        cls,
        value: dict[str, Any] | Missing = MISSING,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Self] | Failure:
        return cast(
            Success[Self] | Failure,
            await _validated_model_codec(cls).safe_parse_async(
                value, error=error, context=context,
            ),
        )

    @classmethod
    async def validate_async(
        cls, value: dict[str, Any] | Missing = MISSING, *, context: Any = None,
    ) -> bool:
        return cast(
            bool, await _validated_model_codec(cls).validate_async(value, context=context),
        )

    @classmethod
    async def parse_json_async(
        cls,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Self:
        return cast(
            Self,
            await _validated_model_codec(cls).parse_json_async(
                data, error=error, context=context,
            ),
        )

    @classmethod
    async def safe_parse_json_async(
        cls,
        data: str | builtins.bytes | bytearray,
        *,
        error: ErrorMap | None = None,
        context: Any = None,
    ) -> Success[Self] | Failure:
        return cast(
            Success[Self] | Failure,
            await _validated_model_codec(cls).safe_parse_json_async(
                data, error=error, context=context,
            ),
        )

    @classmethod
    def encode(cls, value: Self) -> dict[str, Any]:
        return cast(dict[str, Any], _validated_model_codec(cls).encode(value))

    @classmethod
    def safe_encode(
        cls, value: Self,
    ) -> Success[dict[str, Any]] | Failure:
        return cast(
            Success[dict[str, Any]] | Failure,
            _validated_model_codec(cls).safe_encode(value),
        )

    @classmethod
    async def encode_async(cls, value: Self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await _validated_model_codec(cls).encode_async(value),
        )

    @classmethod
    async def safe_encode_async(
        cls, value: Self,
    ) -> Success[dict[str, Any]] | Failure:
        return cast(
            Success[dict[str, Any]] | Failure,
            await _validated_model_codec(cls).safe_encode_async(value),
        )

    @classmethod
    def to_json_schema(
        cls,
        *,
        target: JSONSchemaTarget = "draft-2020-12",
        io: JSONSchemaIO = "output",
        unrepresentable: UnrepresentablePolicy = "throw",
    ) -> JSONSchema:
        return cast(
            JSONSchema,
            _validated_model_codec(cls).to_json_schema(
                target=target,
                io=io,
                unrepresentable=unrepresentable,
            ),
        )


@overload
def dataclass[T](
    cls: type[T],
    /,
    *,
    validate_assignment: bool | None = ...,
    async_fields: bool | None = ...,
) -> type[T]: ...


@overload
def dataclass[T](
    *,
    init: bool = ...,
    repr: bool = ...,
    eq: bool = ...,
    order: bool = ...,
    unsafe_hash: bool = ...,
    frozen: bool = ...,
    match_args: bool = ...,
    kw_only: bool = ...,
    slots: bool = ...,
    weakref_slot: bool = ...,
    validate_assignment: bool | None = ...,
    async_fields: bool | None = ...,
) -> Callable[[type[T]], type[T]]: ...


@typing.dataclass_transform(field_specifiers=(dataclasses.field,))
def dataclass[T](
    cls: type[T] | None = None,
    /,
    **options: Any,
) -> Any:
    """Validate ordinary construction of a dataclass.

    Field annotations are compiled into one native object schema. Constructor
    arguments are decoded values; use ``z.codecs.dataclass`` for encoded input.
    Set ``validate_assignment=True`` to validate later field assignments; the
    default is inherited by decorated subclasses. Set ``async_fields=True`` to
    allow asynchronous field schemas and construct with ``await Model.acreate``.
    """
    from ._validated_dataclasses import dataclass as _validated_dataclass

    return _validated_dataclass(cls, **options)


def _schema_default_spec(
    schema: Schema[Any, Any],
) -> builtins.tuple[str, Any] | None:
    node = schema._node
    if node.kind == "default":
        value = node.args[0]
        if value is MISSING:
            raise TypeError("Model field default cannot be MISSING")
        return ("value", value)
    if node.kind == "default_factory":
        return ("factory", node.args[0])
    return None


def _copy_default(value: Any) -> Callable[[], Any]:
    return lambda: copy.deepcopy(value)


def _model_default_field(spec: builtins.tuple[str, Any]) -> Any:
    kind, value = spec
    if kind == "factory":
        return field(default_factory=value)
    return field(default_factory=_copy_default(value))


def create_model(
    name: str,
    fields: Mapping[str, Schema[Any, Any]] | ObjectSchema,
    *,
    defaults: Mapping[str, Any] | None = None,
    validate_assignment: bool | None = None,
    async_fields: bool | None = None,
    **options: Any,
) -> type[Any]:
    """Create a validated dataclass from runtime field schemas.

    ``fields`` is a mapping of Python field names to schemas, or a plain
    ``z.object`` schema whose shape supplies the fields. Models are
    keyword-only. ``defaults`` supplies decoded constructor defaults; a
    top-level ``.default`` or ``.default_factory`` on a plain field schema is
    also honored. For codec fields, supply the decoded default through
    ``defaults``. Encoded input still uses ``z.codecs.dataclass(Model)`` or the
    generated class methods. Set ``async_fields=True`` to construct with
    ``await Model.acreate(...)`` and asynchronous field schemas.
    """
    if (
        not isinstance(name, str)
        or not name.isidentifier()
        or keyword.iskeyword(name)
    ):
        raise ValueError("Model name must be a valid Python identifier")
    if isinstance(fields, ObjectSchema):
        if fields.unknown_keys != "strip" or fields._catchall is not None:
            raise TypeError(
                "create_model requires a plain strip object schema",
            )
        field_schemas = dict(fields.shape)
    elif isinstance(fields, Mapping):
        field_schemas = dict(fields)
    else:
        raise TypeError("Model fields must be a mapping or object schema")

    for field_name, field_schema in field_schemas.items():
        if (
            not isinstance(field_name, str)
            or not field_name.isidentifier()
            or keyword.iskeyword(field_name)
        ):
            raise ValueError(
                "Model field names must be valid Python identifiers",
            )
        if not isinstance(field_schema, Schema):
            raise TypeError("Model fields must map names to schemas")

    explicit_defaults = {} if defaults is None else dict(defaults)
    if builtins.any(
        not isinstance(key, str) for key in explicit_defaults
    ):
        raise ValueError("Model defaults must use string field names")
    unknown_defaults = builtins.set(explicit_defaults) - builtins.set(
        field_schemas,
    )
    if unknown_defaults:
        raise ValueError(
            "Unknown model defaults: "
            + ", ".join(sorted(unknown_defaults)),
        )

    if options.pop("kw_only", True) is not True:
        raise ValueError("create_model fields are keyword-only")
    if options.get("init", True) is False:
        raise ValueError("create_model requires init=True")

    field_specs: list[Any] = []
    for field_name, field_schema in field_schemas.items():
        annotation = typing.Annotated[Any, field_schema]
        default_spec: builtins.tuple[str, Any] | None
        if field_name in explicit_defaults:
            default_value = explicit_defaults[field_name]
            if default_value is MISSING:
                raise TypeError("Model field default cannot be MISSING")
            default_spec = ("value", default_value)
        else:
            default_spec = _schema_default_spec(field_schema)
        if default_spec is None:
            if field_schema._node.kind == "optional":
                raise ValueError(
                    f"Model field {field_name!r} permits missing input; "
                    "supply a default",
                )
            field_specs.append((field_name, annotation))
        else:
            field_specs.append((
                field_name,
                annotation,
                _model_default_field(default_spec),
            ))

    module = sys._getframe(1).f_globals.get("__name__", "__main__")
    dynamic = dataclasses.make_dataclass(
        name,
        field_specs,
        namespace={"__module__": module},
        kw_only=True,
        **options,
    )
    dynamic.__module__ = module
    from ._validated_dataclasses import dataclass as _validated_dataclass

    return cast(
        type[Any],
        _validated_dataclass(
            dynamic,
            validate_assignment=validate_assignment,
            async_fields=async_fields,
        ),
    )



def adapter[T](annotation: type[T]) -> Schema[T, Any]:
    """Build a schema from a runtime type annotation."""
    from ._annotations import infer_dataclass_field

    return cast(
        Schema[T, Any],
        infer_dataclass_field(annotation, "$root", stack=(), allow_sets=True),
    )


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
from . import _validate_call as _validate_call  # noqa: E402


AsyncCodecSchema = _codecs.AsyncCodecSchema
CodecSchema = _codecs.CodecSchema
codec = _codecs.codec
codec_async = _codecs.codec_async
codecs = _codecs._Codecs()
validate_call = _validate_call.validate_call
_encode_node_for = _codecs._encode_node_for
