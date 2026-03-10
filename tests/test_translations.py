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


@pytest.mark.parametrize("lang", REQUIRED_LANGUAGES)
def test_translation_keys_match_strings(lang: str):
    """Ensure every key in strings.json has a corresponding key in translations."""
    with open(STRINGS_FILE, encoding="utf-8") as f:
        strings_data = json.load(f)

    translation_file = TRANSLATIONS_DIR / f"{lang}.json"
    with open(translation_file, encoding="utf-8") as f:
        translation_data = json.load(f)

    strings_keys = _flatten_keys(strings_data)
    translation_keys = _flatten_keys(translation_data)

    missing_in_translation = strings_keys - translation_keys
    assert not missing_in_translation, (
        f"Keys missing in {lang}.json: {sorted(missing_in_translation)}"
    )


@pytest.mark.parametrize("lang", REQUIRED_LANGUAGES)
def test_no_extra_keys_in_translation(lang: str):
    """Ensure translations don't have keys absent from strings.json."""
    with open(STRINGS_FILE, encoding="utf-8") as f:
        strings_data = json.load(f)

    translation_file = TRANSLATIONS_DIR / f"{lang}.json"
    with open(translation_file, encoding="utf-8") as f:
        translation_data = json.load(f)

    strings_keys = _flatten_keys(strings_data)
    translation_keys = _flatten_keys(translation_data)

    extra_in_translation = translation_keys - strings_keys
    assert not extra_in_translation, (
        f"Extra keys in {lang}.json not in strings.json: {sorted(extra_in_translation)}"
    )


def test_translation_values_not_empty():
    """Ensure no translation value is empty."""
    for lang in REQUIRED_LANGUAGES:
        translation_file = TRANSLATIONS_DIR / f"{lang}.json"

        # Check actual values
        with open(translation_file, encoding="utf-8") as f:
            raw = json.load(f)

        def _check_values(data: dict, prefix: str = "") -> list[str]:
            empty: list[str] = []
            for key, value in data.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    empty.extend(_check_values(value, full_key))
                elif isinstance(value, str) and not value.strip():
                    empty.append(full_key)
            return empty

        empty_keys = _check_values(raw)
        assert not empty_keys, (
            f"Empty translation values in {lang}.json: {sorted(empty_keys)}"
        )
