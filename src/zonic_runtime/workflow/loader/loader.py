"""``WorkflowLoader``: catalog visibility and document loading (design 5.1; plan P1-04).

Steps 1 and 2 read the gateway: the catalog row (visibility) and the builder's export
(the stored document). Steps 3 to 6 run in ``from_stored`` on any stored document, so
the root workflow, every sub-flow and every fixture share one load path: version check,
dialect normalization with layout stripping, validation of the document and of each
node's configuration model, and the revision hash. Loaded documents are cached by
``(agenticWorkflowId, updated_at)``, so an unchanged workflow costs one catalog read.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import BlueprintWarning
from zonic_runtime.models.document import WorkflowDocument
from zonic_runtime.models.document.document import DEFAULT_DOCUMENT_VERSION
from zonic_runtime.models.errors import ValidationProblem, problems_from
from zonic_runtime.models.gateway.catalog import AgentExportRow, AgentRevisionRow, CatalogAgentRow
from zonic_runtime.models.gateway.wire import Filter, FilterOp, OrderBy
from zonic_runtime.models.nodes import config_model_for
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.ports.rows import RowReader
from zonic_runtime.runtime.lru import LruCache
from zonic_runtime.workflow.loader.normalizer import DocumentNormalizer
from zonic_runtime.workflow.loader.revision import revision_hash

_ID = "agentic_workflow_id"


@dataclass(frozen=True)
class WorkflowCaller:
    """Who is asking. Access control is the Cognito token alone (decision 0004): any
    authenticated caller sees every listed workflow, and the document's ``security`` is not read."""

    subject: str


@dataclass(frozen=True)
class LoadedDocument:
    """A validated canonical document of one revision."""

    agentic_workflow_id: str
    document: WorkflowDocument
    #: SHA-256 of the canonical document (design 5.1 step 6).
    revision: str
    #: ``major.minor``; a missing ``schemaVersion`` reads as 1.0.
    version: str
    findings: tuple[BlueprintWarning, ...]


def _invalid(problems: Iterable[ValidationProblem]) -> ZonicError:
    return ZonicError("DOCUMENT_INVALID", problems=[p.wire() for p in problems])


