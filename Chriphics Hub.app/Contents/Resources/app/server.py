#!/usr/bin/env python3
"""CRISPprint Ghana — printing records & account book.

Local-only app: Python standard library + one SQLite file. No install, no cloud.

    python3 server.py                 # start on http://127.0.0.1:8712
    python3 server.py --seed          # start and load sample records (for a demo)
    python3 server.py --backup        # write a timestamped copy of the data and exit
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
# One book wherever Chriphics Hub is started from — the app, the .command or a bare
# `python3 server.py` — so records never fork into two copies.
SUPPORT = os.path.expanduser("~/Library/Application Support/Chriphics Hub")
DB_PATH = os.environ.get("CHRIPHICS_DB") or os.path.join(SUPPORT, "chriphics.db")
BACKUP_DIR = os.environ.get("CHRIPHICS_BACKUP_DIR") or os.path.join(SUPPORT, "Backups")

SHOP = {
    # The shop's trading name, as it appears on a job sheet. The data folder keeps its
    # original "Chriphics Hub" path (see SUPPORT) so an existing book is still found.
    "name": "CRISPprint Ghana",
    "tagline": "Printing & Design Services",
    "phone": "+233 000 000 000",
    "address": "Accra, Ghana",
    "currency": "GHS",
    "currency_symbol": "\u20b5",
}

STATUSES = ["Pending", "Printing", "Ready", "Delivered", "Cancelled"]
CATEGORIES = [
    "Business Cards", "Flyer / Leaflet", "Poster", "Roll-up Banner", "Large Format Banner",
    "Sticker", "ID Card", "Receipt / Invoice Book", "Letterhead", "Thesis Binding",
    "Photocopy", "T-shirt / Mug Print", "Invitation", "Booklet / Magazine", "Signboard",
    "Design Only", "Other",
]
UNITS = ["pcs", "ream", "set", "sqm", "page", "hour", "book", "dozen", "job"]
PAY_METHODS = ["Cash", "MoMo", "Bank Transfer", "Cheque", "Change", "Other"]
KINDS = ["Individual", "Business", "School", "Church", "NGO", "Government"]

# A job is booked work; a quote is a price we have promised but not yet printed.
DOC_KINDS = ["Job", "Quote"]

# Money going out, so the book shows profit and not only takings.
EXPENSE_CATEGORIES = [
    "Paper / Stock", "Ink / Toner", "Finishing", "Substrate", "Outsourced Printing",
    "Transport", "Rent", "Utilities", "Salaries", "Equipment", "Maintenance",
    "Design Assets", "Marketing", "Data / Airtime", "Licences", "Bank Charges",
    "Other",
]

# Work we have been asked about but have not booked.
LEAD_STAGES = ["Prospect", "Meeting", "Proposal", "Won", "Lost"]
LEAD_SOURCES = ["Walk-in", "WhatsApp", "Phone", "Referral", "Instagram", "Facebook",
                "Email", "Flyer / Poster", "Church / School", "Other"]

# The counter screen lets the shop fix one thing at a time — a date, a stage, a name —
# without resending the whole row. Only these fields are reachable that way.
QUICK_JOB = {"title": "text", "priority": ["Normal", "Urgent"],
             "due_date": "date", "valid_until": "date"}
QUICK_LEAD = {"interest": "text", "value": "money", "follow_up": "date"}
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_db = None
# Reentrant: route() holds this while the handlers below take it again.
_lock = threading.RLock()


# --------------------------------------------------------------------------- data

def connect(path=None):
    global _db, DB_PATH
    if path:
        DB_PATH = os.path.abspath(path)
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    _db = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("PRAGMA foreign_keys=ON")
    _db.execute("PRAGMA busy_timeout=5000")
    # An older book is brought forward first: the schema's views and indexes read the
    # columns migrate() adds, so they cannot be created against the old table.
    migrate()
    with open(os.path.join(ROOT, "schema.sql"), "r", encoding="utf-8") as fh:
        _db.executescript(fh.read())
    _db.commit()


# executescript() creates missing tables, but CREATE TABLE IF NOT EXISTS can never add a
# column to a table that already has records. A book written by an older Chriphics Hub is
# brought forward here, column by column, with nothing dropped and nothing rewritten.
JOB_MIGRATIONS = [
    ("kind", "TEXT NOT NULL DEFAULT 'Job'"),
    ("valid_until", "TEXT"),
    ("converted_at", "TEXT"),
]


def migrate():
    tables = {r["name"] for r in _db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "jobs" not in tables:
        return  # a brand-new book: the schema script already carries the new columns
    have = {r["name"] for r in _db.execute("PRAGMA table_info(jobs)").fetchall()}
    for name, ddl in JOB_MIGRATIONS:
        if name not in have:
            _db.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, ddl))
    # Quotes predate the kind column, so a book that only ever had jobs needs no backfill;
    # anything already booked keeps its 'Job' default.
    _db.execute("UPDATE jobs SET kind='Job' WHERE kind IS NULL OR kind=''")


def q(sql, args=()):
    cur = _db.execute(sql, args)
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def today():
    return dt.date.today().isoformat()


def next_ref(prefix="CH"):
    year = dt.date.today().year
    row = one("SELECT ref FROM jobs WHERE ref LIKE ? ORDER BY length(ref) DESC, ref DESC LIMIT 1",
              ("%s-%d-%%" % (prefix, year),))
    n = 1
    if row:
        try:
            n = int(row["ref"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            n = _db.execute("SELECT count(*) c FROM jobs WHERE ref LIKE ?", (prefix + "-%",)).fetchone()["c"] + 1
    return "%s-%d-%04d" % (prefix, year, n)


def num(value, default=0.0, minimum=None):
    try:
        out = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and out < minimum:
        out = minimum
    return round(out, 2)


def text(value, limit=4000, required=False, name="field"):
    out = ("" if value is None else str(value)).strip()
    if required and not out:
        raise ValueError("%s is required" % name)
    return out[:limit]


def money(value):
    return float(round(num(value), 2))


# --------------------------------------------------------------------- dashboards

def dashboard():
    t = today()
    month_start = t[:8] + "01"
    week_start = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    kpi = one("""
      SELECT
        (SELECT count(*) FROM jobs WHERE kind='Job' AND date(created_at)=?) AS jobs_today,
        (SELECT count(*) FROM jobs WHERE kind='Job' AND date(created_at)>=?) AS jobs_week,
        (SELECT count(*) FROM jobs WHERE kind='Job'
           AND status IN ('Pending','Printing','Ready')) AS open_jobs,
        (SELECT count(*) FROM jobs WHERE kind='Job' AND status='Ready') AS ready_jobs,
        (SELECT count(*) FROM jobs WHERE kind='Job' AND status NOT IN ('Delivered','Cancelled')
           AND due_date IS NOT NULL AND due_date < ?) AS overdue_jobs,
        (SELECT count(*) FROM clients WHERE archived=0) AS clients,
        (SELECT count(*) FROM jobs WHERE kind='Quote' AND converted_at IS NULL
           AND status <> 'Cancelled') AS open_quotes,
        (SELECT round(coalesce(sum(total),0),2) FROM job_accounts WHERE kind='Quote'
           AND converted_at IS NULL AND status <> 'Cancelled') AS quoted_value,
        (SELECT count(*) FROM leads WHERE stage NOT IN ('Won','Lost')) AS open_leads,
        (SELECT count(*) FROM leads WHERE stage NOT IN ('Won','Lost')
           AND follow_up IS NOT NULL AND follow_up <= ?) AS leads_due,
        (SELECT round(coalesce(sum(value),0),2) FROM leads WHERE stage NOT IN ('Won','Lost')) AS pipeline_value,
        (SELECT round(coalesce(sum(balance),0),2) FROM job_accounts
           WHERE kind='Job' AND balance > 0 AND status <> 'Cancelled') AS outstanding,
        (SELECT round(coalesce(sum(total),0),2) FROM job_accounts
           WHERE kind='Job' AND status<>'Cancelled' AND date(created_at) >= ?) AS billed_month,
        (SELECT round(coalesce(sum(profit),0),2) FROM job_accounts
           WHERE kind='Job' AND status<>'Cancelled' AND date(created_at) >= ?) AS profit_month,
        (SELECT round(coalesce(sum(amount),0),2) FROM expenses WHERE spent_on >= ?) AS spent_month,
        (SELECT round(coalesce(sum(amount),0),2) FROM payments WHERE kind='Refund') AS refunds_total,
        (SELECT round(coalesce(sum(CASE WHEN kind='Refund' THEN -amount ELSE amount END),0),2)
           FROM payments WHERE paid_at >= ?) AS collected_week,
        (SELECT round(coalesce(sum(CASE WHEN kind='Refund' THEN -amount ELSE amount END),0),2)
           FROM payments WHERE paid_at >= ?) AS collected_month,
        (SELECT round(coalesce(sum(CASE WHEN kind='Refund' THEN -amount ELSE amount END),0),2)
           FROM payments WHERE paid_at = ?) AS collected_today
    """, (t, week_start, t, t, month_start, month_start, month_start, week_start, month_start, t))

    by_status = {r["status"]: r["c"] for r in q(
        "SELECT status, count(*) c FROM jobs WHERE kind='Job' AND status IN ('Pending','Printing','Ready')"
        " GROUP BY status")}

    trend = q("""
      SELECT d.day,
             round(coalesce((SELECT sum(CASE WHEN p.kind='Refund' THEN -p.amount ELSE p.amount END)
                             FROM payments p WHERE p.paid_at = d.day), 0), 2) AS collected,
             round(coalesce((SELECT sum(e.amount) FROM expenses e WHERE e.spent_on = d.day), 0), 2) AS spent,
             (SELECT count(*) FROM jobs j WHERE j.kind='Job' AND date(j.created_at) = d.day) AS new_jobs
      FROM (SELECT date(?, 'localtime', '-' || (s.i) || ' days') AS day
            FROM (SELECT 0 i UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL
                  SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL
                  SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10 UNION ALL SELECT 11 UNION ALL
                  SELECT 12 UNION ALL SELECT 13 UNION ALL SELECT 14) s) d
      ORDER BY d.day
    """, (t,))

    overdue = q("""
      SELECT ja.id, ja.ref, ja.kind, ja.title, ja.category, ja.status, ja.priority, ja.due_date,
             ja.total, ja.quantity, ja.unit, ja.item_count, ja.client,
             cast(julianday(?) - julianday(ja.due_date) AS integer) AS days_late
      FROM job_accounts ja
      WHERE ja.kind='Job' AND ja.status NOT IN ('Delivered','Cancelled')
        AND ja.due_date IS NOT NULL AND ja.due_date < ?
      ORDER BY days_late DESC, ja.balance DESC LIMIT 12
    """, (t, t))

    recent = q("""
      SELECT ja.id, ja.ref, ja.kind, ja.title, ja.category, ja.status, ja.priority, ja.total,
             ja.paid, ja.balance, ja.cost, ja.profit, ja.item_count, ja.due_date, ja.created_at,
             ja.quantity, ja.unit, ja.client
      FROM job_accounts ja
      ORDER BY ja.created_at DESC, ja.id DESC LIMIT 10
    """)

    debtors = q("""
      SELECT ja.client_id AS id, c.name, c.phone, round(sum(ja.balance),2) AS owed, count(*) AS jobs
      FROM job_accounts ja JOIN clients c ON c.id = ja.client_id
      WHERE ja.kind='Job' AND ja.balance > 0 AND ja.status <> 'Cancelled'
      GROUP BY ja.client_id HAVING owed > 0 ORDER BY owed DESC LIMIT 8
    """)

    mix = q("""
      SELECT ja.category, count(*) AS jobs, round(sum(ja.total),2) AS billed,
             round(sum(ja.profit),2) AS profit
      FROM job_accounts ja
      WHERE ja.kind='Job' AND ja.status <> 'Cancelled' AND date(ja.created_at) >= ?
      GROUP BY ja.category ORDER BY billed DESC LIMIT 8
    """, ((dt.date.today() - dt.timedelta(days=89)).isoformat(),))

    spend = q("""
      SELECT e.category, count(*) AS n, round(sum(e.amount),2) AS amount
      FROM expenses e WHERE e.spent_on >= ?
      GROUP BY e.category ORDER BY amount DESC LIMIT 8
    """, (month_start,))

    quotes = q("""
      SELECT ja.id, ja.ref, ja.title, ja.category, ja.total, ja.valid_until, ja.client, ja.created_at,
             cast(julianday(coalesce(ja.valid_until, date('now','localtime')))
                  - julianday(date('now','localtime')) AS integer) AS days_left
      FROM job_accounts ja
      WHERE ja.kind='Quote' AND ja.converted_at IS NULL AND ja.status <> 'Cancelled'
      ORDER BY ja.valid_until IS NULL, ja.valid_until ASC LIMIT 8
    """)

    follow_ups = q("""
      SELECT id, name, phone, source, interest, value, stage, follow_up,
             cast(julianday(follow_up) - julianday(date('now','localtime')) AS integer) AS days
      FROM leads WHERE stage NOT IN ('Won','Lost') AND follow_up IS NOT NULL
      ORDER BY follow_up ASC LIMIT 8
    """)

    return {"kpi": kpi, "by_status": by_status, "trend": trend, "overdue": overdue,
            "recent": recent, "debtors": debtors, "mix": mix, "spend": spend,
            "quotes": quotes, "follow_ups": follow_ups, "shop": SHOP, "today": t}


# ------------------------------------------------------------------------- jobs

def job_filters(params):
    where, args = ["1=1"], []
    kind = params.get("kind", [None])[0]
    if kind in ("Job", "Quote"):
        where.append("j.kind = ?")
        args.append(kind)
    status = params.get("status", [None])[0]
    if status and status != "all":
        if status == "open":
            where.append("j.status IN ('Pending','Printing','Ready')")
        else:
            where.append("j.status = ?")
            args.append(status)
    category = params.get("category", [None])[0]
    if category and category != "all":
        where.append("j.category = ?")
        args.append(category)
    client = params.get("client", [None])[0]
    if client:
        where.append("j.client_id = ?")
        args.append(int(client))
    if params.get("debtors", [""])[0] == "1":
        where.append("ja.balance > 0")
    if params.get("overdue", [""])[0] == "1":
        where.append("j.status NOT IN ('Delivered','Cancelled') AND j.due_date IS NOT NULL AND j.due_date < ?")
        args.append(today())
    date_from, date_to = params.get("from", [None])[0], params.get("to", [None])[0]
    if date_from:
        where.append("date(j.created_at) >= ?")
        args.append(date_from)
    if date_to:
        where.append("date(j.created_at) <= ?")
        args.append(date_to)
    search = text(params.get("q", [""])[0], 120)
    if search:
        where.append("(j.ref LIKE ? OR j.title LIKE ? OR j.description LIKE ? OR c.name LIKE ?"
                     " OR c.phone LIKE ? OR EXISTS (SELECT 1 FROM job_items it"
                     " WHERE it.job_id = j.id AND it.title LIKE ?))")
        like = "%" + search + "%"
        args += [like] * 6
    sorts = {"created": "j.created_at DESC, j.id DESC", "due": "j.due_date IS NULL, j.due_date ASC",
             "balance": "ja.balance DESC", "value": "ja.total DESC", "client": "c.name ASC",
             "profit": "ja.profit DESC"}
    order = sorts.get(params.get("sort", ["created"])[0], sorts["created"])
    return " AND ".join(where), args, order


def list_jobs(params):
    where, args, order = job_filters(params)
    rows = q("""
      SELECT j.id, j.ref, j.kind, j.title, j.category, j.status, j.priority, j.due_date,
             j.valid_until, j.converted_at, j.quantity, j.unit,
             j.unit_price, j.extras, j.discount, j.size, j.description, j.created_at, j.updated_at,
             j.client_id, c.name AS client, c.phone AS client_phone,
             ja.total, ja.paid, ja.balance, ja.cost, ja.profit, ja.item_count
      FROM jobs j
      JOIN clients c ON c.id = j.client_id
      JOIN job_accounts ja ON ja.id = j.id
      WHERE %s ORDER BY %s LIMIT 1000
    """ % (where, order), args)
    return rows


def job_detail(job_id):
    # Columns are named, not j.* — jobs.total is the legacy header maths and would hide the
    # item-aware total the view works out.
    job = one("""
      SELECT j.id, j.ref, j.client_id, j.kind, j.title, j.category, j.description, j.size,
             j.quantity, j.unit, j.unit_price, j.extras, j.discount, j.status, j.priority,
             j.due_date, j.valid_until, j.converted_at, j.created_at, j.updated_at, j.closed_at,
             c.name AS client, c.phone AS client_phone, c.whatsapp AS client_whatsapp,
             c.email AS client_email, c.address AS client_address, c.kind AS client_kind,
             ja.total, ja.paid, ja.balance, ja.cost, ja.profit, ja.item_count
      FROM jobs j JOIN clients c ON c.id = j.client_id JOIN job_accounts ja ON ja.id = j.id
      WHERE j.id = ?
    """, (job_id,))
    if not job:
        return None
    job["items"] = items_of(job_id)
    job["payments"] = q("SELECT * FROM payments WHERE job_id = ? ORDER BY paid_at DESC, id DESC", (job_id,))
    job["expenses"] = q("SELECT * FROM expenses WHERE job_id = ? ORDER BY spent_on DESC, id DESC", (job_id,))
    job["events"] = q("SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC", (job_id,))
    return job


def clean_job(payload):
    data = {}
    data["client_id"] = int(num(payload.get("client_id"), -1))
    if data["client_id"] < 1 or not one("SELECT id FROM clients WHERE id = ?", (data["client_id"],)):
        raise ValueError("Choose a client for this job")
    data["title"] = text(payload.get("title"), 200, True, "job title")
    data["category"] = text(payload.get("category"), 60) or "Other"
    if data["category"] not in CATEGORIES:
        data["category"] = "Other"
    data["kind"] = "Quote" if text(payload.get("kind"), 10) == "Quote" else "Job"
    data["description"] = text(payload.get("description"), 4000)
    data["size"] = text(payload.get("size"), 120)
    data["quantity"] = max(int(num(payload.get("quantity"), 1, 0)), 0)
    data["unit"] = text(payload.get("unit"), 20) or "pcs"
    data["unit_price"] = money(payload.get("unit_price"))
    data["extras"] = money(payload.get("extras"))
    data["discount"] = money(payload.get("discount"))
    data["status"] = text(payload.get("status"), 20) or "Pending"
    if data["status"] not in STATUSES:
        data["status"] = "Pending"
    data["priority"] = "Urgent" if text(payload.get("priority"), 10) == "Urgent" else "Normal"
    due = text(payload.get("due_date"), 10)
    data["due_date"] = due or None
    valid = text(payload.get("valid_until"), 10)
    data["valid_until"] = valid or None
    return data


def clean_items(payload):
    """The lines of one order. Anything with no title is a blank row the form left open."""
    raw = payload.get("items")
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = [raw]
    items = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        title = text(row.get("title"), 200)
        qty = max(int(num(row.get("quantity"), 0, 0)), 0)
        price = money(row.get("unit_price"))
        cost = money(row.get("unit_cost"))
        if not title and qty <= 0 and price <= 0:
            continue
        category = text(row.get("category"), 60) or "Other"
        if category not in CATEGORIES:
            category = "Other"
        items.append({
            "title": title or "Print item",
            "category": category,
            "size": text(row.get("size"), 120),
            "quantity": qty or 1,
            "unit": text(row.get("unit"), 20) or "pcs",
            "unit_price": price,
            "unit_cost": cost,
            "position": len(items),
        })
    return items[:60]


def replace_items(job_id, items):
    with _lock:
        _db.execute("DELETE FROM job_items WHERE job_id = ?", (job_id,))
        for row in items or []:
            _db.execute("""
              INSERT INTO job_items (job_id, title, category, size, quantity, unit,
                                     unit_price, unit_cost, position)
              VALUES (:job_id, :title, :category, :size, :quantity, :unit,
                      :unit_price, :unit_cost, :position)
            """, dict(row, job_id=job_id))
        _db.commit()


def items_of(job_id):
    return q("SELECT * FROM job_items WHERE job_id = ? ORDER BY position, id", (job_id,))


def create_job(payload):
    data = clean_job(payload)
    items = clean_items(payload)
    prefix = "Q" if data["kind"] == "Quote" else "CH"
    ref = text(payload.get("ref"), 20) or next_ref(prefix)
    if one("SELECT id FROM jobs WHERE ref = ?", (ref,)):
        ref = next_ref(prefix)
    with _lock:
        cur = _db.execute("""
          INSERT INTO jobs (ref, client_id, kind, title, category, description, size, quantity, unit,
                            unit_price, extras, discount, status, priority, due_date, valid_until)
          VALUES (:ref, :client_id, :kind, :title, :category, :description, :size, :quantity, :unit,
                  :unit_price, :extras, :discount, :status, :priority, :due_date, :valid_until)
        """, dict(data, ref=ref))
        job_id = cur.lastrowid
        label = "Quote" if data["kind"] == "Quote" else "Job"
        _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'status', ?)",
                    (job_id, label + " created as " + data["status"]))
        _db.commit()
    if items:
        replace_items(job_id, items)
        _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                    (job_id, "%d item line(s) added" % len(items)))
        _db.commit()
    return job_detail(job_id)


def update_items(job_id, items):
    """Lines are replaced wholesale, and the record says what moved."""
    before = items_of(job_id)
    after = items or []

    def shape(rows):
        return [(r["title"], r["category"], r["size"], r["quantity"], r["unit"],
                 r["unit_price"], r["unit_cost"]) for r in rows]

    if shape(before) == shape(after):
        return
    replace_items(job_id, after)
    old_total = round(sum(b["line_total"] for b in before), 2)
    new_total = round(sum(max(a["quantity"] * a["unit_price"], 0) for a in after), 2)
    detail = "Items: %d line(s)" % len(after)
    if before and after:
        detail += ", lines worth %s %.2f -> %s %.2f" % (
            SHOP["currency_symbol"], old_total, SHOP["currency_symbol"], new_total)
    elif not before:
        detail = "Itemised into %d line(s) worth %s %.2f" % (len(after), SHOP["currency_symbol"], new_total)
    else:
        detail = "Item lines removed"
    _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)", (job_id, detail))
    _db.commit()


FIELD_LABELS = {
    "client_id": "Client", "title": "Job", "category": "Service", "description": "Brief",
    "size": "Specification", "quantity": "Quantity", "unit": "Unit", "unit_price": "Unit price",
    "extras": "Materials", "discount": "Discount", "status": "Status", "priority": "Priority",
    "due_date": "Due date", "kind": "Type", "valid_until": "Quote valid until",
    "interest": "Interest", "value": "Value", "follow_up": "Follow-up", "stage": "Stage",
}
MONEY_FIELDS = ("unit_price", "extras", "discount", "unit_cost", "amount", "value")


def describe(field, value):
    if value is None or value == "":
        return "nothing"
    if field in MONEY_FIELDS:
        return "%s %.2f" % (SHOP["currency_symbol"], float(value))
    return str(value)


def update_job(job_id, payload):
    existing = one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not existing:
        raise LookupError("Job not found")
    data = clean_job(payload)
    changes = []
    for key, value in data.items():
        if existing[key] != value:
            changes.append("%s: %s -> %s" % (FIELD_LABELS.get(key, key), describe(key, existing[key]),
                                             describe(key, value)))
    with _lock:
        if changes:
            _db.execute("""
              UPDATE jobs SET client_id=:client_id, kind=:kind, title=:title, category=:category,
                description=:description, size=:size, quantity=:quantity, unit=:unit,
                unit_price=:unit_price, extras=:extras, discount=:discount, status=:status,
                priority=:priority, due_date=:due_date, valid_until=:valid_until,
                updated_at=datetime('now','localtime'),
                converted_at=CASE WHEN :kind='Job' THEN coalesce(converted_at, datetime('now','localtime'))
                                  ELSE NULL END,
                closed_at=CASE WHEN :status IN ('Delivered','Cancelled')
                               THEN coalesce(closed_at, datetime('now','localtime')) ELSE NULL END
              WHERE id=:id
            """, dict(data, id=job_id))
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'status', ?)",
                        (job_id, "; ".join(changes)[:1000]))
            _db.commit()
        lines = clean_items(payload)
        # A PUT that doesn't mention items must not empty the order.
        if lines is not None:
            update_items(job_id, lines)
    return job_detail(job_id)


def set_status(job_id, status):
    if status not in STATUSES:
        raise ValueError("Unknown status " + status)
    with _lock:
        _db.execute("""
          UPDATE jobs SET status=?, updated_at=datetime('now','localtime'),
            closed_at=CASE WHEN ? IN ('Delivered','Cancelled')
                           THEN coalesce(closed_at, datetime('now','localtime')) ELSE NULL END
          WHERE id=?
        """, (status, status, job_id))
        _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'status', ?)",
                    (job_id, "Status -> " + status))
        _db.commit()
    return job_detail(job_id)


def quick_value(spec, field, value):
    """One checked value for a single-field edit; anything odd is refused before the write."""
    label = FIELD_LABELS.get(field, field)
    if spec == "date":
        out = text(value, 10)
        if out and not DATE_ONLY.match(out):
            raise ValueError("Pick a real date for the %s" % label.lower())
        return out or None
    if spec == "money":
        return money(value)
    if spec == "text":
        return text(value, 200, True, label)
    if isinstance(spec, list):
        out = text(value, 20)
        if out not in spec:
            raise ValueError("Choose one of " + ", ".join(spec))
        return out
    raise ValueError("Cannot check the %s" % label)


def quick_edit(table, fields, row_id, field, value):
    """Write one whitelisted column and leave the rest of the row untouched."""
    spec = fields.get(field)
    if spec is None:
        raise ValueError("Nothing here to change called %s" % field)
    row = one("SELECT * FROM %s WHERE id = ?" % table, (row_id,))
    if not row:
        raise LookupError("Record not found")
    new = quick_value(spec, field, value)
    if row[field] == new:
        return False
    with _lock:
        _db.execute("UPDATE %s SET %s = ?, updated_at = datetime('now','localtime') WHERE id = ?"
                    % (table, field), (new, row_id))
        _db.commit()
    return True


def quick_job(job_id, field, value):
    if field == "status":
        return set_status(job_id, text(value, 20))
    if quick_edit("jobs", QUICK_JOB, job_id, field, value):
        row = one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        with _lock:
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'edit', ?)",
                        (job_id, "%s set to %s" % (FIELD_LABELS.get(field, field),
                                                   describe(field, row[field]))))
            _db.commit()
    return job_detail(job_id)


def quick_lead(lid, field, value):
    if field == "stage":
        return set_lead_stage(lid, text(value, 20))
    quick_edit("leads", QUICK_LEAD, lid, field, value)
    return lead_detail(lid)


def add_note(job_id, detail):
    detail = text(detail, 2000, True, "note")
    with _lock:
        _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)", (job_id, detail))
        _db.execute("UPDATE jobs SET updated_at=datetime('now','localtime') WHERE id=?", (job_id,))
        _db.commit()
    return job_detail(job_id)


def convert_quote(job_id, status="Pending"):
    """A quote the client said yes to becomes a booked job, and the record shows the link."""
    with _lock:
        quote = one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not quote:
            raise LookupError("Quote not found")
        if quote["kind"] != "Quote":
            raise ValueError("This is already a booked job")
        old_ref = quote["ref"]
        new_ref = next_ref("CH")
        if status not in STATUSES:
            status = "Pending"
        _db.execute("""
          UPDATE jobs SET kind='Job', ref=?, status=?, converted_at=datetime('now','localtime'),
            closed_at=CASE WHEN ? IN ('Delivered','Cancelled')
                           THEN coalesce(closed_at, datetime('now','localtime')) ELSE NULL END,
            updated_at=datetime('now','localtime')
          WHERE id=?
        """, (new_ref, status, status, job_id))
        _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'status', ?)",
                    (job_id, "Accepted and booked as %s (was quote %s)" % (new_ref, old_ref)))
        _db.commit()
    return job_detail(job_id)


# ----------------------------------------------------------------------- expenses

def clean_expense(payload):
    amount = num(payload.get("amount"), 0, 0)
    if amount <= 0:
        raise ValueError("Enter an amount greater than zero")
    category = text(payload.get("category"), 60) or "Other"
    if category not in EXPENSE_CATEGORIES:
        category = "Other"
    method = text(payload.get("method"), 30) or "Cash"
    if method not in PAY_METHODS:
        method = "Cash"
    job_id = payload.get("job_id")
    job_id = int(job_id) if job_id not in (None, "", 0, "0") else None
    if job_id and not one("SELECT id FROM jobs WHERE id=?", (job_id,)):
        raise LookupError("Job not found")
    spent = text(payload.get("spent_on"), 10) or today()
    return {
        "spent_on": spent,
        "category": category,
        "payee": text(payload.get("payee"), 160),
        "amount": round(amount, 2),
        "method": method,
        "reference": text(payload.get("reference"), 80),
        "job_id": job_id,
        "note": text(payload.get("note"), 500),
    }


def expense_detail(eid):
    return one("""
      SELECT e.*, j.ref, j.title AS job_title FROM expenses e
      LEFT JOIN jobs j ON j.id = e.job_id WHERE e.id = ?
    """, (eid,))


def list_expenses(params):
    where, args = ["1=1"], []
    category = params.get("category", [None])[0]
    if category and category != "all":
        where.append("e.category = ?")
        args.append(category)
    if params.get("job", [None])[0]:
        where.append("e.job_id = ?")
        args.append(int(params["job"][0]))
    overhead = params.get("overhead", [""])[0]
    if overhead == "1":
        where.append("e.job_id IS NULL")
    elif overhead == "0":
        where.append("e.job_id IS NOT NULL")
    date_from, date_to = params.get("from", [None])[0], params.get("to", [None])[0]
    if date_from:
        where.append("e.spent_on >= ?")
        args.append(date_from)
    if date_to:
        where.append("e.spent_on <= ?")
        args.append(date_to)
    search = text(params.get("q", [""])[0], 120)
    if search:
        where.append("(e.payee LIKE ? OR e.note LIKE ? OR e.reference LIKE ? OR e.category LIKE ? OR j.ref LIKE ?)")
        like = "%" + search + "%"
        args += [like] * 5
    rows = q("""
      SELECT e.*, j.ref, j.title AS job_title FROM expenses e
      LEFT JOIN jobs j ON j.id = e.job_id
      WHERE %s ORDER BY e.spent_on DESC, e.id DESC LIMIT 2000
    """ % " AND ".join(where), args)
    month_start = today()[:8] + "01"
    return {
        "rows": rows,
        "count": len(rows),
        "total": round(sum(r["amount"] for r in rows), 2),
        "totals": one("""
          SELECT round(coalesce(sum(amount),0),2) AS all_time,
                 round(coalesce(sum(CASE WHEN spent_on >= ? THEN amount ELSE 0 END),0),2) AS this_month,
                 round(coalesce(sum(CASE WHEN spent_on >= ? THEN amount ELSE 0 END),0),2) AS this_week,
                 round(coalesce(sum(CASE WHEN job_id IS NOT NULL THEN amount ELSE 0 END),0),2) AS on_jobs,
                 round(coalesce(sum(CASE WHEN job_id IS NULL THEN amount ELSE 0 END),0),2) AS overhead
          FROM expenses
        """, (month_start, (dt.date.today() - dt.timedelta(days=6)).isoformat())),
        "by_category": q("""
          SELECT category, count(*) AS n, round(sum(amount),2) AS amount FROM expenses
          WHERE spent_on >= ? GROUP BY category ORDER BY amount DESC
        """, (month_start,)),
        "months": q("SELECT * FROM monthly_pnl LIMIT 14"),
    }


def create_expense(payload):
    data = clean_expense(payload)
    with _lock:
        cur = _db.execute("""
          INSERT INTO expenses (spent_on, category, payee, amount, method, reference, job_id, note)
          VALUES (:spent_on, :category, :payee, :amount, :method, :reference, :job_id, :note)
        """, data)
        eid = cur.lastrowid
        if data["job_id"]:
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                        (data["job_id"], "Expense %s %.2f recorded for %s" % (
                            SHOP["currency_symbol"], data["amount"], data["category"])))
        _db.commit()
    return expense_detail(eid)


def update_expense(eid, payload):
    if not one("SELECT id FROM expenses WHERE id=?", (eid,)):
        raise LookupError("Expense not found")
    data = clean_expense(payload)
    with _lock:
        _db.execute("""
          UPDATE expenses SET spent_on=:spent_on, category=:category, payee=:payee, amount=:amount,
            method=:method, reference=:reference, job_id=:job_id, note=:note WHERE id=:id
        """, dict(data, id=eid))
        _db.commit()
    return expense_detail(eid)


def delete_expense(eid):
    row = one("SELECT * FROM expenses WHERE id=?", (eid,))
    if not row:
        raise LookupError("Expense not found")
    with _lock:
        _db.execute("DELETE FROM expenses WHERE id=?", (eid,))
        if row["job_id"]:
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                        (row["job_id"], "Expense of %s %.2f removed" % (SHOP["currency_symbol"], row["amount"])))
        _db.commit()
    return {"ok": True}


# -------------------------------------------------------------------------- leads

def clean_lead(payload):
    data = {
        "name": text(payload.get("name"), 160, True, "who asked"),
        "phone": text(payload.get("phone"), 40),
        "whatsapp": text(payload.get("whatsapp") or payload.get("phone"), 40),
        "email": text(payload.get("email"), 160),
        "source": text(payload.get("source"), 40) or "Walk-in",
        "interest": text(payload.get("interest"), 200),
        "value": money(payload.get("value")),
        "stage": text(payload.get("stage"), 20) or "Prospect",
        "follow_up": text(payload.get("follow_up"), 10) or None,
        "note": text(payload.get("note"), 2000),
    }
    if data["source"] not in LEAD_SOURCES:
        data["source"] = "Other"
    if data["stage"] not in LEAD_STAGES:
        data["stage"] = "Prospect"
    return data


def lead_detail(lid):
    return one("""
      SELECT l.*, c.name AS client, j.ref, j.title AS job_title
      FROM leads l LEFT JOIN clients c ON c.id = l.client_id
      LEFT JOIN jobs j ON j.id = l.job_id WHERE l.id = ?
    """, (lid,))


def list_leads(params):
    where, args = ["1=1"], []
    stage = params.get("stage", [None])[0]
    if stage and stage != "all":
        if stage == "open":
            where.append("l.stage NOT IN ('Won','Lost')")
        else:
            where.append("l.stage = ?")
            args.append(stage)
    source = params.get("source", [None])[0]
    if source and source != "all":
        where.append("l.source = ?")
        args.append(source)
    if params.get("followup", [""])[0] == "1":
        where.append("l.stage NOT IN ('Won','Lost') AND l.follow_up IS NOT NULL AND l.follow_up <= ?")
        args.append(today())
    search = text(params.get("q", [""])[0], 120)
    if search:
        where.append("(l.name LIKE ? OR l.phone LIKE ? OR l.interest LIKE ? OR l.note LIKE ?)")
        like = "%" + search + "%"
        args += [like] * 4
    rows = q("""
      SELECT l.*, c.name AS client, j.ref, j.title AS job_title,
             cast(julianday(coalesce(l.follow_up, date('now','localtime')))
                  - julianday(date('now','localtime')) AS integer) AS days_to_follow
      FROM leads l LEFT JOIN clients c ON c.id = l.client_id LEFT JOIN jobs j ON j.id = l.job_id
      WHERE %s ORDER BY CASE l.stage WHEN 'Lost' THEN 1 ELSE 0 END,
                        l.follow_up IS NULL, l.follow_up ASC, l.updated_at DESC
      LIMIT 2000
    """ % " AND ".join(where), args)
    return {
        "rows": rows,
        "count": len(rows),
        "pipeline": q("""
          SELECT stage, count(*) AS n, round(coalesce(sum(value),0),2) AS value
          FROM leads WHERE stage NOT IN ('Won','Lost') GROUP BY stage
        """),
        "totals": one("""
          SELECT count(*) AS total,
                 coalesce(sum(CASE WHEN stage NOT IN ('Won','Lost') THEN 1 ELSE 0 END),0) AS open,
                 round(coalesce(sum(CASE WHEN stage NOT IN ('Won','Lost') THEN value ELSE 0 END),0),2) AS open_value,
                 coalesce(sum(CASE WHEN stage NOT IN ('Won','Lost')
                                    AND follow_up IS NOT NULL AND follow_up <= ? THEN 1 ELSE 0 END),0) AS due_today,
                 coalesce(sum(CASE WHEN stage='Won' THEN 1 ELSE 0 END),0) AS won,
                 coalesce(sum(CASE WHEN stage='Lost' THEN 1 ELSE 0 END),0) AS lost
          FROM leads
        """, (today(),)),
    }


def create_lead(payload):
    data = clean_lead(payload)
    with _lock:
        cur = _db.execute("""
          INSERT INTO leads (name, phone, whatsapp, email, source, interest, value, stage,
                             follow_up, note)
          VALUES (:name, :phone, :whatsapp, :email, :source, :interest, :value, :stage,
                  :follow_up, :note)
        """, data)
        lid = cur.lastrowid
        _db.commit()
    return lead_detail(lid)


def update_lead(lid, payload):
    if not one("SELECT id FROM leads WHERE id=?", (lid,)):
        raise LookupError("Enquiry not found")
    data = clean_lead(payload)
    with _lock:
        _db.execute("""
          UPDATE leads SET name=:name, phone=:phone, whatsapp=:whatsapp, email=:email, source=:source,
            interest=:interest, value=:value, stage=:stage, follow_up=:follow_up, note=:note,
            updated_at=datetime('now','localtime'),
            closed_at=CASE WHEN :stage IN ('Won','Lost')
                           THEN coalesce(closed_at, datetime('now','localtime')) ELSE NULL END
          WHERE id=:id
        """, dict(data, id=lid))
        _db.commit()
    return lead_detail(lid)


def set_lead_stage(lid, stage):
    if stage not in LEAD_STAGES:
        raise ValueError("Unknown stage " + stage)
    with _lock:
        _db.execute("""
          UPDATE leads SET stage=?, updated_at=datetime('now','localtime'),
            closed_at=CASE WHEN ? IN ('Won','Lost')
                           THEN coalesce(closed_at, datetime('now','localtime')) ELSE NULL END
          WHERE id=?
        """, (stage, stage, lid))
        _db.commit()
    return lead_detail(lid)


def delete_lead(lid):
    with _lock:
        _db.execute("DELETE FROM leads WHERE id=?", (lid,))
        _db.commit()
    return {"ok": True}


def convert_lead(lid, payload):
    """An enquiry that came good: become a client, and book the work if there are details."""
    lead = one("SELECT * FROM leads WHERE id=?", (lid,))
    if not lead:
        raise LookupError("Enquiry not found")
    if lead["client_id"]:
        cid = lead["client_id"]
    else:
        client = create_client({
            "name": lead["name"], "phone": lead["phone"], "whatsapp": lead["whatsapp"],
            "email": lead["email"], "notes": text(payload.get("client_notes") or lead["note"], 4000),
            "kind": text(payload.get("kind") or "Individual", 40),
        })
        cid = client["id"]
    with _lock:
        _db.execute("UPDATE leads SET client_id=?, stage='Won', updated_at=datetime('now','localtime'),"
                    " closed_at=coalesce(closed_at, datetime('now','localtime')) WHERE id=?", (cid, lid))
        _db.commit()
    job = None
    title = text(payload.get("title") or lead["interest"], 200)
    if title:
        job_payload = {
            "client_id": cid, "title": title,
            "category": text(payload.get("category"), 60) or "Other",
            "quantity": payload.get("quantity") or 1,
            "unit": payload.get("unit") or "pcs",
            "unit_price": payload.get("unit_price") or lead["value"],
            "due_date": payload.get("due_date") or "",
            "priority": payload.get("priority") or "Normal",
            "description": text(payload.get("description") or lead["note"], 4000),
            "status": "Pending", "kind": "Job",
        }
        if payload.get("items"):
            job_payload["items"] = payload["items"]
        job = create_job(job_payload)
        with _lock:
            _db.execute("UPDATE leads SET job_id=? WHERE id=?", (job["id"], lid))
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                        (job["id"], "Booked from enquiry: %s (%s)" % (lead["name"], lead["source"])))
            _db.commit()
    return {"lead": lead_detail(lid), "client_id": cid, "job": job}


# ----------------------------------------------------------------------- clients

def list_clients(params):
    search = text(params.get("q", [""])[0], 120)
    where, args = ["c.archived = 0"], []
    if params.get("archived", [""])[0] == "1":
        where = ["c.archived = 1"]
    kind = params.get("kind", [None])[0]
    if kind and kind != "all":
        where.append("c.kind = ?")
        args.append(kind)
    if search:
        where.append("(c.name LIKE ? OR c.phone LIKE ? OR c.whatsapp LIKE ? OR c.email LIKE ? OR c.address LIKE ?)")
        like = "%" + search + "%"
        args += [like] * 5
    return q("""
      SELECT c.*, ca.billed, ca.received, ca.open_job_balance, ca.account_credit, ca.open_quotes,
             round(ca.open_job_balance - ca.account_credit, 2) AS balance_due,
             (SELECT count(*) FROM jobs j WHERE j.client_id = c.id AND j.kind='Job') AS jobs,
             (SELECT max(created_at) FROM jobs j WHERE j.client_id = c.id AND j.kind='Job') AS last_job,
             (SELECT count(*) FROM jobs j WHERE j.client_id = c.id AND j.kind='Job'
                AND j.status IN ('Pending','Printing','Ready')) AS active_jobs
      FROM clients c JOIN client_accounts ca ON ca.id = c.id
      WHERE %s ORDER BY c.name COLLATE NOCASE LIMIT 2000
    """ % " AND ".join(where), args)


def clean_client(payload):
    data = {
        "name": text(payload.get("name"), 160, True, "client name"),
        "phone": text(payload.get("phone"), 40),
        "whatsapp": text(payload.get("whatsapp") or payload.get("phone"), 40),
        "email": text(payload.get("email"), 160),
        "address": text(payload.get("address"), 400),
        "kind": text(payload.get("kind"), 40) or "Individual",
        "notes": text(payload.get("notes"), 4000),
    }
    if data["kind"] not in KINDS:
        data["kind"] = "Individual"
    return data


def create_client(payload):
    data = clean_client(payload)
    with _lock:
        cur = _db.execute("""
          INSERT INTO clients (name, phone, whatsapp, email, address, kind, notes)
          VALUES (:name, :phone, :whatsapp, :email, :address, :kind, :notes)
        """, data)
        _db.commit()
        cid = cur.lastrowid
    return client_detail(cid)


def update_client(cid, payload):
    if not one("SELECT id FROM clients WHERE id=?", (cid,)):
        raise LookupError("Client not found")
    data = clean_client(payload)
    with _lock:
        _db.execute("""
          UPDATE clients SET name=:name, phone=:phone, whatsapp=:whatsapp, email=:email,
            address=:address, kind=:kind, notes=:notes WHERE id=:id
        """, dict(data, id=cid))
        _db.commit()
    return client_detail(cid)


def archive_client(cid, archived):
    with _lock:
        _db.execute("UPDATE clients SET archived=? WHERE id=?", (1 if archived else 0, cid))
        _db.commit()


def delete_client(cid):
    row = one("SELECT count(*) c FROM jobs WHERE client_id=?", (cid,))
    if row and row["c"]:
        raise ValueError("This client has %d job(s). Archive them instead of deleting." % row["c"])
    with _lock:
        _db.execute("DELETE FROM clients WHERE id=?", (cid,))
        _db.commit()


def client_detail(cid):
    client = one("SELECT * FROM clients WHERE id=?", (cid,))
    if not client:
        return None
    acct = one("SELECT *, round(open_job_balance - account_credit,2) AS balance_due FROM client_accounts WHERE id=?", (cid,))
    client.update(acct)
    client["jobs"] = q("""
      SELECT ja.id, ja.ref, ja.kind, ja.title, ja.category, ja.status, ja.priority, ja.due_date,
             ja.valid_until, ja.quantity, ja.unit, ja.item_count, ja.total, ja.paid, ja.balance,
             ja.created_at
      FROM job_accounts ja WHERE ja.client_id = ? ORDER BY ja.created_at DESC, ja.id DESC
    """, (cid,))
    client["payments"] = q("""
      SELECT p.*, j.ref, j.title FROM payments p LEFT JOIN jobs j ON j.id = p.job_id
      WHERE p.client_id = ? ORDER BY p.paid_at DESC, p.id DESC LIMIT 400
    """, (cid,))
    return client


# --------------------------------------------------------------------- payments

def create_payment(payload):
    amount = num(payload.get("amount"), 0, 0)
    if amount <= 0:
        raise ValueError("Enter an amount greater than zero")
    client_id = int(num(payload.get("client_id"), -1))
    job_id = payload.get("job_id")
    job_id = int(job_id) if job_id not in (None, "", 0, "0") else None
    with _lock:
        if job_id:
            job = one("SELECT id, client_id, kind, ref FROM jobs WHERE id=?", (job_id,))
            if not job:
                raise LookupError("Job not found")
            if job["kind"] == "Quote":
                raise ValueError("%s is a quote, not a bill yet. Book it as a job first." % job["ref"])
            client_id = job["client_id"]
        if client_id < 1 or not one("SELECT id FROM clients WHERE id=?", (client_id,)):
            raise ValueError("Choose a client")
        kind = text(payload.get("kind"), 20) or "Payment"
        if kind not in ("Deposit", "Payment", "Refund", "Credit"):
            kind = "Payment"
        method = text(payload.get("method"), 30) or "Cash"
        if method not in PAY_METHODS:
            method = "Cash"
        paid_at = text(payload.get("paid_at"), 10) or today()
        cur = _db.execute("""
          INSERT INTO payments (client_id, job_id, amount, kind, method, reference, note, paid_at)
          VALUES (?,?,?,?,?,?,?,?)
        """, (client_id, job_id, round(amount, 2), kind, method,
              text(payload.get("reference"), 80), text(payload.get("note"), 500), paid_at))
        if job_id:
            balance = one("SELECT balance FROM job_accounts WHERE id=?", (job_id,))
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'payment', ?)",
                        (job_id, "%s %.2f received (%s) - balance now %.2f" % (
                            SHOP["currency_symbol"], amount, method,
                            balance["balance"] if balance else 0)))
        _db.commit()
        pid = cur.lastrowid
    return one("SELECT p.*, c.name AS client, j.ref FROM payments p "
               "JOIN clients c ON c.id=p.client_id LEFT JOIN jobs j ON j.id=p.job_id WHERE p.id=?", (pid,))


def list_payments(params):
    where, args = ["1=1"], []
    if params.get("client", [None])[0]:
        where.append("p.client_id = ?")
        args.append(int(params["client"][0]))
    if params.get("job", [None])[0]:
        where.append("p.job_id = ?")
        args.append(int(params["job"][0]))
    if params.get("method", [None])[0] not in (None, "", "all"):
        where.append("p.method = ?")
        args.append(params["method"][0])
    if params.get("from", [None])[0]:
        where.append("p.paid_at >= ?")
        args.append(params["from"][0])
    if params.get("to", [None])[0]:
        where.append("p.paid_at <= ?")
        args.append(params["to"][0])
    search = text(params.get("q", [""])[0], 120)
    if search:
        where.append("(c.name LIKE ? OR p.reference LIKE ? OR p.note LIKE ? OR j.ref LIKE ?)")
        like = "%" + search + "%"
        args += [like, like, like, like]
    rows = q("""
      SELECT p.id, p.amount, p.kind, p.method, p.reference, p.note, p.paid_at, p.recorded_at,
             p.client_id, p.job_id, c.name AS client, j.ref, j.title, j.status AS job_status
      FROM payments p JOIN clients c ON c.id = p.client_id LEFT JOIN jobs j ON j.id = p.job_id
      WHERE %s ORDER BY p.paid_at DESC, p.id DESC LIMIT 2000
    """ % " AND ".join(where), args)
    total = sum(r["amount"] if r["kind"] != "Refund" else -r["amount"] for r in rows)
    credit = one("SELECT round(coalesce(sum(CASE WHEN kind='Refund' THEN -amount ELSE amount END),0),2) "
                 "AS credit FROM payments WHERE job_id IS NULL")
    return {"rows": rows, "total": round(total, 2), "count": len(rows),
            "credit_on_account": credit["credit"]}


def accounts_view(params):
    ledger = list_payments(params)
    debtors = q("""
      SELECT ja.id, ja.ref, ja.client_id, ja.client, c.phone, ja.title, ja.category, ja.status,
             ja.due_date, ja.total, ja.paid, ja.balance, ja.created_at
      FROM job_accounts ja JOIN clients c ON c.id = ja.client_id
      WHERE ja.kind='Job' AND ja.balance > 0 AND ja.status <> 'Cancelled' AND c.archived = 0
      ORDER BY ja.balance DESC LIMIT 500
    """)
    credit = q("""
      SELECT c.id, c.name, c.phone, round(sum(CASE WHEN p.kind='Refund' THEN -p.amount ELSE p.amount END),2) AS credit
      FROM payments p JOIN clients c ON c.id = p.client_id
      WHERE p.job_id IS NULL GROUP BY c.id HAVING credit <> 0 ORDER BY credit DESC
    """)
    return {"ledger": ledger, "debtors": debtors, "credit": credit,
            "spent": q("SELECT * FROM monthly_pnl LIMIT 13"),
            "totals": one("""
              SELECT round(coalesce(sum(balance),0),2) AS receivable,
                     round(coalesce(sum(CASE WHEN due_date IS NOT NULL AND due_date < ?
                                              AND status NOT IN ('Delivered','Cancelled')
                                              THEN balance ELSE 0 END),0),2) AS overdue_value
              FROM job_accounts WHERE kind='Job' AND status <> 'Cancelled' AND balance > 0
            """, (today(),)),
            "spent_total": one("""
              SELECT round(coalesce(sum(amount),0),2) AS all_time,
                     round(coalesce(sum(CASE WHEN spent_on >= ? THEN amount ELSE 0 END),0),2) AS this_month
              FROM expenses
            """, (today()[:8] + "01",))}


# ---------------------------------------------------------------------- reports

def report(params):
    date_from = params.get("from", [None])[0] or today()[:8] + "01"
    date_to = params.get("to", [None])[0] or today()
    period = params.get("period", ["day"])[0]
    if period not in ("day", "week", "month"):
        period = "day"

    def _totals(sql_date):
        return one("""
          SELECT
            (SELECT round(coalesce(sum(ja.total),0),2) FROM job_accounts ja
              JOIN jobs j ON j.id=ja.id WHERE j.kind='Job' AND j.status<>'Cancelled'
              AND date(j.created_at) BETWEEN ? AND ?) AS billed,
            (SELECT round(coalesce(sum(ja.cost),0),2) FROM job_accounts ja
              JOIN jobs j ON j.id=ja.id WHERE j.kind='Job' AND j.status<>'Cancelled'
              AND date(j.created_at) BETWEEN ? AND ?) AS cost,
            (SELECT round(coalesce(sum(ja.profit),0),2) FROM job_accounts ja
              JOIN jobs j ON j.id=ja.id WHERE j.kind='Job' AND j.status<>'Cancelled'
              AND date(j.created_at) BETWEEN ? AND ?) AS profit,
            (SELECT round(coalesce(sum(e.amount),0),2) FROM expenses e
              WHERE e.spent_on BETWEEN ? AND ?) AS spent,
            (SELECT round(coalesce(sum(CASE WHEN p.kind='Refund' THEN -p.amount ELSE p.amount END),0),2)
              FROM payments p WHERE p.paid_at BETWEEN ? AND ?) AS collected,
            (SELECT count(*) FROM jobs WHERE kind='Job' AND status<>'Cancelled'
              AND date(created_at) BETWEEN ? AND ?) AS jobs,
            (SELECT count(*) FROM jobs WHERE kind='Quote' AND date(created_at) BETWEEN ? AND ?) AS quotes,
            (SELECT count(*) FROM jobs WHERE kind='Job' AND status='Delivered'
              AND date(coalesce(closed_at,updated_at)) BETWEEN ? AND ?) AS delivered
        """, (sql_date, date_to, sql_date, date_to, sql_date, date_to, sql_date, date_to,
              sql_date, date_to, sql_date, date_to, sql_date, date_to, sql_date, date_to))

    buckets = q("""
      SELECT CAST(strftime(CASE WHEN ?='week' THEN '%Y-%W' WHEN ?='month' THEN '%Y-%m' ELSE '%Y-%m-%d' END,
                           created_at) AS TEXT) AS bucket,
             count(*) AS jobs, round(sum(total),2) AS billed, round(sum(cost),2) AS cost,
             round(sum(profit),2) AS profit,
             round(sum(paid),2) AS collected, round(sum(balance),2) AS still_due
      FROM job_accounts WHERE kind='Job' AND status<>'Cancelled' AND date(created_at) BETWEEN ? AND ?
      GROUP BY bucket ORDER BY bucket
    """, (period, period, date_from, date_to))

    spend_buckets = q("""
      SELECT CAST(strftime(CASE WHEN ?='week' THEN '%Y-%W' WHEN ?='month' THEN '%Y-%m' ELSE '%Y-%m-%d' END,
                           spent_on) AS TEXT) AS bucket,
             count(*) AS lines, round(sum(amount),2) AS amount
      FROM expenses WHERE spent_on BETWEEN ? AND ? GROUP BY bucket ORDER BY bucket
    """, (period, period, date_from, date_to))

    payment_days = q("""
      SELECT date(paid_at) AS day, round(sum(CASE WHEN kind='Refund' THEN -amount ELSE amount END),2) AS cash
      FROM payments WHERE paid_at BETWEEN ? AND ? GROUP BY day ORDER BY day
    """, (date_from, date_to))

    methods = q("""
      SELECT method, count(*) AS n, round(sum(CASE WHEN kind='Refund' THEN -amount ELSE amount END),2) AS amount
      FROM payments WHERE paid_at BETWEEN ? AND ? GROUP BY method ORDER BY amount DESC
    """, (date_from, date_to))

    by_category = q("""
      SELECT category, count(*) AS jobs, round(sum(total),2) AS billed, round(sum(cost),2) AS cost,
             round(sum(profit),2) AS profit, round(sum(balance),2) AS due
      FROM job_accounts WHERE kind='Job' AND status<>'Cancelled' AND date(created_at) BETWEEN ? AND ?
      GROUP BY category ORDER BY billed DESC
    """, (date_from, date_to))

    spend_by_category = q("""
      SELECT category, count(*) AS lines, round(sum(amount),2) AS amount
      FROM expenses WHERE spent_on BETWEEN ? AND ? GROUP BY category ORDER BY amount DESC
    """, (date_from, date_to))

    top_clients = q("""
      SELECT c.id, c.name, count(ja.id) AS jobs, round(sum(ja.total),2) AS billed,
             round(sum(ja.paid),2) AS paid, round(sum(ja.profit),2) AS profit,
             round(sum(ja.balance),2) AS due
      FROM job_accounts ja JOIN clients c ON c.id = ja.client_id
      WHERE ja.kind='Job' AND ja.status<>'Cancelled' AND date(ja.created_at) BETWEEN ? AND ?
      GROUP BY c.id ORDER BY billed DESC LIMIT 20
    """, (date_from, date_to))

    top_jobs = q("""
      SELECT id, ref, title, category, client, total, cost, profit, status
      FROM job_accounts WHERE kind='Job' AND status<>'Cancelled'
        AND date(created_at) BETWEEN ? AND ?
      ORDER BY profit DESC LIMIT 10
    """, (date_from, date_to))

    return {"range": {"from": date_from, "to": date_to, "period": period},
            "totals": _totals(date_from), "buckets": buckets, "spend_buckets": spend_buckets,
            "payment_days": payment_days,
            "methods": methods, "by_category": by_category, "spend_by_category": spend_by_category,
            "top_clients": top_clients, "top_jobs": top_jobs,
            "shop": SHOP}


# --------------------------------------------------------------------- csv export

def export_rows(name, params):
    if name == "jobs":
        rows, cols = list_jobs(params), [
            ("ref", "Job ref"), ("kind", "Type"), ("created_at", "Booked on"), ("client", "Client"),
            ("client_phone", "Phone"), ("title", "Job"), ("category", "Service"),
            ("size", "Specification"), ("item_count", "Item lines"),
            ("quantity", "Qty"), ("unit", "Unit"),
            ("unit_price", "Unit price"), ("extras", "Materials"), ("discount", "Discount"),
            ("total", "Total"), ("cost", "Cost"), ("profit", "Profit"),
            ("paid", "Paid"), ("balance", "Balance due"),
            ("status", "Status"), ("due_date", "Due date"), ("valid_until", "Quote valid until")]
    elif name == "items":
        rows = q("""
          SELECT i.title, i.category, i.size, i.quantity, i.unit, i.unit_price, i.unit_cost,
                 i.line_total, i.line_cost, round(i.line_total - i.line_cost, 2) AS profit,
                 j.ref, j.status, j.kind, c.name AS client, j.created_at
          FROM job_items i JOIN jobs j ON j.id = i.job_id JOIN clients c ON c.id = j.client_id
          ORDER BY j.created_at DESC, i.position LIMIT 5000
        """)
        cols = [("ref", "Job ref"), ("client", "Client"), ("title", "Item"), ("category", "Service"),
                ("size", "Specification"), ("quantity", "Qty"), ("unit", "Unit"),
                ("unit_price", "Unit price"), ("unit_cost", "Unit cost"), ("line_total", "Line total"),
                ("line_cost", "Line cost"), ("profit", "Line profit"), ("status", "Job status")]
    elif name == "expenses":
        rows, cols = list_expenses(params)["rows"], [
            ("spent_on", "Date"), ("category", "Category"), ("payee", "Paid to"), ("amount", "Amount"),
            ("method", "Method"), ("reference", "Transaction ref"), ("ref", "Job ref"),
            ("job_title", "Job"), ("note", "Note")]
    elif name == "leads":
        rows, cols = list_leads(params)["rows"], [
            ("created_at", "Asked on"), ("name", "Who"), ("phone", "Phone"), ("whatsapp", "WhatsApp"),
            ("email", "Email"), ("source", "Source"), ("interest", "Interested in"), ("value", "Est. value"),
            ("stage", "Stage"), ("follow_up", "Follow up"), ("client", "Client"), ("ref", "Job ref"),
            ("note", "Note")]
    elif name == "clients":
        rows, cols = list_clients(params), [
            ("name", "Client"), ("kind", "Type"), ("phone", "Phone"), ("whatsapp", "WhatsApp"),
            ("email", "Email"), ("address", "Address"), ("jobs", "Jobs"), ("active_jobs", "Active"),
            ("open_quotes", "Open quotes"),
            ("billed", "Billed"), ("received", "Received"), ("balance_due", "Balance due"),
            ("last_job", "Last job"), ("notes", "Notes")]
    elif name == "payments":
        rows, cols = list_payments(params)["rows"], [
            ("paid_at", "Date"), ("client", "Client"), ("ref", "Job ref"), ("title", "Job"),
            ("kind", "Type"), ("amount", "Amount"), ("method", "Method"),
            ("reference", "Transaction ref"), ("note", "Note")]
    elif name == "accounts":
        rows, cols = accounts_view(params)["debtors"], [
            ("ref", "Job ref"), ("client", "Client"), ("phone", "Phone"), ("title", "Job"),
            ("status", "Status"), ("due_date", "Due"), ("total", "Total"), ("paid", "Paid"),
            ("balance", "Outstanding")]
    else:
        raise LookupError("Unknown export: " + name)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["%s export" % SHOP["name"], "range %s to %s" % (
        params.get("from", ["-"])[0], params.get("to", ["-"])[0]),
        "generated " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")])
    writer.writerow([label for _, label in cols])
    for row in rows:
        writer.writerow(["" if row.get(key) is None else row.get(key) for key, _ in cols])
    return buf.getvalue(), "%s-%s.csv" % (name, today()), len(rows)


# ------------------------------------------------------------------- printable job

def receipt_html(job_id):
    job = job_detail(job_id)
    if not job:
        return "<h1>Record not found</h1>"
    esc = lambda v: (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if v is not None else "")
    is_quote = job["kind"] == "Quote"
    sym = SHOP["currency_symbol"]
    items = job["items"]
    if items:
        lines = "".join(
            "<tr><td>%s<div class=small>%s</div></td><td class=r>%s %s</td><td class=r>%s %.2f</td>"
            "<td class=r>%s %.2f</td></tr>" % (
                esc(it["title"]),
                esc(" · ".join(x for x in (it["size"], it["category"]) if x)),
                it["quantity"], esc(it["unit"]), sym, it["unit_price"], sym, it["line_total"])
            for it in items)
    else:
        lines = "<tr><td>%s<div class=small>%s</div></td><td class=r>%s %s</td><td class=r>%s %.2f</td>" \
                "<td class=r>%s %.2f</td></tr>" % (
                    esc(job["title"]), esc(job["size"] or ""), job["quantity"], esc(job["unit"]),
                    sym, job["unit_price"], sym, job["quantity"] * job["unit_price"])
    extras_row = ("<tr><td>Materials / finishing</td><td class=r></td><td class=r></td>"
                  "<td class=r>%s %.2f</td></tr>" % (sym, job["extras"])) if job["extras"] else ""
    disc_row = ("<tr><td>Discount</td><td class=r></td><td class=r></td>"
                "<td class=r>- %s %.2f</td></tr>" % (sym, job["discount"])) if job["discount"] else ""
    payments = "".join(
        "<tr><td>%s</td><td>%s</td><td class=r>%s %.2f</td><td>%s</td></tr>" % (
            esc(p["paid_at"]), esc(p["kind"]), sym, p["amount"], esc(p["method"]))
        for p in job["payments"])
    spend = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td class=r>%s %.2f</td></tr>" % (
            esc(e["spent_on"]), esc(e["category"]), esc(e["payee"] or "-"), sym, e["amount"])
        for e in job["expenses"])

    if is_quote:
        money_block = """<table class=totals><tbody>
 <tr><td>Quoted total</td><td class="r big">%(sym)s %(total).2f</td></tr>
 <tr><td>Valid until</td><td class=r>%(valid)s</td></tr></tbody></table>
