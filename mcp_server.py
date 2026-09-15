"""
Analytics MCP Server
====================
Dual-mode server:
  - MCP tools   → for AI clients (Claude Desktop, Cursor, etc.) at /mcp
  - REST routes  → for n8n HTTP Request nodes at /tools/*
  - /ping        → keep-alive for Render free tier

Run locally:
    python mcp_server.py

Run with uvicorn (production):
    uvicorn mcp_server:app --host 0.0.0.0 --port 8000
"""

import os
import re
import math
import duckdb
import uvicorn
import pandas as pd

from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# ── Load .env ──────────────────────────────────────────────────────────────────
load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
DB_PATH   = os.getenv("DUCKDB_PATH", "/tmp/analytics.duckdb")
HOST      = os.getenv("HOST", "0.0.0.0")
PORT      = int(os.getenv("PORT", "8000"))
MCP_MOUNT = os.getenv("MCP_MOUNT_PATH", "/mcp")

# ── DuckDB ─────────────────────────────────────────────────────────────────────
conn = duckdb.connect(DB_PATH)

try:
    conn.execute("SELECT 1")
    print(f"✓ DuckDB initialized at {DB_PATH}")
except Exception as e:
    print(f"✗ DuckDB failed to initialize: {e}")
    raise

# ── MCP Server ─────────────────────────────────────────────────────────────────
mcp = MCPServer(
    name="analytics-server",
    instructions=(
        "Analytics server backed by DuckDB. "
        "Call get_schema_overview first to understand loaded tables. "
        "Use execute_cleaning_query only for DELETE/UPDATE/ALTER. "
        "Use run_analysis_query for SELECT only."
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS — JSON-safe serialization
# ══════════════════════════════════════════════════════════════════════════════

def safe_float(val) -> float | None:
    """
    Convert a numeric value to float, returning None for anything
    that is not valid JSON (NaN, Inf, -Inf, None).
    """
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def safe_df_to_records(df: pd.DataFrame) -> list:
    """
    Convert a DataFrame to a list of dicts, replacing every
    NaN / NaT / Inf / -Inf with None so json.dumps never chokes.
    """
    # First pass: pandas-level replace
    df = df.where(df.notna(), other=None)

    records = df.to_dict(orient="records")

    # Second pass: catch float nan/inf that slipped through
    cleaned = []
    for record in records:
        cleaned_record = {}
        for k, v in record.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                cleaned_record[k] = None
            else:
                cleaned_record[k] = v
        cleaned.append(cleaned_record)

    return cleaned


# ══════════════════════════════════════════════════════════════════════════════
# TOOL LOGIC — shared by both MCP tools and REST endpoints
# ══════════════════════════════════════════════════════════════════════════════

def _get_schema_overview() -> dict:
    tables_raw = conn.execute("SHOW TABLES").fetchall()
    if not tables_raw:
        return {
            "tables": {},
            "table_count": 0,
            "message": "No tables loaded yet. Upload a CSV first.",
        }

    schema = {}
    for (tbl,) in tables_raw:
        try:
            cols      = conn.execute(f"DESCRIBE {tbl}").fetchall()
            row_count = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            sample_df = conn.execute(f"SELECT * FROM {tbl} LIMIT 3").fetchdf()
            schema[tbl] = {
                "columns": [
                    {"name": c[0], "type": c[1], "nullable": c[2]}
                    for c in cols
                ],
                "row_count": row_count,
                "sample_rows": safe_df_to_records(sample_df),
            }
        except Exception as e:
            schema[tbl] = {"error": str(e)}

    return {"tables": schema, "table_count": len(schema)}


# ── Cleaning ───────────────────────────────────────────────────────────────────
ALLOWED_PREFIXES = ("DELETE FROM", "UPDATE", "ALTER TABLE")
BLOCKED_KEYWORDS = ("DROP", "TRUNCATE", "CREATE", "INSERT", "REPLACE", "COPY")


def _execute_cleaning_query(sql: str) -> dict:
    normalized = sql.strip().upper()

    for kw in BLOCKED_KEYWORDS:
        if kw in normalized:
            return {
                "success": False,
                "error": f"Blocked keyword '{kw}' detected. Only DELETE/UPDATE/ALTER allowed.",
                "sql": sql,
            }

    if not any(normalized.startswith(p) for p in ALLOWED_PREFIXES):
        return {
            "success": False,
            "error": f"Statement must start with one of: {ALLOWED_PREFIXES}",
            "sql": sql,
        }

    try:
        result = conn.execute(sql)
        try:
            rows_affected = result.rowcount if result.rowcount >= 0 else "n/a (ALTER)"
        except Exception:
            rows_affected = "n/a"
        return {"success": True, "rows_affected": rows_affected, "sql": sql}
    except Exception as e:
        return {"success": False, "error": str(e), "sql": sql}


# ── Insights ───────────────────────────────────────────────────────────────────
def _generate_initial_insights(table_name: str) -> dict:
    tables = [t[0] for t in conn.execute("SHOW TABLES").fetchall()]
    if table_name not in tables:
        return {"error": f"Table '{table_name}' not found. Available: {tables}"}

    try:
        cols_raw  = conn.execute(f"DESCRIBE {table_name}").fetchall()
        cols      = [(c[0], c[1]) for c in cols_raw]
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]

        # ── Null counts ────────────────────────────────────────────────────────
        null_exprs = ", ".join(
            f"SUM(CASE WHEN \"{c}\" IS NULL THEN 1 ELSE 0 END) AS \"{c}_nulls\""
            for c, _ in cols
        )
        nulls_df = conn.execute(
            f"SELECT {null_exprs} FROM {table_name}"
        ).fetchdf()
        nulls = safe_df_to_records(nulls_df)[0]

        # ── Numeric stats ──────────────────────────────────────────────────────
        numeric_types = (
            "INTEGER", "BIGINT", "DOUBLE", "FLOAT",
            "DECIMAL", "NUMERIC", "HUGEINT", "SMALLINT", "TINYINT",
        )
        numeric_cols  = [c for c, t in cols if any(nt in t.upper() for nt in numeric_types)]
        numeric_stats = {}

        for col in numeric_cols:
            row = conn.execute(
                f'SELECT AVG("{col}"), MIN("{col}"), MAX("{col}"), STDDEV("{col}") '
                f'FROM {table_name}'
            ).fetchone()
            # safe_float handles None, nan, inf — all invalid JSON
            numeric_stats[col] = {
                "avg":    safe_float(row[0]),
                "min":    safe_float(row[1]),
                "max":    safe_float(row[2]),
                "stddev": safe_float(row[3]),
            }

        # ── Date ranges ────────────────────────────────────────────────────────
        date_types  = ("DATE", "TIMESTAMP", "DATETIME")
        date_cols   = [c for c, t in cols if any(dt in t.upper() for dt in date_types)]
        date_ranges = {}

        for col in date_cols:
            row = conn.execute(
                f'SELECT MIN("{col}"), MAX("{col}") FROM {table_name}'
            ).fetchone()
            date_ranges[col] = {
                "earliest": str(row[0]) if row[0] is not None else None,
                "latest":   str(row[1]) if row[1] is not None else None,
            }

        # ── Top-5 frequencies for first 3 string columns ───────────────────────
        string_types = ("VARCHAR", "TEXT", "CHAR", "STRING")
        string_cols  = [
            c for c, t in cols
            if any(st in t.upper() for st in string_types)
        ][:3]
        freq_stats = {}

        for col in string_cols:
            rows = conn.execute(
                f"""
                SELECT "{col}", COUNT(*) AS cnt
                FROM {table_name}
                WHERE "{col}" IS NOT NULL
                GROUP BY "{col}"
                ORDER BY cnt DESC
                LIMIT 5
                """
            ).fetchall()
            freq_stats[col] = [{"value": r[0], "count": r[1]} for r in rows]

        return {
            "table_name":      table_name,
            "row_count":       row_count,
            "column_count":    len(cols),
            "null_counts":     nulls,
            "numeric_stats":   numeric_stats,
            "date_ranges":     date_ranges,
            "top_frequencies": freq_stats,
        }

    except Exception as e:
        return {"error": str(e), "table_name": table_name}


# ── Analysis query ─────────────────────────────────────────────────────────────
def _run_analysis_query(sql: str) -> dict:
    normalized = sql.strip().upper()

    if not normalized.startswith("SELECT"):
        return {
            "success": False,
            "error": "Only SELECT statements are allowed.",
            "sql": sql,
        }

    for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER"):
        if f" {kw} " in f" {normalized} ":
            return {
                "success": False,
                "error": f"Blocked keyword '{kw}' found inside SELECT.",
                "sql": sql,
            }

    try:
        df = conn.execute(sql).fetchdf()
        return {
            "success":   True,
            "rows":      safe_df_to_records(df),
            "row_count": len(df),
            "columns":   list(df.columns),
        }
    except Exception as e:
        return {"success": False, "error": str(e), "sql": sql}


# ── CSV loader ─────────────────────────────────────────────────────────────────
def _load_csv_to_table(file_path: str, table_name: str) -> dict:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return {
            "success": False,
            "error": "Invalid table_name. Use letters, numbers, underscores only.",
        }

    if not os.path.exists(file_path):
        return {"success": False, "error": f"File not found: {file_path}"}

    try:
        conn.execute(
            f"CREATE OR REPLACE TABLE {table_name} AS "
            f"SELECT * FROM read_csv_auto('{file_path}')"
        )
        row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        cols      = conn.execute(f"DESCRIBE {table_name}").fetchall()
        return {
            "success":    True,
            "table_name": table_name,
            "row_count":  row_count,
            "columns":    [{"name": c[0], "type": c[1]} for c in cols],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# MCP TOOLS — for AI clients (Claude Desktop, Cursor, etc.)
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_schema_overview() -> dict:
    """Returns schema, column types, row count, and 3-row sample for every loaded table."""
    return _get_schema_overview()


@mcp.tool()
async def execute_cleaning_query(sql: str) -> dict:
    """
    Executes a single SQL cleaning statement (DELETE FROM / UPDATE / ALTER TABLE).
    Pass one statement at a time. No semicolons.
    """
    return _execute_cleaning_query(sql)


@mcp.tool()
async def generate_initial_insights(table_name: str) -> dict:
    """
    Computes descriptive statistics for a table: nulls, averages,
    date ranges, and top value frequencies. Feed this to an LLM for narration.
    """
    return _generate_initial_insights(table_name)


@mcp.tool()
async def run_analysis_query(sql: str) -> dict:
    """Executes a read-only SELECT query and returns results as JSON."""
    return _run_analysis_query(sql)


@mcp.tool()
async def load_csv_to_table(file_path: str, table_name: str) -> dict:
    """Loads a CSV file from disk into DuckDB as a named table."""
    return _load_csv_to_table(file_path, table_name)


# ══════════════════════════════════════════════════════════════════════════════
# FASTAPI + LIFESPAN
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Analytics Server",
    version="1.0.0",
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════════════════
# REST ENDPOINTS — called by n8n HTTP Request nodes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/ping")
def ping():
    """Keep-alive for Render free tier. n8n WF5 hits this every 10 min."""
    return {"status": "ok"}


@app.post("/tools/get_schema_overview")
def rest_get_schema_overview():
    return _get_schema_overview()


@app.post("/tools/load_csv_to_table")
async def rest_load_csv_to_table(request: Request):
    body = await request.json()
    return _load_csv_to_table(body["file_path"], body["table_name"])


@app.post("/tools/execute_cleaning_query")
async def rest_execute_cleaning_query(request: Request):
    body = await request.json()
    return _execute_cleaning_query(body["sql"])


@app.post("/tools/generate_initial_insights")
async def rest_generate_initial_insights(request: Request):
    body = await request.json()
    return _generate_initial_insights(body["table_name"])


@app.post("/tools/run_analysis_query")
async def rest_run_analysis_query(request: Request):
    body = await request.json()
    return _run_analysis_query(body["sql"])


# ══════════════════════════════════════════════════════════════════════════════
# MOUNT MCP — AI clients connect here
# ══════════════════════════════════════════════════════════════════════════════

security = TransportSecuritySettings(
    allowed_hosts=[
        "host.docker.internal:*",
        "localhost:*",
        "127.0.0.1:*",
    ]
)

app.mount(
    "/",
    mcp.streamable_http_app(
        transport_security=security
    )
)

# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"Starting Analytics Server on {HOST}:{PORT}")
    print(f"  REST tools : http://{HOST}:{PORT}/tools/*")
    print(f"  MCP client : http://{HOST}:{PORT}/mcp")
    print(f"  Keep-alive : http://{HOST}:{PORT}/ping")
    uvicorn.run(app, host=HOST, port=PORT)