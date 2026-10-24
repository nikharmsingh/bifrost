from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class ResearchState(TypedDict):
    messages: Annotated[list, add_messages]
    query: str
    findings: list[str]
    final_answer: str