<p class=terms>Prices hold until the date above. Booking this quote starts the work and lets us
take a deposit. To accept, reply to %(phone)s or %(address)s.</p>""" % {
            "sym": sym, "total": job["total"], "valid": esc(job["valid_until"] or "not set"),
            "phone": esc(SHOP["phone"]), "address": esc(SHOP["name"])}
    else:
        money_block = """<table class=totals><tbody>
 <tr><td>Total</td><td class="r big">%(sym)s %(total).2f</td></tr>
 <tr><td>Paid to date</td><td class=r>%(sym)s %(paid).2f</td></tr>
 <tr><td><b>Balance due</b></td><td class="r big">%(sym)s %(balance).2f</td></tr></tbody></table>
<table><thead><tr><th>Date</th><th>Type</th><th class=r>Amount</th><th>Method</th></tr></thead>
<tbody>%(payments)s</tbody></table>""" % {
            "sym": sym, "total": job["total"], "paid": job["paid"], "balance": job["balance"],
            "payments": payments or "<tr><td colspan=4>No payments recorded yet</td></tr>"}

    return """<!doctype html><html><head><meta charset=utf-8><title>%(ref)s - %(shop)s</title>
<style>
 body{font:14px/1.5 Poppins,-apple-system,Helvetica,Arial,sans-serif;color:#111;max-width:720px;margin:24px auto;padding:0 16px}
 .muted{color:#666;font-size:12px} .small{color:#666;font-size:11px}
 header{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #111;padding-bottom:10px;margin-bottom:16px}
 .badge{display:inline-block;border:1px solid #111;padding:1px 8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
 table{width:100%%;border-collapse:collapse;margin:10px 0}
 th,td{padding:6px 8px;border-bottom:1px solid #ddd;text-align:left;font-size:13px}
 th{background:#f4f4f4;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
 .r{text-align:right} .totals{margin-top:14px;border-top:2px solid #111}
 .totals td{font-size:15px;border:0;padding:4px 8px} .big{font-weight:700;font-size:18px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 24px}
 .terms{font-size:12px;color:#444;border-top:1px solid #111;padding-top:8px}
 .sig{margin-top:44px;display:flex;gap:40px} .sig div{flex:1;border-top:1px solid #111;padding-top:6px;font-size:12px}
 .logo{display:block;height:42px;width:auto;margin:0 0 5px}
 @media print{body{margin:0 auto}.noprint{display:none}}
 a{color:#0a58ca}
</style></head><body>
<header><div><img class=logo src="/img/brand.png" alt="%(shop)s"><div class=muted>%(tagline)s &middot; %(phone)s &middot; %(address)s</div></div>
<div style=text-align:right><div class=big>%(ref)s</div>
<div class=badge>%(heading)s</div><div class=muted>%(status)s &middot; %(created)s</div></div></header>
<div class=grid>
 <div><b>Client</b><br>%(client)s</div><div><b>Contact</b><br>%(phone_c)s</div>
 <div><b>Work</b><br>%(title)s (%(category)s)</div><div><b>Specification</b><br>%(size)s</div>
 <div><b>%(due_label)s</b><br>%(due)s</div><div><b>Priority</b><br>%(priority)s</div>
</div>
%(desc)s
<table><thead><tr><th>Item</th><th class=r>Qty</th><th class=r>Unit</th><th class=r>Amount</th></tr></thead>
<tbody>%(lines)s%(extras_row)s%(disc_row)s</tbody></table>
%(money_block)s
%(spend_block)s
<div class=sig><div>Customer signature / date</div><div>%(shop)s received by</div></div>
<p class=noprint><a href="#" onclick="window.print();return false">Print this sheet</a> &nbsp;
<a href="/#/jobs/%(id)s">Back to the record</a></p>
</body></html>""" % {
        "ref": esc(job["ref"]), "shop": SHOP["name"], "tagline": SHOP["tagline"],
        "phone": SHOP["phone"], "address": SHOP["address"], "status": esc(job["status"]),
        "heading": "Estimate" if is_quote else "Job sheet",
        "created": esc(job["created_at"][:10]), "client": esc(job["client"]),
        "phone_c": esc(job["client_phone"] or "not on file"),
        "title": esc(job["title"]), "category": esc(job["category"]),
        "size": esc(job["size"] or "-"),
        "due_label": "Ready by" if is_quote else "Due date",
        "due": esc(job["due_date"] or "Not set"), "priority": esc(job["priority"]),
        "desc": ("<div><b>Notes</b><br>%s</div>" % esc(job["description"])) if job["description"] else "",
        "lines": lines, "extras_row": extras_row, "disc_row": disc_row,
        "money_block": money_block,
        "spend_block": ("" if is_quote or not spend else
                        "<div class=muted>Costs booked against this job</div>"
                        "<table><thead><tr><th>Date</th><th>Category</th><th>Paid to</th>"
                        "<th class=r>Amount</th></tr></thead><tbody>%s</tbody></table>" % spend),
        "id": job["id"],
    }


# --------------------------------------------------------------------------- seed

def seed():
    if one("SELECT count(*) c FROM clients")["c"]:
        print("Database already has records - seed skipped.")
        return
    people = [
        ("Kwame Mensah", "0244 118 220", "Business", "East Legon, Accra"),
        ("Grace Ofori", "055 702 1180", "Individual", "Spintex, Accra"),
        ("Bethel Chapel International", "0208 445 991", "Church", "Adenta, Accra"),
        ("Accra Technical School", "0302 771 440", "School", "Korle Buentsoe"),
        ("Nii Adjei Enterprises", "0277 660 118", "Business", "Makola Market"),
    ]
    with _lock:
        for name, phone, kind, address in people:
            _db.execute("INSERT INTO clients (name, phone, kind, address) VALUES (?,?,?,?)",
                        (name, phone, kind, address))
        jobs = [
            (1, "500 business cards", "Business Cards", "250gsm gloss, double sided", 5, "set", 45.0, 20.0, 0, "Ready", "2 boxes"),
            (2, "Wedding invitations", "Invitation", "A6 kraft, gold foil", 120, "pcs", 3.5, 0, 50.0, "Printing", "200"),
            (3, "Convention programme flyers", "Flyer / Leaflet", "A5 120gsm", 800, "pcs", 0.75, 60.0, 0, "Pending", None),
            (4, "Graduation thesis binding", "Thesis Binding", "Hardcover, gold spine", 12, "pcs", 55.0, 0, 0, "Delivered", "2 weeks"),
            (5, "Shop front banner", "Large Format Banner", "3m x 1.2m flex", 1, "sqm", 0, 180.0, 0, "Delivered", "60"),
            (1, "Roll-up banner", "Roll-up Banner", "80cm x 200cm", 2, "pcs", 320.0, 0, 40.0, "Pending", None),
            (3, "Harvest thank-you posters", "Poster", "A2 satin", 40, "pcs", 12.0, 0, 0, "Printing", None),
        ]
        for cid, title, cat, size, qty, unit, up, extras, disc, status, due in jobs:
            day = dt.date.today() - dt.timedelta(days=abs(hash(title)) % 24)
            cur = _db.execute("""
              INSERT INTO jobs (ref, client_id, kind, title, category, size, quantity, unit, unit_price,
                                extras, discount, status, due_date, created_at, updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (next_ref(), cid, "Job", title, cat, size, qty, unit, up, extras, disc, status,
                  (day + dt.timedelta(days=6)).isoformat() if due else None,
                  day.isoformat() + " 09:12", day.isoformat() + " 09:12"))
            jid = cur.lastrowid
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?,?,?)",
                        (jid, "status", "Job created as Pending"))
            total = one("SELECT total FROM job_accounts WHERE id=?", (jid,))["total"]
            part = round(total * 0.5, 2)
            if part > 0:
                _db.execute("INSERT INTO payments (client_id, job_id, amount, kind, method, paid_at) "
                            "VALUES (?,?,?,?,?,?)",
                            (cid, jid, part, "Deposit", "MoMo", day.isoformat()))
        # One order with several products on it, the way a real counter ticket looks.
        cur = _db.execute("""
          INSERT INTO jobs (ref, client_id, kind, title, category, quantity, unit, unit_price,
                            status, due_date, description)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (next_ref(), 4, "Job", "Speech day pack", "Other", 0, "job", 0, "Printing",
              (dt.date.today() + dt.timedelta(days=4)).isoformat(),
              "Programmes, orders of service and two banners for the same event."))
        pack_id = cur.lastrowid
        for title, cat, size, qty, unit, price, cost in [
            ("Programme A5", "Booklet / Magazine", "A5 115gsm saddle stitch", 300, "pcs", 1.8, 0.9),
            ("Order of service", "Flyer / Leaflet", "DL 120gsm", 300, "pcs", 1.2, 0.6),
            ("Stage banner", "Large Format Banner", "4m x 1m flex", 1, "pcs", 380.0, 210.0),
            ("Thank-you cards", "Business Cards", "300gsm gloss", 2, "set", 45.0, 18.0),
        ]:
            _db.execute("""
              INSERT INTO job_items (job_id, title, category, size, quantity, unit, unit_price,
                                     unit_cost, position)
              VALUES (?,?,?,?,?,?,?,?,?)
            """, (pack_id, title, cat, size, qty, unit, price, cost, _db.execute(
                "SELECT coalesce(max(position),-1)+1 n FROM job_items WHERE job_id=?",
                (pack_id,)).fetchone()["n"]))
        _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?,?,?)",
                    (pack_id, "note", "Itemised into 4 line(s)"))

        # Costs the shop paid out for that job, and shop overhead.
        _db.execute("INSERT INTO expenses (spent_on, category, payee, amount, method, job_id, note) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (today(), "Paper / Stock", "Makola Paper Supplies", 420.0, "MoMo", pack_id,
                     "115gsm and 300gsm stock for the speech day pack"))
        for category, payee, amount, method, back in [
            ("Ink / Toner", "Adom Print Supplies", 680.0, "Bank Transfer", 9),
            ("Rent", "Landlord - 1st floor", 2500.0, "Bank Transfer", 12),
            ("Utilities", "ECG prepaid", 310.0, "MoMo", 6),
            ("Data / Airtime", "MTN", 90.0, "MoMo", 3),
            ("Maintenance", "Riproll service", 450.0, "Cash", 15),
        ]:
            _db.execute("INSERT INTO expenses (spent_on, category, payee, amount, method, note) "
                        "VALUES (?,?,?,?,?,?)",
                        ((dt.date.today() - dt.timedelta(days=back)).isoformat(), category, payee,
                         amount, method, "Sample record - delete it if it is not yours"))

        # A quote that has gone out but has not been booked.
        qcur = _db.execute("""
          INSERT INTO jobs (ref, client_id, kind, title, category, size, quantity, unit, unit_price,
                            status, valid_until, created_at, updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (next_ref("Q"), 2, "Quote", "Wedding welcome boards", "Signboard", "60x90cm foam board",
              2, "pcs", 450.0, "Pending",
              (dt.date.today() + dt.timedelta(days=10)).isoformat(),
              (dt.date.today() - dt.timedelta(days=2)).isoformat() + " 14:05",
              (dt.date.today() - dt.timedelta(days=2)).isoformat() + " 14:05"))
        _db.execute("INSERT INTO job_items (job_id, title, category, size, quantity, unit, unit_price, unit_cost, position) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (qcur.lastrowid, "Welcome board", "Signboard", "60x90cm 10mm foam", 2, "pcs", 450.0, 240.0, 0))
        _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?,?,?)",
                    (qcur.lastrowid, "status", "Quote created as Pending"))

        for name, phone, source, interest, value, stage, follow_back in [
            ("Auntie Adjoa Tailors", "0209 553 118", "Walk-in", "Care labels and 500 tags", 780.0, "Prospect", 2),
            ("Divine Seed Ministry", "0244 909 221", "WhatsApp", "Conference materials, 400 delegates", 3200.0, "Proposal", -1),
            ("Kofi Antwi - Photographer", "055 118 7744", "Referral", "Wedding album prints", 1450.0, "Meeting", 4),
            ("Legon Hall Committee", "0302 664 880", "Email", "Hall brochure and posters", 980.0, "Won", None),
            ("Oxford Preparatory", "0277 200 441", "Facebook", "Sports day certificates", 520.0, "Lost", None),
        ]:
            day = dt.date.today() - dt.timedelta(days=abs(hash(name)) % 20)
            _db.execute("""
              INSERT INTO leads (name, phone, source, interest, value, stage, follow_up, note,
                                 created_at, updated_at, closed_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (name, phone, source, interest, value, stage,
                  (dt.date.today() + dt.timedelta(days=follow_back)).isoformat() if follow_back is not None else None,
                  "Sample enquiry - chase or delete.",
                  day.isoformat() + " 11:30", day.isoformat() + " 11:30",
                  day.isoformat() + " 16:00" if stage in ("Won", "Lost") else None))
        _db.commit()
    print("Loaded sample clients, jobs (one itemised), a quote, expenses and enquiries. "
          "Delete the data file to start clean.")


# -------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "ChriphicsHub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % args))

    # helpers
    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_file(self, path):
        if not path or ".." in path:
            return self.send(404, "Not found", "text/plain")
        full = os.path.normpath(os.path.join(PUBLIC, path))
        if not full.startswith(PUBLIC) or not os.path.isfile(full):
            return self.send(404, "Not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self.send(200, fh.read(), ctype)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("Request body is not valid JSON")
        return parsed if isinstance(parsed, dict) else {}

    def csv_response(self, csv_text, filename):
        self.send(200, csv_text.encode("utf-8-sig"), "text/csv; charset=utf-8",
                  {"Content-Disposition": 'attachment; filename="%s"' % filename})

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # routing
    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        url = urlparse(self.path)
        path = unquote(url.path)
        params = parse_qs(url.query)
        try:
            with _lock:
                result = self.dispatch(method, path, params)
            if result is not None:
                code, payload = result
                self.emit(code, payload, path, params)
        except (ValueError, LookupError) as err:
            self.send(400, {"error": str(err)})
        except sqlite3.Error as err:
            self.send(500, {"error": "Database error: %s" % err})
        except Exception as err:  # noqa: BLE001 - keep the shop running
            self.send(500, {"error": "%s: %s" % (type(err).__name__, err)})

    def emit(self, code, payload, path, params):
        if isinstance(payload, tuple) and payload[0] == "csv":
            return self.csv_response(payload[1], payload[2])
        if isinstance(payload, tuple) and payload[0] == "raw":
            return self.send(code, payload[1], payload[2])
        if isinstance(payload, tuple) and payload[0] == "file":
            with open(payload[1], "rb") as fh:
                return self.send(200, fh.read(), payload[3],
                                 {"Content-Disposition": 'attachment; filename="%s"' % payload[2]})
        return self.send(code, payload)

    def dispatch(self, method, path, params):
        seg = [s for s in path.split("/") if s]
        if method == "GET" and (not seg or seg[0] != "api"):
            if path.startswith("/print/"):
                return 200, ("raw", receipt_html(int(path.rsplit("/", 1)[1])), "text/html; charset=utf-8")
            if not seg:
                return 200, ("raw", open(os.path.join(PUBLIC, "index.html"), encoding="utf-8").read(),
                             "text/html; charset=utf-8")
            self.send_file(os.path.join(*seg))
            return None
        if not seg or seg[0] != "api":
            raise LookupError("Unknown route " + path)
        rest = seg[1:]
        head = rest[0] if rest else ""

        if method == "GET" and head == "bootstrap":
            return 200, {"shop": SHOP, "statuses": STATUSES, "categories": CATEGORIES,
                         "units": UNITS, "methods": PAY_METHODS, "kinds": KINDS,
                         "doc_kinds": DOC_KINDS, "expense_categories": EXPENSE_CATEGORIES,
                         "lead_stages": LEAD_STAGES, "lead_sources": LEAD_SOURCES,
                         "db": DB_PATH.replace(os.path.expanduser("~"), "~"),
                         "open_jobs": one("SELECT count(*) c FROM jobs WHERE kind='Job'"
                                          " AND status IN ('Pending','Printing','Ready')")["c"],
                         "clients": one("SELECT count(*) c FROM clients WHERE archived=0")["c"],
                         "open_quotes": one("SELECT count(*) c FROM jobs WHERE kind='Quote'"
                                            " AND converted_at IS NULL AND status <> 'Cancelled'")["c"],
                         "open_leads": one("SELECT count(*) c FROM leads"
                                           " WHERE stage NOT IN ('Won','Lost')")["c"],
                         "counts": {r["status"]: r["c"] for r in q(
                             "SELECT status, count(*) c FROM jobs WHERE kind='Job' GROUP BY status")}}
        if method == "GET" and head == "dashboard":
            return 200, dashboard()
        if head == "clients":
            if len(rest) >= 2:
                cid = int(rest[1])
                if len(rest) == 3 and rest[2] == "restore" and method == "POST":
                    archive_client(cid, False)
                    return 200, {"ok": True}
                if len(rest) == 2:
                    if method == "GET":
                        detail = client_detail(cid)
                        return (200, detail) if detail else (400, {"error": "Client not found"})
                    if method == "PUT":
                        return 200, update_client(cid, self.body())
                    if method == "DELETE":
                        if params.get("hard", [""])[0] == "1":
                            delete_client(cid)
                        else:
                            archive_client(cid, True)
                        return 200, {"ok": True}
            elif method == "GET":
                return 200, list_clients(params)
            elif method == "POST":
                return 201, create_client(self.body())
        if head == "jobs":
            if len(rest) >= 2:
                jid = int(rest[1])
                if len(rest) == 2:
                    if method == "GET":
                        detail = job_detail(jid)
                        return (200, detail) if detail else (400, {"error": "Job not found"})
                    if method == "PUT":
                        return 200, update_job(jid, self.body())
                    if method == "DELETE":
                        with _lock:
                            _db.execute("DELETE FROM jobs WHERE id=?", (jid,))
                            _db.commit()
                        return 200, {"ok": True}
                elif len(rest) == 3 and method == "POST":
                    action = rest[2]
                    if action == "status":
                        return 200, set_status(jid, text(self.body().get("status"), 20))
                    if action == "quick":
                        payload = self.body()
                        return 200, quick_job(jid, text(payload.get("field"), 20), payload.get("value"))
                    if action == "note":
                        return 200, add_note(jid, self.body().get("note"))
                    if action == "convert":
                        return 200, convert_quote(jid, text(self.body().get("status"), 20) or "Pending")
                    if action == "expense":
                        payload = self.body()
                        payload["job_id"] = jid
                        return 201, create_expense(payload)
                    if action == "payment":
                        payload = self.body()
                        payload["job_id"] = jid
                        return 201, create_payment(payload)
            elif method == "GET":
                return 200, list_jobs(params)
            elif method == "POST":
                return 201, create_job(self.body())
        if head == "expenses":
            if len(rest) == 2:
                eid = int(rest[1])
                if method == "PUT":
                    return 200, update_expense(eid, self.body())
                if method == "DELETE":
                    return 200, delete_expense(eid)
                if method == "GET":
                    row = expense_detail(eid)
                    return (200, row) if row else (400, {"error": "Expense not found"})
            elif method == "GET":
                return 200, list_expenses(params)
            elif method == "POST":
                return 201, create_expense(self.body())
        if head == "leads":
            if len(rest) >= 2:
                lid = int(rest[1])
                if len(rest) == 3 and method == "POST":
                    if rest[2] == "stage":
                        return 200, set_lead_stage(lid, text(self.body().get("stage"), 20))
                    if rest[2] == "quick":
                        payload = self.body()
                        return 200, quick_lead(lid, text(payload.get("field"), 20), payload.get("value"))
                    if rest[2] == "convert":
                        return 200, convert_lead(lid, self.body())
                if len(rest) == 2:
                    if method == "GET":
                        row = lead_detail(lid)
                        return (200, row) if row else (400, {"error": "Enquiry not found"})
                    if method == "PUT":
                        return 200, update_lead(lid, self.body())
                    if method == "DELETE":
                        return 200, delete_lead(lid)
            elif method == "GET":
                return 200, list_leads(params)
            elif method == "POST":
                return 201, create_lead(self.body())
        if method == "GET" and head == "payments" and len(rest) == 1:
            return 200, list_payments(params)
        if method == "POST" and head == "payments":
            return 201, create_payment(self.body())
        if head == "payments" and len(rest) == 2 and method == "DELETE":
            row = one("SELECT * FROM payments WHERE id=?", (int(rest[1]),))
            if not row:
                raise LookupError("Payment not found")
            with _lock:
                _db.execute("DELETE FROM payments WHERE id=?", (row["id"],))
                if row["job_id"]:
                    _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'payment', ?)",
                                (row["job_id"], "Payment of %s %.2f removed" % (SHOP["currency_symbol"], row["amount"])))
                _db.commit()
            return 200, {"ok": True}
        if method == "GET" and head == "accounts":
            return 200, accounts_view(params)
        if method == "GET" and head == "report":
            return 200, report(params)
        if method == "GET" and head == "export" and len(rest) == 2:
            name = rest[1].replace(".csv", "")
            rows, filename, _count = export_rows(name, params)
            return 200, ("csv", rows, filename)
        if method == "GET" and head == "backup":
            tmp = write_backup()
            return 200, ("file", tmp, os.path.basename(tmp), "application/x-sqlite3")
        raise LookupError("Unknown route " + method + " " + path)


def write_backup():
    """Snapshot the book into BACKUP_DIR and return the new file's path."""
    path = os.path.join(BACKUP_DIR, "backup-%s.db" % dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with _lock:
        dest = sqlite3.connect(path)
        with dest:
            _db.backup(dest)
        # Roll back to a plain file so the copy the user keeps is one complete .db.
        dest.execute("PRAGMA journal_mode=DELETE")
        dest.close()
    for extra in ("-wal", "-shm"):
        try:
            os.remove(path + extra)
        except OSError:
            pass
    return path


def watch_parent():
    """The .app holds our stdin open. When it closes, the app is gone — exit too,
    so a killed app never leaves an engine behind holding the data file."""
    if os.environ.get("CHRIPHICS_WATCH_STDIN") != "1":
        return

    def loop():
        try:
            while sys.stdin.read(1):
                pass
        except Exception:  # noqa: BLE001 - any read failure means the pipe is gone
            pass
        os._exit(0)

    threading.Thread(target=loop, daemon=True).start()


def serve(port, open_browser, seed_first):
    if seed_first:
        seed()
    watch_parent()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("%s is running at %s" % (SHOP["name"], url))
    print("Data file: %s" % DB_PATH)
    print("Press Ctrl+C (or close this window) to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    ap = argparse.ArgumentParser(description="CRISPprint Ghana records & accounts")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CHRIPHICS_PORT", 8712)))
    ap.add_argument("--db", default=None, help="alternative SQLite file")
    ap.add_argument("--seed", action="store_true", help="load sample records if the book is empty")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--backup", action="store_true", help="write data/backup-<timestamp>.db and exit")
    args = ap.parse_args()
    connect(args.db)
    if args.backup:
        print("Backup written to %s" % write_backup())
        return
    serve(args.port, not args.no_browser, args.seed)


if __name__ == "__main__":
    main()
