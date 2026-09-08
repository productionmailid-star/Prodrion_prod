from pathlib import Path
from tree_sitter import Parser
from tree_sitter_language_pack import get_language


class CodeChunker:

    def __init__(self):

        self.languages = {
            ".py": get_language("python"),
            ".js": get_language("javascript"),
            ".ts": get_language("typescript"),
            ".tsx": get_language("tsx"),
        }

    def chunk_file(self, file_path):

        suffix = Path(file_path).suffix

        if suffix not in self.languages:
            return []

        parser = Parser()
        parser.language = self.languages[suffix]

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()

        tree = parser.parse(bytes(code, "utf8"))
        root = tree.root_node

        chunks = []

        stack = [root]

        while stack:
            node = stack.pop()

            if node.type in [
                "function_definition",
                "class_definition",
                "method_definition",
                "function_declaration",
                "lexical_declaration"
            ]:

                chunks.append({
                    "file": str(file_path),
                    "type": node.type,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "code": code[node.start_byte:node.end_byte]
                })

            stack.extend(node.children)

        return chunks