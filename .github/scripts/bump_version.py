import re
import sys
import subprocess

def get_latest_tag():
    try:
        # Get all tags sorted by version-like order
        result = subprocess.run(
            ["git", "tag", "--list", "--sort=-v:refname"],
            capture_output=True,
            text=True,
            check=True
        )
        tags = result.stdout.strip().split("\n")
        return tags[0] if tags and tags[0] else None
    except Exception:
        return None

def parse_version(v_str):
    # Parses X.Y.Z-beta.N or X.Y.Z
    # Returns (major, minor, patch, is_beta, beta_num)
    core = v_str.split('-')[0]
    parts = list(map(int, core.split('.')))
    while len(parts) < 3:
        parts.append(0)
    
    is_beta = '-' in v_str and 'beta' in v_str
    beta_num = -1
    if is_beta:
        match = re.search(r'beta\.(\d+)', v_str)
        if match:
            beta_num = int(match.group(1))
    
    return parts[0], parts[1], parts[2], is_beta, beta_num

def bump_version(current, bump_type, release_status, all_tags=None):
    is_target_beta = release_status == "beta"
    
    if not current:
        if is_target_beta:
            return "1.0.0-beta.0"
        return "1.0.0"

    major, minor, patch, is_curr_beta, curr_beta_num = parse_version(current)
    
    # Find latest STABLE tag for base calculation
    latest_stable = (0, 0, 0)
    if all_tags:
        for t in all_tags:
            t = t.lstrip('v')
            if t and '-' not in t:
                s_major, s_minor, s_patch, _, _ = parse_version(t)
                latest_stable = (s_major, s_minor, s_patch)
                break
    else:
        try:
            res = subprocess.run(
                ["git", "tag", "--list", "--sort=-v:refname"],
                capture_output=True, text=True
            )
            for t in res.stdout.strip().split("\n"):
                t = t.lstrip('v')
                if t and '-' not in t:
                    s_major, s_minor, s_patch, _, _ = parse_version(t)
                    latest_stable = (s_major, s_minor, s_patch)
                    break
        except: pass

    # Calculate Target Stable Core
    if bump_type == "major":
        target_core = (latest_stable[0] + 1, 0, 0)
    elif bump_type == "minor":
        target_core = (latest_stable[0], latest_stable[1] + 1, 0)
    else: # patch
        target_core = (latest_stable[0], latest_stable[1], latest_stable[2] + 1)
    
    target_core_str = f"{target_core[0]}.{target_core[1]}.{target_core[2]}"

    if is_target_beta:
        # If current version's core matches target_core and is a beta, increment beta_num
        current_core = (major, minor, patch)
        if current_core == target_core and is_curr_beta:
            return f"{target_core_str}-beta.{curr_beta_num + 1}"
        else:
            return f"{target_core_str}-beta.0"
    else:
        # Stable release
        # If we are currently on a beta for this EXACT core, just strip the beta
        # (e.g. 1.1.0-beta.5 -> 1.1.0)
        current_core = (major, minor, patch)
        if current_core == target_core and is_curr_beta:
            return target_core_str
        
        # Otherwise, if we were on an OLDER version, the target_core is already bumped
        return target_core_str

def update_files(new_version):
    # Update pyproject.toml
    with open("pyproject.toml", "r") as f:
        content = f.read()
    # Match version = "X.Y.Z"
    content = re.sub(r'version\s*=\s*"[^"]+"', f'version = "{new_version}"', content, count=1)
    with open("pyproject.toml", "w") as f:
        f.write(content)

    # Update manifest.json
    import json
    with open("custom_components/openwrt/manifest.json", "r") as f:
        manifest = json.load(f)
    manifest["version"] = new_version
    with open("custom_components/openwrt/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python bump_version.py <major|minor|patch> <stable|beta>")
        sys.exit(1)
    
    bump_type = sys.argv[1].lower()
    release_status = sys.argv[2].lower()
    
    latest_tag = get_latest_tag()
    current_v = latest_tag.lstrip('v') if latest_tag else None
    
    new_v = bump_version(current_v, bump_type, release_status)
    print(f"New Version: {new_v}")
    
    update_files(new_v)
    
    # Write to file for GitHub Actions
    with open("VERSION.txt", "w") as f:
        f.write(new_v)
