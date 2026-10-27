"""Run only offline tests in the already-present pinned Hermes image."""
from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
(ROOT / "artifacts").mkdir(exist_ok=True)
IMAGE = "nousresearch/hermes-agent:v0.22.1@sha256:861c9756ed6c558530c73d5c27860343b00c0d5efd6997a1d8ec6416c8a2b485"
command = [
    "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only",
    "--tmpfs", "/tmp:mode=1777",
    "--mount", f"type=bind,src={ROOT},dst=/work,readonly",
    "--workdir", "/work", "-e", "PYTHONPATH=/work/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
    "-e", "HERMES_HOME=/tmp/moonawn-hermes-test",
    "--entrypoint", "/opt/hermes/.venv/bin/python", IMAGE,
    "-B", "-m", "unittest", "discover", "-s", "tests", "-q",
]
result = subprocess.run(command, capture_output=True, text=True, timeout=240)
output = result.stdout + result.stderr
match = re.search(r"Ran (\d+) tests", output)
report = {
    "image": IMAGE, "passed": result.returncode == 0,
    "tests_run": int(match.group(1)) if match else None,
    "network": "none", "root_filesystem": "read-only",
    "mounts": ["this independent project read-only; no production state/config mount"],
    "entrypoint": "Python unittest; no gateway startup",
    "live_integration_verified": False,
    "test_output": output.strip(),
}
(ROOT / "artifacts/container-verification.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
raise SystemExit(result.returncode)
