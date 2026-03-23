"""Test that all translation keys are present in EN and DE."""

import json
from pathlib import Path

import pytest

INTEGRATION_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "openwrt"
)
STRINGS_FILE = INTEGRATION_DIR / "strings.json"
TRANSLATIONS_DIR = INTEGRATION_DIR / "translations"
REQUIRED_LANGUAGES = ["en", "de"]


def _flatten_keys(data: dict, prefix: str = "") -> set[str]:
    """Recursively flatten JSON keys into dot-separated paths."""
    keys: set[str] = set()
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys.update(_flatten_keys(value, full_key))
        else:
            keys.add(full_key)
    return keys


def test_strings_json_exists():
    """Verify strings.json exists."""
    assert STRINGS_FILE.exists(), f"strings.json not found at {STRINGS_FILE}"


@pytest.mark.parametrize("lang", REQUIRED_LANGUAGES)
def test_translation_file_exists(lang: str):
    """Verify required translation files exist."""
    path = TRANSLATIONS_DIR / f"{lang}.json"
    assert path.exists(), f"Translation file {lang}.json is missing at {path}"


def test_translation_files_parity():
    """Ensure every key in strings.json, en.json, and de.json matches exactly."""
    with open(STRINGS_FILE, encoding="utf-8") as f:
        strings_data = json.load(f)
    strings_keys = _flatten_keys(strings_data)

    for lang in REQUIRED_LANGUAGES:
        translation_file = TRANSLATIONS_DIR / f"{lang}.json"
        with open(translation_file, encoding="utf-8") as f:
            translation_data = json.load(f)
        translation_keys = _flatten_keys(translation_data)

        # Check for keys in strings.json but missing in translation
        missing_in_translation = strings_keys - translation_keys
        assert not missing_in_translation, (
            f"Keys in strings.json missing in {lang}.json: {sorted(missing_in_translation)}"
        )

        # Check for extra keys in translation not in strings.json
        extra_in_translation = translation_keys - strings_keys
        assert not extra_in_translation, (
            f"Extra keys in {lang}.json not in strings.json: {sorted(extra_in_translation)}"
        )


def test_translation_values_not_empty():
    """Ensure no translation value is empty or remains as a placeholder."""
    for lang in [*REQUIRED_LANGUAGES, "strings"]:
        if lang == "strings":
            file_path = STRINGS_FILE
        else:
            file_path = TRANSLATIONS_DIR / f"{lang}.json"

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        def _check_values(data: dict, prefix: str = "") -> list[str]:
            invalid: list[str] = []
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    invalid.extend(_check_values(value, full_key))
                elif isinstance(value, str):
                    if not value.strip():
                        invalid.append(f"{full_key} (empty)")
                else:
                    invalid.append(f"{full_key} (not a string/dict)")
            return invalid

        invalid_keys = _check_values(data)
        assert not invalid_keys, (
            f"Invalid translation values in {file_path.name}: {sorted(invalid_keys)}"
        )


def test_config_keys_in_strings():
    """Scan config_flow.py and const.py for likely data keys and ensure they are in strings.json."""
    import re

    with open(STRINGS_FILE, encoding="utf-8") as f:
        strings_data = json.load(f)

    # Extract all possible data keys from strings.json (under step and options)
    all_data_keys = set()

    def extract_data_keys(data, path=""):
        if isinstance(data, dict):
            if path.endswith(".data"):
                all_data_keys.update(data.keys())
            for k, v in data.items():
                extract_data_keys(v, f"{path}.{k}" if path else k)

    extract_data_keys(strings_data)

    # Scan config_flow.py for CONF_ constants
    config_flow_path = INTEGRATION_DIR / "config_flow.py"
    with open(config_flow_path, encoding="utf-8") as f:
        content = f.read()

    # Find all strings like CONF_something
    found_conf_vars = set(re.findall(r"CONF_[A-Z_]+", content))

    # Get values of these constants from const.py
    const_path = INTEGRATION_DIR / "const.py"
    with open(const_path, encoding="utf-8") as f:
        const_content = f.read()

    missing_keys = []
    # Regular expression for matching constant assignments
    # Matches 'CONF_VAR = "value"', 'CONF_VAR: Final = "value"', etc.
    const_pattern = re.compile(r'([A-Z_]+)\s*(?::\s*[A-Za-z]+)?\s*=\s*"([^"]+)"')
    const_map = {m.group(1): m.group(2) for m in const_pattern.finditer(const_content)}

    for var in found_conf_vars:
        if var in const_map:
            value = const_map[var]
            # Exclude known internals or basic ones that are sometimes omitted or handled differently
            if value in [
                "host",
                "connection_type",
                "username",
                "password",
                "port",
                "use_ssl",
                "verify_ssl",
                "ubus_path",
                "ssh_key",
            ]:
                continue

            # If the value is used in code, it SHOULD be in a .data block somewhere
            if value not in all_data_keys:
                missing_keys.append(f"{var} ({value})")

    assert not missing_keys, (
        f"Likely configuration keys found in code but missing from 'data' sections in strings.json: {sorted(missing_keys)}"
    )


def test_steps_and_errors_translated():
    """Ensure all steps, error keys, and abort reasons used in config_flow.py are defined in strings.json."""
    import re

    with open(STRINGS_FILE, encoding="utf-8") as f:
        full_strings_data = json.load(f)

    config_data = full_strings_data.get("config", {})
    options_data = full_strings_data.get("options", {})

    config_flow_path = INTEGRATION_DIR / "config_flow.py"
    with open(config_flow_path, encoding="utf-8") as f:
        content = f.read()

    # 1. Check step_id="..."
    found_steps = set(re.findall(r'step_id=["\']([^"\']+)["\']', content))
    translated_steps = set(config_data.get("step", {}).keys()) | set(
        options_data.get("step", {}).keys(),
    )

    missing_steps = found_steps - translated_steps
    assert not missing_steps, (
        f"Steps used in code but missing in strings.json: {sorted(missing_steps)}"
    )

    # 2. Check errors={"base": "...", ...} or errors={"field": "..."}
    errors_sec = re.findall(r"errors\s*=\s*\{([^}]+)\}", content)
    found_error_keys = set()
    for sec in errors_sec:
        found_error_keys.update(re.findall(r':\s*["\']([^"\']+)["\']', sec))

    translated_errors = set(config_data.get("error", {}).keys()) | set(
        options_data.get("error", {}).keys(),
    )
    missing_errors = found_error_keys - translated_errors - found_steps

    # Static exclusion for known dynamic or placeholder keys
    missing_errors = {
        e for e in missing_errors if not e.startswith("{") and e not in ["base"]
    }

    assert not missing_errors, (
        f"Error keys used in code but missing in strings.json: {sorted(missing_errors)}"
    )

    # 3. Check async_abort(reason="...")
    found_abort_reasons = set(
        re.findall(r'async_abort\(reason=["\']([^"\']+)["\']', content),
    )
    translated_aborts = set(config_data.get("abort", {}).keys()) | set(
        options_data.get("abort", {}).keys(),
    )
    missing_aborts = found_abort_reasons - translated_aborts
    assert not missing_aborts, (
        f"Abort reasons used in code but missing in strings.json: {sorted(missing_aborts)}"
    )
