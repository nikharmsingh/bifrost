from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class ResearchState(TypedDict):
    messages: Annotated[list, add_messages]
    query: str
    findings: list[str]
    final_answer: str