class WorkflowLoader:
    def __init__(
        self,
        normalizer: DocumentNormalizer,
        settings: RuntimeSettings,
        manifest: NodeRuntimeManifest,
        repository: RowReader | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._normalizer = normalizer
        self._settings = settings
        self._manifest = manifest
        self._repository = repository
        self._monotonic = monotonic
        self._documents: LruCache[tuple[str, datetime], LoadedDocument] = LruCache(settings.caches.documents)
        self._catalog: tuple[float, list[CatalogAgentRow]] | None = None

    @classmethod
    def from_resources(
        cls, resources: RuntimeResources, repository: RowReader | None = None, **kwargs: Any
    ) -> WorkflowLoader:
        manifest = resources.manifest
        normalizer = DocumentNormalizer(
            resources.dialects,
            lambda node_type: entry.kind if (entry := manifest.node_types.get(node_type)) else None,
            default_model=resources.models.default_model,
            rule_expression=resources.settings.compat.rule_expression,
        )
        return cls(normalizer, resources.settings, manifest, repository, **kwargs)

    # -- steps 1 and 2: the gateway ------------------------------------------

    async def catalog(self, caller: WorkflowCaller) -> list[CatalogAgentRow]:
        """Workflows the caller may run, one row each; the rows are cached ``catalog.cacheSeconds``."""
        now = self._monotonic()
        if self._catalog is None or now - self._catalog[0] >= self._settings.catalog.cache_seconds:
            rows = await self._repo().read(
                CatalogAgentRow, filters=self._channel_filters(), order_by=[OrderBy(column="name")]
            )
            self._catalog = (now, self._unique(rows))
        return list(self._catalog[1])

    async def load(self, agentic_workflow_id: str, caller: WorkflowCaller) -> LoadedDocument:
        """The caller's view of one workflow; unknown and invisible workflows are the same error."""
        rows = await self._repo().read(
            CatalogAgentRow,
            filters=[Filter(column=_ID, value=agentic_workflow_id), *self._channel_filters()],
        )
        row = next(iter(self._unique(rows)), None)
        if row is None:
            raise ZonicError("WORKFLOW_NOT_FOUND", agentic_workflow_id=agentic_workflow_id)
        return (await self._revisions({agentic_workflow_id: row.updated_at}))[0]

    async def load_exports(self, ids: Sequence[str]) -> list[LoadedDocument]:
        """Stored documents by ID with one ``in`` read (sub-flows: not catalog entries, so the
        sub-flow node checks the caller's visibility)."""
        rows = await self._repo().read(
            AgentRevisionRow, filters=[Filter(column=_ID, op=FilterOp.IN, value=list(ids))]
        )
        updated = {row.agentic_workflow_id: row.updated_at for row in rows}
        missing = [i for i in ids if i not in updated]
        if missing:
            raise ZonicError("WORKFLOW_NOT_FOUND", agentic_workflow_id=missing[0])
        return await self._revisions({i: updated[i] for i in ids})

    async def _revisions(self, revisions: dict[str, datetime]) -> list[LoadedDocument]:
        found = {i: doc for i, at in revisions.items() if (doc := self._documents.get((i, at))) is not None}
        misses = [i for i in revisions if i not in found]
        if misses:
            rows = await self._repo().read(
                AgentExportRow, filters=[Filter(column=_ID, op=FilterOp.IN, value=misses)]
            )
            stored = {row.agentic_workflow_id: row.export_json for row in rows}
            for i in misses:
                if i not in stored:
                    raise ZonicError("WORKFLOW_NOT_FOUND", agentic_workflow_id=i)
                found[i] = self.from_stored(stored[i], i)
                self._documents.put((i, revisions[i]), found[i])
        return [found[i] for i in revisions]

    def _channel_filters(self) -> list[Filter]:
        catalog = self._settings.catalog
        if catalog.include_unpublished:
            return []
        return [Filter(column="runtime_channel", op=FilterOp.IN, value=list(catalog.runtime_channels))]

    def _unique(self, rows: list[CatalogAgentRow]) -> list[CatalogAgentRow]:
        """One row per workflow (the view has one per channel), without excluded IDs."""
        excluded = tuple(self._settings.catalog.excluded_id_prefixes)
        seen: dict[str, CatalogAgentRow] = {}
        for row in rows:
            if not row.agentic_workflow_id.startswith(excluded):
                seen.setdefault(row.agentic_workflow_id, row)
        return list(seen.values())

    def _repo(self) -> RowReader:
        if self._repository is None:
            raise RuntimeError("WorkflowLoader has no gateway repository")
        return self._repository

    # -- steps 3 to 6: any stored document -------------------------------------

    def from_stored(self, stored: Any, agentic_workflow_id: str | None = None) -> LoadedDocument:
        if not isinstance(stored, dict):
            raise _invalid([ValidationProblem(path="", message="must be an object", type="dict_type")])
        # A builder template keeps its document in a ``document`` member (case F-02).
        raw = (
            stored["document"]
            if not isinstance(stored.get("nodes"), list) and isinstance(stored.get("document"), dict)
            else stored
        )

        version = raw.get("schemaVersion", DEFAULT_DOCUMENT_VERSION)
        if not isinstance(version, str) or not version:
            raise _invalid(
                [ValidationProblem(path="schemaVersion", message="must be a string", type="string_type")]
            )
        if version.split(".")[0] not in self._settings.document_versions:
            raise ZonicError(
                "DOCUMENT_VERSION_UNSUPPORTED",
                version=version,
                accepted=list(self._settings.document_versions),
            )

        try:
            normalized = self._normalizer.normalize(raw)
        except ValidationError as exc:
            raise _invalid(problems_from(exc)) from exc
        problems: list[ValidationProblem] = []
        for index, node in enumerate(normalized.document.nodes):
            entry = self._manifest.node_types.get(node.type)
            if entry is None:
                continue  # the blueprint reports unknown types (manifest onUnknownType)
            try:
                config_model_for(entry.kind).model_validate(node.config)
            except ValidationError as exc:
                problems += problems_from(exc, prefix=f"nodes[{index}].config")
        if problems:
            raise _invalid(problems)

        document = normalized.document
        return LoadedDocument(
            agentic_workflow_id=agentic_workflow_id or document.agentic_workflow_id or "",
            document=document,
            revision=revision_hash(normalized.canonical),
            version=version,
            findings=tuple(normalized.findings),
        )
