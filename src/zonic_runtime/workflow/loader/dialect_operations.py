"""The normalizer's operations (design 5.2), ported from the Node.js runtime's ``dialect-operations.ts``.

Each operation is generic: ``resources/dialects.json`` says which construct it rewrites,
with which field names, and what note it records. An operation returns one ``Firing``
per rewrite it made (none means the rule did not fire); a firing's values fill the
rule's note template, and a firing may carry a finding code and severity of its own
(an empty model defaulted, an unsupported rule expression).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from zonic_runtime.models.resources.misc import DialectRules
from zonic_runtime.runtime.paths import JsonObject, delete_path, get_path, json_type, set_path

Severity = Literal["error", "warning", "info"]
RuleExpressionMode = Literal["unsupported", "parse_simple"]


@dataclass(frozen=True)
class Firing:
    values: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    severity: Severity | None = None


@dataclass(frozen=True)
class OperationContext:
    document: JsonObject
    dialects: DialectRules
    default_model: str | None = None
    rule_expression: RuleExpressionMode = "unsupported"

    def operator(self, value: Any) -> Any:
        """A condition operator spelling -> the builder's canonical operator."""
        if not isinstance(value, str):
            return value
        aliases = self.dialects.operator_aliases
        return aliases.get(value.strip().lower(), aliases.get(value, value))


Operation = Callable[[JsonObject, dict[str, Any], OperationContext], list[Firing]]


def _list_at(target: JsonObject, path: str) -> list[Any] | None:
    value = get_path(target, path)
    return value if isinstance(value, list) else None


def _once(changed: bool, **values: Any) -> list[Firing]:
    return [Firing(values)] if changed else []


def _move(target: JsonObject, source: str, dest: str, drop_parent: bool = False) -> bool:
    value = get_path(target, source)
    if value is None and not _has(target, source):
        return False
    delete_path(target, source)
    set_path(target, dest, value)
    if drop_parent:
        parent = ".".join(source.split(".")[:-1])
        if parent and get_path(target, parent) == {}:
            delete_path(target, parent)
    return True


def _has(target: JsonObject, path: str) -> bool:
    sentinel = object()
    return get_path(target, path, sentinel) is not sentinel


def _is_tool_ref(item: Any) -> bool:
    return isinstance(item, dict) and isinstance(item.get("toolType"), str)


def variables_from_list(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    items = _list_at(target, args["from"])
    if items is None:
        return []
    name_field, fields = args["nameField"], args.get("fields", [])
    variables: JsonObject = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get(name_field), str) and item[name_field]:
            variables[item[name_field]] = {f: item[f] for f in fields if f in item}
    set_path(target, args["to"], variables)
    return [Firing({"count": len(variables)})]


