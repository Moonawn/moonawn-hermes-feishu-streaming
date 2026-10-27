"""Optional research-cache restoration from pinned public GitHub source only.

This is not imported by the package or run by tests. A failed fetch/hash check
never replaces a valid cached file. No installer or downloaded source executes.
"""
from hashlib import sha256
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
lock = json.loads((ROOT / "docs/source-lock.json").read_text())
failures = []
for entry in lock["files"]:
    target = ROOT / ".reference-cache" / entry["repo"].replace("/", "--") / entry["path"]
    if target.is_file() and sha256(target.read_bytes()).hexdigest() == entry["sha256"]:
        continue
    url = "https://raw.githubusercontent.com/" + "/".join(
        [entry["repo"], entry["revision"], entry["path"]])
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "moonawn-feishu-research"}), timeout=20) as response:
            content = response.read(2_000_001)
        if len(content) > 2_000_000 or sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("reference content differs from reviewed hash")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        print("Restored", entry["repo"], entry["path"])
    except Exception as error:
        failures.append(entry["repo"] + "/" + entry["path"] + ": " + type(error).__name__)
print(json.dumps({"failures": failures, "no_downloaded_source_executed": True}, indent=2))
raise SystemExit(bool(failures))
