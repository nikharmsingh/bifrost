import subprocess
import sys

from fastmcp import FastMCP

from core.logging import get_logger

logger = get_logger(__name__)
mcp = FastMCP("code-runner")

MAX_TIMEOUT = 30


@mcp.tool()
def run_python(code: str, timeout: int = 10) -> str:
    """Execute a Python snippet in a subprocess and return stdout/stderr.

    WARNING: this is NOT a sandbox — code runs with the server's full permissions.
    Only expose this server over local stdio to trusted clients, or run it inside a locked-down container.
    """
    timeout = min(max(timeout, 1), MAX_TIMEOUT)
    logger.info("Running Python snippet (%d chars)", len(code))
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"
    output = result.stdout + result.stderr
    return output.strip() or "(no output)"


if __name__ == "__main__":
    mcp.run()
