def split_text(text, max_chars=1200):
    """
    Splits large code safely for embeddings.
    """

    parts = []

    start = 0
    length = len(text)

    while start < length:
        parts.append(text[start:start + max_chars])
        start += max_chars

    return parts