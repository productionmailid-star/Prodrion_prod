import ollama


class LocalLLM:
    """
    Thin wrapper around Ollama's chat API.
    Defaults to codellama with low temperature for deterministic output.

    Supports response prefilling: pass `prefill` to force the model to
    begin its reply with a specific string, locking it into a format.
    """

    def __init__(self, model: str = "codellama", temperature: float = 0.1):
        self.model = model
        self.temperature = temperature

    def chat(self, system: str, user: str, prefill: str = "") -> str:
        """
        Chat with optional prefill.

        prefill: a string that is injected as the start of the assistant
                 turn, forcing the model to continue from that prefix.
                 This is the most reliable way to enforce output formats
                 with instruction-tuned local models like CodeLlama.
        """
        messages = [
            {"role": "system",    "content": system},
            {"role": "user",      "content": user},
        ]

        if prefill:
            messages.append({"role": "assistant", "content": prefill})

        response = ollama.chat(
            model=self.model,
            messages=messages,
            options={
                "temperature": self.temperature,
            },
        )

        content = response["message"]["content"]

        # When prefill is used Ollama returns only the *continuation*,
        # so we re-attach the prefix to give callers the full string.
        if prefill:
            content = prefill + content

        return content
