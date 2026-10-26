"""Write ``generated/`` from the Pydantic models; ``--check`` fails when a committed file differs.

``generated/`` is the published contract (design Section 3.6): one JSON Schema per
exported model, grouped by family, plus the OpenAPI document once the app exists.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated"


def _import_all_models() -> None:
    import zonic_runtime.models as models_pkg

    for info in pkgutil.walk_packages(models_pkg.__path__, prefix="zonic_runtime.models."):
        importlib.import_module(info.name)


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def expected_files() -> dict[Path, str]:
    _import_all_models()
    from zonic_runtime.models.registry import exported_models

    files: dict[Path, str] = {}
    index: dict[str, list[str]] = {}
    for (group, name), model in exported_models().items():
        schema = model.model_json_schema(by_alias=True, mode="serialization")
        files[GENERATED / "schemas" / group / f"{name}.schema.json"] = _dump(schema)
        index.setdefault(group, []).append(name)
    files[GENERATED / "schemas" / "index.json"] = _dump(index)

    try:
        from zonic_runtime.main import create_openapi  # available from task P2-03
    except ImportError:
        create_openapi = None
    if create_openapi is not None:
        files[GENERATED / "openapi.json"] = _dump(create_openapi())
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when generated/ is not current")
    args = parser.parse_args(argv)

    expected = expected_files()
    present = {p for p in GENERATED.rglob("*.json")} if GENERATED.exists() else set()
    stale = sorted(present - set(expected))
    changed = sorted(
        p for p, text in expected.items() if not p.exists() or p.read_text(encoding="utf-8") != text
    )

    if args.check:
        for path in changed:
            print(f"out of date: {path.relative_to(ROOT)}")
        for path in stale:
            print(f"stale: {path.relative_to(ROOT)}")
        if changed or stale:
            print("run: uv run python -m scripts.gen")
            return 1
        print(f"generated/ is current ({len(expected)} files)")
        return 0

    for path in stale:
        path.unlink()
    for path, text in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(expected)} files, removed {len(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
