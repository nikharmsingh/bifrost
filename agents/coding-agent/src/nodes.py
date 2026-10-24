from functools import lru_cache

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from core.config import get_settings

from .state import CodingState


@lru_cache
def _get_model() -> ChatAnthropic:
    settings = get_settings()
    return ChatAnthropic(model=settings.default_model, api_key=settings.anthropic_api_key)


def coder(state: CodingState) -> CodingState:
    response = _get_model().invoke([HumanMessage(content=f"Write clean Python code for: {state['task']}")])
    return {**state, "code": response.content}


def reviewer(state: CodingState) -> CodingState:
    response = _get_model().invoke(
        [HumanMessage(content=f"Review this code for bugs, style, and correctness:\n\n{state['code']}")]
    )
    return {**state, "review": response.content}
