#!/usr/bin/env python3
"""Check quality_scale.yaml against Home Assistant core's quality scale rules.

Hassfest skips this file for custom integrations, so this script downloads
hassfest's quality scale code from the core release of the installed
homeassistant, which `uv run` keeps at the version pyproject.toml pins. Every
rule must be listed, in core's order, as done or as exempt with a comment, and
the rules marked done must pass core's validators.
"""

import sys
import tarfile
import tempfile
import urllib.request
from importlib import import_module, metadata
from pathlib import Path

import probatio

try:
    from homeassistant.const import __version__ as ha_version
    from homeassistant.util.yaml import load_yaml_dict
except ImportError:
    ha_version = "2026.11.0"
    import yaml

    def load_yaml_dict(path: str) -> dict[str, object]:
        with open(path, encoding="utf-8") as file:
            return yaml.safe_load(file)


from packaging.requirements import Requirement
from probatio.humanize import humanize_error


def _find_integration() -> Path:
    manifests = list(
        (Path(__file__).resolve().parents[1] / "custom_components").glob(
            "*/manifest.json"
        )
    )
    if not manifests:
        sys.exit("No custom component found in custom_components/")
    return manifests[0].parent


INTEGRATION = _find_integration()
CORE_ARCHIVE = "https://github.com/home-assistant/core/archive/refs/tags/{}.tar.gz"
# The parts of core's source that hassfest's quality scale code needs
CORE_FILES = (
    ".strict-typing",
    "script/__init__.py",
    "script/hassfest/",
    "packages/hassfest/hassfest/",
)
# A package without type hints, which the self-check adds as a requirement
UNTYPED = "untyped-self-check"
type Document = dict[str, object]


def installed(requirement: str) -> str:
    """Return the requirement pinned to its installed version."""
    name = Requirement(requirement).name
    try:
        return f"{name}=={metadata.version(name)}"
    except metadata.PackageNotFoundError:
        sys.exit(
            f"manifest.json requires {name}, which isn't installed: add it to pyproject.toml"
        )


def status_of(value: object) -> object:
    """Return a rule's status, whether given alone or with a comment."""
    return value.get("status") if isinstance(value, dict) else value


