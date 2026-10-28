"""Three-level sub-flow documents for tests (P3-09): parent -> child -> grandchild (an LLM).

IDs are UUIDs, as the catalog's are. The child declares ``topic`` only, so with
``subflow.inheritFlow: declared`` it sees ``flow.topic`` but not ``flow.secret``.
"""

from __future__ import annotations

import uuid
from typing import Any

PARENT, CHILD, GRANDCHILD = (
    str(uuid.uuid5(uuid.NAMESPACE_URL, f"zonic-test:{n}")) for n in ("parent", "child", "grand")
)
USER = [{"role": "user", "content": "Topic {{nodes.start.output.topic}}: {{system.userQuery}}"}]


def _doc(
    workflow_id: str, nodes: list[dict[str, Any]], edges: list[tuple[str, str]], **extra: Any
) -> dict[str, Any]:
    return {
        "schemaVersion": "2.0",
        "agenticWorkflowId": workflow_id,
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "name": "Start",
                "config": {"interfaceInputs": [{"name": "topic"}]},
            },
            *nodes,
        ],
        "edges": [{"source": a, "target": b} for a, b in edges],
        **extra,
    }


def subflow(node_id: str, child: str, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "workflow",
        "name": node_id,
        "config": {
            "agenticWorkflowId": child,
            "inputs": [{"key": "topic", "value": "{{flow.topic}}"}],
            **config,
        },
    }


def output(value: str, **more: str) -> dict[str, Any]:
    mapping = [{"target": "answer", "value": value}, *({"target": k, "value": v} for k, v in more.items())]
    return {"id": "out", "type": "output", "name": "Out", "config": {"outputMapping": mapping}}


def grandchild() -> dict[str, Any]:
    llm = {"id": "llm", "type": "llm", "name": "llm", "config": {"messages": USER}}
    return _doc(GRANDCHILD, [llm, output("{{nodes.llm.output.text}}")], [("start", "llm"), ("llm", "out")])


def child(**output_extra: str) -> dict[str, Any]:
    nodes = [subflow("grand", GRANDCHILD), output("{{nodes.grand.output.answer}}", **output_extra)]
    variables = {"topic": {"name": "topic", "type": "string"}}
    return _doc(CHILD, nodes, [("start", "grand"), ("grand", "out")], variables=variables)


def parent(**output_extra: str) -> dict[str, Any]:
    setter = {
        "id": "set",
        "type": "variable",
        "name": "set",
        "config": {
            "updates": [
                {"path": "flow.topic", "op": "set", "value": "boilers"},
                {"path": "flow.secret", "op": "set", "value": "s3cret"},
            ]
        },
    }
    nodes = [setter, subflow("child", CHILD), output("{{nodes.child.output.answer}}", **output_extra)]
    return _doc(PARENT, nodes, [("start", "set"), ("set", "child"), ("child", "out")])


def chain() -> dict[str, dict[str, Any]]:
    return {PARENT: parent(), CHILD: child(), GRANDCHILD: grandchild()}
