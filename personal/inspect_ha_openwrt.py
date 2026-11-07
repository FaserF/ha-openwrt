"""Inspect all OpenWrt entities, states, integration entries, and reload status."""

import json
import urllib.request

MCPHUB_URL = "https://mcphub.tailnet.fabiseitz.de/api/tools/call"
HEADERS = {
    "Authorization": "Bearer mcphub_7dc607c3bf36cdeae0e84b811d3ffd9969bfd90ac5d7877034510a1917d22ce5",
    "Content-Type": "application/json",
}


def call_tool(tool_name: str, args: dict):
    url = f"{MCPHUB_URL}/{tool_name}"
    req = urllib.request.Request(
        url, data=json.dumps(args).encode("utf-8"), headers=HEADERS
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        content = res.get("content", [])
        if content and "text" in content[0]:
            try:
                return json.loads(content[0]["text"])
            except Exception:
                return content[0]["text"]
        return res


print("=== 1. Checking Integration Config Entries ===")
integrations = call_tool("home-assistant-ha_get_integration", {"limit": 100})
if isinstance(integrations, dict):
    entries = integrations.get("entries", [])
    print(f"Total config entries returned: {len(entries)}")
    for e in entries:
        domain = e.get("domain", "")
        title = e.get("title", "")
        if (
            "openwrt" in domain.lower()
            or "xiaomi" in title.lower()
            or "openwrt" in title.lower()
        ):
            print(
                f"  -> Found OpenWrt Entry: {title} (domain: {domain}, entry_id: {e.get('entry_id')}, state: {e.get('state')})"
            )

print("\n=== 2. Fetching all OpenWrt Entities via search ===")
all_entities = []
offset = 0
while True:
    res = call_tool(
        "home-assistant-ha_search", {"query": "openwrt", "offset": offset, "limit": 50}
    )
    if not isinstance(res, dict) or "entities" not in res:
        break
    ents = res.get("entities", [])
    if not ents:
        break
    all_entities.extend(ents)
    if not res.get("has_more"):
        break
    offset = res.get("next_offset") or (offset + len(ents))

print(f"Total OpenWrt entities found: {len(all_entities)}")
bad_states = []
states_summary = {}

for ent in all_entities:
    eid = ent.get("entity_id")
    state = ent.get("state")
    fname = ent.get("friendly_name")
    states_summary[state] = states_summary.get(state, 0) + 1
    # Buttons are naturally 'unknown' in HA until pressed; but for sensors/binary_sensors/switches/device_trackers:
    domain = eid.split(".")[0]
    if domain not in ("button", "scene") and state in ("unavailable", "unknown"):
        bad_states.append(ent)

print("\nState summary across all entities:")
for s, count in sorted(states_summary.items()):
    print(f"  {s}: {count}")

print(
    f"\nEntities with unavailable/unknown state (excluding buttons): {len(bad_states)}"
)
for ent in bad_states:
    print(
        f"  - {ent.get('entity_id')} [{ent.get('state')}]: {ent.get('friendly_name')}"
    )

print("\n=== 4. Detailed check of all unavailable / unknown entities ===")
for ent in all_entities:
    if ent.get("state") in ("unavailable", "unknown"):
        print(
            f"  {ent.get('entity_id')} [{ent.get('state')}]: {ent.get('friendly_name')}"
        )
