from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from core.config import get_settings

_model = ChatAnthropic(
    model="claude-sonnet-4-6",
    api_key=get_settings().anthropic_api_key,
)


def coder(state: dict) -> dict:
    response = _model.invoke(
        [HumanMessage(content=f"Write clean Python code for: {state['task']}")]
    )
    return {**state, "code": response.content}


def reviewer(state: dict) -> dict:
    response = _model.invoke(
        [HumanMessage(content=f"Review this code for bugs, style, and correctness:\n\n{state['code']}")]
    )
    return {**state, "review": response.content}
