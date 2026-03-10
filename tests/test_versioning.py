import sys
import os

# Add the script directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.github', 'scripts')))

from bump_version import bump_version, parse_version

def test_logic():
    scenarios = [
        # (current, bump, status, all_tags, expected)
        (None, "patch", "stable", [], "1.0.0"),
        (None, "minor", "beta", [], "1.0.0-beta.0"),
        ("1.0.0", "patch", "stable", ["v1.0.0"], "1.0.1"),
        ("1.0.0", "minor", "stable", ["v1.0.0"], "1.1.0"),
        ("1.0.0", "major", "stable", ["v1.0.0"], "2.0.0"),
        ("1.0.0", "patch", "beta", ["v1.0.0"], "1.0.1-beta.0"),
        ("1.0.1-beta.0", "patch", "beta", ["v1.0.1-beta.0", "v1.0.0"], "1.0.1-beta.1"),
        ("1.0.1-beta.1", "patch", "stable", ["v1.0.1-beta.1", "v1.0.0"], "1.0.1"),
        ("1.0.1-beta.1", "minor", "beta", ["v1.0.1-beta.1", "v1.0.0"], "1.1.0-beta.0"),
        ("1.1.0-beta.0", "minor", "stable", ["v1.1.0-beta.0", "v1.0.0"], "1.1.0"),
    ]
    
    failed = 0
    for current, bump, status, tags, expected in scenarios:
        result = bump_version(current, bump, status, all_tags=tags)
        if result == expected:
            print(f"PASS: {current} + {bump} ({status}) -> {result}")
        else:
            print(f"FAIL: {current} + {bump} ({status}) -> {result} (Expected: {expected})")
            failed += 1

    if failed == 0:
        print("\nAll logical tests passed!")
    else:
        print(f"\n{failed} tests failed.")
        sys.exit(1)

if __name__ == "__main__":
    test_logic()
