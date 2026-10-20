from collections.abc import Sequence
from typing import Any

BACKEND: str
MAX_DEPTH: int

class NativeSchema:
    def __init__(
        self, kind: str, children: Sequence[NativeSchema], args: Sequence[Any],
    ) -> None: ...
    @property
    def kind(self) -> str: ...
    @property
    def children(self) -> tuple[NativeSchema, ...]: ...
    @property
    def args(self) -> tuple[Any, ...]: ...
    def parse(
        self, value: Any, missing: Any, issue_type: type[Any], error_type: type[Exception],
    ) -> Any: ...
    def parse_json(self, data: str | bytes | bytearray) -> tuple[bool, Any]: ...
