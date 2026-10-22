"""Python shapes consumed structurally by the native evaluator.

Rust reads these names at runtime. The protocols below make the contract
visible to type checkers; the conformance helpers fail type checking if the
concrete classes drift from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, Self

from ._core import Schema
from ._errors import Issue, Path, ValidationError
from ._native import NativeIssueBundle, NativeSchema


type RawIssue = tuple[
    str,
    str,
    Path,
    tuple[tuple[RawIssue, ...], ...],
    Mapping[str, Any],
]


class SchemaNode(Protocol):
    """Shape returned by a lazy factory and read as ``._node`` by Rust."""

    @property
    def _node(self) -> NativeSchema: ...


class IssueValue(Protocol):
    """Issue attributes read by ``Issue::from_python``."""

    @property
    def code(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def path(self) -> Path: ...

    @property
    def branches(self) -> tuple[tuple[IssueValue, ...], ...]: ...

    @property
    def params(self) -> Mapping[str, Any]: ...


class ValidationErrorValue(Protocol):
    """Validation error shape read by error normalization and materialization."""

    @property
    def issues(self) -> tuple[IssueValue, ...]: ...

    @classmethod
    def _from_bundle(cls, bundle: NativeIssueBundle) -> Self: ...


def _issue_conforms(issue: Issue) -> IssueValue:
    return issue


def _validation_error_conforms(error: ValidationError) -> ValidationErrorValue:
    return error


def _schema_conforms(schema: Schema[Any, Any]) -> SchemaNode:
    return schema