class Hassfest:
    """Core's quality scale code, run on the integration."""

    def __init__(self, core: Path) -> None:
        """Import hassfest from core's source and load the integration."""
        sys.path[:0] = [str(core), str(core / "packages/hassfest")]
        # Install UNTYPED there: package metadata only, with no py.typed. Core
        # reads its files from RECORD, which must not be empty.
        dist_info = core / f"{UNTYPED.replace('-', '_')}-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {UNTYPED}\nVersion: 1.0\n", encoding="utf-8"
        )
        (dist_info / "RECORD").write_text(
            f"{dist_info.name}/METADATA,,\n", encoding="utf-8"
        )
        self.quality_scale = import_module("script.hassfest.quality_scale")
        model = import_module("hassfest.model")
        self.config = model.Config(
            specific_integrations=None,
            root=core,
            action="validate",
            requirements=False,
        )
        self.integration = model.Integration(INTEGRATION, self.config)
        self.integration.load_manifest()
        with (core / ".strict-typing").open("a", encoding="utf-8") as file:
            file.write(f"\nhomeassistant.components.{INTEGRATION.name}.*\n")
        self.integration.manifest["requirements"] = [
            installed(requirement) for requirement in self.integration.requirements
        ]
        self.rule_names: list[str] = [
            rule.name for rule in self.quality_scale.ALL_RULES
        ]

    def check(self, document: Document) -> list[str]:
        """Return each way quality_scale.yaml fails core's quality scale."""
        errors: list[str] = []
        try:
            self.quality_scale.SCHEMA(document)
        except probatio.Invalid as err:
            errors.extend(humanize_error(document, err).splitlines())
        rules = document.get("rules")
        if not isinstance(rules, dict):
            return errors

        listed = [name for name in rules if name in self.rule_names]
        expected = [name for name in self.rule_names if name in rules]
        wrong = next((i for i, n in enumerate(listed) if n != expected[i]), None)
        if wrong is not None:
            errors.append(
                f"{expected[wrong]} must come before {listed[wrong]}, as in core's"
                " list of rules"
            )

        statuses = {name: status_of(value) for name, value in rules.items()}
        errors.extend(
            f"{name} is todo: do it, or mark it exempt with a comment"
            for name, status in statuses.items()
            if status == "todo"
        )

        done = {name for name, status in statuses.items() if status == "done"}
        for name in self.rule_names:
            validator = self.quality_scale.VALIDATORS.get(name)
            if name not in done or validator is None:
                continue
            found = validator.validate(self.config, self.integration, rules_done=done)
            if found:
                errors.extend(f"{name}: {error}" for error in found)
        return errors

    def self_check(self, document: Document) -> dict[str, bool]:
        """Return whether changes to the file produce each expected error."""
        rules = document["rules"]
        assert isinstance(rules, dict)
        first, second, *rest = self.rule_names
        missing = {name: value for name, value in rules.items() if name != first}
        swapped = {name: rules[name] for name in (second, first, *rest)}
        cases = [
            ("a rule marked todo", {**rules, first: "todo"}, f"{first} is todo"),
            (
                "a missing rule",
                missing,
                f"required key not provided at 'rules.{first}'",
            ),
            (
                "two rules out of order",
                swapped,
                f"{first} must come before {second}",
            ),
            (
                "an exemption without a comment",
                {**rules, first: {"status": "exempt"}},
                f"required key not provided at 'rules.{first}.comment'",
            ),
        ]
        documents = [
            (case, {**document, "rules": changed}, error)
            for case, changed, error in cases
        ]
        documents.append(
            (
                "a key outside rules",
                {**document, first: "done"},
                f"not a valid option at '{first}'",
            )
        )
        rejected = {
            case: any(error in found for found in self.check(changed))
            for case, changed, error in documents
        }

        config_flow = self.integration.manifest["config_flow"]
        self.integration.manifest["config_flow"] = False
        try:
            rejected["config-flow done without its manifest flag"] = any(
                "config-flow: Integration does not set config_flow" in found
                for found in self.check(document)
            )
        finally:
            self.integration.manifest["config_flow"] = config_flow

        requirements = self.integration.manifest["requirements"]
        self.integration.manifest["requirements"] = [
            *requirements,
            installed(f"{UNTYPED}>=1"),
        ]
        try:
            rejected["a requirement without type hints"] = any(
                f"strict-typing: Requirements ['{UNTYPED}==1.0']" in found
                for found in self.check(document)
            )
        finally:
            self.integration.manifest["requirements"] = requirements
        return rejected


def extract_core(version: str, destination: Path) -> None:
    """Extract the parts of core's source that hassfest's checks need."""
    url = CORE_ARCHIVE.format(version)
    try:
        with (
            urllib.request.urlopen(url, timeout=60) as response,
            tarfile.open(fileobj=response, mode="r|gz") as archive,
        ):
            for member in archive:
                member.name = member.name.partition("/")[2]
                if member.isfile() and member.name.startswith(CORE_FILES):
                    archive.extract(member, destination, filter="data")
    except OSError as err:
        sys.exit(f"Cannot download Home Assistant {version}'s source from {url}: {err}")


def main() -> int:
    """Check the file and that the check rejects faulty copies of it."""
    path = INTEGRATION / "quality_scale.yaml"
    document = load_yaml_dict(str(path))
    with tempfile.TemporaryDirectory() as core:
        extract_core(ha_version, Path(core))
        try:
            hassfest = Hassfest(Path(core))
            errors = hassfest.check(document)
            rejected = {} if errors else hassfest.self_check(document)
        except Exception as err:
            err.add_note(
                f"Home Assistant {ha_version}'s hassfest may have changed:"
                f" update {Path(__file__).name} to match"
            )
            raise
    for error in errors:
        print(f"{path.name}: {error}", file=sys.stderr)
    if errors:
        return 1
    if missed := [case for case, caught in rejected.items() if not caught]:
        print(f"Self-check: the check accepted {', '.join(missed)}", file=sys.stderr)
        return 1
    statuses = [status_of(value) for value in document["rules"].values()]
    print(
        f"{path.name} meets Home Assistant {ha_version}'s quality scale:"
        f" {statuses.count('done')} rules done, {statuses.count('exempt')} exempt"
    )
    print(f"Self-check: the check rejected {', '.join(rejected)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
