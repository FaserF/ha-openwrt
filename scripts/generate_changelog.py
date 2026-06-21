import argparse
import re
import subprocess
import sys

# Noise filter — commits matching ANY pattern are silently dropped
NOISE_PATTERNS = [
    r"^\s*$",
    r"^(Update|Aktualisier[et]?|Add|Adds|Adde|Delete|Deletes|Remove|Removes|Rename|Renames|Move|Moves|Fix|Edit|Change|Modify)\s+[\w\-\.\/]+\.\w{1,10}\s*$",
    r"^Merge (pull request|branch|remote-tracking branch)\b",
    r"^Merge from\b",
    r"^(chore|build)(\([^)]*\))?:\s*(bump|release|version)\b",
    r"^(bump|release)(\s+version)?\s+v?\d",
    r"^v?\d+\.\d+\.\d+\s*$",
    r"^\[skip[- ]ci\]",
    r"^chore: regenerate (manifest|connections|changelog)\b",
    r"^(auto.?generated?|automated?|bot:)\b",
    r"^Revert \"Revert",
    r"^Initial commit\s*$",
    r"^WIP\b",
    r"^wip\b",
    r"^.{1,3}$",
    r"\[skip[- ]ci\]\s*$",
]

# Category order & display labels
CATEGORY_ORDER = [
    "breaking",
    "feat",
    "fix",
    "security",
    "perf",
    "refactor",
    "ui",
    "docs",
    "test",
    "ci",
    "chore",
    "other",
]

CATEGORY_EMOJI = {
    "breaking": "💥 Breaking Changes",
    "feat": "✨ New Features",
    "fix": "🐛 Bug Fixes",
    "security": "🔒 Security",
    "perf": "⚡ Performance",
    "refactor": "♻️ Code Improvements",
    "ui": "🎨 UI / Translations",
    "docs": "📚 Documentation",
    "test": "🧪 Tests",
    "ci": "🔄 CI / CD",
    "chore": "🔧 Maintenance",
    "other": "📦 Other Changes",
}

# Conventional commit type -> bucket mapping
TYPE_MAP = {
    "feat": "feat",
    "feature": "feat",
    "fix": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "security": "security",
    "sec": "security",
    "perf": "perf",
    "optim": "perf",
    "refactor": "refactor",
    "refact": "refactor",
    "ui": "ui",
    "style": "ui",
    "ux": "ui",
    "docs": "docs",
    "doc": "docs",
    "test": "test",
    "tests": "test",
    "ci": "ci",
    "cd": "ci",
    "build": "ci",
    "chore": "chore",
    "maint": "chore",
    "deps": "chore",
    "bump": "chore",
    "revert": "fix",
}

# Scope overrides
SCOPE_MAP = {
    "ui": "ui",
    "translation": "ui",
    "translate": "ui",
    "docs": "docs",
    "readme": "docs",
    "test": "test",
    "tests": "test",
    "ci": "ci",
    "workflow": "ci",
    "actions": "ci",
}

MAX_PER_SECTION = 15
NEVER_COLLAPSE = {"breaking", "security"}


def get_norm_key(msg: str) -> str:
    n = msg.lower()
    # Strip conventional commit prefix
    n = re.sub(
        r"^(feat|fix|docs|style|refactor|perf|test|chore|ci|security|build|ui|ux|revert)(\([^)]*\))?(!)?:\s*",
        "",
        n,
    )
    # Remove punctuation
    n = re.sub(r"[\.\!\?\,\;\:\"'`]", "", n)
    # Remove common stop words
    n = re.sub(r"\b(the|a|an|for|of|in|to|with|from|on|at|by)\b", "", n)
    # Normalize spaces
    n = re.sub(r"\s+", " ", n)
    return n.strip()


