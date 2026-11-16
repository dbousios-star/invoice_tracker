"""SQLite connection, schema initialization and CRUD queries."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "invoices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    afm TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mark TEXT UNIQUE,
    invoice_number TEXT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_date TEXT NOT NULL,
    total_net REAL,
    total_vat REAL,
    total_gross REAL,
    qr_raw_url TEXT,
    image_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    product_code TEXT,
    raw_description TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price_net REAL NOT NULL,
    discount_percent REAL DEFAULT 0.0,
    line_total_net REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoices_supplier_date ON invoices(supplier_id, invoice_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_invoice ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_items_code ON invoice_items(product_code);
CREATE INDEX IF NOT EXISTS idx_items_norm ON invoice_items(normalized_name);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------- suppliers ----------
def get_or_create_supplier(conn: sqlite3.Connection, afm: str, name: str) -> int:
    row = conn.execute("SELECT id, name FROM suppliers WHERE afm = ?", (afm,)).fetchone()
    if row:
        if name and row["name"] in ("", "Άγνωστος προμηθευτής") and name != row["name"]:
            conn.execute("UPDATE suppliers SET name = ? WHERE id = ?", (name, row["id"]))
        return row["id"]
    cur = conn.execute("INSERT INTO suppliers (afm, name) VALUES (?, ?)", (afm, name or "Άγνωστος προμηθευτής"))
    return cur.lastrowid


def list_suppliers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT s.id, s.afm, s.name, COUNT(i.id) AS invoice_count
           FROM suppliers s LEFT JOIN invoices i ON i.supplier_id = s.id
           GROUP BY s.id ORDER BY s.name COLLATE NOCASE"""
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- invoices ----------
def get_invoice_by_mark(conn: sqlite3.Connection, mark: str) -> Optional[dict]:
    if not mark:
        return None
    row = conn.execute("SELECT * FROM invoices WHERE mark = ?", (mark,)).fetchone()
    return dict(row) if row else None


def insert_invoice_with_items(conn: sqlite3.Connection, invoice: dict, items: list[dict]) -> int:
    """Insert header + items. Caller's get_conn() context commits atomically."""
    cur = conn.execute(
        """INSERT INTO invoices (mark, invoice_number, supplier_id, invoice_date,
                                 total_net, total_vat, total_gross, qr_raw_url, image_path)
           VALUES (:mark, :invoice_number, :supplier_id, :invoice_date,
                   :total_net, :total_vat, :total_gross, :qr_raw_url, :image_path)""",
        invoice,
    )
    invoice_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO invoice_items (invoice_id, product_code, raw_description, normalized_name,
                                      quantity, unit_price_net, discount_percent, line_total_net)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (invoice_id, it.get("product_code"), it["raw_description"], it["normalized_name"],
             it["quantity"], it["unit_price_net"], it.get("discount_percent") or 0.0, it["line_total_net"])
            for it in items
        ],
    )
    return invoice_id


def get_invoice_header(conn: sqlite3.Connection, invoice_id: int) -> Optional[dict]:
    row = conn.execute(
        """SELECT i.*, s.name AS supplier_name, s.afm AS supplier_afm
           FROM invoices i JOIN suppliers s ON s.id = i.supplier_id WHERE i.id = ?""",
        (invoice_id,),
    ).fetchone()
    return dict(row) if row else None


def get_invoice_items(conn: sqlite3.Connection, invoice_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id", (invoice_id,)).fetchall()
    return [dict(r) for r in rows]


def list_invoices(conn: sqlite3.Connection, supplier_id: Optional[int] = None) -> list[dict]:
    sql = """SELECT i.id, i.mark, i.invoice_number, i.invoice_date, i.total_net, i.total_gross,
                    i.supplier_id, s.name AS supplier_name, s.afm AS supplier_afm,
                    (SELECT COUNT(*) FROM invoice_items it WHERE it.invoice_id = i.id) AS item_count
             FROM invoices i JOIN suppliers s ON s.id = i.supplier_id"""
    params: tuple = ()
    if supplier_id:
        sql += " WHERE i.supplier_id = ?"
        params = (supplier_id,)
    sql += " ORDER BY i.invoice_date DESC, i.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def delete_invoice(conn: sqlite3.Connection, invoice_id: int) -> bool:
    cur = conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    return cur.rowcount > 0


# ---------- items ----------
def get_item(conn: sqlite3.Connection, item_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM invoice_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def update_item(conn: sqlite3.Connection, item_id: int, quantity: float, unit_price_net: float) -> None:
    conn.execute(
        "UPDATE invoice_items SET quantity = ?, unit_price_net = ?, line_total_net = ? WHERE id = ?",
        (quantity, unit_price_net, round(quantity * unit_price_net, 4), item_id),
    )


def get_supplier_history_items(conn: sqlite3.Connection, supplier_id: int, before_invoice: dict) -> list[dict]:
    """Items from the same supplier's invoices that precede `before_invoice`
    (by invoice_date, then id), newest first."""
    rows = conn.execute(
        """SELECT it.*, i.invoice_date, i.id AS inv_id
           FROM invoice_items it JOIN invoices i ON i.id = it.invoice_id
           WHERE i.supplier_id = ? AND i.id != ?
             AND (i.invoice_date < ? OR (i.invoice_date = ? AND i.id < ?))
           ORDER BY i.invoice_date DESC, i.id DESC, it.id ASC""",
        (supplier_id, before_invoice["id"], before_invoice["invoice_date"],
         before_invoice["invoice_date"], before_invoice["id"]),
    ).fetchall()
    return [dict(r) for r in rows]


def search_products(conn: sqlite3.Connection, normalized_query: str, limit: int = 300) -> list[dict]:
    like = f"%{normalized_query}%"
    rows = conn.execute(
        """SELECT it.id, it.product_code, it.raw_description, it.normalized_name, it.unit_price_net,
                  it.quantity, i.invoice_date, i.id AS invoice_id, s.id AS supplier_id, s.name AS supplier_name
           FROM invoice_items it
           JOIN invoices i ON i.id = it.invoice_id
           JOIN suppliers s ON s.id = i.supplier_id
           WHERE it.normalized_name LIKE ? OR it.product_code LIKE ?
           ORDER BY it.normalized_name, i.invoice_date DESC, i.id DESC
           LIMIT ?""",
        (like, like, limit),
    ).fetchall()
    return [dict(r) for r in rows]
