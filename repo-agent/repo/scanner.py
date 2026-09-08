from pathlib import Path

from repo.filters import IGNORE_DIRS, SUPPORTED_EXTENSIONS


class RepoScanner:

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)

    def should_ignore(self, path: Path):
        return any(part in IGNORE_DIRS for part in path.parts)

    def scan_files(self):
        files = []

        for file in self.repo_path.rglob("*"):

            if file.is_dir():
                continue

            if self.should_ignore(file):
                continue

            if file.suffix not in SUPPORTED_EXTENSIONS:
                continue

            files.append(str(file.relative_to(self.repo_path)))

        return files

    def build_tree(self):
        tree = {}

        for file in self.scan_files():
            parts = file.split("/")
            current = tree

            for part in parts:
                current = current.setdefault(part, {})

        return tree