from collections import deque
from typing import Any


class MemoryStore:
    """Simple in-process conversation memory. Replace with a vector store for production."""

    def __init__(self, max_turns: int = 20) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=max_turns * 2)

    def add(self, role: str, content: str) -> None:
        self._history.append({"role": role, "content": content})

    def messages(self) -> list[dict[str, Any]]:
        return list(self._history)

    def clear(self) -> None:
        self._history.clear()
