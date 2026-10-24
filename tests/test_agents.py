"""Smoke-test that each LangGraph agent's graph still builds (formerly the per-agent "deploy" workflows)."""

import os
import subprocess
import sys

import pytest

from conftest import ROOT

# Workspace packages on the path explicitly, so the subprocess doesn't depend on the venv's editable .pth files
WORKSPACE_PATH = os.pathsep.join(str(ROOT / "packages" / name / "src") for name in ("core", "llm"))


@pytest.mark.parametrize("agent", ["research-agent", "coding-agent"])
def test_agent_graph_compiles(agent):
    # Each agent is its own `src` package, so import it in a subprocess rooted at the agent
    result = subprocess.run(
        [sys.executable, "-c", "from src.graph import graph; print(type(graph).__name__)"],
        cwd=ROOT / "agents" / agent,
        env={**os.environ, "PYTHONPATH": WORKSPACE_PATH},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "CompiledStateGraph" in result.stdout
