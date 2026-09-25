"""Data tools: uploaded-file analysis and read-only SQL."""

from __future__ import annotations

import asyncio
import csv
import io
import re
import sqlite3
import statistics
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field

from agentplat.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

_FILE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,80}\.(csv|txt|md)$")


class FileAnalyzeArgs(ToolArgs):
    file_id: str = Field(max_length=100, description="Name of an uploaded file, e.g. sales.csv")


class FileAnalyze(Tool[FileAnalyzeArgs]):
    name: ClassVar[str] = "file_analyze"
    description: ClassVar[str] = "Summarise an uploaded CSV or text file."
    args_model = FileAnalyzeArgs
    required_permissions = frozenset({"files:read"})

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()

    async def run(self, args: FileAnalyzeArgs, ctx: ToolContext) -> ToolResult:
        if not _FILE_ID.match(args.file_id):
            raise ToolError("invalid file id")
        path = (self.base_dir / args.file_id).resolve()
        if self.base_dir not in path.parents or not path.is_file():  # path traversal guard
            raise ToolError(f"file {args.file_id!r} not found")
        raw = path.read_text(errors="replace")[:1_000_000]
        summary = _summarise_csv(raw) if path.suffix == ".csv" else _summarise_text(raw)
        return ToolResult(
            {"file_id": args.file_id, **summary}, tainted=True, source=f"file:{args.file_id}"
        )


def _summarise_csv(raw: str) -> dict[str, Any]:
    rows = list(csv.DictReader(io.StringIO(raw)))
    columns = list(rows[0].keys()) if rows else []
    numeric: dict[str, dict[str, float]] = {}
    for col in columns:
        try:
            values = [float(r[col]) for r in rows if r[col] not in ("", None)]
        except ValueError:
            continue
        if values:
            numeric[col] = {
                "min": min(values),
                "max": max(values),
                "mean": round(statistics.fmean(values), 4),
                "sum": round(sum(values), 4),
            }
    return {
        "kind": "csv",
        "rows": len(rows),
        "columns": columns,
        "numeric": numeric,
        "preview": rows[:3],
    }


def _summarise_text(raw: str) -> dict[str, Any]:
    return {
        "kind": "text",
        "lines": raw.count("\n") + 1,
        "words": len(raw.split()),
        "preview": raw[:1500],
    }


class SqlQueryArgs(ToolArgs):
    query: str = Field(max_length=2000, description="A single read-only SELECT statement")


class SqlQuery(Tool[SqlQueryArgs]):
    """Read-only SQL on a sample database.

    Defence in depth: the connection is opened read-only, and an sqlite
    authorizer rejects every action except reading, so even a clever
    statement (ATTACH, PRAGMA writes, INSERT in a CTE) is refused by the engine
    itself rather than by fragile string checks.
    """

    name: ClassVar[str] = "sql_query"
    description: ClassVar[str] = (
        "Run a read-only SELECT on the sample business database "
        "(tables: customers(id,name,city), orders(id,customer_id,amount,status))."
    )
    args_model = SqlQueryArgs
    required_permissions = frozenset({"db:read"})
    timeout_s = 5.0

    MAX_ROWS = 100
    MAX_VM_STEPS = 2_000_000

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        ensure_sample_db(db_path)

    async def run(self, args: SqlQueryArgs, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._run_sync, args.query)

    def _run_sync(self, query: str) -> ToolResult:
        if ";" in query.strip().rstrip(";"):
            raise ToolError("only a single statement is allowed")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            conn.set_authorizer(_read_only_authorizer)
            steps = {"n": 0}

            def progress() -> int:
                steps["n"] += 1000
                return 1 if steps["n"] > self.MAX_VM_STEPS else 0

            conn.set_progress_handler(progress, 1000)
            try:
                cur = conn.execute(query)
                rows = cur.fetchmany(self.MAX_ROWS + 1)
            except sqlite3.DatabaseError as exc:
                raise ToolError(f"query rejected: {exc}") from exc
            cols = [d[0] for d in cur.description or []]
            return ToolResult(
                {
                    "columns": cols,
                    "rows": [list(r) for r in rows[: self.MAX_ROWS]],
                    "truncated": len(rows) > self.MAX_ROWS,
                }
            )
        finally:
            conn.close()


_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,  # recursive CTEs are reads; the step limit bounds them
}


def _read_only_authorizer(action: int, arg1: str | None, *_: object) -> int:
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def ensure_sample_db(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL,
                                 status TEXT);
            INSERT INTO customers VALUES (1,'Asha','Pune'),(2,'Ravi','Mumbai'),(3,'Meera','Delhi');
            INSERT INTO orders VALUES (1,1,1200.0,'paid'),(2,1,450.5,'paid'),
                                      (3,2,999.0,'refunded'),(4,3,300.0,'paid'),
                                      (5,2,150.0,'pending');
            """
        )
        conn.commit()
    finally:
        conn.close()
