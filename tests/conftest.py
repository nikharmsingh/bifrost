import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative_path: str) -> ModuleType:
    """Import a module by file path — every Vercel app is `api/index.py`, so normal imports would collide."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
