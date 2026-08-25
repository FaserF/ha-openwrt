import json
import os
import re
import sys
import urllib.request


def get_latest_ha_version():
    try:
        url = "https://pypi.org/pypi/homeassistant/json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data["info"]["version"]
    except Exception as e:  # noqa: BLE001
        print(f"Error fetching HA version: {e}")
        return "2026.6.2"


def get_service_version(repo_name):
    headers = {"User-Agent": "Mozilla/5.0"}

    if repo_name == "ha-openwrt":
        try:
            req = urllib.request.Request(
                "https://sysupgrade.openwrt.org/api/v1/latest", headers=headers
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data["latest"][0]
        except Exception as e:  # noqa: BLE001
            print(f"Error fetching OpenWrt version: {e}")
            return "25.12.4"

    elif repo_name == "hass-valetudo":
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/Hypfer/Valetudo/releases/latest",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data["tag_name"].lstrip("v")
        except Exception as e:  # noqa: BLE001
            print(f"Error fetching Valetudo version: {e}")
            return "2026.6.0"

    elif repo_name == "ha-NintendoSwitchCFW":
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/Atmosphere-NX/Atmosphere/releases/latest",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data["tag_name"].lstrip("v")
        except Exception as e:  # noqa: BLE001
            print(f"Error fetching Atmosphere version: {e}")
            return "1.8.0"

    return None


def clean_and_update_template(file_path, integration_version, ha_version, repo_name):
    if not os.path.exists(file_path):
        return False

    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    original_content = content

    # Format the integration version cleanly (ensure single "v" prefix)
    clean_ver = integration_version.lstrip("v")
    target_ver = f"v{clean_ver}"

    # Split by field block: a block starts with "  - type:"
    blocks = re.split(r"(\s*-\s*type:)", content)

    service_version = get_service_version(repo_name)

    for i in range(2, len(blocks), 2):
        block_content = blocks[i]

        # Determine if this is a field block we want to update
        field_id_match = re.search(r"id:\s*([a-zA-Z0-9_-]+)", block_content)
        if not field_id_match:
            continue
        field_id = field_id_match.group(1)

        # 1. Integration version placeholder
        if field_id in ("integration_version", "version"):

            def repl_ver(match):
                quote = match.group(1) or ""
                prefix = match.group(2) or ""
                return f"placeholder: {quote}{prefix}{target_ver}{quote}"

            new_block = re.sub(
                r'placeholder:\s*(["\']?)(e\.g\.\s*)?[^\n"\']+\1',
                repl_ver,
                block_content,
            )
            if new_block != block_content:
                blocks[i] = new_block
                block_content = new_block

        # 2. HA version placeholder
        elif field_id == "ha_version":

            def repl_ha(match):
                quote = match.group(1) or ""
                prefix = match.group(2) or ""
                return f"placeholder: {quote}{prefix}{ha_version}{quote}"

            new_block = re.sub(
                r'placeholder:\s*(["\']?)(e\.g\.\s*)?[^\n"\']+\1',
                repl_ha,
                block_content,
            )
            if new_block != block_content:
                blocks[i] = new_block
                block_content = new_block

        # 3. Service versions
        elif service_version and (
            (field_id == "openwrt_version" and repo_name == "ha-openwrt")
            or (field_id == "valetudo_version" and repo_name == "hass-valetudo")
            or (
                field_id == "atmosphere_version" and repo_name == "ha-NintendoSwitchCFW"
            )
        ):

            def repl_service(match):
                quote = match.group(1) or ""
                prefix = match.group(2) or ""
                atmosphere_prefix = match.group(3) or ""
                return f"placeholder: {quote}{prefix}{atmosphere_prefix}{service_version}{quote}"

            new_block = re.sub(
                r'placeholder:\s*(["\']?)(e\.g\.\s*)?(Atmosphere\s*)?[^\n"\']+\1',
                repl_service,
                block_content,
            )
            if new_block != block_content:
                blocks[i] = new_block
                block_content = new_block

        # 4. Make steps and expected optional
        if field_id in ("steps", "expected", "steps_to_reproduce", "expected_behavior"):
            new_block = re.sub(r"required:\s*true", "required: false", block_content)
            if new_block != block_content:
                blocks[i] = new_block
                block_content = new_block

    content = "".join(blocks)

    # 5. Privacy/Datenschutz Filter
    lines = content.splitlines()
    new_lines = []
    skip_mode = False
    skip_indent = 0

    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())

        if skip_mode:
            if indent > skip_indent:
                continue
            else:
                skip_mode = False

        if "- type: input" in line or "- type: textarea" in line:
            field_id = ""
            label_text = ""
            for j in range(1, 10):
                if i + j >= len(lines):
                    break
                next_line = lines[i + j]
                next_indent = len(next_line) - len(next_line.lstrip())
                if next_indent <= indent:
                    break
                if "id:" in next_line:
                    field_id = next_line.split("id:")[-1].strip().strip("'\"")
                if "label:" in next_line:
                    label_text = next_line.split("label:")[-1].strip().strip("'\"")

            # Identify fields to remove
            sensitive_ids = {
                "cf_zone",
                "api_key",
                "api_token",
                "token",
                "password",
                "phone_number",
                "phone",
            }
            sensitive_labels = {
                "api key",
                "api token",
                "password",
                "token",
                "private key",
                "phone number",
                "phone",
            }

            if field_id.lower() in sensitive_ids or any(
                sl in label_text.lower() for sl in sensitive_labels
            ):
                print(f"Removing sensitive field: id={field_id}, label={label_text}")
                skip_mode = True
                skip_indent = indent
                continue

        if "description:" in line:
            desc_lower = line.lower()
            if any(
                k in desc_lower
                for k in ["domain", "host", "ip address", "url", "instance", "address"]
            ):
                if "not share" not in desc_lower and "private" not in desc_lower:  # noqa: SIM102
                    line = (
                        line.rstrip()
                        + " (Do NOT share sensitive passwords, credentials, or public API keys. Use example.com or 192.168.1.1 instead.)"
                    )

        new_lines.append(line)

    updated_content = "\n".join(new_lines) + "\n"

    if updated_content != original_content:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(updated_content)
        return True
    return False


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else "v1.0.0"
    if not version.startswith("v") and "." in version:
        version = "v" + version

    ha_version = get_latest_ha_version()
    repo_name = os.path.basename(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    print(
        f"Updating templates for {repo_name} with Integration Version: {version}, HA Version: {ha_version}"
    )

    template_dir = ".github/ISSUE_TEMPLATE"
    if os.path.exists(template_dir):
        for filename in os.listdir(template_dir):
            if filename.endswith(".yml") or filename.endswith(".yaml"):
                path = os.path.join(template_dir, filename)
                changed = clean_and_update_template(
                    path, version, ha_version, repo_name
                )
                if changed:
                    print(f"Updated: {path}")
