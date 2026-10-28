"""Content parts, discriminated by ``type`` (design 14.2; plan P3-08, case M-06).

Messages, results and ``content.part`` events carry lists of parts; a client has one renderer per
type. Parts are data and references, never code: chart options are checked against an allow list
and hold no functions, HTML is sanitized on the server and shown without script permission. The
shapes are the Node.js runtime's, so one client reads both runtimes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.engine.records import DatasetRef
from zonic_runtime.models.registry import exported


@exported("content")
class MarkdownPart(ZonicModel):
    type: Literal["markdown"] = "markdown"
    text: str = ""


class TableColumn(ZonicModel):
    key: str
    label: str | None = None
    type: str | None = None


@exported("content")
class TablePart(ZonicModel):
    """Rows inline, at most ``content.inlineRowLimit``."""

    type: Literal["table"] = "table"
    title: str | None = None
    columns: list[TableColumn] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    #: The full data when the rows are a preview.
    dataset_ref: DatasetRef | None = None


@exported("content")
class GridPart(ZonicModel):
    """A server-mode grid paging through ``GET /v1/datasets/{datasetId}``."""

    type: Literal["grid"] = "grid"
    title: str | None = None
    #: Column definitions as the builder writes them (``field``, ``headerName``, ...).
    columns: list[dict[str, Any]] = Field(default_factory=list)
    dataset_ref: DatasetRef | None = None
    page_size: int | None = Field(default=None, gt=0)
    #: Filters, grouping, page sizes, theme and height of the builder's grid configuration.
    features: dict[str, Any] = Field(default_factory=dict)


@exported("content")
class ChartPart(ZonicModel):
    """An ECharts option: data only, top-level keys on ``content.chartOptionKeys``, no functions."""

    type: Literal["chart"] = "chart"
    library: Literal["echarts"] = "echarts"
    option: dict[str, Any] = Field(default_factory=dict)
    dataset_ref: DatasetRef | None = None


class CardField(ZonicModel):
    label: str
    value: Any = None


class CardAction(ZonicModel):
    label: str
    action: str | None = None
    payload: dict[str, Any] | None = None


@exported("content")
class CardPart(ZonicModel):
    type: Literal["card"] = "card"
    title: str = ""
    body: str | None = None
    fields: list[CardField] = Field(default_factory=list)
    actions: list[CardAction] = Field(default_factory=list)


@exported("content")
class JsonPart(ZonicModel):
    type: Literal["json"] = "json"
    value: Any = None


@exported("content")
class FilePart(ZonicModel):
    type: Literal["file"] = "file"
    name: str
    media_type: str
    download_ref: str


@exported("content")
class HtmlPart(ZonicModel):
    """A builder ``htmlTemplate`` view, sanitized on the server; shown in a sandboxed ``iframe``."""

    type: Literal["html"] = "html"
    html: str = ""
    dataset_refs: list[DatasetRef] = Field(default_factory=list)


@exported("content")
class CitationPart(ZonicModel):
    type: Literal["citation"] = "citation"
    title: str
    url: str
    snippet: str | None = None


@exported("content")
class ErrorPart(ZonicModel):
    type: Literal["error"] = "error"
    code: str
    message: str


@exported("content")
class FormPart(ZonicModel):
    """A schema-driven form answering an interrupt (design 9.4)."""

    type: Literal["form"] = "form"
    interrupt_id: str
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    ui_schema: dict[str, Any] | None = None


ContentPart = Annotated[
    MarkdownPart
    | TablePart
    | GridPart
    | ChartPart
    | CardPart
    | JsonPart
    | FilePart
    | HtmlPart
    | CitationPart
    | ErrorPart
    | FormPart,
    Field(discriminator="type"),
]
CONTENT_PART: TypeAdapter[Any] = TypeAdapter(ContentPart)
CONTENT_PARTS: TypeAdapter[Any] = TypeAdapter(list[ContentPart])


def plain(part: ZonicModel) -> dict[str, Any]:
    """A part as stored and sent: camelCase, without unset optional fields."""
    return part.wire()
