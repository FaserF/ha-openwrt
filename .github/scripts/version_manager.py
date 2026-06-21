import argparse
import datetime
import glob
import json
import os
import re
import subprocess


def find_manifest():
    matches = glob.glob("custom_components/*/manifest.json")
    return matches[0] if matches else None


MANIFEST_FILE = find_manifest()


def get_current_version(manifest_path=None):
    if manifest_path is None:
        manifest_path = MANIFEST_FILE
    try:
        tags = (
            subprocess.check_output(["git", "tag"], stderr=subprocess.DEVNULL)
            .decode()
            .splitlines()
        )
        v_tags = []
        for tag in tags:
            tag = tag.strip()
            match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:(b)(\d+)|(-dev)(\d+))?$", tag)
            if match:
                y, m, p, bp, bn, dp, dn = match.groups()
                v_tags.append(
                    {
                        "tag": tag,
                        "key": (
                            int(y),
                            int(m),
                            int(p),
                            (1 if bp else (0 if dp else 2)),
                            (int(bn) if bp else (int(dn) if dp else 0)),
                        ),
                    }
                )
        if v_tags:
            return sorted(v_tags, key=lambda x: x["key"], reverse=True)[0]["tag"]
    except subprocess.CalledProcessError, IndexError, ValueError:
        pass
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            return json.load(f).get("version", "1.0.0")
    return "1.0.0"


def write_version(v, manifest_path=None):
    if manifest_path is None:
        manifest_path = MANIFEST_FILE
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        data["version"] = v
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    if os.path.exists("pyproject.toml"):
        with open("pyproject.toml", encoding="utf-8") as f:
            content = f.read()
        content = re.sub(
            r'^version\s*=\s*".*?"', f'version = "{v}"', content, flags=re.MULTILINE
        )
        with open("pyproject.toml", "w", encoding="utf-8") as f:
            f.write(content)


def calculate_version(rtype, level="patch", curr=None, now=None):
    if now is None:
        now = datetime.datetime.now()
    if curr is None:
        curr = get_current_version(MANIFEST_FILE)

    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:(b)(\d+)|(-dev)(\d+))?$", curr)
    if not match:
        return "1.0.0"

    v1_str, v2_str, v3_str, b_p, b_n, d_p, d_n = match.groups()
    v1, v2, v3 = int(v1_str), int(v2_str), int(v3_str)
    stype, snum = ("b", int(b_n)) if b_p else (("-dev", int(d_n)) if d_p else (None, 0))

    # Detect scheme based on major version (e.g. 2026 is CalVer, 1 or 2 is SemVer)
    is_calver = v1 >= 2020

    if is_calver:
        # CalVer Bumping Logic (Year.Month.Patch)
        year, month = now.year, now.month
        new_cyc = (year != v1) or (month != v2)
        p = 0 if new_cyc else v3

        if rtype == "stable":
            if stype:
                return f"{year}.{month}.{p}"
            return f"{year}.{month}.0" if new_cyc else f"{year}.{month}.{p + 1}"
        if rtype == "beta":
            if new_cyc:
                return f"{year}.{month}.0b0"
            if stype == "b":
                return f"{year}.{month}.{p}b{snum + 1}"
            if stype == "-dev":
                return f"{year}.{month}.{p}b0"
            return f"{year}.{month}.{p + 1}b0"
        if rtype in ["dev", "nightly"]:
            if new_cyc:
                return f"{year}.{month}.0-dev0"
            if stype == "-dev":
                return f"{year}.{month}.{p}-dev{snum + 1}"
            return f"{year}.{month}.{p + 1}-dev0"
    else:
        # SemVer Bumping Logic (Major.Minor.Patch)
        if rtype == "stable":
            if stype:
                return f"{v1}.{v2}.{v3}"
            if level == "major":
                return f"{v1 + 1}.0.0"
            if level == "minor":
                return f"{v1}.{v2 + 1}.0"
            return f"{v1}.{v2}.{v3 + 1}"
        if rtype == "beta":
            if stype == "b":
                return f"{v1}.{v2}.{v3}b{snum + 1}"
            if level == "major":
                return f"{v1 + 1}.0.0b0"
            if level == "minor":
                return f"{v1}.{v2 + 1}.0b0"
            return f"{v1}.{v2}.{v3 + 1}b0"
        if rtype in ["dev", "nightly"]:
            if stype == "-dev":
                return f"{v1}.{v2}.{v3}-dev{snum + 1}"
            if level == "major":
                return f"{v1 + 1}.0.0-dev0"
            if level == "minor":
                return f"{v1}.{v2 + 1}.0-dev0"
            return f"{v1}.{v2}.{v3 + 1}-dev0"

    return curr


def main():
    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers(dest="command")

    bump_parser = subparsers.add_parser("bump")
    bump_parser.add_argument(
        "--type", choices=["stable", "beta", "dev", "nightly"], required=True
    )
    bump_parser.add_argument(
        "--level", choices=["major", "minor", "patch"], default="patch"
    )

    args = p.parse_args()

    if args.command == "bump":
        new_v = calculate_version(args.type, args.level)
        write_version(new_v)
        print(new_v)


if __name__ == "__main__":
    main()
