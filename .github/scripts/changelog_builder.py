import subprocess, re, argparse
def get_commits(tag=None):
    cmd = ["git", "log", f"{tag}..HEAD" if tag else "HEAD", "--pretty=format:%H|%h|%s"]
    try:
        out = subprocess.check_output(cmd, text=True).strip()
        return out.split("\n") if out else []
    except: return []
def build_changelog(commits, url):
    cats = {"✨ Features": [], "🐛 Bug Fixes": [], "📦 Dependencies": [], "🔧 Maintenance & CI": [], "🚀 Other": []}
    for l in commits:
        if "|" not in l: continue
        fh, sh, sub = l.split("|", 2); sl = sub.lower()
        if any(x in sl for x in ["chore: release", "chore: bump", "merge ", "[skip ci]"]): continue
        sub = re.sub(r"\(#(\d+)\)", rf"([#\1]({url}/pull/\1))", sub)
        entry = f"- {sub} ([{sh}]({url}/commit/{fh}))"
        if re.match(r"^(feat|add|new|✨)", sl): cats["✨ Features"].append(entry)
        elif re.match(r"^(fix|bug|patch|fixed|fixes|🐛)", sl): cats["🐛 Bug Fixes"].append(entry)
        elif re.match(r"^(deps|dep|update|bump|renovate|📦|⬆️)", sl): cats["📦 Dependencies"].append(entry)
        elif re.match(r"^(chore|ci|workflow|config|ruff|🔧)", sl): cats["🔧 Maintenance & CI"].append(entry)
        else: cats["🚀 Other"].append(entry)
    changelog = "## Changelog\n\n"
    for title, items in cats.items():
        if items: changelog += f"### {title}\n" + "\n".join(items) + "\n\n"
    return changelog or "No significant changes."
if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--from-tag"); p.add_argument("--repo-url", required=True); p.add_argument("--output", default="CHANGELOG_BODY.md")
    args = p.parse_args(); commits = get_commits(args.from_tag); changelog = build_changelog(commits, args.repo_url)
    with open(args.output, "w", encoding="utf-8") as f: f.write(changelog)
