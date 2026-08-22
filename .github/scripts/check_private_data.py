#!/usr/bin/env python3
"""Reject private installation data from newly added Git lines."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
MAC_RE = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f])")
SECRET_RES = (
    re.compile(r"(?i)\b(?:bearer|token)\s+[a-z0-9._~-]{20,}"),
    re.compile(r"\b(?:ghp|github_pat|sk-proj|sk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
)
SYNTHETIC_MAC_RES = (
    re.compile(r"(?i)^00:00:00:00:00:00$"),
    re.compile(r"(?i)^00:11:22:33:44:[0-9a-f]{2}$"),
    re.compile(r"(?i)^02:00:00:00:00:[0-9a-f]{2}$"),
    re.compile(r"(?i)^11:22:33:44:55:66$"),
    re.compile(r"(?i)^aa:bb:cc:dd:ee:ff$"),
)
PRIVATE_IPV4_NETWORKS = (
    ipaddress.ip_network("10" + ".0.0.0/8"),
    ipaddress.ip_network("172" + ".16.0.0/12"),
    ipaddress.ip_network("192" + ".168.0.0/16"),
)


@dataclass(frozen=True)
class AddedLine:
    """One added line and its destination location."""

    path: str
    line_number: int
    text: str


@dataclass(frozen=True)
class Violation:
    """A privacy category at a source location."""

    path: str
    line_number: int
    category: str


def _run_git(args: list[str]) -> str:
    """Run Git without interpreting repository content as shell syntax."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={Path.cwd().resolve().as_posix()}", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _added_lines(diff: str) -> list[AddedLine]:
    """Extract destination lines from a zero-context unified diff."""
    path = ""
    destination_line = 0
    added: list[AddedLine] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            destination_line = int(match.group(1)) if match else 0
            continue
        if not path or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(AddedLine(path, destination_line, line[1:]))
            destination_line += 1
        elif not line.startswith("-"):
            destination_line += 1
    return added


def _load_private_patterns() -> tuple[re.Pattern[str], ...]:
    """Load optional local and CI-only installation denylist patterns."""
    raw_patterns: list[str] = []
    local_file = Path(".private-data-patterns")
    if local_file.is_file():
        raw_patterns.extend(local_file.read_text(encoding="utf-8").splitlines())
    raw_patterns.extend(os.environ.get("PRIVATE_DATA_PATTERNS", "").splitlines())
    return tuple(
        re.compile(pattern, re.IGNORECASE)
        for raw in raw_patterns
        if (pattern := raw.strip()) and not pattern.startswith("#")
    )


def _is_synthetic_mac(value: str) -> bool:
    return any(pattern.fullmatch(value) for pattern in SYNTHETIC_MAC_RES)


def find_violations(
    lines: Iterable[AddedLine],
    private_patterns: Iterable[re.Pattern[str]],
) -> list[Violation]:
    """Return privacy violations without retaining or printing their values."""
    violations: set[Violation] = set()
    patterns = tuple(private_patterns)
    for line in lines:
        for value in IPV4_RE.findall(line.text):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_IPV4_NETWORKS):
                violations.add(Violation(line.path, line.line_number, "private IPv4"))
        for value in MAC_RE.findall(line.text):
            if not _is_synthetic_mac(value):
                violations.add(Violation(line.path, line.line_number, "MAC address"))
        if any(pattern.search(line.text) for pattern in SECRET_RES):
            violations.add(Violation(line.path, line.line_number, "credential-like value"))
        if any(pattern.search(line.text) for pattern in patterns):
            violations.add(Violation(line.path, line.line_number, "installation denylist"))
    return sorted(violations, key=lambda item: (item.path, item.line_number, item.category))


def _self_test() -> None:
    """Verify detection without embedding usable private fixture values."""
    private_ip = "192" + ".168.42.7"
    private_mac = "7c" + ":10:c9:12:34:56"
    lines = [
        AddedLine("sample.py", 1, f"host = '{private_ip}'"),
        AddedLine("sample.py", 2, f"mac = '{private_mac}'"),
        AddedLine("sample.py", 3, "host = '192.0.2.10'"),
        AddedLine("sample.py", 4, "mac = '02:00:00:00:00:01'"),
    ]
    categories = {item.category for item in find_violations(lines, ())}
    if categories != {"private IPv4", "MAC address"}:
        raise RuntimeError("privacy scanner self-test failed")


def main() -> int:
    """Scan staged or branch-added lines."""
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--staged", action="store_true")
    source.add_argument("--base")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
    if args.staged:
        diff = _run_git(["diff", "--cached", "--unified=0", "--no-ext-diff"])
    elif args.base:
        diff = _run_git(["diff", "--unified=0", "--no-ext-diff", args.base])
    else:
        parser.error("one of --staged or --base is required")

    violations = find_violations(_added_lines(diff), _load_private_patterns())
    for violation in violations:
        print(f"{violation.path}:{violation.line_number}: {violation.category}")
    if violations:
        print(f"Private-data check failed with {len(violations)} finding(s).")
        return 1
    print("Private-data check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
