from langgraph.graph import StateGraph, END
from .state import ResearchState
from .nodes import researcher, synthesizer

builder = StateGraph(ResearchState)

builder.add_node("researcher", researcher)
builder.add_node("synthesizer", synthesizer)

builder.set_entry_point("researcher")
builder.add_edge("researcher", "synthesizer")
builder.add_edge("synthesizer", END)

graph = builder.compile()
