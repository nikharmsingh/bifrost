import sys
from .graph import graph


def run(query: str) -> str:
    result = graph.invoke({"query": query, "messages": [], "findings": [], "final_answer": ""})
    return result["final_answer"]


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "What are the latest advances in AI agents?"
    print(run(query))
