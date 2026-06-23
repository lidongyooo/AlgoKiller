from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _disable_import_cache() -> None:
    importlib.invalidate_caches()
    for path in SRC.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


_disable_import_cache()

from algokiller_harness.cli import main  # noqa: E402


DEFAULT_TRACE_FILE = "/Users/lidongyooo/custom/tiktok/traces/trace_1009_main.log"
DEFAULT_MODE = "ciphertext"


def _default_args() -> list[str]:
    args = sys.argv[1:]
    if any(arg == "--resume-session" or arg.startswith("--resume-session=") for arg in args):
        return []
    defaults = []
    if not any(arg == "--trace-file" or arg.startswith("--trace-file=") for arg in args):
        defaults.extend(["--trace-file", DEFAULT_TRACE_FILE])
    if not any(arg == "--mode" or arg.startswith("--mode=") for arg in args):
        defaults.extend(["--mode", DEFAULT_MODE])
    if not args:
        defaults.append("--interactive")
    return defaults


if __name__ == "__main__":
    sys.argv[1:1] = _default_args()
    raise SystemExit(main())
