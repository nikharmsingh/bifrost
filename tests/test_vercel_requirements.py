"""Guard against deploying an app whose imports aren't in the requirements file Vercel installs for it.

Tests run against the full uv workspace, so a missing dependency only shows up in production
(this is how `httpx` took the landing page down). This checks each app's imports statically instead.
"""

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Import name -> distribution name, where they differ
IMPORT_TO_DIST = {"bs4": "beautifulsoup4", "sqlalchemy": "sqlalchemy", "multipart": "python-multipart"}
# Imports satisfied by another listed distribution's own dependencies
PROVIDED_BY = {"pydantic": "fastapi", "starlette": "fastapi"}


def _vercel_apps() -> list[Path]:
    builds = json.loads((ROOT / "vercel.json").read_text())["builds"]
    return [ROOT / b["src"] for b in builds]


def _effective_requirements(entrypoint: Path) -> tuple[Path, set[str]]:
    # Vercel uses the requirements.txt closest to the entrypoint, falling back to the repo root
    for directory in [entrypoint.parent, *entrypoint.parents]:
        candidate = directory / "requirements.txt"
        if candidate.exists():
            break
        if directory == ROOT:
            break
    names = set()
    for line in candidate.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return candidate, names


def _third_party_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return {m for m in modules if m not in sys.stdlib_module_names}


@pytest.mark.parametrize("entrypoint", _vercel_apps(), ids=lambda p: p.parts[-3])
def test_app_imports_are_in_its_vercel_requirements(entrypoint):
    requirements_file, requirements = _effective_requirements(entrypoint)
    missing = []
    for module in sorted(_third_party_imports(entrypoint)):
        dist = IMPORT_TO_DIST.get(module, module).lower()
        if dist not in requirements and PROVIDED_BY.get(module) not in requirements:
            missing.append(dist)
    assert not missing, (
        f"{entrypoint.relative_to(ROOT)} imports {missing} but {requirements_file.relative_to(ROOT)} doesn't list them"
    )