def get_formatted_item(display: str, hashes: list[str], repo: str) -> str:
    if hashes:
        links = []
        for h in hashes:
            if repo:
                links.append(f"[{h}](https://github.com/{repo}/commit/{h})")
            else:
                links.append(f"`{h}`")
        hash_str = ", ".join(links)
        return f"{display} ({hash_str})"
    return display


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-tag", default="")
    parser.add_argument("--total-commits", default="")
    parser.add_argument("--repo", default="")
    args = parser.parse_args()

    from_tag = args.from_tag
    repo = args.repo

    if from_tag:
        git_range = f"{from_tag}..HEAD"
    else:
        git_range = ""

    try:
        cmd = ["git", "log"]
        if git_range:
            cmd.append(git_range)
        else:
            cmd.extend(["--max-count=2000"])
        cmd.append("--pretty=format:%h %s")

        # Run git command
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        raw_lines = res.stdout.splitlines()
    except subprocess.CalledProcessError:
        raw_lines = []

    total_raw = int(args.total_commits) if args.total_commits else len(raw_lines)

    buckets = {k: [] for k in CATEGORY_ORDER}
    seen_items = {}

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([0-9a-fA-F]+)\s+(.*)$", line)
        if match:
            commit_hash = match.group(1)
            msg = match.group(2).strip()
        else:
            commit_hash = ""
            msg = line

        if not msg:
            continue

        # Noise check
        skip = False
        for pattern in NOISE_PATTERNS:
            if re.search(pattern, msg):
                skip = True
                break
        if skip:
            continue

        bucket = "other"
        display = msg
        is_break = False

        # Parse conventional commits
        cc_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)(\([^)]*\))?(!)?:\s*(.+)$", msg)
        if cc_match:
            raw_type = cc_match.group(1).lower()
            raw_scope = (
                cc_match.group(2).replace("(", "").replace(")", "").lower().strip()
                if cc_match.group(2)
                else ""
            )
            is_break = bool(cc_match.group(3))
            desc = cc_match.group(4).strip()

            if raw_scope and raw_scope in SCOPE_MAP:
                bucket = SCOPE_MAP[raw_scope]
            elif raw_type in TYPE_MAP:
                bucket = TYPE_MAP[raw_type]

            desc_cap = desc[0].upper() + desc[1:] if desc else desc
            if raw_scope:
                display = f"**{raw_scope}:** {desc_cap}"
            else:
                display = desc_cap
        else:
            display = msg[0].upper() + msg[1:] if msg else msg
            msg_lower = msg.lower()
            # Heuristics for non-conventional commits
            if re.search(
                r"\b(general\s+fix|small\s+fix|bug\s+fix|fix(es|ed)?\b|fix\s+\w|general\s+improve)",
                msg_lower,
            ):
                bucket = "fix"
            elif re.search(
                r"\b(ci\b|linter?|lint\s+fix|pipeline|workflow|github\s+action|generate[_\s]changelog|changelog\s+)",
                msg_lower,
            ):
                bucket = "ci"
            elif re.search(
                r"\b(update\s+depend|bump\s+depend|renovate|dependency\s+update|upgrade\s+dep)",
                msg_lower,
            ):
                bucket = "chore"
            elif re.search(
                r"\b(add(ed|s)?\s+(missing\s+)?feature|new\s+feature|add\s+support)",
                msg_lower,
            ):
                bucket = "feat"
            elif re.search(r"\b(security|vulnerability|cve|auth(en|oriz))", msg_lower):
                bucket = "security"
            elif re.search(r"\b(perf(ormance)?|speed|faster|optim)", msg_lower):
                bucket = "perf"
            elif re.search(
                r"\b(refactor(ing)?|clean.?up|improve(d|s|ment)?)\b", msg_lower
            ):
                bucket = "refactor"
            elif re.search(r"\b(doc(s|ument(ation)?)?|readme|wiki|guide)\b", msg_lower):
                bucket = "docs"
            elif re.search(r"\b(test(s|ing)?|spec|unit\s+test)", msg_lower):
                bucket = "test"
            elif re.search(
                r"\b(ui\b|ux\b|layout|style|theme|translation|translations|strings|lang)\b",
                msg_lower,
            ):
                bucket = "ui"

        norm_key = get_norm_key(display)

        if is_break:
            break_display = f"**{display}**"
            break_key = f"breaking:{norm_key}"
            if break_key in seen_items:
                existing_break = seen_items[break_key]
                if commit_hash and commit_hash not in existing_break["hashes"]:
                    existing_break["hashes"].append(commit_hash)
            else:
                break_item = {
                    "display": break_display,
                    "hashes": [commit_hash] if commit_hash else [],
                }
                seen_items[break_key] = break_item
                buckets["breaking"].append(break_item)

        if norm_key in seen_items:
            existing_item = seen_items[norm_key]
            if commit_hash and commit_hash not in existing_item["hashes"]:
                existing_item["hashes"].append(commit_hash)
            continue

        item = {"display": display, "hashes": [commit_hash] if commit_hash else []}
        seen_items[norm_key] = item
        buckets[bucket].append(item)

    out = []
    has_any = False
    filtered_count = sum(len(buckets[k]) for k in CATEGORY_ORDER)

    # Breaking changes callout
    if buckets["breaking"]:
        has_any = True
        out.append("> [!CAUTION]")
        out.append(
            "> **This release contains breaking changes. Please review before updating.**"
        )
        out.append(">")
        for item in buckets["breaking"]:
            formatted = get_formatted_item(item["display"], item["hashes"], repo)
            out.append(f"> - {formatted}")
        out.append("")

    for key in CATEGORY_ORDER:
        if key == "breaking":
            continue
        bucket_items = buckets[key]
        if not bucket_items:
            continue
        has_any = True

        out.append(f"### {CATEGORY_EMOJI[key]}")
        out.append("")

        collapse = (len(bucket_items) > MAX_PER_SECTION) and (key not in NEVER_COLLAPSE)

        if collapse:
            for i in range(MAX_PER_SECTION):
                formatted = get_formatted_item(
                    bucket_items[i]["display"], bucket_items[i]["hashes"], repo
                )
                out.append(f"- {formatted}")
            remaining = len(bucket_items) - MAX_PER_SECTION
            out.append("")
            out.append("<details>")
            out.append(f"<summary>Show {remaining} more changes…</summary>")
            out.append("")
            for i in range(MAX_PER_SECTION, len(bucket_items)):
                formatted = get_formatted_item(
                    bucket_items[i]["display"], bucket_items[i]["hashes"], repo
                )
                out.append(f"- {formatted}")
            out.append("")
            out.append("</details>")
        else:
            for item in bucket_items:
                formatted = get_formatted_item(item["display"], item["hashes"], repo)
                out.append(f"- {formatted}")
        out.append("")

    if not has_any:
        out.append("> *No categorised changes found in this release.*")
        out.append(
            "> Most commits were maintenance, dependency updates, or automated changes."
        )
        out.append("")

    range_str = f"{from_tag}..HEAD" if from_tag else "all history"
    out.append("---")

    if total_raw > 0:
        out.append(
            f"*{filtered_count} significant changes from {total_raw} total commits since `{from_tag}`.*"
        )
    else:
        out.append(f"*Changelog generated from `{range_str}`.*")

    print("\n".join(out))


if __name__ == "__main__":
    main()
