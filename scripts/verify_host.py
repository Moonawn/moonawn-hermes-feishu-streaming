"""Verify the built plugin with the real Hermes manager, without live access."""
from pathlib import Path
import json
import subprocess
import argparse

ROOT = Path(__file__).resolve().parents[1]
(ROOT / "artifacts").mkdir(exist_ok=True)
IMAGE = "nousresearch/hermes-agent:v0.22.1@sha256:861c9756ed6c558530c73d5c27860343b00c0d5efd6997a1d8ec6416c8a2b485"
parser = argparse.ArgumentParser()
parser.add_argument("--release-dir", type=Path, default=ROOT / "dist")
args = parser.parse_args()
relative_release = args.release_dir.resolve().relative_to(ROOT)
result = subprocess.run([
    "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only",
    "--tmpfs", "/tmp:mode=1777", "--mount", f"type=bind,src={ROOT},dst=/work,readonly",
    "--workdir", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HERMES_HOME=/tmp/moonawn-host-smoke",
    "--entrypoint", "/opt/hermes/.venv/bin/python", IMAGE, "-B", "scripts/host_smoke.py", "/work/" + str(relative_release),
], capture_output=True, text=True, timeout=900)
if result.returncode:
    report = {"passed": False, "image": IMAGE, "output": (result.stdout + result.stderr).strip()}
else:
    report = json.loads(result.stdout.strip().splitlines()[-1])
    report["image"] = IMAGE
(ROOT / "artifacts/host-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(result.returncode)
