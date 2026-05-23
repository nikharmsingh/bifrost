def build_system_prompt(role: str, context: str = "") -> str:
    base = f"You are {role}."
    return f"{base}\n\n{context}".strip() if context else base
