# Code Agent

A local AI agent that understands your entire codebase and generates precise,
line-specific change suggestions based on a developer goal.

Runs 100% locally using **Ollama** + **CodeLlama** — no API keys required.

---

## How it works

```
Your Goal
    │
    ▼
[1] Scan        — finds all source files in your repo
[2] Chunk       — splits files into functions/classes using tree-sitter
[3] Index       — embeds every chunk into a local ChromaDB vector store
[4] Validate    — checks the goal makes sense for this codebase
[5] Discover    — semantic search + LLM picks the files to change
[6] Locate      — LLM reads those files and pinpoints exact lines
[7] Plan        — LLM generates suggested replacement code per line
    │
    ▼
Change Plan  (file · line numbers · current code · suggested code)
```

You decide what to apply. The agent only suggests.

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) installed and running

---

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Pull required Ollama models
ollama pull codellama
ollama pull nomic-embed-text
```

---

## Usage

```bash
python main.py
```

You will be prompted for:
1. **Repository path** — absolute or relative path to your codebase
2. **Your goal** — plain English description of what you want to change

### Example goals

```
Deactivate user accounts instead of deleting them
Add rate limiting to the login endpoint
Return paginated results from the /products endpoint
Add input validation to the create-order route
Log all failed authentication attempts to a file
```

---

## Directory structure

```
code_agent/
│   main.py               ← entry point (run this)
│   requirements.txt
│
├── agents/
│   │   __init__.py
│   │   llm.py            ← Ollama wrapper (CodeLlama)
│   │   goal_validator.py ← checks goal fits the repo
│   │   discovery.py      ← finds relevant files
│   │   behaviour_locator.py ← pinpoints exact lines
│   │   planner.py        ← generates change suggestions
│   │   reporter.py       ← formats final output
│
└── repo/
    │   __init__.py
    │   scanner.py        ← walks the repo, respects ignore rules
    │   chunker.py        ← tree-sitter AST chunking
    │   indexer.py        ← ChromaDB vector store
    │   splitter.py       ← splits large chunks for embedding
    │   filters.py        ← ignored dirs + supported extensions
    │   tree_view.py      ← optional tree printer
```

---

## Supported languages

`.py` · `.js` · `.ts` · `.tsx` · `.jsx` · `.java` · `.cpp` · `.c` · `.go` · `.rs`

Add more in `repo/filters.py` and `repo/chunker.py`.

---

## Re-indexing

On the first run the repo is indexed automatically.  
On subsequent runs you are asked whether to re-index.  
Re-index when you make significant changes to the codebase.

---

## Notes

- The `chroma_db/` folder is created automatically in your working directory.
- All processing is local — no data leaves your machine.
- Suggestions are read-only; the agent never writes to your files.
