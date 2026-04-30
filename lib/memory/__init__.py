"""JuliusCaesar memory library — llm-wiki + SQLite FTS5.

Source of truth is markdown files under `<instance>/memory/L1/` and `L2/`.
The SQLite index is derived and rebuildable.

Public API:
    connect(instance_dir: Path) -> sqlite3.Connection
    parse_markdown(path: Path, instance_dir: Path) -> Entry | None
    upsert(conn, entry: Entry) -> None
    rebuild(conn, instance_dir: Path) -> tuple[int, int]
    search(conn, query: str, limit: int = 10) -> list[Row]
    get(conn, slug: str) -> Row | None
    backlinks_for(conn, slug: str) -> list[str]
    touch_accessed(conn, slug: str) -> None
"""

from .db import (
    Entry,
    backlinks_for,
    connect,
    db_path,
    get,
    memory_dir,
    parse_markdown,
    rebuild,
    search,
    touch_accessed,
    upsert,
)

__all__ = [
    "Entry",
    "backlinks_for",
    "connect",
    "db_path",
    "get",
    "memory_dir",
    "parse_markdown",
    "rebuild",
    "search",
    "touch_accessed",
    "upsert",
]
