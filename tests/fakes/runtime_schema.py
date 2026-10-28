"""The ``agent_runtime`` views and procedures on ``InMemoryGateway`` (plan P2-03, P2-04).

Ported from the Node.js runtime's ``in-memory-runtime-schema.ts``: the same contracts as
``db/runtime`` (reason tokens, owner checks, idempotency, active-run caps, fencing by attempt).
Failures raise what the HTTP gateway would after translating the database's reason token
(``translate_status``), so services see the same catalog codes as against the real gateway.

Covered: sessions (``sp_session_create``, ``sp_session_update``, ``v_session_summary``), runs and
messages (``sp_run_start``, ``sp_run_transition``, ``sp_run_finish``, ``v_run``, ``v_message``),
session state (``v_session_state``), feedback (``sp_feedback_save``) and datasets (``v_dataset``,
``sp_dataset_save``, chunk 0 from ``sp_run_finish``, P3-05). Later tasks add the procedures they use.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.fakes.in_memory_gateway import InMemoryGateway
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.http_gateway import translate_status

SCHEMA = "runtime"
SESSIONS, RUNS, MESSAGES = "runtime_session", "runtime_run", "runtime_message"
FEEDBACK, STATE = "runtime_feedback", "runtime_session_state"
INTERRUPTS, CHECKPOINTS = "runtime_interrupt", "runtime_checkpoint"
DATASETS = "runtime_dataset"

TRANSITIONS = frozenset(
    {
        ("queued", "running"),
        ("queued", "failed"),
        ("queued", "cancelled"),
        ("running", "waiting_input"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "timed_out"),
        ("running", "queued"),
        ("waiting_input", "running"),
        ("waiting_input", "failed"),
        ("waiting_input", "cancelled"),
        ("waiting_input", "timed_out"),
    }
)
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
ACTIVE = frozenset({"queued", "running", "waiting_input"})


def fault(reason: str, status: int = 409) -> ZonicError:
    if status == 400:
        return ZonicError("DATA_REQUEST_INVALID", gateway_status=400, reason=reason)
    return translate_status(status, {"error": reason})


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _later(seconds: Any) -> str:
    return (datetime.now(UTC) + timedelta(seconds=int(seconds))).isoformat()


def _unlease(run: dict[str, Any]) -> None:
    run.update(lease_owner=None, lease_address=None, lease_expires_at=None)


class RuntimeSchema:
    def __init__(self, gateway: InMemoryGateway) -> None:
        self.g = gateway
        views = (
            "v_session_summary",
            "v_message",
            "v_run",
            "v_session_state",
            "v_dataset",
            "v_interrupt_pending",
            "v_checkpoint",
        )
        for view in views:
            gateway.define_view(SCHEMA, view, getattr(self, view))
        for name in (
            "sp_session_create",
            "sp_session_update",
            "sp_run_start",
            "sp_run_transition",
            "sp_run_finish",
            "sp_feedback_save",
            "sp_dataset_save",
            "sp_retention_purge",
            "sp_interrupt_create",
            "sp_interrupt_answer",
            "sp_run_claim",
            "sp_run_heartbeat",
            "sp_run_release",
            "sp_checkpoint_save",
        ):
            gateway.on_procedure(SCHEMA, name, getattr(self, name))

    def t(self, name: str) -> list[dict[str, Any]]:
        return self.g.table(SCHEMA, name)

    def session(self, session_id: str) -> dict[str, Any] | None:
        return next((r for r in self.t(SESSIONS) if r["session_id"] == session_id), None)

    def run(self, run_id: str) -> dict[str, Any] | None:
        return next((r for r in self.t(RUNS) if r["run_id"] == run_id), None)

    # -- views ---------------------------------------------------------------------------

    def v_session_summary(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        keys = (
            "session_id",
            "agentic_workflow_id",
            "title",
            "status",
            "tenant_id",
            "created_at",
            "updated_at",
            "last_message_at",
            "owner_sub",
        )
        return [{k: r.get(k) for k in keys} for r in self.t(SESSIONS)]

    def v_message(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        rows = []
        for row in self.t(MESSAGES):
            owner = (self.session(row["session_id"]) or {}).get("owner_sub")
            rating = next(
                (
                    f["rating"]
                    for f in self.t(FEEDBACK)
                    if f["message_id"] == row["message_id"] and f["owner_sub"] == owner
                ),
                None,
            )
            rows.append({**row, "feedback_rating": rating, "owner_sub": owner})
        return rows

    def v_run(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        rows = []
        for row in self.t(RUNS):
            pending = [
                i["interrupt_id"]
                for i in self.t(INTERRUPTS)
                if i["run_id"] == row["run_id"] and i["status"] == "pending"
            ]
            checkpoint = next((c for c in self.t(CHECKPOINTS) if c["run_id"] == row["run_id"]), None)
            rows.append(
                {
                    **{k: v for k, v in row.items() if k not in ("finish_hash",)},
                    "pending_interrupts": pending,
                    "has_checkpoint": checkpoint is not None,
                    "last_event_seq": int(
                        (checkpoint or {}).get("body", {}).get("lastEventSeq", row.get("last_event_seq", 0))
                        or 0
                    ),
                }
            )
        return rows

    def v_dataset(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        return [dict(r) for r in self.t(DATASETS)]

    def v_interrupt_pending(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        rows = []
        for row in self.t(INTERRUPTS):
            run = self.run(row["run_id"]) or {}
            if row["status"] == "pending":
                rows.append({**row, "session_id": run.get("session_id"), "owner_sub": run.get("owner_sub")})
        return rows

    def v_checkpoint(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        rows = []
        for row in self.t(CHECKPOINTS):
            run = self.run(row["run_id"]) or {}
            body = row.get("body") or {}
            rows.append(
                {
                    **row,
                    "owner_sub": run.get("owner_sub"),
                    "format": body.get("format"),
                    "langgraph_version": body.get("langgraphVersion"),
                    "last_event_seq": int(body.get("lastEventSeq") or 0),
                    "run_status": run.get("status"),
                    "run_attempt": run.get("attempt"),
                }
            )
        return rows

    def v_session_state(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        return [
            {**r, "owner_sub": (self.session(r["session_id"]) or {}).get("owner_sub")} for r in self.t(STATE)
        ]

    # -- helpers -------------------------------------------------------------------------

    def append_messages(self, session_id: str, run_id: str, messages: list[dict[str, Any]]) -> str | None:
        if not messages:
            return None
        session = self.session(session_id)
        assert session is not None
        first = session["next_seq"] + 1
        session["next_seq"] += len(messages)
        session["last_message_at"] = _now()
        last = None
        for index, item in enumerate(messages):
            message_id = item.get("messageId") or str(uuid.uuid4())
            if any(m["message_id"] == message_id for m in self.t(MESSAGES)):
                continue
            self.t(MESSAGES).append(
                {
                    "message_id": message_id,
                    "session_id": session_id,
                    "run_id": run_id,
                    "node_id": item.get("nodeId"),
                    "visit": item.get("visit"),
                    "seq": first + index,
                    "role": item.get("role"),
                    "kind": item.get("kind"),
                    "visibility": item.get("visibility", "chat"),
                    "thread": item.get("thread", "messages"),
                    "content_parts": item.get("contentParts", []),
                    "text_preview": str(item.get("textPreview", ""))[:2000],
                    "created_at": _now(),
                }
            )
            last = message_id
        return last

    def close(self, run_id: str) -> None:
        for row in self.t(INTERRUPTS):
            if row["run_id"] == run_id and row["status"] == "pending":
                row["status"] = "cancelled"
        self.g.tables[(SCHEMA, CHECKPOINTS)] = [c for c in self.t(CHECKPOINTS) if c["run_id"] != run_id]

    def new_session(
        self, owner: str, workflow_id: str, *, tenant: str = "", title: str = "", email: str = ""
    ) -> dict[str, Any]:
        stamp = _now()
        row = {
            "session_id": str(uuid.uuid4()),
            "owner_sub": owner,
            "owner_email": email,
            "tenant_id": tenant,
            "agentic_workflow_id": workflow_id,
            "title": title[:200],
            "status": "active",
            "next_seq": 0,
            "created_at": stamp,
            "updated_at": stamp,
            "last_message_at": stamp,
        }
        self.t(SESSIONS).append(row)
        return row

    # -- procedures ------------------------------------------------------------------------

    async def sp_session_create(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        session = _json(args[0]) or {}
        if not session.get("ownerSub") or not session.get("agenticWorkflowId"):
            raise fault("invalid_argument", 400)
        row = self.new_session(
            session["ownerSub"],
            session["agenticWorkflowId"],
            tenant=session.get("tenantId") or "",
            title=str(session.get("title") or ""),
            email=session.get("ownerEmail") or "",
        )
        return [{"o_session_id": row["session_id"]}]

    async def sp_session_update(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        session_id, owner, patch = args[0], args[1], _json(args[2]) or {}
        row = self.session(session_id)
        if row is None:
            raise fault("session_not_found")
        if row["owner_sub"] != owner:
            raise fault("owner_mismatch")
        if "status" in patch and patch["status"] not in ("active", "archived"):
            raise fault("invalid_argument", 400)
        if "title" in patch:
            row["title"] = str(patch["title"])[:200]
        if "status" in patch:
            row["status"] = patch["status"]
        row["updated_at"] = _now()
        return []

    async def sp_run_start(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        run = _json(args[0]) or {}
        user_message = _json(args[1])
        owner, tenant = run.get("ownerSub"), run.get("tenantId") or ""
        status, limits = run.get("status", "running"), run.get("limits") or {}
        if (
            not owner
            or not run.get("agenticWorkflowId")
            or not run.get("mode")
            or status not in ("running", "queued")
        ):
            raise fault("invalid_argument", 400)
        key = run.get("idempotencyKey")
        if key is not None:
            existing = next(
                (r for r in self.t(RUNS) if r["owner_sub"] == owner and r.get("idempotency_key") == key), None
            )
            if existing is not None:
                first = sorted(
                    (
                        m
                        for m in self.t(MESSAGES)
                        if m["run_id"] == existing["run_id"] and m["kind"] == "user_input"
                    ),
                    key=lambda m: m["seq"],
                )
                return [
                    {
                        "o_run_id": existing["run_id"],
                        "o_session_id": existing["session_id"],
                        "o_message_id": first[0]["message_id"] if first else None,
                    }
                ]
        if run.get("sessionId") is None:
            session = self.new_session(
                owner, run["agenticWorkflowId"], tenant=tenant, title=str(run.get("title") or "")
            )
        else:
            found = self.session(run["sessionId"])
            if found is None:
                raise fault("session_not_found")
            if found["owner_sub"] != owner:
                raise fault("owner_mismatch")
            if found["status"] == "archived":
                found["status"] = "active"
            session = found
        active = [r for r in self.t(RUNS) if r["status"] in ACTIVE]
        if sum(1 for r in active if r["session_id"] == session["session_id"]) >= limits.get(
            "maxActivePerSession", 1
        ):
            raise fault("run_in_progress")
        if (
            "maxActivePerUser" in limits
            and sum(1 for r in active if r["owner_sub"] == owner) >= limits["maxActivePerUser"]
        ):
            raise fault("run_limit_reached")
        if (
            "maxActivePerTenant" in limits
            and sum(1 for r in active if r["tenant_id"] == tenant) >= limits["maxActivePerTenant"]
        ):
            raise fault("run_limit_reached")
        run_id = run.get("runId") or str(uuid.uuid4())
        stamp = _now()
        self.t(RUNS).append(
            {
                "run_id": run_id,
                "session_id": session["session_id"],
                "owner_sub": owner,
                "tenant_id": tenant,
                "agentic_workflow_id": run["agenticWorkflowId"],
                "workflow_revision": run.get("workflowRevision", ""),
                "document_version": run.get("documentVersion", ""),
                "status": status,
                "mode": run["mode"],
                "input": run.get("input") or {},
                "result": None,
                "error": None,
                "usage": {},
                "node_timeline": [],
                "idempotency_key": key,
                "queued_at": stamp,
                "started_at": stamp if status == "running" else None,
                "finished_at": None,
                "updated_at": stamp,
                "attempt": 1 if status == "running" else 0,
                "last_event_seq": 0,
                "priority": int(run.get("priority") or 0),
                "callback": run.get("callback"),
                "lease_owner": run.get("leaseOwner") if status == "running" else None,
                "lease_address": run.get("leaseAddress") if status == "running" else None,
                "lease_expires_at": (
                    _later(run["leaseSeconds"]) if status == "running" and "leaseSeconds" in run else None
                ),
            }
        )
        message_id = None
        if isinstance(user_message, dict):
            message = {
                **user_message,
                "role": user_message.get("role", "user"),
                "kind": user_message.get("kind", "user_input"),
            }
            message_id = self.append_messages(session["session_id"], run_id, [message])
        return [{"o_run_id": run_id, "o_session_id": session["session_id"], "o_message_id": message_id}]

    async def sp_run_transition(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        run_id, to, patch = args[0], args[1], _json(args[2]) or {}
        run = self.run(run_id)
        if run is None:
            raise fault("run_not_found")
        if "ownerSub" in patch and run["owner_sub"] != patch["ownerSub"]:
            raise fault("owner_mismatch")
        if "attempt" in patch and run["attempt"] != patch["attempt"]:
            raise fault("stale_attempt")
        if run["status"] == to:
            return []
        if (run["status"], to) not in TRANSITIONS:
            raise fault("invalid_transition")
        run["status"] = to
        for key in ("result", "error"):
            if key in patch:
                run[key] = patch[key]
        if to in TERMINAL or to in ("queued", "waiting_input"):
            _unlease(run)
        elif "leaseOwner" in patch:
            run.update(lease_owner=patch["leaseOwner"], lease_address=patch.get("leaseAddress"))
            if "leaseSeconds" in patch:
                run["lease_expires_at"] = _later(patch["leaseSeconds"])
        if to == "queued":
            run["queued_at"] = _now()
        if to == "running":
            run["started_at"] = run["started_at"] or _now()
        if to in TERMINAL:
            run["finished_at"] = _now()
            self.close(run_id)
        if patch.get("newAttempt"):
            run["attempt"] += 1
        run["updated_at"] = _now()
        return []

    async def sp_run_finish(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        run_id, attempt, save = args[0], args[1], _json(args[2]) or {}
        digest = hashlib.md5(json.dumps(save, sort_keys=True).encode()).hexdigest()
        run = self.run(run_id)
        if run is None:
            raise fault("run_not_found")
        if run["attempt"] != attempt:
            raise fault("stale_attempt")
        if run["status"] not in ACTIVE:
            if run.get("finish_hash") == digest:
                return []
            raise fault("already_finished")
        status = save.get("status")
        if status not in TERMINAL:
            raise fault("invalid_argument", 400)
        if (run["status"], status) not in TRANSITIONS:
            raise fault("invalid_transition")
        state = save.get("sessionState")
        if isinstance(state, dict):
            current = next(
                (
                    r
                    for r in self.t(STATE)
                    if r["session_id"] == run["session_id"]
                    and r["agentic_workflow_id"] == run["agentic_workflow_id"]
                ),
                None,
            )
            expected = state.get("expectedVersion")
            if current is None:
                self.t(STATE).append(
                    {
                        "session_id": run["session_id"],
                        "agentic_workflow_id": run["agentic_workflow_id"],
                        "flow": state.get("flow") or {},
                        "version": 1,
                        "updated_at": _now(),
                    }
                )
            elif expected is None or current["version"] == expected:
                current.update(
                    flow=state.get("flow") or {}, version=current["version"] + 1, updated_at=_now()
                )
            else:
                raise fault("version_conflict")
        self.append_messages(run["session_id"], run_id, list(save.get("messages") or []))
        known = {(r["dataset_id"], r["chunk_no"]) for r in self.t(DATASETS)}
        for d in save.get("datasets") or []:
            if (d["datasetId"], 0) in known:
                continue
            self.t(DATASETS).append(
                {
                    "dataset_id": d["datasetId"],
                    "chunk_no": 0,
                    "run_id": run_id,
                    "session_id": run["session_id"],
                    "node_id": d.get("nodeId") or "",
                    "visit": d.get("visit") or 1,
                    "owner_sub": run["owner_sub"],
                    "store": d.get("store"),
                    "cache_key": d.get("cacheKey"),
                    "kind": d.get("kind"),
                    "content_type": d.get("contentType") or "application/json",
                    "row_count": d.get("rowCount"),
                    "columns": d.get("columns"),
                    "size_bytes": d.get("sizeBytes") or 0,
                    "sha256": d.get("sha256") or "",
                    "body": None,
                    "created_at": _now(),
                    "expires_at": d.get("expiresAt"),
                }
            )
        run.update(
            status=status,
            result=save.get("result"),
            error=save.get("error"),
            finished_at=_now(),
            updated_at=_now(),
            finish_hash=digest,
        )
        _unlease(run)
        for key, column in (
            ("usage", "usage"),
            ("nodeTimeline", "node_timeline"),
            ("lastEventSeq", "last_event_seq"),
        ):
            if key in save:
                run[column] = save[key]
        self.close(run_id)
        return []

    async def sp_feedback_save(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        feedback = _json(args[0]) or {}
        message = next((m for m in self.t(MESSAGES) if m["message_id"] == feedback.get("messageId")), None)
        if message is None:
            raise fault("not_found")
        session = self.session(message["session_id"])
        if session is None or session["owner_sub"] != feedback.get("ownerSub"):
            raise fault("owner_mismatch")
        rows = self.t(FEEDBACK)
        existing = next(
            (
                r
                for r in rows
                if r["message_id"] == feedback["messageId"] and r["owner_sub"] == feedback["ownerSub"]
            ),
            None,
        )
        if existing is not None:
            existing.update(
                rating=feedback.get("rating"), comment=feedback.get("comment") or "", updated_at=_now()
            )
        else:
            rows.append(
                {
                    "feedback_id": str(uuid.uuid4()),
                    "message_id": feedback["messageId"],
                    "owner_sub": feedback["ownerSub"],
                    "rating": feedback.get("rating"),
                    "comment": feedback.get("comment") or "",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            )
        return []

    async def sp_dataset_save(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        run_id, attempt, chunks = args[0], args[1], _json(args[2]) or []
        run = self.run(run_id)
        if run is None:
            raise fault("run_not_found")
        if run["attempt"] != attempt:
            raise fault("stale_attempt")
        rows = self.t(DATASETS)
        for c in chunks:
            if int(c.get("chunkNo") or 0) < 1:
                continue
            key = (c["datasetId"], int(c["chunkNo"]))
            existing = next((r for r in rows if (r["dataset_id"], r["chunk_no"]) == key), None)
            if existing is not None:
                existing["body"] = c.get("body")
                continue
            rows.append(
                {
                    "dataset_id": c["datasetId"],
                    "chunk_no": int(c["chunkNo"]),
                    "run_id": run_id,
                    "session_id": run["session_id"],
                    "node_id": c.get("nodeId") or "",
                    "visit": c.get("visit") or 1,
                    "owner_sub": run["owner_sub"],
                    "store": "database",
                    "cache_key": None,
                    "kind": c.get("kind") or "rows",
                    "content_type": c.get("contentType") or "application/json",
                    "row_count": None,
                    "columns": None,
                    "size_bytes": 0,
                    "sha256": "",
                    "body": c.get("body"),
                    "created_at": _now(),
                    "expires_at": c.get("expiresAt"),
                }
            )
        return []

    async def sp_interrupt_create(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        """``005_procedures.sql``: the pause save, idempotent per (run, attempt, first interrupt)."""
        interrupt, checkpoint, messages, attempt = (_json(a) for a in args[:4])
        items = interrupt.get("interrupts", [interrupt])
        run_id = interrupt.get("runId") or (items[0] if items else {}).get("runId")
        if not run_id or not items:
            raise fault("invalid_argument", 400)
        run = self.run(run_id)
        if run is None:
            raise fault("run_not_found")
        if run["attempt"] != attempt:
            raise fault("stale_attempt")
        first = items[0].get("interruptId")
        if first and any(i["interrupt_id"] == first for i in self.t(INTERRUPTS)):
            return [{"o_interrupt_id": first}]
        if (run["status"], "waiting_input") not in TRANSITIONS:
            raise fault("invalid_transition")
        for item in items:
            self.t(INTERRUPTS).append(
                {
                    "interrupt_id": item.get("interruptId") or str(uuid.uuid4()),
                    "run_id": run_id,
                    "node_id": item.get("nodeId"),
                    "kind": item.get("kind"),
                    "prompt": item.get("prompt") or {},
                    "answer_schema": item.get("answerSchema") or {},
                    "ui_schema": item.get("uiSchema") or {},
                    "answer": None,
                    "status": "pending",
                    "attempt": attempt,
                    "requested_at": _now(),
                    "answered_at": None,
                    "answered_by": None,
                    "expires_at": item.get("expiresAt"),
                }
            )
        body = checkpoint.get("body", checkpoint)
        rows = [c for c in self.t(CHECKPOINTS) if c["run_id"] != run_id]
        rows.append(
            {
                "run_id": run_id,
                "attempt": attempt,
                "kind": "pause",
                "body": body,
                "created_at": _now(),
                "expires_at": checkpoint.get("expiresAt"),
            }
        )
        self.g.tables[(SCHEMA, CHECKPOINTS)] = rows
        self.append_messages(run["session_id"], run_id, messages or [])
        run["status"] = "waiting_input"
        run["updated_at"] = _now()
        return [{"o_interrupt_id": first}]

    async def sp_interrupt_answer(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        interrupt_id, owner, answer = args[0], args[1], _json(args[2])
        row = next((i for i in self.t(INTERRUPTS) if i["interrupt_id"] == interrupt_id), None)
        if row is None:
            raise fault("interrupt_not_found")
        run = self.run(row["run_id"]) or {}
        if run.get("owner_sub") != owner:
            raise fault("owner_mismatch")
        if row["status"] == "answered":
            raise fault("already_answered")
        if row["status"] == "expired" or (row.get("expires_at") and str(row["expires_at"]) < _now()):
            raise fault("interrupt_expired")
        if row["status"] != "pending":
            raise fault("interrupt_closed")
        row.update(status="answered", answer=answer, answered_at=_now(), answered_by=owner)
        return []

    async def sp_run_claim(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        """``005_procedures.sql``: queued runs (round-robin across tenants, then priority) and running
        runs whose lease expired, leased to the instance; each claim increments the attempt."""
        instance, limit, lease_seconds = _json(args[0]) or {}, int(args[1]), int(args[2])
        now = _now()
        found = [
            r
            for r in self.t(RUNS)
            if r["status"] == "queued"
            or (r["status"] == "running" and r.get("lease_expires_at") and str(r["lease_expires_at"]) < now)
        ]
        ranks: dict[str, int] = {}
        ranked = []
        for run in sorted(found, key=lambda r: (-int(r.get("priority") or 0), str(r["queued_at"]))):
            ranks[run["tenant_id"]] = ranks.get(run["tenant_id"], 0) + 1
            ranked.append((ranks[run["tenant_id"]], run))
        picked = [
            r
            for _, r in sorted(
                ranked, key=lambda x: (x[0], -int(x[1].get("priority") or 0), str(x[1]["queued_at"]))
            )
        ][:limit]
        claimed = []
        for run in picked:
            previous = run["status"]
            run.update(
                status="running",
                lease_owner=instance.get("instanceId"),
                lease_address=instance.get("address"),
                lease_expires_at=_later(lease_seconds),
                attempt=run["attempt"] + 1,
                started_at=run["started_at"] or now,
                updated_at=now,
            )
            claimed.append(
                {
                    "runId": run["run_id"],
                    "attempt": run["attempt"],
                    "mode": run["mode"],
                    "sessionId": run["session_id"],
                    "previousStatus": previous,
                }
            )
        return [{"o_runs": json.dumps(claimed)}]

    async def sp_run_heartbeat(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        instance_id, run_ids, lease_seconds = args[0], _json(args[1]) or [], int(args[2])
        lost = []
        for run_id in run_ids:
            run = self.run(run_id)
            if run is not None and run.get("lease_owner") == instance_id and run["status"] == "running":
                run["lease_expires_at"] = _later(lease_seconds)
            else:
                lost.append(run_id)
        return [{"o_lost": json.dumps(lost)}]

    async def sp_run_release(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        instance_id, run_ids = args[0], _json(args[1]) or []
        for run_id in run_ids:
            run = self.run(run_id)
            if run is None or run.get("lease_owner") != instance_id or run["status"] != "running":
                continue
            _unlease(run)
            if run["mode"] == "async":
                run.update(status="queued", queued_at=_now())
            else:
                run.update(status="failed", error={"code": "SHUTDOWN", "retryable": True}, finished_at=_now())
        return []

    async def sp_checkpoint_save(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        run_id, attempt, body = args[0], int(args[1]), _json(args[2]) or {}
        run = self.run(run_id)
        if run is None:
            raise fault("run_not_found")
        if run["attempt"] != attempt:
            raise fault("stale_attempt")
        if run["status"] != "running":
            raise fault("invalid_transition")
        rows = [c for c in self.t(CHECKPOINTS) if c["run_id"] != run_id]
        rows.append(
            {
                "run_id": run_id,
                "attempt": attempt,
                "kind": "async",
                "body": body,
                "created_at": _now(),
                "expires_at": None,
            }
        )
        self.g.tables[(SCHEMA, CHECKPOINTS)] = rows
        return []

    async def sp_retention_purge(self, _: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        before, now = str(args[0]), _now()
        for name in (CHECKPOINTS, DATASETS):
            rows = self.t(name)
            rows[:] = [r for r in rows if not (r.get("expires_at") and str(r["expires_at"]) < now)]
        for session in self.t(SESSIONS):
            if session.get("status") == "active" and str(session.get("last_message_at") or "") < before:
                session["status"] = "archived"
        return []


def runtime_gateway(gateway: InMemoryGateway | None = None) -> tuple[InMemoryGateway, RuntimeSchema]:
    fake = gateway or InMemoryGateway()
    return fake, RuntimeSchema(fake)
