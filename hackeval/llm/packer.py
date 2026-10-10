"""Pack a repository into LLM input: README, file tree, key source files, up to a token cap."""

from __future__ import annotations

import re
from pathlib import Path

from ..quality import iter_code_files

CHARS_PER_TOKEN = 4
KEY_NAMES = re.compile(
    r"(^|/)(main|app|index|server|api|routes?|views?|controllers?|models?|schema|handlers?|service|"
    r"urls|manage|application)\.\w+$", re.I)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def pack_repo(root: Path, token_cap: int = 30000) -> tuple[str, dict]:
    budget = token_cap * CHARS_PER_TOKEN
    parts: list[str] = []
    meta = {"readme_chars": 0, "files_included": [], "files_total": 0, "truncated": False}

    readmes = sorted((p for p in root.iterdir() if p.is_file() and p.name.lower().startswith("readme")),
                     key=lambda p: -p.stat().st_size) if root.exists() else []
    if readmes:
        txt = readmes[0].read_text(errors="ignore")[: int(budget * 0.25)]
        parts.append(f'<file path="{readmes[0].name}">\n{txt}\n</file>')
        meta["readme_chars"] = len(txt)

    files = list(iter_code_files(root))
    meta["files_total"] = len(files)
    tree = "\n".join(sorted(rel for _, rel in files))[: int(budget * 0.08)]
    parts.append(f"<file_tree>\n{tree}\n</file_tree>")

    def priority(item):
        p, rel = item
        size = p.stat().st_size
        key = 0 if KEY_NAMES.search(rel) else 1
        depth = rel.count("/")
        test = 1 if re.search(r"test|spec|mock|fixture", rel, re.I) else 0
        return (test, key, depth, -min(size, 20000))

    used = sum(len(x) for x in parts)
    for p, rel in sorted(files, key=priority):
        if used >= budget:
            meta["truncated"] = True
            break
        try:
            if p.stat().st_size > 200_000:
                continue
            txt = p.read_text(errors="ignore")
        except OSError:
            continue
        room = budget - used
        if len(txt) > min(room, 24000):
            txt = txt[: min(room, 24000)] + "\n...[file truncated]"
            meta["truncated"] = True
        block = f'<file path="{rel}">\n{txt}\n</file>'
        parts.append(block)
        used += len(block)
        meta["files_included"].append(rel)
    packed = "\n\n".join(parts)
    meta["tokens_est"] = estimate_tokens(packed)
    return packed, meta
