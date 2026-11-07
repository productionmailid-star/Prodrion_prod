import uuid

from llm import get_embedder
from repo.splitter import split_text


def _make_client(persist_path: str):
    """
    Support both modern chromadb (>=0.4) and older versions gracefully.
    """
    try:
        import chromadb

        # Modern API — PersistentClient (>=0.4.0)
        return chromadb.PersistentClient(path=persist_path)
    except AttributeError:
        # Older API fallback
        import chromadb
        from chromadb.config import Settings

        return chromadb.Client(
            Settings(
                persist_directory=persist_path,
                anonymized_telemetry=False,
            )
        )


class CodeIndexer:

    COLLECTION_NAME = "codebase"

    def __init__(self, persist_path: str = "./chroma_db"):
        """``persist_path`` must be unique per repo being analysed — callers
        that index more than one repo over the process lifetime (the
        production worker) MUST pass a distinct, job-scoped path, or every
        repo after the first silently reuses the first repo's stale index
        (``is_empty()`` would already be False, so nothing gets re-indexed,
        and search() returns chunks from the wrong codebase entirely). The
        standalone interactive CLI (repo-agent/main.py), which only ever
        analyses one repo per run, is fine relying on the default.
        """
        self.client = _make_client(persist_path)
        self.collection = self.client.get_or_create_collection(name=self.COLLECTION_NAME)

    # ── Status helpers ────────────────────────────────────────

    def is_empty(self) -> bool:
        """Return True if the collection has no vectors yet."""
        return self.collection.count() == 0

    def clear(self):
        """Drop and recreate the collection (full re-index)."""
        self.client.delete_collection(self.COLLECTION_NAME)
        self.collection = self.client.get_or_create_collection(name=self.COLLECTION_NAME)

    # ── Embedding ────────────────────────────────────────────

    def embed(self, text: str):
        """
        Embed text via the configured provider (llm.get_embedder).
        Retries with progressively smaller input on failure.
        """
        size = len(text)
        attempts = 5

        while attempts > 0:
            try:
                vectors = get_embedder().embed([text[:size]])
                if vectors and len(vectors[0]) > 0:
                    return vectors[0]
            except Exception:
                pass

            attempts -= 1
            size = max(200, int(size * 0.7))

        return None

    # ── Indexing ─────────────────────────────────────────────

    def add_chunks(self, chunks):
        added = 0

        for chunk in chunks:
            if len(chunk["code"]) < 50:
                continue

            for piece in split_text(chunk["code"]):
                emb = self.embed(piece)
                if emb is None:
                    continue

                self.collection.add(
                    ids=[str(uuid.uuid4())],
                    embeddings=[emb],
                    documents=[piece],
                    metadatas=[
                        {
                            "file": chunk["file"],
                            "start_line": chunk["start_line"],
                            "end_line": chunk["end_line"],
                            "type": chunk["type"],
                        }
                    ],
                )
                added += 1

        print(f"  Indexed {added} chunk(s)")

    # ── Search ───────────────────────────────────────────────

    def search(self, query: str, k: int = 5):
        emb = self.embed(query)

        if emb is None:
            raise RuntimeError(
                "Query embedding failed. "
                "Is 'nomic-embed-text' pulled in Ollama? "
                "Run: ollama pull nomic-embed-text"
            )

        return self.collection.query(
            query_embeddings=[emb],
            n_results=min(k, max(self.collection.count(), 1)),
        )
