"""One-off script — run with: uv run scripts/seed_db.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.logging import get_logger

logger = get_logger("seed_db")


def main() -> None:
    logger.info("Seeding database...")
    # TODO: add your seeding logic here
    logger.info("Done.")


if __name__ == "__main__":
    main()
