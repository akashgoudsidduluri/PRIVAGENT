"""Built-in Local SQLite MCP Server for PrivAgent.

Provides read and mutation tools over local SQLite databases.
Can be executed as a stdio MCP server:
    python -m backend.app.mcp.servers.sqlite_server
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from mcp.server import MCPServer

server = MCPServer("sqlite_explorer")


def _get_db_path(db_name: str) -> str:
    clean = os.path.basename((db_name or "").strip()) or "privagent.db"
    if os.path.isfile(clean):
        return clean
    app_data = os.environ.get("PRIVAGENT_DATA_DIR", ".")
    return os.path.join(app_data, clean)


@server.tool(name="list_tables", description="List all tables and views in the local SQLite database.")
def list_tables(database: str = "privagent.db") -> str:
    target = _get_db_path(database)
    if not os.path.exists(target):
        with sqlite3.connect(target) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT);")
            conn.execute("INSERT OR IGNORE INTO users VALUES (1, 'Akash', 'akash@example.com', 'admin');")
            conn.execute("INSERT OR IGNORE INTO users VALUES (2, 'Guest', 'guest@example.com', 'user');")
            conn.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, action TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);")
            conn.commit()

    with sqlite3.connect(target) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%';")
        rows = cursor.fetchall()
        if not rows:
            return "No user tables found in database."
        return "\n".join(f"{r[1].upper()}: {r[0]}" for r in rows)


@server.tool(name="describe_table", description="Get schema column definitions for a given SQLite table.")
def describe_table(table_name: str, database: str = "privagent.db") -> str:
    target = _get_db_path(database)
    # Sanitize table_name identifier
    clean_table = "".join(c for c in table_name if c.isalnum() or c == "_")
    with sqlite3.connect(target) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({clean_table});")
        columns = cursor.fetchall()
        if not columns:
            return f"Table '{table_name}' does not exist or has no columns."
        lines = [f"Table '{table_name}' Schema:"]
        for col in columns:
            lines.append(f"• {col[1]} ({col[2]}){' [PK]' if col[5] else ''}{' NOT NULL' if col[3] else ''}")
        return "\n".join(lines)


@server.tool(name="read_query", description="Read-only SQL SELECT query on the database.")
def read_query(query: str, database: str = "privagent.db") -> str:
    cleaned = query.strip()
    if not cleaned.lower().startswith("select") and not cleaned.lower().startswith("pragma"):
        return "Error: read_query only allows SELECT or PRAGMA statements."
    target = _get_db_path(database)
    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(cleaned)
        rows = cursor.fetchmany(50)
        if not rows:
            return "Query returned 0 rows."
        result = [dict(r) for r in rows]
        return json.dumps(result, indent=2, default=str)


@server.tool(name="delete_table", description="Drop or delete an existing table from the SQLite database.")
def delete_table(table_name: str, database: str = "privagent.db") -> str:
    target = _get_db_path(database)
    clean_table = "".join(c for c in table_name if c.isalnum() or c == "_")
    with sqlite3.connect(target) as conn:
        cursor = conn.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {clean_table};")
        conn.commit()
    return f"Successfully dropped table '{table_name}' from {database}."


if __name__ == "__main__":
    server.run(transport="stdio")
