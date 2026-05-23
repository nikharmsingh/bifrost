import subprocess
from fastmcp import FastMCP
from core.logging import get_logger

logger = get_logger(__name__)
mcp = FastMCP("code-runner")


@mcp.tool()
def run_python(code: str, timeout: int = 10) -> str:
    """Execute a Python snippet in a subprocess and return stdout/stderr."""
    logger.info("Running Python snippet (%d chars)", len(code))
    result = subprocess.run(
        ["python", "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout + result.stderr
    return output.strip() or "(no output)"


if __name__ == "__main__":
    mcp.run()
