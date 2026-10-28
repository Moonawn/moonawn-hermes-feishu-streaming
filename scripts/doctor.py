"""Run from a source checkout or the unpacked plugin directory."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src' if (ROOT / 'src/moonawn_feishu').is_dir() else ROOT))
from moonawn_feishu.doctor import main

if __name__ == '__main__':
    raise SystemExit(main())
