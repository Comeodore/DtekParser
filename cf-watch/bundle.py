"""Packs dtek-brain for upload: brain.py as worker.py, the Europe/Kyiv tz file, and every app module that
app/brain.py imports (found by importing it, so the list cannot fall behind the code).

    python cf-watch/bundle.py <out.json>

Writes {"files": {path: text}, "hashes": {path: sha256[:16]}} and prints the hashes.
"""
import base64
import hashlib
import json
import subprocess
import sys
import zoneinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def app_modules() -> list[str]:
    probe = "import sys, app.brain; print('\\n'.join(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.'))))"
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return out.split()


def module_path(name: str) -> str:
    path = Path(*name.split("."))
    return str(path / "__init__.py") if (ROOT / path).is_dir() else f"{path}.py"


def kyiv_tzif() -> str:
    for base in zoneinfo.TZPATH:
        path = Path(base) / "Europe" / "Kyiv"
        if path.is_file():
            return f'DATA = "{base64.b64encode(path.read_bytes()).decode()}"\n'
    raise SystemExit("Europe/Kyiv tz file not found on this machine")


def main() -> None:
    files = {"worker.py": (ROOT / "cf-watch" / "brain.py").read_text(), "kyiv_tzif.py": kyiv_tzif()}
    for name in app_modules():
        path = module_path(name)
        files[path] = (ROOT / path).read_text()
    hashes = {path: hashlib.sha256(text.encode()).hexdigest()[:16] for path, text in files.items()}
    Path(sys.argv[1]).write_text(json.dumps({"files": files, "hashes": hashes}, ensure_ascii=False))
    print(json.dumps(hashes, indent=1))


if __name__ == "__main__":
    main()
