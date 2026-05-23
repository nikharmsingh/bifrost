from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from .nodes import coder, reviewer


class CodingState(TypedDict):
    messages: Annotated[list, add_messages]
    task: str
    code: str
    review: str


builder = StateGraph(CodingState)
builder.add_node("coder", coder)
builder.add_node("reviewer", reviewer)

builder.set_entry_point("coder")
builder.add_edge("coder", "reviewer")
builder.add_edge("reviewer", END)

graph = builder.compile()
