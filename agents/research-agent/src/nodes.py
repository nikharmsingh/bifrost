from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from core.config import get_settings
from .state import ResearchState

_model = ChatAnthropic(
    model="claude-sonnet-4-6",
    api_key=get_settings().anthropic_api_key,
)


def researcher(state: ResearchState) -> ResearchState:
    response = _model.invoke(
        [HumanMessage(content=f"Research the following topic and provide key findings:\n\n{state['query']}")]
    )
    return {**state, "findings": [response.content]}


def synthesizer(state: ResearchState) -> ResearchState:
    findings_text = "\n".join(state["findings"])
    response = _model.invoke(
        [HumanMessage(content=f"Synthesize these findings into a clear answer:\n\n{findings_text}")]
    )
    return {**state, "final_answer": response.content}
