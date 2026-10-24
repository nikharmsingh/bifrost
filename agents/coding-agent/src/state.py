from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class CodingState(TypedDict):
    messages: Annotated[list, add_messages]
    task: str
    code: str
    review: str
