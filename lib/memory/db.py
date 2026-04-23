"""SQLite schema + FTS5 index for JuliusCaesar memory.

Source of truth is the .md files under <instance>/memory/L1/ and L2/.
The DB is a derived index. Call rebuild() after bulk edits; individual
writes go through upsert().

All functions take an explicit `instance_dir` (Path) — the library holds
no global state, making it safe to operate on multiple instances from
the same process.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - tiny fallback
    yaml = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    slug           TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    layer          TEXT NOT NULL CHECK (layer IN ('L1','L2')),
    type           TEXT,
    state          TEXT DEFAULT 'draft' CHECK (state IN ('draft','reviewed','verified','stale','archived')),
    path           TEXT NOT NULL UNIQUE,
    created        TEXT,
    updated        TEXT,
    last_verified  TEXT,
    last_accessed  TEXT,
    tags           TEXT,
    body           TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    slug UNINDEXED,
    title,
    tags,
    body,
    content='entries',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS backlinks (
    from_slug TEXT NOT NULL,
    to_slug   TEXT NOT NULL,
    PRIMARY KEY (from_slug, to_slug)
);

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts (rowid, slug, title, tags, body)
    VALUES (new.rowid, new.slug, new.title, new.tags, new.body);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts (entries_fts, rowid, slug, title, tags, body)
    VALUES ('delete', old.rowid, old.slug, old.title, old.tags, old.body);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts (entries_fts, rowid, slug, title, tags, body)
    VALUES ('delete', old.rowid, old.slug, old.title, old.tags, old.body);
    INSERT INTO entries_fts (rowid, slug, title, tags, body)
    VALUES (new.rowid, new.slug, new.title, new.tags, new.body);
END;
"""


# --- Path helpers ------------------------------------------------------------


def memory_dir(instance_dir: Path) -> Path:
    return instance_dir / "memory"


def db_path(instance_dir: Path) -> Path:
    return memory_dir(instance_dir) / "index.sqlite"


# --- Entry dataclass ---------------------------------------------------------


@dataclass
class Entry:
    slug: str
    title: str
    layer: str
    type: str | None
    state: str
    path: str
    created: str | None
    updated: str | None
    last_verified: str | None
    last_accessed: str | None
    tags: list[str]
    links: list[str]
    body: str


# --- Connection --------------------------------------------------------------


