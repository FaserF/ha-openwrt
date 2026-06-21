import datetime
import os
import re
import subprocess
import sys


def run_cmd(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def main():
    # Set encoding to UTF-8
    sys.stdout.reconfigure(encoding="utf-8")

    release_type = os.environ.get("RELEASE_TYPE", "beta")
    bump_level = os.environ.get("BUMP_LEVEL", "patch")
    repo = os.environ.get("REPO", "faserf/ha-openwrt").lower()

    # 1. Dry run get next version
    # Run version_manager.py bump and then restore files
    version = run_cmd(
        [
            "python",
            ".github/scripts/version_manager.py",
            "bump",
            "--type",
            release_type,
            "--level",
            bump_level,
        ]
    )
    # Revert modified files to keep clean state
    for path in ["custom_components/openwrt/manifest.json", "pyproject.toml"]:
        try:
            run_cmd(["git", "checkout", "--", path])
        except Exception:
            pass
    if os.path.exists("VERSION"):
        try:
            run_cmd(["git", "checkout", "--", "VERSION"])
        except Exception:
            try:
                os.remove("VERSION")
            except Exception:
                pass

    print(f"Calculated Version: {version}")
    tag = f"v{version}"
    is_prerelease = "false" if release_type == "stable" else "true"

    # Extract base version without suffixes (e.g. 2.3.1 from 2.3.1b1)
    base_version = version
    base_match = re.match(r"^(\d+\.\d+\.\d+)", version)
    if base_match:
        base_version = base_match.group(1)

    # 2. Determine changelog base tag (previous tag to diff against)
    changelog_from = ""
    changelog_label = "initial release — full history"

    # Get all tags matching SemVer pattern
    try:
        raw_tags = run_cmd(
            ["git", "tag", "-l", "[0-9]*", "v[0-9]*", "--sort=-v:refname"]
        ).splitlines()
    except Exception:
        raw_tags = []

    semver_tags = []
    for t in raw_tags:
        t = t.strip()
        if re.match(r"^v?\d+\.\d+\.\d+(?:(?:b|-dev|-nightly)\d+)?$", t):
            semver_tags.append(t)

    latest_tag = semver_tags[0] if semver_tags else ""

    if release_type == "stable":
        stable_tags = [t for t in semver_tags if re.match(r"^v?\d+\.\d+\.\d+$", t)]
        if stable_tags:
            changelog_from = stable_tags[0]
            changelog_label = f"since last stable release (`{changelog_from}`)"
    elif release_type == "beta":
        beta_prefix_pattern = rf"^v?{base_version}(?:b|-beta)\d+$"
        prev_beta_tags = [t for t in semver_tags if re.match(beta_prefix_pattern, t)]
        if prev_beta_tags:
            changelog_from = prev_beta_tags[0]
            changelog_label = f"since previous beta (`{changelog_from}`)"
        else:
            stable_tags = [t for t in semver_tags if re.match(r"^v?\d+\.\d+\.\d+$", t)]
            if stable_tags:
                changelog_from = stable_tags[0]
                changelog_label = f"since last stable release (`{changelog_from}`) — first beta of {base_version}"
    else:
        if latest_tag:
            changelog_from = latest_tag
            changelog_label = f"since `{latest_tag}`"

    print(f"Changelog range start tag: '{changelog_from}' ({changelog_label})")

    # 3. Count commits
    diff_range = f"{changelog_from}..HEAD" if changelog_from else "HEAD"
    try:
        total_commit_count = int(run_cmd(["git", "rev-list", "--count", diff_range]))
    except Exception:
        total_commit_count = 0

    # 4. Generate Changelog
    changelog_md = (
        "_Changelog could not be generated automatically. See commit history._"
    )
    if os.path.exists("scripts/generate_changelog.py"):
        try:
            cl_args = [
                "python",
                "scripts/generate_changelog.py",
                "--total-commits",
                str(total_commit_count),
            ]
            if changelog_from:
                cl_args.extend(["--from-tag", changelog_from])
            if repo:
                cl_args.extend(["--repo", repo])
            changelog_md = run_cmd(cl_args)
            if not changelog_md.strip():
                changelog_md = "_No categorised changes detected._"
        except Exception as e:
            print(f"Error calling changelog generator: {e}")

    # 5. Channel decoration
    if release_type == "stable":
        channel_badge = "![Stable](https://img.shields.io/badge/channel-stable-brightgreen?style=flat-square)"
    elif release_type == "beta":
        channel_badge = "![Beta](https://img.shields.io/badge/channel-beta-orange?style=flat-square)"
    else:
        channel_badge = "![Nightly](https://img.shields.io/badge/channel-nightly-blue?style=flat-square)"

    # 6. Analyze diff impact
    changed_files = []
    try:
        diff_cmd = ["git", "diff", "--name-only"]
        if changelog_from:
            diff_cmd.append(changelog_from)
        changed_files = run_cmd(diff_cmd).splitlines()
    except Exception:
        pass

    changed_files = [f.strip() for f in changed_files if f.strip()]
    total_files = len(changed_files)
    integration_count = 0
    translation_count = 0
    test_count = 0
    ci_count = 0
    docs_count = 0

    for f in changed_files:
        if f.startswith("custom_components/openwrt/translations/"):
            translation_count += 1
        elif f.startswith("custom_components/"):
            integration_count += 1
        elif f.startswith("tests/"):
            test_count += 1
        elif f.startswith(".github/") or f.startswith("scripts/"):
            ci_count += 1
        elif f.startswith("docs/") or f.endswith(".md"):
            docs_count += 1

    # Count breaking changes
    breaking_count = 0
    try:
        log_cmd = ["git", "log", "--format=%B"]
        if changelog_from:
            log_cmd.append(diff_range)
        log_msgs = run_cmd(log_cmd)
        # Find matches of BREAKING CHANGE or BREAKING: or conventional breaking !:
        breaking_count = len(
            re.findall(
                r"\bBREAKING CHANGE\b|\bBREAKING:\b|^[a-zA-Z]+!:",
                log_msgs,
                re.MULTILINE,
            )
        )
    except Exception:
        pass

    # Determine risk severity
    severity = "Low"
    alert_type = "NOTE"
    preamble = "This release introduces minor updates and code improvements."

    if breaking_count > 0:
        severity = "Critical"
        alert_type = "CAUTION"
        preamble = f"This release contains **{breaking_count} breaking change(s)**! Please review the changelog carefully and create a Home Assistant backup before updating."
    elif integration_count > 8:
        severity = "High"
        alert_type = "WARNING"
        preamble = "This release contains major updates to the integration logic. Creating a Home Assistant backup before updating is recommended."
    elif integration_count > 2 or translation_count > 5:
        severity = "Medium"
        alert_type = "TIP"
        preamble = "This release contains standard updates and feature enhancements."

    if release_type != "stable":
        preamble = f"ℹ️ **This is a {release_type} build.** It contains preview features for testing.<br><br>{preamble}"

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
        "  ·  ".join(impact_summary)
        if impact_summary
        else "No codebase changes detected."
    )

    # Build risk warning note
    prerelease_note = (
        f"> [!{alert_type}]\n"
        f"> **Release Risk: {severity}**\n"
        f"> {preamble}\n"
        f">\n"
        f"> **Affected areas:** {impact_str}\n"
    )

    # Assemble release body
    released_at = (
        datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M") + " UTC"
    )
    body_parts = [
        f"# OpenWrt {version}  {channel_badge}",
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
        f"| **Channel** | {release_type} |",
        f"| **Released** | {released_at} |",
        f"| **Commits included** | {total_commit_count} — {changelog_label} |",
        "",
        "---",
        "",
        f"*📖 [Documentation](https://github.com/{repo}#readme)  ·  🐛 [Report an Issue](https://github.com/{repo}/issues/new/choose)  ·  📦 [All Releases](https://github.com/{repo}/releases)*",
    ]

    body = "\n".join(body_parts)

    with open("release_body.md", "w", encoding="utf-8") as f:
        f.write(body)

    # Write GITHUB_OUTPUT variables
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"version={version}\n")
            f.write(f"tag={tag}\n")
            f.write(f"is_prerelease={is_prerelease}\n")
            # Write multiline output for release_body
            import uuid

            delimiter = f"DELIMITER_{uuid.uuid4().hex}"
            f.write(f"release_body<<{delimiter}\n")
            f.write(body)
            f.write(f"\n{delimiter}\n")


if __name__ == "__main__":
    main()
