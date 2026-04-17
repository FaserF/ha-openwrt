import argparse, datetime, json, os, re, subprocess, glob
def find_manifest():
    matches = glob.glob("custom_components/*/manifest.json")
    return matches[0] if matches else None
def get_current_version(manifest_path):
    try:
        tags = subprocess.check_output(["git", "tag"], stderr=subprocess.DEVNULL).decode().splitlines()
        v_tags = []
        for tag in tags:
            tag = tag.strip()
            match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:(b)(\d+)|(-dev)(\d+))?$", tag)
            if match:
                y, m, p, bp, bn, dp, dn = match.groups()
                v_tags.append({"tag": tag, "key": (int(y), int(m), int(p), (1 if bp else (0 if dp else 2)), (int(bn) if bp else (int(dn) if dp else 0)))})
        if v_tags: return sorted(v_tags, key=lambda x: x["key"], reverse=True)[0]["tag"]
    except: pass
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, "r") as f: return json.load(f).get("version", "2026.1.0")
    return "2026.1.0"
def write_version(v, manifest_path):
    with open("VERSION", "w") as f: f.write(v)
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, "r") as f: data = json.load(f)
        data["version"] = v
        with open(manifest_path, "w") as f: json.dump(data, f, indent=2); f.write("\n")
def calculate_version(rtype, curr):
    now = datetime.datetime.now(); year, month = now.year, now.month
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:(b)(\d+)|(-dev)(\d+))?$", curr)
    if match:
        cy, cm, cp, b_p, b_n, d_p, d_n = match.groups(); cy, cm, cp = int(cy), int(cm), int(cp)
        stype, snum = ("b", int(b_n)) if b_p else (("-dev", int(d_n)) if d_p else (None, 0))
    else: cy, cm, cp, stype, snum = 0, 0, 0, None, 0
    new_cyc = year != cy or month != cm; p = 0 if new_cyc else cp
    if rtype == "stable":
        if stype: return f"{year}.{month}.{p}"
        return f"{year}.{month}.0" if new_cyc else f"{year}.{month}.{p+1}"
    if rtype == "beta":
        if new_cyc: return f"{year}.{month}.0b0"
        return f"{year}.{month}.{p}b{snum+1}" if stype == "b" else f"{year}.{month}.{p+1}b0"
    if rtype in ["dev", "nightly"]:
        if new_cyc: return f"{year}.{month}.0-dev0"
        return f"{year}.{month}.{p}-dev{snum+1}" if stype == "-dev" else f"{year}.{month}.{p+1}-dev0"
    raise ValueError(f"Unknown type: {rtype}")
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["get", "bump"]); parser.add_argument("--type", choices=["stable", "beta", "nightly", "dev"]); parser.add_argument("--manifest", default=None)
    args = parser.parse_args(); m_path = args.manifest or find_manifest()
    if args.action == "get": print(get_current_version(m_path))
    elif args.action == "bump":
        v = calculate_version(args.type, get_current_version(m_path)); write_version(v, m_path); print(v)
