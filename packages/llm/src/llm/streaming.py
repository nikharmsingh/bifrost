from collections.abc import Generator
from .client import get_client


def stream_text(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    system: str = "You are a helpful assistant.",
) -> Generator[str, None, None]:
    client = get_client()
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        yield from stream.text_stream
