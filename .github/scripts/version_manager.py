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


def get_current_version(manifest_path):
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
    except (subprocess.CalledProcessError, IndexError, ValueError):
        pass
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path) as f:
            return json.load(f).get("version", "1.0.0")
    return "1.0.0"


def write_version(v, manifest_path):
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path) as f:
            data = json.load(f)
        data["version"] = v
        with open(manifest_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    # Update pyproject.toml if it exists
    if os.path.exists("pyproject.toml"):
        with open("pyproject.toml") as f:
            content = f.read()
        content = re.sub(
            r'^version\s*=\s*".*?"', f'version = "{v}"', content, flags=re.MULTILINE
        )
        with open("pyproject.toml", "w") as f:
            f.write(content)


def calculate_version(rtype, curr):
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:(b)(\d+)|(-dev)(\d+))?$", curr)
    if not match:
        # Fallback for old CalVer or invalid versions
        return "1.5.0"

    major, minor, patch, b_p, b_n, d_p, d_n = match.groups()
    major, minor, patch = int(major), int(minor), int(patch)
    stype, snum = ("b", int(b_n)) if b_p else (("-dev", int(d_n)) if d_p else (None, 0))

    if rtype == "stable":
        if stype:
            return f"{major}.{minor}.{patch}"
        return f"{major}.{minor}.{patch + 1}"

    if rtype == "beta":
        if stype == "b":
            return f"{major}.{minor}.{patch}b{snum + 1}"
        return f"{major}.{minor}.{patch + 1}b0"

    if rtype in ["dev", "nightly"]:
        if stype == "-dev":
            return f"{major}.{minor}.{patch}-dev{snum + 1}"
        return f"{major}.{minor}.{patch + 1}-dev0"

    raise ValueError(f"Unknown type: {rtype}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["get", "bump"])
    parser.add_argument("--type", choices=["stable", "beta", "nightly", "dev"])
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args()
    m_path = args.manifest or find_manifest()
    if args.action == "get":
        print(get_current_version(m_path))
    elif args.action == "bump":
        v = calculate_version(args.type, get_current_version(m_path))
        write_version(v, m_path)
        print(v)
