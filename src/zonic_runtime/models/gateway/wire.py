"""The pgfoundation v1 wire contract, mirrored (``ZonicPostgresqlServer_FastAPI`` api/v1/schemas).

Declared here rather than imported, so the runtime has no build-time dependency
on the gateway project. Field names are snake_case because that is the gateway's
wire format; this module and the gateway adapter are the only places that speak it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FilterOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    IN = "in"
    NOT_IN = "not_in"
    LIKE = "like"
    ILIKE = "ilike"
    BETWEEN = "between"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class Filter(BaseModel):
    model_config = ConfigDict(frozen=True)

    column: str
    op: FilterOp = FilterOp.EQ
    value: Any = None


class OrderBy(BaseModel):
    model_config = ConfigDict(frozen=True)

    column: str
    descending: bool = False


class _SchemaQualified(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    db_schema: str | None = Field(default=None, alias="schema")


class ViewIn(_SchemaQualified):
    columns: list[str] | None = None
    filters: list[Filter] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = None
    offset: int | None = None


class ProcedureIn(_SchemaQualified):
    args: list[Any] | None = None


class FunctionIn(_SchemaQualified):
    args: dict[str, Any] | list[Any] | None = None
    scalar: bool = False


BatchMode = Literal["parallel", "sequential", "transactional"]


class BatchItem(BaseModel):
    id: str
    datasource: str
    op: Literal["view", "procedure", "function"]  # principle: allow rule-1-node-type gateway-operation-name
    payload: dict[str, Any] = Field(default_factory=dict)


class BatchRequest(BaseModel):
    mode: BatchMode
    items: list[BatchItem]


class QueryResult(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    elapsed_ms: float = 0.0

    def first(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


class BatchItemError(BaseModel):
    type: str
    message: str


class BatchItemResult(BaseModel):
    id: str
    status: Literal["ok", "error"]
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    elapsed_ms: float | None = None
    error: BatchItemError | None = None


class BatchResult(BaseModel):
    mode: BatchMode
    elapsed_ms: float = 0.0
    ok_count: int = 0
    error_count: int = 0
    results: list[BatchItemResult] = Field(default_factory=list)

    def by_id(self) -> dict[str, BatchItemResult]:
        return {r.id: r for r in self.results}