def delete_if_empty(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path = args["path"]
    if not _has(target, path) or get_path(target, path) not in args.get("empty", []):
        return []
    delete_path(target, path)
    return [] if args.get("silent") else [Firing()]


def set_value(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    """Sets ``value`` at ``path`` unless the document already has one there."""
    path = args["path"]
    if _has(target, path):
        return []
    set_path(target, path, args.get("value"))
    return [Firing({"value": args.get("value")})]


def replace_value(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path = args["path"]
    if not _has(target, path):
        return []
    value = get_path(target, path)
    key = json.dumps(value) if not isinstance(value, str) else value
    mapping = args.get("map", {})
    if key not in mapping:
        return []
    if mapping[key] is None:
        delete_path(target, path)
    else:
        set_path(target, path, mapping[key])
    return [Firing({"value": value})]


def append_list(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    items = _list_at(target, args["from"])
    if not items:
        return []
    set_path(target, args["to"], [*(_list_at(target, args["to"]) or []), *items])
    delete_path(target, args["from"])
    return [Firing({"count": len(items)})]


def rename_keys_in_list(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    count = 0
    for path in args.get("lists", []):
        for item in _list_at(target, path) or []:
            if not isinstance(item, dict):
                continue
            for old, new in args.get("rename", {}).items():
                if old in item:
                    item.setdefault(new, item[old])
                    del item[old]
                    count += 1
    return _once(count > 0, count=count)


def _condition_group(raw: JsonObject, logic_key: str, operator_key: str, ctx: OperationContext) -> JsonObject:
    conditions = []
    for condition in raw.get("conditions") or []:
        if isinstance(condition, dict):
            rest = {k: v for k, v in condition.items() if k not in {operator_key, "op", "logic"}}
            conditions.append({**rest, "op": ctx.operator(condition.get(operator_key, condition.get("op")))})
    logic = raw.get(logic_key, raw.get("conditionLogic", "AND"))
    return {"conditionLogic": str(logic).upper(), "conditions": conditions}


def condition_groups_in_list(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    count = 0
    for path in args.get("lists", []):
        for item in _list_at(target, path) or []:
            if isinstance(item, dict) and isinstance(item.get(args["from"]), dict):
                item[args["to"]] = _condition_group(
                    item.pop(args["from"]), args["logicKey"], args["operatorKey"], ctx
                )
                count += 1
    return _once(count > 0, count=count)


def model_ref(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path = args["path"]
    value = get_path(target, path)
    if isinstance(value, str):
        model_id = value.strip()
        if not model_id and ctx.default_model:
            set_path(target, path, {"modelId": ctx.default_model})
            return [Firing({"modelId": ctx.default_model}, code=args.get("emptyCode"), severity="warning")]
        set_path(target, path, {"modelId": model_id})
        return [Firing({"modelId": model_id})]
    if not isinstance(value, dict) or isinstance(value.get("modelId"), str):
        return []
    ref = {new: value[old] for old, new in args.get("fields", {}).items() if value.get(old) is not None}
    set_path(target, path, {"modelId": "", **ref})
    return [Firing({"modelId": ref.get("modelId", "")})]


def prompt_template(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    entries = [e for e in _list_at(target, args["from"]) or [] if isinstance(e, dict)]
    delete_path(target, args["from"])
    system_to = args.get("systemTo")
    system = [str(e.get("text", "")) for e in entries if e.get("role") == "system"]
    others = [
        {"role": str(e.get("role", "user")), "content": str(e.get("text", ""))}
        for e in entries
        if system_to is None or e.get("role") != "system"
    ]
    if system_to and system:
        set_path(target, system_to, "\n\n".join(system))
    set_path(target, args["othersTo"], [*(_list_at(target, args["othersTo"]) or []), *others])
    return [Firing({"count": len(entries)})]


def rename_field(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    return _once(_move(target, args["from"], args["to"], bool(args.get("dropParent"))))


def rename_fields(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    count = sum(_move(target, old, new, True) for old, new in args.get("fields", {}).items())
    return _once(count > 0, count=count)


def structured_output(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path, enable = args["path"], args["enableField"]
    value = get_path(target, path)
    if not isinstance(value, dict) or enable not in value:
        return []
    enabled = value[enable] is True
    if enabled:
        del value[enable]
    else:
        delete_path(target, path)
    return [Firing({"enabled": enabled})]


def routing_tools(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    tools = _list_at(target, args["toolsPath"])
    if tools is None:
        return []
    id_field, tool_id_path = args["idField"], args["edgeToolIdPath"]
    by_tool_id = {
        get_path(edge, tool_id_path): edge
        for edge in ctx.document.get("edges") or []
        if isinstance(edge, dict)
        and edge.get("source") == target.get("id")
        and edge.get("type") == args["edgeType"]
        and isinstance(get_path(edge, tool_id_path), str)
    }
    routing = [r for r in _list_at(target, args["to"]) or [] if isinstance(r, dict)]
    firings: list[Firing] = []
    remaining = []
    for tool in tools:
        edge = by_tool_id.get(tool.get(id_field)) if isinstance(tool, dict) else None
        if edge is None:
            remaining.append(tool)
            continue
        config = tool.get("config") if isinstance(tool.get("config"), dict) else {}
        entry: JsonObject = {
            "id": tool[id_field],
            "name": str(tool.get("name", "")),
            "target": str(edge["target"]),
        }
        if tool.get("alias"):
            entry["alias"] = tool["alias"]
        for key in ("title", "description"):
            if isinstance(tool.get(key), str):
                entry[key] = tool[key]
        if isinstance(config.get("schema"), dict):
            entry["schema"] = config["schema"]
        if isinstance(config.get("parameters"), dict) and config["parameters"]:
            entry["parameters"] = config["parameters"]
        if config.get("enabled") is False:
            entry["enabled"] = False
        if config.get("returnDirect") is True:
            entry["returnDirect"] = True
        routing.append(entry)
        edge["sourceHandle"] = f"{args['handlePrefix']}{edge['target']}"
        firings.append(Firing({"name": tool.get("alias") or tool.get("name"), "target": edge["target"]}))
    if firings:
        set_path(target, args["toolsPath"], remaining)
        set_path(target, args["to"], routing)
    return firings


def platform_tool_refs(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path = args["toolsPath"]
    tools = _list_at(target, path)
    if tools is None:
        return []
    id_field, config_key, type_key, server_path = (
        args["idField"],
        args["configKey"],
        args["typeKey"],
        args["serverIdPath"],
    )
    firings: list[Firing] = []
    converted = []
    for tool in tools:
        config = tool.get(config_key) if isinstance(tool, dict) else None
        if _is_tool_ref(tool) or not isinstance(config, dict) or not isinstance(tool.get(id_field), str):
            converted.append(tool)
            continue
        settings = {k: v for k, v in config.items() if k not in {type_key, "toolId", "schema", "enabled"}}
        if not isinstance(config.get(type_key), str):
            converted.append(tool)
            continue
        server_id = get_path(settings, server_path)
        delete_path(settings, server_path)
        if settings.get("settings") == {}:
            del settings["settings"]
        if isinstance(tool.get("alias"), str):
            settings["alias"] = tool["alias"]
        ref: JsonObject = {
            "toolType": config[type_key],
            "name": str(tool.get("name", "")),
            "id": tool[id_field],
        }
        for key in ("title", "description"):
            if isinstance(tool.get(key), str):
                ref[key] = tool[key]
        if isinstance(config.get("toolId"), str):
            ref["toolId"] = config["toolId"]
        if isinstance(server_id, str):
            ref["serverId"] = server_id
        if isinstance(config.get("schema"), dict):
            ref["schema"] = config["schema"]
        ref["enabled"] = config.get("enabled") is not False
        ref["settings"] = settings
        converted.append(ref)
        firings.append(Firing({"name": tool.get("name")}))
    if firings:
        set_path(target, path, converted)
    return firings


def expand_configured_tools(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path = args["toolsPath"]
    tools = _list_at(target, path)
    if tools is None:
        return []
    container, list_key, type_key = args["containerKey"], args["listKey"], args["typeKey"]
    firings: list[Firing] = []
    expanded: list[Any] = []
    for tool in tools:
        if _is_tool_ref(tool) or not isinstance(tool, dict) or not isinstance(tool.get(container), dict):
            expanded.append(tool)
            continue
        server = {k: v for k, v in tool[container].items() if k != list_key}
        for item in tool[container].get(list_key) or []:
            if not isinstance(item, dict):
                continue
            rest = {
                k: v for k, v in item.items() if k not in {type_key, "name", "description", "enabled", "id"}
            }
            ref: JsonObject = {
                "toolType": str(item.get(type_key, container)),
                "name": str(item.get("name", "")),
            }
            if isinstance(item.get("description"), str):
                ref["description"] = item["description"]
            if isinstance(item.get("id"), str):
                ref["id"] = item["id"]
            ref["enabled"] = item.get("enabled") is not False
            ref["settings"] = {**rest, "server": server}
            expanded.append(ref)
            firings.append(Firing({"name": item.get("name")}))
    if firings or len(expanded) != len(tools):
        set_path(target, path, expanded)
    return firings


def typed_tool_refs(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path = args["toolsPath"]
    tools = _list_at(target, path)
    if tools is None:
        return []
    type_key, settings_key = args["typeKey"], args["settingsKey"]
    firings: list[Firing] = []
    converted = []
    for tool in tools:
        if _is_tool_ref(tool) or not isinstance(tool, dict) or not isinstance(tool.get(type_key), str):
            converted.append(tool)
            continue
        ref: JsonObject = {"toolType": tool[type_key], "name": str(tool.get("name", ""))}
        for key in ("title", "description"):
            if isinstance(tool.get(key), str):
                ref[key] = tool[key]
        ref["enabled"] = tool.get("enabled") is not False
        ref["settings"] = tool[settings_key] if isinstance(tool.get(settings_key), dict) else {}
        converted.append(ref)
        firings.append(Firing({"type": tool[type_key], "name": tool.get("name")}))
    if firings:
        set_path(target, path, converted)
    return firings


def rule_branches(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    operator_key, logic_key, default_logic = (
        args["operatorKey"],
        args.get("logicKey"),
        args.get("defaultLogicValue"),
    )
    count = 0
    for rule in _list_at(target, args["path"]) or []:
        if not isinstance(rule, dict):
            continue
        if logic_key and default_logic is not None and rule.get(logic_key) == default_logic:
            rule["isDefault"] = True
            rule[logic_key] = "AND"
        if logic_key and isinstance(rule.get(logic_key), str):
            rule[logic_key] = rule[logic_key].upper()
        for old, new in args.get("rename", {}).items():
            if old in rule:
                rule[new] = rule.pop(old)
                count += 1
        for condition in rule.get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            for key in args.get("dropConditionKeys", []):
                if condition.pop(key, None) is not None:
                    count += 1
            if operator_key not in condition:
                continue
            canonical = ctx.operator(condition.pop(operator_key))
            if operator_key != "op" or canonical != condition.get("op"):
                count += 1
            condition["op"] = canonical
    return _once(count > 0, count=count)


_LITERAL = r"'(?:[^']*)'|\"(?:[^\"]*)\"|-?\d+(?:\.\d+)?|true|false|null"


def _parse_simple(expression: str, ctx: OperationContext) -> JsonObject | None:
    """``{{path}} <operator> <literal>`` -> one condition, or ``None`` (design 5.2, D-12)."""
    spellings = sorted(
        {*ctx.dialects.operator_aliases, *ctx.dialects.canonical_operators}, key=len, reverse=True
    )
    operators = "|".join(re.escape(s) for s in spellings)
    match = re.fullmatch(
        rf"\s*(\{{\{{[^{{}}]+\}}\}})\s*({operators})\s*({_LITERAL})\s*", expression, re.IGNORECASE
    )
    if match is None:
        return None
    field_, spelling, literal = match.groups()
    value: Any = literal[1:-1] if literal[0] in "'\"" else json.loads(literal.lower())
    return {"field": field_, "op": ctx.operator(spelling), "value": value}


def rule_expressions(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    firings: list[Firing] = []
    for rule in _list_at(target, args["path"]) or []:
        if not isinstance(rule, dict) or not rule.get("expression") or rule.get("conditions"):
            continue
        condition = (
            _parse_simple(str(rule["expression"]), ctx) if ctx.rule_expression == "parse_simple" else None
        )
        if condition is None:
            values = {"ruleId": rule.get("ruleId"), "expression": rule["expression"]}
            firings.append(Firing(values, code=args["unsupportedCode"], severity="error"))
            continue
        rule["conditions"] = [condition]
        del rule["expression"]
        firings.append(Firing({"ruleId": rule.get("ruleId"), "expression": json.dumps(condition)}))
    return firings


def output_mapping(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    path = args["path"]
    value = get_path(target, path)
    if isinstance(value, list):
        count = 0
        mapped = []
        for item in value:
            shape = next(
                (
                    s
                    for s in args.get("listShapes", [])
                    if isinstance(item, dict) and s["target"] in item and s["value"] in item
                ),
                None,
            )
            if shape is None:
                mapped.append(item)
                continue
            rest = {k: v for k, v in item.items() if k not in {shape["target"], shape["value"]}}
            mapped.append({**rest, "target": item[shape["target"]], "value": item[shape["value"]]})
            count += 1
        if count:
            set_path(target, path, mapped)
        return _once(count > 0, count=count)
    shape = args.get("objectShape")
    if isinstance(value, dict) and isinstance(shape, dict):
        entries = []
        for name, item in value.items():
            entry: JsonObject = {
                "target": name,
                "value": item.get(shape["value"]) if isinstance(item, dict) else item,
            }
            if isinstance(item, dict) and isinstance(item.get(shape["type"]), str):
                entry["type"] = item[shape["type"]]
            entries.append(entry)
        set_path(target, path, entries)
        return [Firing({"count": len(entries)})]
    return []


def mapping_list(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    value = get_path(target, args["path"])
    if not isinstance(value, dict):
        return []
    set_path(target, args["path"], [{"key": k, "value": v} for k, v in value.items()])
    return [Firing({"count": len(value)})]


def script_inputs(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    value = get_path(target, args["path"])
    if not isinstance(value, dict):
        return []
    values, schema = value.get(args["valuesKey"]), value.get(args["schemaKey"])
    items = [{"key": k, "value": v} for k, v in values.items()] if isinstance(values, dict) else []
    set_path(target, args["path"], items)
    if isinstance(schema, dict):
        set_path(target, args["schemaTo"], schema)
    return [Firing({"type": json_type(values)})]


def require_field(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    """A field the runtime needs is missing or empty: a finding with the rule's code."""
    value = get_path(target, args["path"])
    if value not in (None, "", [], {}):
        return []
    return [Firing({"path": args["path"]}, code=args["code"], severity=args.get("severity", "warning"))]


def allowed_values(target: JsonObject, args: dict[str, Any], ctx: OperationContext) -> list[Firing]:
    """A field holds a value outside a list in ``dialects.json`` (``allowedFrom``): an error."""
    value = get_path(target, args["path"])
    allowed = getattr(ctx.dialects, args["allowedFrom"])
    if value in (None, "") or value in allowed:
        return []
    return [Firing({"value": value, "allowed": allowed}, code=args["code"], severity="error")]


OPERATIONS: Mapping[str, Operation] = MappingProxyType(
    {
        "variablesFromList": variables_from_list,
        "deleteIfEmpty": delete_if_empty,
        "setValue": set_value,
        "replaceValue": replace_value,
        "appendList": append_list,
        "renameKeysInList": rename_keys_in_list,
        "conditionGroupsInList": condition_groups_in_list,
        "modelRef": model_ref,
        "promptTemplate": prompt_template,
        "renameField": rename_field,
        "renameFields": rename_fields,
        "structuredOutput": structured_output,
        "routingTools": routing_tools,
        "platformToolRefs": platform_tool_refs,
        "expandConfiguredTools": expand_configured_tools,
        "typedToolRefs": typed_tool_refs,
        "ruleBranches": rule_branches,
        "ruleExpressions": rule_expressions,
        "outputMapping": output_mapping,
        "mappingList": mapping_list,
        "scriptInputs": script_inputs,
        "requireField": require_field,
        "allowedValues": allowed_values,
    }
)