def connect(instance_dir: Path) -> sqlite3.Connection:
    db_path(instance_dir).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(instance_dir))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --- Frontmatter parsing -----------------------------------------------------


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def parse_markdown(path: Path, instance_dir: Path) -> Entry:
    """Parse a markdown file with YAML frontmatter into an Entry."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"Missing YAML frontmatter: {path}")
    raw_fm, body = m.group(1), m.group(2)

    fm = _load_yaml(raw_fm)

    slug = fm.get("slug") or _slug_from_path(path, instance_dir)
    layer = fm.get("layer") or _layer_from_path(path, instance_dir)

    links_explicit = fm.get("links") or []
    if isinstance(links_explicit, str):
        links_explicit = [links_explicit]
    links_wiki = WIKILINK_RE.findall(body)
    links = sorted({*links_explicit, *links_wiki})

    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    return Entry(
        slug=slug,
        title=str(fm.get("title") or slug),
        layer=layer,
        type=fm.get("type"),
        state=fm.get("state") or "draft",
        path=str(path.relative_to(instance_dir)),
        created=_iso(fm.get("created")),
        updated=_iso(fm.get("updated")),
        last_verified=_iso(fm.get("last_verified")),
        last_accessed=None,
        tags=[str(t) for t in tags],
        links=[str(l) for l in links],
        body=body.strip(),
    )


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _layer_from_path(p: Path, instance_dir: Path) -> str:
    rel = p.relative_to(instance_dir)
    parts = rel.parts
    # rel = memory/L1/foo.md or memory/L2/bar/baz.md
    if len(parts) >= 2 and parts[0] == "memory" and parts[1] == "L1":
        return "L1"
    return "L2"


def _slug_from_path(p: Path, instance_dir: Path) -> str:
    rel = p.relative_to(instance_dir).with_suffix("")
    parts = list(rel.parts)
    # Strip leading memory/ + L1|L2 prefix
    if parts and parts[0] == "memory":
        parts = parts[1:]
    if parts and parts[0] in ("L1", "L2"):
        parts = parts[1:]
    return "/".join(parts)


def _load_yaml(raw: str) -> dict:
    if yaml is not None:
        return yaml.safe_load(raw) or {}
    # Minimal fallback: key: value (no nesting).
    out: dict = {}
    key = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("- ") and key:
            out.setdefault(key, []).append(line[2:].strip())
            continue
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            key = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                out[key] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            elif v:
                out[key] = v.strip("'\"")
            else:
                out[key] = None
    return out


# --- Writes ------------------------------------------------------------------


def upsert(conn: sqlite3.Connection, entry: Entry) -> None:
    conn.execute(
        """
        INSERT INTO entries (slug, title, layer, type, state, path, created, updated, last_verified, tags, body)
        VALUES (:slug, :title, :layer, :type, :state, :path, :created, :updated, :last_verified, :tags, :body)
        ON CONFLICT(slug) DO UPDATE SET
            title=excluded.title,
            layer=excluded.layer,
            type=excluded.type,
            state=excluded.state,
            path=excluded.path,
            created=excluded.created,
            updated=excluded.updated,
            last_verified=excluded.last_verified,
            tags=excluded.tags,
            body=excluded.body
        """,
        {
            "slug": entry.slug,
            "title": entry.title,
            "layer": entry.layer,
            "type": entry.type,
            "state": entry.state,
            "path": entry.path,
            "created": entry.created,
            "updated": entry.updated,
            "last_verified": entry.last_verified,
            "tags": ",".join(entry.tags),
            "body": entry.body,
        },
    )
    conn.execute("DELETE FROM backlinks WHERE from_slug = ?", (entry.slug,))
    conn.executemany(
        "INSERT OR IGNORE INTO backlinks (from_slug, to_slug) VALUES (?, ?)",
        [(entry.slug, target) for target in entry.links],
    )


def delete_missing(conn: sqlite3.Connection, present_slugs: Iterable[str]) -> int:
    present = set(present_slugs)
    rows = conn.execute("SELECT slug FROM entries").fetchall()
    gone = [r["slug"] for r in rows if r["slug"] not in present]
    for s in gone:
        conn.execute("DELETE FROM entries WHERE slug = ?", (s,))
        conn.execute("DELETE FROM backlinks WHERE from_slug = ? OR to_slug = ?", (s, s))
    return len(gone)


def rebuild(conn: sqlite3.Connection, instance_dir: Path) -> tuple[int, int]:
    """Re-scan .md files under memory/L1 and memory/L2 and sync DB."""
    paths = list(_iter_md_files(instance_dir))
    for p in paths:
        try:
            entry = parse_markdown(p, instance_dir)
        except ValueError as e:
            print(f"[skip] {p}: {e}")
            continue
        upsert(conn, entry)
    removed = delete_missing(conn, (_slug_from_path(p, instance_dir) for p in paths))
    conn.commit()
    return len(paths), removed


def _iter_md_files(instance_dir: Path) -> Iterator[Path]:
    mem = memory_dir(instance_dir)
    for base in ("L1", "L2"):
        d = mem / base
        if not d.exists():
            continue
        for p in sorted(d.rglob("*.md")):
            yield p


# --- Reads -------------------------------------------------------------------


def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[sqlite3.Row]:
    """FTS5 ranked search. Terms are quoted as phrases for safety."""
    terms = [f'"{t}"' for t in query.split() if t.strip()]
    fts_q = " ".join(terms) if terms else query
    return conn.execute(
        """
        SELECT e.slug, e.title, e.layer, e.type, e.state, e.path, e.last_verified,
               snippet(entries_fts, 3, '[', ']', '…', 12) AS snip,
               bm25(entries_fts) AS score
        FROM entries_fts
        JOIN entries e ON e.rowid = entries_fts.rowid
        WHERE entries_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (fts_q, limit),
    ).fetchall()


def get(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entries WHERE slug = ?", (slug,)).fetchone()


def backlinks_for(conn: sqlite3.Connection, slug: str) -> list[str]:
    rows = conn.execute(
        "SELECT from_slug FROM backlinks WHERE to_slug = ? ORDER BY from_slug",
        (slug,),
    ).fetchall()
    return [r["from_slug"] for r in rows]


def touch_accessed(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute(
        "UPDATE entries SET last_accessed = ? WHERE slug = ?",
        (datetime.now().isoformat(timespec="seconds"), slug),
    )
    conn.commit()
