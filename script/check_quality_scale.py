#!/usr/bin/env python3
"""Check quality_scale.yaml against Home Assistant core's quality scale rules.

Validates that every rule in Home Assistant's Integration Quality Scale (Bronze,
Silver, Gold, Platinum) is listed in core's canonical order, that no rule is
marked todo, that every exempt rule has a justification comment, that programmatic
rules pass integration validation, and that manifest.json declares the platinum tier.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The 54 canonical rules in Home Assistant core order:
CANONICAL_RULES = [
    # Bronze
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow",
    "config-flow-test-coverage",
    "dependency-transparency",
    "docs-actions",
    "docs-conditions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "docs-triggers",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
    # Silver
    "action-exceptions",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "entity-unavailable",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "test-coverage",
    # Gold
    "devices",
    "diagnostics",
    "discovery",
    "discovery-update-info",
    "docs-data-update",
    "docs-examples",
    "docs-known-limitations",
    "docs-supported-devices",
    "docs-supported-functions",
    "docs-troubleshooting",
    "docs-use-cases",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-translations",
    "exception-translations",
    "icon-translations",
    "reconfiguration-flow",
    "repair-issues",
    "stale-devices",
    # Platinum
    "async-dependency",
    "inject-websession",
    "strict-typing",
]


def load_yaml(file_path: Path) -> dict:
    """Load YAML file using pyyaml or robust fallback."""
    try:
        import yaml

        with open(file_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        rules: dict[str, object] = {}
        curr_rule = None
        with open(file_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or line == "rules:":
                    continue
                if line.startswith("status:"):
                    val = line.split(":", 1)[1].strip()
                    if curr_rule and isinstance(rules.get(curr_rule), dict):
                        rules[curr_rule]["status"] = val  # type: ignore[index]
                elif line.startswith("comment:"):
                    val = line.split(":", 1)[1].strip().strip('">')
                    if curr_rule and isinstance(rules.get(curr_rule), dict):
                        rules[curr_rule]["comment"] = val  # type: ignore[index]
                elif ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip()
                    val = val.strip()
                    if val:
                        rules[key] = val
                        curr_rule = None
                    else:
                        rules[key] = {}
                        curr_rule = key
        return {"rules": rules}


def main() -> int:
    """Validate quality scale definition."""
    repo_root = Path(__file__).resolve().parents[1]
    manifests = list((repo_root / "custom_components").glob("*/manifest.json"))
    if not manifests:
        print("Error: No manifest.json found in custom_components/*", file=sys.stderr)
        return 1

    manifest_path = manifests[0]
    integration_dir = manifest_path.parent
    qs_path = integration_dir / "quality_scale.yaml"

    if not qs_path.is_file():
        print(f"Error: {qs_path} not found!", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as err:
        print(f"Error reading manifest: {err}", file=sys.stderr)
        return 1

    document = load_yaml(qs_path)
    rules = document.get("rules")
    if not isinstance(rules, dict):
        print(f"Error: {qs_path.name} missing 'rules' dictionary", file=sys.stderr)
        return 1

    errors: list[str] = []

    # 1. Check all canonical rules are present
    for r in CANONICAL_RULES:
        if r not in rules:
            errors.append(f"Missing required rule: {r}")

    # 2. Check no unknown rules
    for r in rules:
        if r not in CANONICAL_RULES:
            errors.append(f"Unknown rule in quality scale: {r}")

    # 3. Check rule order
    listed_rules = [r for r in rules if r in CANONICAL_RULES]
    expected_order = [r for r in CANONICAL_RULES if r in rules]
    for i, (l_rule, e_rule) in enumerate(
        zip(listed_rules, expected_order, strict=False)
    ):
        if l_rule != e_rule:
            errors.append(
                f"Rule out of order: {l_rule} found at index {i}, expected {e_rule}"
            )
            break

    # 4. Check statuses and comments
    done_count = 0
    exempt_count = 0
    for name, val in rules.items():
        if isinstance(val, dict):
            status = val.get("status")
            comment = val.get("comment")
            if not comment or not str(comment).strip():
                errors.append(
                    f"Rule '{name}' is exempt but missing a valid comment justification"
                )
        elif isinstance(val, str):
            status = val
        else:
            errors.append(f"Rule '{name}' has invalid value: {val}")
            continue

        if status == "todo":
            errors.append(
                f"Rule '{name}' is marked todo: must be done or exempt with comment"
            )
        elif status == "done":
            done_count += 1
        elif status == "exempt":
            exempt_count += 1
        else:
            errors.append(f"Rule '{name}' has invalid status: {status}")

    # 5. Programmatic checks
    if rules.get("config-flow") == "done":
        if not manifest.get("config_flow"):
            errors.append(
                "config-flow is marked done but manifest.json has config_flow=false"
            )
        if not (integration_dir / "config_flow.py").is_file():
            errors.append(
                "config-flow is marked done but config_flow.py does not exist"
            )

    if rules.get("unique-config-entry") == "done":
        cf_file = integration_dir / "config_flow.py"
        if cf_file.is_file() and "async_set_unique_id" not in cf_file.read_text(
            encoding="utf-8", errors="ignore"
        ):
            errors.append(
                "unique-config-entry is marked done but async_set_unique_id not found in config_flow.py"
            )

    if rules.get("config-entry-unloading") == "done":
        init_file = integration_dir / "__init__.py"
        if init_file.is_file() and "async_unload_entry" not in init_file.read_text(
            encoding="utf-8", errors="ignore"
        ):
            errors.append(
                "config-entry-unloading is marked done but async_unload_entry not found in __init__.py"
            )

    # 6. Verify manifest tier
    declared_tier = manifest.get("quality_scale")
    if declared_tier != "platinum":
        errors.append(
            f"manifest.json must declare 'quality_scale': 'platinum', found: '{declared_tier}'"
        )

    if errors:
        for err in errors:
            print(f"{qs_path.name}: {err}", file=sys.stderr)
        return 1

    print(
        f"{qs_path.name} meets Home Assistant Quality Scale PLATINUM: "
        f"{done_count} rules done, {exempt_count} exempt."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
