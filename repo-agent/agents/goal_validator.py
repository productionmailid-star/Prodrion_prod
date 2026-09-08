from agents.llm import LocalLLM


class GoalValidator:
    """
    Checks whether a developer goal is achievable within the
    type of repository that has been scanned.
    """

    def __init__(self, repo_files: list):
        self.llm = LocalLLM()
        self.repo_files = repo_files

    def validate(self, goal: str) -> str:

        repo_text = "\n".join(self.repo_files)

        system = """\
You are a software architect reviewing whether a developer goal
is achievable within the given codebase.

Repository characteristics:
- Backend API (Python, Node, Java, Go, Rust, or similar)
- No frontend HTML/CSS/JS rendering (unless .tsx/.jsx files are listed)
- Logic lives in files listed below

Rules:
- If the goal requires creating a frontend UI that doesn't exist → INVALID
- If the goal is achievable by changing backend logic alone → VALID
- If unsure, lean toward VALID

Respond with ONLY one word: VALID or INVALID
"""

        user = f"""\
Goal:
{goal}

Repository files:
{repo_text}
"""

        return self.llm.chat(system, user).strip()
