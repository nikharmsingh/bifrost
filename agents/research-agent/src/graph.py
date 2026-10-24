from langgraph.graph import END, StateGraph

from .nodes import researcher, synthesizer
from .state import ResearchState

builder = StateGraph(ResearchState)

builder.add_node("researcher", researcher)
builder.add_node("synthesizer", synthesizer)

builder.set_entry_point("researcher")
builder.add_edge("researcher", "synthesizer")
builder.add_edge("synthesizer", END)

graph = builder.compile()
