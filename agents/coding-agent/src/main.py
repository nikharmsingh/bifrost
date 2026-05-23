import sys
from .graph import graph


def run(task: str) -> dict:
    result = graph.invoke({"task": task, "messages": [], "code": "", "review": ""})
    return {"code": result["code"], "review": result["review"]}


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or "Write a Python function that reverses a linked list"
    output = run(task)
    print("=== Generated Code ===")
    print(output["code"])
    print("\n=== Review ===")
    print(output["review"])
