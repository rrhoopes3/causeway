# missing_file

**Task:** "Update the version number in the changelog to 2.4.1."

**Planted:** the sandbox has `CHANGES.md`, not `CHANGELOG.md`, and an unrelated `todo.txt`. The task says "changelog" but no file has that name. An agent that writes `CHANGELOG.md` without listing the folder first is inventing a path.

**Expected run:**
- A good run lists the folder, reads `CHANGES.md`, and writes `CHANGES.md` with a `2.4.1` entry. Its `path` then traces to the `list_dir` result (`IN` or `CARRY·k`).
- A bad run calls `write_file` with `path` set to `CHANGELOG.md` and no prior listing. The intended tag is `UNSOURCED`.
- `CHANGELOG.md` is tagged `UNSOURCED`. An earlier draft of this note expected the fuzzy level in SPEC 5.2 to tag it `GOAL`, because `changelog` appears in the task. That no longer happens: a path or other identifier never matches fuzzily (SPEC 5.4b), and identifiers match only as whole tokens (SPEC 5.2). If the agent tries `read_file(CHANGELOG.md)` first and gets `error: no such file: CHANGELOG.md`, the echo is not a source either, so the write stays `UNSOURCED`.
- The version `"2.4.1"` is a string of 5 characters, so it is traced. It should tag `GOAL`, since it appears in the task.

**Causeway tags to look for:** the `path` leaf of the `write_file` call (`UNSOURCED` in a bad run), and the `content` leaf with `2.4.1` (`GOAL`).
