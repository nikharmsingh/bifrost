from langgraph.graph import END, StateGraph

from .nodes import coder, reviewer
from .state import CodingState

builder = StateGraph(CodingState)
builder.add_node("coder", coder)
builder.add_node("reviewer", reviewer)

builder.set_entry_point("coder")
builder.add_edge("coder", "reviewer")
builder.add_edge("reviewer", END)

graph = builder.compile()
