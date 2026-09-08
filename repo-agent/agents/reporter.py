class Reporter:
    """
    Assembles and prints the final change plan in the exact format:

        GOAL:
        <goal>
        FILES_TO_MODIFY:
        <file1>
        ----------------------------------------
        FILE: <path>
        REASON:
        ...
        PATCH:
        ```diff
        @@ line N
        - real line from file (verified)
        + suggested replacement
        ```
        ----------------------------------------
    """

    _SEP = "-" * 40

    def display(self, goal: str, files: list, patches: list[dict]):

        # ── Header ───────────────────────────────────────────
        print()
        print("GOAL:")
        print(goal)
        print()
        print("FILES_TO_MODIFY:")
        for f in files:
            print(f)
        print()

        # ── Per-file patches ─────────────────────────────────
        for entry in patches:
            print(self._SEP)

            # Print warnings first if any
            for w in entry.get("warnings", []):
                print(w)
            if entry.get("warnings"):
                print()

            print(entry["patch"].strip())
            print()

        # ── Footer ───────────────────────────────────────────
        print(self._SEP)
        print("NOTE: These are suggestions only.")
        print("The - lines have been verified against your actual source files.")
        print("Review every + line before applying it to your codebase.")
        print(self._SEP)
        print()
