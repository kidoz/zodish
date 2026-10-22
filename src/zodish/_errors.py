"""Issues, validation errors, and parse results.

This module has no dependency on the schema builders, so ``zodish.schemas``
can import it directly and re-export the public names.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, Self, cast


type Path = builtins.tuple[str | int, ...]


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
    issue = builtins.object.__new__(Issue)
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


# Keep the public module path stable after the private-module split.
for _public_class in (
    Issue, UnrepresentableError, ValidationError, Success, Failure,
):
    _public_class.__module__ = "zodish.schemas"
