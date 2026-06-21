#!/usr/bin/env python3
import os
import re
import subprocess
from datetime import datetime


def run_git(args):
    try:
        return (
            subprocess.check_output(["git"] + args, stderr=subprocess.DEVNULL)
            .decode("utf-8")
            .strip()
        )
    except subprocess.CalledProcessError:
        return ""


def main():
    rtype = os.environ.get("RELEASE_TYPE", "beta")
    repo = os.environ.get("REPO", "").lower()

    # Calculate version
    version = (
        subprocess.check_output(
            ["python", ".github/scripts/version_manager.py", "bump", "--type", rtype]
        )
        .decode("utf-8")
        .strip()
    )
    run_git(["checkout", "--", "custom_components/db_infoscreen/manifest.json"])

    print(f"Calculated Version: {version}")
    tag = f"v{version}"
    is_prerelease = "false" if rtype == "stable" else "true"

    # Base version without suffixes
    base_version = version
    m = re.match(r"^(\d+\.\d+\.\d+)", version)
    if m:
        base_version = m.group(1)

    changelog_from = ""
    changelog_label = "initial release — full history"

    # Get sorted list of tags
    tags_raw = run_git(["tag", "-l", "[0-9]*", "v[0-9]*", "--sort=-v:refname"])
    tags = [t.strip() for t in tags_raw.splitlines() if t.strip()]
    latest_tag = ""
    for t in tags:
        if re.match(r"^v?\d+\.\d+\.\d+(?:(?:b|-dev|-nightly)\d+)?$", t):
            latest_tag = t
            break

    if rtype == "stable":
        for t in tags:
            if re.match(r"^v?\d+\.\d+\.\d+$", t):
                changelog_from = t
                changelog_label = f"since last stable release (`{t}`)"
                break
    elif rtype == "beta":
        prev_beta_pat = re.compile(rf"^v?{re.escape(base_version)}(?:b|-beta)\d+$")
        for t in tags:
            if prev_beta_pat.match(t):
                changelog_from = t
                changelog_label = f"since previous beta (`{t}`)"
                break
        if not changelog_from:
            for t in tags:
                if re.match(r"^v?\d+\.\d+\.\d+$", t):
                    changelog_from = t
                    changelog_label = f"since last stable release (`{t}`) — first beta of {base_version}"
                    break
    else:
        if latest_tag:
            changelog_from = latest_tag
            changelog_label = f"since `{latest_tag}`"

    print(f"Changelog range start tag: '{changelog_from}' ({changelog_label})")

    # Count commits
    total_commit_count = 0
    if changelog_from:
        count_range = f"{changelog_from}..HEAD"
    else:
        count_range = "HEAD"

    commit_count_raw = run_git(["rev-list", "--count", count_range])
    if commit_count_raw.isdigit():
        total_commit_count = int(commit_count_raw)

    # Generate Changelog
    changelog_md = ""
    if os.path.exists("scripts/generate_changelog.py"):
        try:
            changelog_md = (
                subprocess.check_output(
                    [
                        "python",
                        "scripts/generate_changelog.py",
                        "--from-tag",
                        changelog_from,
                        "--total-commits",
                        str(total_commit_count),
                        "--repo",
                        repo,
                    ]
                )
                .decode("utf-8")
                .strip()
            )
        except Exception:
            changelog_md = (
                "_Changelog could not be generated automatically. See commit history._"
            )
    else:
        changelog_md = "_Changelog script not found._"

    if not changelog_md:
        changelog_md = "_No categorised changes detected._"

    # Channel decorations
    channel_badge = {
        "stable": "![Stable](https://img.shields.io/badge/channel-stable-brightgreen?style=flat-square)",
        "beta": "![Beta](https://img.shields.io/badge/channel-beta-orange?style=flat-square)",
    }.get(
        rtype,
        "![Nightly](https://img.shields.io/badge/channel-nightly-blue?style=flat-square)",
    )

    # Analyze diff impact
    diff_range = f"{changelog_from}..HEAD" if changelog_from else "HEAD"
    changed_files_raw = run_git(["diff", "--name-only", diff_range])
    changed_files = [f.strip() for f in changed_files_raw.splitlines() if f.strip()]

    total_files = len(changed_files)
    integration_count = 0
    translation_count = 0
    ci_count = 0
    docs_count = 0
    test_count = 0

    for f in changed_files:
        if f.startswith("custom_components/db_infoscreen/translations/"):
            translation_count += 1
        elif f.startswith("custom_components/"):
            integration_count += 1
        elif f.startswith("tests/"):
            test_count += 1
        elif f.startswith(".github/") or f.startswith("scripts/"):
            ci_count += 1
        elif f.startswith("docs/") or f.endswith(".md"):
            docs_count += 1

    breaking_count = 0
    log_msgs = run_git(["log", diff_range, "--format=%B"])
    for msg in log_msgs.split("\n"):
        if re.search(r"\bBREAKING CHANGE\b|\bBREAKING:\b|^[a-zA-Z]+!:", msg):
            breaking_count += 1

    # Determine Risk Severity
    severity = "Low"
    alert_type = "NOTE"
    preamble = "This release introduces minor updates and code improvements."

    if breaking_count > 0:
        severity = "Critical"
        alert_type = "CAUTION"
        preamble = f"This release contains **{breaking_count} breaking change(s)**! Please review the changelog carefully before updating."
    elif integration_count > 8:
        severity = "High"
        alert_type = "WARNING"
        preamble = "This release contains significant changes to core features. Please verify integration behavior after updating."
    elif integration_count > 2 or translation_count > 5:
        severity = "Medium"
        alert_type = "TIP"
        preamble = "This release contains standard updates and feature enhancements to the integration logic or translations."

    if rtype != "stable":
        preamble = f"ℹ️ **This is a {rtype} build.** It contains preview features for testing.<br><br>{preamble}"

    impact_summary = []
    if total_files > 0:
        if integration_count > 0:
            pct = round((integration_count / total_files) * 100)
            impact_summary.append(f"⚙️ Core ({integration_count} files · {pct}%)")
        if translation_count > 0:
            pct = round((translation_count / total_files) * 100)
            impact_summary.append(
                f"🗣️ Translations ({translation_count} files · {pct}%)"
            )
        if test_count > 0:
            pct = round((test_count / total_files) * 100)
            impact_summary.append(f"🧪 Tests ({test_count} files · {pct}%)")
        if ci_count > 0:
            pct = round((ci_count / total_files) * 100)
            impact_summary.append(f"🚀 CI/CD ({ci_count} files · {pct}%)")
        if docs_count > 0:
            pct = round((docs_count / total_files) * 100)
            impact_summary.append(f"📖 Docs ({docs_count} files · {pct}%)")

    impact_str = (
        " · ".join(impact_summary)
        if impact_summary
        else "No codebase changes detected."
    )

    prerelease_note = (
        f"\n> [!{alert_type}]\n"
        f"> **Release Risk: {severity}**\n"
        f"> {preamble}\n"
        f">\n"
        f"> **Affected areas:** {impact_str}\n"
    )

    released_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"
    body_parts = [
        f"# DB-Infoscreen {version}  {channel_badge}",
        "",
        prerelease_note,
        "## 📋 What's Changed",
        "",
        changelog_md,
        "",
        "## 📊 Release Details",
        "",
        "| | |",
        "|---|---|",
        f"| **Version** | `{version}` |",
        f"| **Channel** | {rtype} |",
        f"| **Released** | {released_at} |",
        f"| **Commits included** | {total_commit_count} — {changelog_label} |",
        "",
        "---",
        "",
        f"*📖 [Documentation](https://faserf.github.io/ha-db_infoscreen/)  ·  🐛 [Report an Issue](https://github.com/{repo}/issues/new/choose)  ·  📦 [All Releases](https://github.com/{repo}/releases)*",
    ]

    body = "\n".join(body_parts)
    with open("release_body.md", "w", encoding="utf-8") as f:
        f.write(body)

    # Output parameters for GITHUB_OUTPUT
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"version={version}\n")
            f.write(f"tag={tag}\n")
            f.write(f"is_prerelease={is_prerelease}\n")
            # Write multiline output for release_body
            import uuid

            delimiter = f"gh_release_{uuid.uuid4().hex}"
            f.write(f"release_body<<{delimiter}\n")
            f.write(body + "\n")
            f.write(f"{delimiter}\n")


if __name__ == "__main__":
    main()
