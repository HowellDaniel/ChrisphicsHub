#!/usr/bin/env python3
"""Chrisphics Hub — the printing records and account book of CRISPprint Ghana.

Local-first app: Python standard library + one SQLite file. Optional outbound job notices
use the shop's configured WhatsApp Business and email providers.

    python3 server.py                 # start on http://127.0.0.1:8712
    python3 server.py --seed          # start and load sample records (for a demo)
    python3 server.py --backup        # write a timestamped copy of the data and exit
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
import hashlib
import ipaddress
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import ssl
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
# One book wherever Chrisphics Hub is started from — the app, the .command or a bare
# `python3 server.py` — so records never fork into two copies.
SUPPORT = os.path.expanduser("~/Library/Application Support/Chrisphics Hub")
DB_PATH = os.environ.get("CHRISPHICS_DB") or os.path.join(SUPPORT, "chrisphics.db")
BACKUP_DIR = os.environ.get("CHRISPHICS_BACKUP_DIR") or os.path.join(SUPPORT, "Backups")
# Where the book lived while the shop's name was misspelled. adopt_legacy_book() copies it
# forward on the first run of the corrected build; the old file is never touched.
LEGACY_DB = os.path.expanduser("~/Library/Application Support/Chriphics Hub/chriphics.db")

SHOP = {
    # The shop's trading name, as it appears on a job sheet. The phone stays empty until the shop
    # sets CHRISPHICS_SHOP_PHONE: these details go out to customers, and a number that reaches
    # nobody is worse than no number at all.
    "name": os.environ.get("CHRISPHICS_SHOP_NAME", "CRISPprint Ghana"),
    "tagline": os.environ.get("CHRISPHICS_SHOP_TAGLINE", "Printing & Design Services"),
    "phone": os.environ.get("CHRISPHICS_SHOP_PHONE", "").strip(),
    "address": os.environ.get("CHRISPHICS_SHOP_ADDRESS", "Accra, Ghana"),
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

# Hard ceilings for the book. An unbounded float is what let a "payment" of 1e30 sit beside a
# job of 1,250, so every money field is checked against these before it reaches SQLite.
MONEY_MAX = 10_000_000.0
QUANTITY_MAX = 1_000_000

# A job is booked work; a quote is a price we have promised but not yet printed.
DOC_KINDS = ["Job", "Quote"]

# Money going out, so the book shows profit and not only takings.
EXPENSE_CATEGORIES = [
    "Paper / Stock", "Ink / Toner", "Finishing", "Substrate", "Outsourced Printing",
    "Transport", "Rent", "Utilities", "Salaries", "Equipment", "Maintenance",
    "Design Assets", "Marketing", "Data / Airtime", "Licences", "Bank Charges", "Spoilage",
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
_sessions = {}
AUTH_PASSWORD = os.environ.get("CHRISPHICS_AUTH_PASSWORD", "")
_notification_wakeup = threading.Event()
# Reentrant: route() holds this while the handlers below take it again.
_lock = threading.RLock()


# --------------------------------------------------------------------------- data

def legacy_book_path():
    """The book left behind in the misspelled support folder, or None when there is nothing to
    bring forward. Only ever considered on the default path, so a test, a Windows box or a Render
    location never pulls in a book it has not been asked for. connect() asks this before opening
    the new file, because opening it would create an empty book and hide the old one."""
    if DB_PATH != os.path.join(SUPPORT, "chrisphics.db"):
        return None
    if os.path.exists(DB_PATH) or not os.path.exists(LEGACY_DB):
        return None
    return LEGACY_DB


def adopt_legacy_book(source_path):
    """Copy the old book into the corrected folder, once. The old file is read only and never
    moved, so the shop always still has the folder it started with."""
    if not source_path:
        return
    source = sqlite3.connect("file:%s?mode=ro" % source_path, uri=True)
    try:
        with _lock:
            source.backup(_db)
            _db.commit()
        sys.stdout.write("Book copied from the older folder: %s\n" % source_path)
    finally:
        source.close()


def connect(path=None):
    global _db, DB_PATH
    if path:
        DB_PATH = os.path.abspath(path)
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    pending = legacy_book_path()
    _db = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    _db.execute("PRAGMA journal_mode=WAL")
    _db.execute("PRAGMA foreign_keys=ON")
    _db.execute("PRAGMA busy_timeout=5000")
    adopt_legacy_book(pending)
    # An older book is brought forward first: the schema's views and indexes read the
    # columns migrate() adds, so they cannot be created against the old table.
    migrate()
    with open(os.path.join(ROOT, "schema.sql"), "r", encoding="utf-8") as fh:
        _db.executescript(fh.read())
    _db.commit()
    default_updates_consent()


# executescript() creates missing tables, but CREATE TABLE IF NOT EXISTS can never add a
# column to a table that already has records. A book written by an older Chrisphics Hub is
# brought forward here, column by column, with nothing dropped and nothing rewritten.
JOB_MIGRATIONS = [
    ("kind", "TEXT NOT NULL DEFAULT 'Job'"),
    ("valid_until", "TEXT"),
    ("converted_at", "TEXT"),
]
CLIENT_MIGRATIONS = [
    ("whatsapp_updates", "INTEGER NOT NULL DEFAULT 0"),
    ("email_updates", "INTEGER NOT NULL DEFAULT 0"),
]
NOTIFICATION_MIGRATIONS = [
    ("auto_send", "INTEGER NOT NULL DEFAULT 0"),
    ("delivery_state", "TEXT NOT NULL DEFAULT 'Manual'"),
    ("delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("delivery_next_at", "REAL"),
    ("delivery_error", "TEXT NOT NULL DEFAULT ''"),
    ("provider_id", "TEXT NOT NULL DEFAULT ''"),
]
SIGNAL_MIGRATIONS = [
    # A send that names nobody in the book is money out, so the notice needs a second
    # way to be recorded than a payment against a client.
    ("expense_id", "INTEGER REFERENCES expenses(id) ON DELETE SET NULL"),
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
    for table, migrations in (("clients", CLIENT_MIGRATIONS),
                              ("notifications", NOTIFICATION_MIGRATIONS),
                              ("money_signals", SIGNAL_MIGRATIONS)):
        if table not in tables:
            continue
        have = {r["name"] for r in _db.execute("PRAGMA table_info(%s)" % table).fetchall()}
        for name, ddl in migrations:
            if name not in have:
                _db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
    # Quotes predate the kind column, so a book that only ever had jobs needs no backfill;
    # anything already booked keeps its 'Job' default.
    _db.execute("UPDATE jobs SET kind='Job' WHERE kind IS NULL OR kind=''")


def default_updates_consent():
    """The shop asked for clients to be told when their job has been processed, but the two
    consent columns arrived in a book that was already written, and every row in it defaulted to
    no. Each client is opted in once here, on the first launch that carries this code; after that
    only the client screen decides."""
    if state_value("consent_default"):
        return
    with _lock:
        _db.execute("UPDATE clients SET whatsapp_updates = 1, email_updates = 1 WHERE archived = 0")
        _db.commit()
    set_state("consent_default", "1")
    sys.stdout.write("Clients are opted in to WhatsApp and email job updates "
                     "(turn it off per client on their record)\n")


def q(sql, args=()):
    # The shop server shares one SQLite connection across request and worker threads.
    # route() already holds this reentrant lock for HTTP requests, but background workers
    # also call q() directly; serialize both execution and cursor consumption.
    with _lock:
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
            n = one("SELECT count(*) c FROM jobs WHERE ref LIKE ?", (prefix + "-%",))["c"] + 1
    return "%s-%d-%04d" % (prefix, year, n)


def num(value, default=0.0, minimum=None, maximum=None):
    try:
        out = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    # inf and nan both survive round() and land in the book: nan is stored as NULL and inf
    # answers "not a float" the moment anything tries to int() it.
    if out != out or out in (float("inf"), float("-inf")):
        return default
    if minimum is not None and out < minimum:
        out = minimum
    if maximum is not None and out > maximum:
        out = maximum
    return round(out, 2)


def text(value, limit=4000, required=False, name="field"):
    out = ("" if value is None else str(value)).strip()
    if required and not out:
        raise ValueError("%s is required" % name)
    return out[:limit]


def money(value):
    return float(num(value, 0.0, 0.0, MONEY_MAX))


def path_id(value):
    """A record number taken from a URL. A link that names no row is 'not found', not a
    server fault, and a number too wide for a SQLite INTEGER must never reach a query."""
    try:
        out = int(str(value).strip())
    except (TypeError, ValueError):
        raise LookupError("That link does not name a record in the book")
    if not 1 <= out <= 9223372036854775807:
        raise LookupError("That link does not name a record in the book")
    return out


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
    # `paid=full` is the settled side of the book: work that was billed and has been paid
    # right down to nothing. Cancelled jobs are not settlements, so they stay out of it.
    paid = params.get("paid", [""])[0]
    if paid == "full":
        where.append("j.kind = 'Job' AND j.status <> 'Cancelled' AND ja.total > 0 AND ja.balance <= 0.005")
    elif paid == "due":
        where.append("j.kind = 'Job' AND j.status <> 'Cancelled' AND ja.balance > 0.005")
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
             ja.total, ja.paid, ja.balance, ja.cost, ja.profit, ja.item_count,
             (SELECT count(*) FROM notifications n
               WHERE n.job_id = j.id AND n.state = 'Queued') AS to_send
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
             c.whatsapp_updates AS client_whatsapp_updates,
             c.email_updates AS client_email_updates,
             ja.total, ja.paid, ja.balance, ja.cost, ja.profit, ja.item_count,
             (SELECT max(p.paid_at) FROM payments p WHERE p.job_id = j.id) AS last_paid
      FROM jobs j JOIN clients c ON c.id = j.client_id JOIN job_accounts ja ON ja.id = j.id
      WHERE j.id = ?
    """, (job_id,))
    if not job:
        return None
    job["items"] = items_of(job_id)
    job["payments"] = q("SELECT * FROM payments WHERE job_id = ? ORDER BY paid_at DESC, id DESC", (job_id,))
    job["expenses"] = q("SELECT * FROM expenses WHERE job_id = ? ORDER BY spent_on DESC, id DESC", (job_id,))
    job["spoilage"] = q("""
      SELECT s.id, s.quantity, s.reason, s.spoiled_on, e.amount, e.category
      FROM spoiled_work s JOIN expenses e ON e.id = s.expense_id
      WHERE s.job_id = ? ORDER BY s.spoiled_on DESC, s.id DESC
    """, (job_id,))
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
    data["quantity"] = min(max(int(num(payload.get("quantity"), 1, 0)), 0), QUANTITY_MAX)
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
        qty = min(max(int(num(row.get("quantity"), 0, 0)), 0), QUANTITY_MAX)
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
        with _lock:
            replace_items(job_id, items)
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                        (job_id, "%d item line(s) added" % len(items)))
            _db.commit()
    # A booked job's initial status is its first customer update; quotes stay manual.
    queue_message(job_id, "Quote" if data["kind"] == "Quote" else data["status"])
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
    with _lock:
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
    queue_message(job_id, data["status"])
    return job_detail(job_id)


def set_status(job_id, status):
    if status not in STATUSES:
        raise ValueError("Unknown status " + status)
    before = one("SELECT status FROM jobs WHERE id = ?", (job_id,))
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
    # Moving the job is what the client is waiting to hear about, so the news is written now.
    if before and before["status"] != status:
        queue_message(job_id, status)
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
    queue_message(job_id, status)
    return job_detail(job_id)


# ------------------------------------------------------------------ client messages

# Automatic delivery requires explicit client consent and configured provider credentials.
# The four stages a client is waiting on: that the order is in, on the press, off the press,
# and delivered. A quote or a cancellation is the shop's judgement, so those stay manual.
AUTO_NOTIFY_EVENTS = frozenset(("Pending", "Printing", "Ready", "Delivered"))
DELIVERY_RETRY_SECONDS = (60, 300, 900, 3600, 21600, 86400, 86400)
DELIVERY_MAX_ATTEMPTS = len(DELIVERY_RETRY_SECONDS) + 1
WHATSAPP_API_VERSION = os.environ.get("CHRISPHICS_WHATSAPP_API_VERSION", "v22.0")
WHATSAPP_TOKEN = os.environ.get("CHRISPHICS_WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("CHRISPHICS_WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_TEMPLATE = os.environ.get("CHRISPHICS_WHATSAPP_TEMPLATE", "")
WHATSAPP_TEMPLATE_LANGUAGE = os.environ.get("CHRISPHICS_WHATSAPP_TEMPLATE_LANGUAGE", "en")
EMAIL_SMTP_HOST = os.environ.get("CHRISPHICS_EMAIL_SMTP_HOST", "")
EMAIL_SMTP_PORT = os.environ.get("CHRISPHICS_EMAIL_SMTP_PORT", "587")
EMAIL_SMTP_USERNAME = os.environ.get("CHRISPHICS_EMAIL_SMTP_USERNAME", "")
EMAIL_SMTP_PASSWORD = os.environ.get("CHRISPHICS_EMAIL_SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("CHRISPHICS_EMAIL_FROM", EMAIL_SMTP_USERNAME)
EMAIL_FROM_NAME = os.environ.get("CHRISPHICS_EMAIL_FROM_NAME", SHOP["name"])
EMAIL_SMTP_SECURITY = os.environ.get("CHRISPHICS_EMAIL_SMTP_SECURITY", "starttls").lower()

COUNTRY_DIAL = "233"        # Ghana: 059 387 2873 and +233 59 387 2873 are the same number.
NOTIFY_EVENTS = ["Quote", "Booked", "Pending", "Printing", "Ready", "Delivered", "Cancelled"]
NOTIFY_STATES = ["Queued", "Opened", "Sent"]
NOTIFY_CHANNELS = ["WhatsApp", "Email"]

# The news, in the shop's own voice, one entry per event. {fields} come from the job.
NOTIF_NEWS = {
    "Quote": "Here is the quote {ref} we discussed: {what} — {total}. {hold}"
             "Say the word and we book it for you.",
    "Booked": "Your order {ref} is booked: {what} — {total}. {due}"
              "We will message you when it goes on the press and again when it is ready.",
    "Pending": "Your order {ref} for {what} is confirmed and waiting its turn in the queue. {due}",
    "Printing": "Your order {ref} for {what} is on the press right now. {due}"
                "We will tell you as soon as it is off the machine.",
    "Ready": "Good news: your order {ref} for {what} is ready for collection at {address}. {balance}"
             "Let us know when you are coming.",
    "Delivered": "Your order {ref} has been delivered. {balance}"
                 "Thank you for your business — we appreciate it.",
    "Cancelled": "Your order {ref} has been cancelled. {balance}"
                 "Call {phone} if you would like us to run it again.",
}

# The short phrase an email subject is built from.
NOTIF_HEADLINE = {
    "Quote": "quote ready", "Booked": "order booked", "Pending": "order in the queue",
    "Printing": "on the press", "Ready": "ready for collection",
    "Delivered": "delivered", "Cancelled": "cancelled",
}


def wa_number(raw):
    """A wa.me number: digits only, country code in front, no plus and no trunk zero."""
    d = re.sub(r"\D", "", str(raw or ""))
    if not d:
        return ""
    if d.startswith("00"):
        d = d[2:]
    if d.startswith(COUNTRY_DIAL) and len(d) >= 12:
        return d
    if len(d) == 10 and d.startswith("0"):
        return COUNTRY_DIAL + d[1:]
    if len(d) == 9:            # local subscriber number written without the leading 0
        return COUNTRY_DIAL + d
    return d


def nice_date(value):
    if not value:
        return ""
    raw = str(value)[:10]
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return raw


def job_what(job):
    """One phrase the client recognises: what is being printed for them."""
    lines = job.get("items") or []
    if lines:
        first = lines[0]
        phrase = "%s %s %s" % (first["quantity"], first["unit"], first["title"])
        if len(lines) > 1:
            phrase += " + %d other line%s" % (len(lines) - 1, "" if len(lines) == 2 else "s")
        size = first.get("size") or ""
    else:
        phrase = "%s %s %s" % (job["quantity"], job["unit"], job["title"])
        size = job.get("size") or ""
    return phrase + (" (%s)" % size if size else "")


def message_fields(job):
    sym = SHOP["currency_symbol"]
    balance = round(float(job.get("balance") or 0), 2)
    if job.get("kind") == "Quote":
        money_note = ""
    elif balance > 0.005:
        money_note = "Balance to pay on collection: %s%.2f. " % (sym, balance)
    elif balance < -0.005:
        money_note = "You are in credit by %s%.2f with us. " % (sym, -balance)
    else:
        money_note = "Everything on this job is settled. "
    due = nice_date(job.get("due_date"))
    hold = nice_date(job.get("valid_until"))
    return {
        "client": job.get("client") or "there",
        "shop": SHOP["name"],
        "ref": job["ref"],
        "what": job_what(job),
        "category": job.get("category") or "",
        "total": "%s%.2f" % (sym, float(job.get("total") or 0)),
        "due": ("We are working to have it ready by %s. " % due) if due else "",
        "hold": ("The price holds until %s. " % hold) if hold else "",
        "balance": money_note,
        "address": SHOP["address"],
        "phone": SHOP["phone"],
    }


def message_body(job, event, channel):
    f = message_fields(job)
    news = NOTIF_NEWS[event].format(**f)
    if channel == "WhatsApp":
        # Until the shop sets its own number, the message says nothing rather than handing the
        # client a placeholder that reaches nobody.
        return "Hello %s, %s here.\n\n%s%s" % (
            f["client"], f["shop"], news,
            "\n\nCall or WhatsApp %s if anything needs changing." % f["phone"] if f["phone"] else "")
    sign = "Kind regards,\n%s — %s" % (f["shop"], SHOP["tagline"])
    contact = " | ".join(x for x in (f["phone"], f["address"]) if x)
    return "Hello %s,\n\n%s\n\n%s%s" % (f["client"], news, sign, "\n" + contact if contact else "")


def automatic_whatsapp_body(job, event):
    return "Hello %s, your print job %s (%s) is now %s. We will keep you updated." % (
        job.get("client") or "there", job["ref"], job_what(job), event)


def message_subject(job, event):
    return "%s %s — %s" % (job["ref"], NOTIF_HEADLINE[event], SHOP["name"])


def message_target(job, channel):
    """Where this client can be reached on this channel, or '' if they cannot."""
    if channel == "WhatsApp":
        return wa_number(job.get("client_whatsapp") or job.get("client_phone"))
    email = text(job.get("client_email"), 120).strip()
    return email if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) else ""


def message_link(row):
    if row["channel"] == "WhatsApp":
        return "https://wa.me/%s?text=%s" % (row["to_address"], quote(row["body"], safe=""))
    return "mailto:%s?subject=%s&body=%s" % (
        row["to_address"], quote(row["subject"], safe=""),
        quote(row["body"].replace("\n", "\r\n"), safe=""))


def notify_rows(job_id=None, event=None):
    """The messages owed on a job, oldest event first, WhatsApp before email."""
    where = (" WHERE job_id = ?" if job_id else "") + (" AND event = ?" if event else "")
    args = tuple(x for x in (job_id, event) if x is not None)
    order = {name: i for i, name in enumerate(NOTIFY_EVENTS)}
    rows = q("SELECT * FROM notifications%s" % where, args)
    rows.sort(key=lambda r: (order.get(r["event"], 99), 0 if r["channel"] == "WhatsApp" else 1, r["id"]))
    for r in rows:
        r["link"] = message_link(r)
    return rows


def queue_message(job_id, event, automatic=None):
    """Queue a customer update for delivery or a deliberate manual handoff."""
    if event not in NOTIFY_EVENTS:
        raise ValueError("There is no client message called " + event)
    job = job_detail(job_id)
    if not job:
        raise LookupError("Job not found")
    if automatic is None:
        automatic = event in AUTO_NOTIFY_EVENTS and job["kind"] == "Job"
    with _lock:
        fresh, skipped_consent = [], []
        for channel in NOTIFY_CHANNELS:
            address = message_target(job, channel)
            if not address:
                continue
            old = one("SELECT body, subject, to_address, auto_send, state, delivery_state "
                      "FROM notifications WHERE job_id = ? AND event = ? AND channel = ?",
                      (job_id, event, channel))
            channel_auto = automatic or bool(
                old and old["auto_send"] and old["delivery_state"] != "Cancelled")
            consent_key = "client_%s_updates" % channel.lower()
            if channel_auto and not job.get(consent_key):
                skipped_consent.append(channel)
                continue
            # The short wording exists for the WhatsApp template, which is all an automatic send
            # can carry. Until a provider is set up the message leaves through the shop's own
            # WhatsApp or Mail, so it should go out in the shop's full voice instead.
            provider_ready = not notification_config_error(channel)
            body = (automatic_whatsapp_body(job, event)
                    if channel_auto and channel == "WhatsApp" and provider_ready
                    else message_body(job, event, channel))
            subject = message_subject(job, event) if channel == "Email" else ""
            _db.execute("""
              INSERT INTO notifications
                (job_id, client_id, event, channel, to_address, subject, body, auto_send,
                 delivery_state, delivery_next_at)
              VALUES (:job_id, :client_id, :event, :channel, :to_address, :subject, :body,
                      :auto_send, :delivery_state, :delivery_next_at)
              ON CONFLICT(job_id, event, channel) DO UPDATE SET
                to_address = CASE WHEN notifications.state='Sent'
                                        THEN notifications.to_address ELSE excluded.to_address END,
                subject = CASE WHEN notifications.state='Sent'
                                     THEN notifications.subject ELSE excluded.subject END,
                body = CASE WHEN notifications.state='Sent'
                            THEN notifications.body ELSE excluded.body END,
                state = CASE WHEN notifications.state='Sent' THEN 'Sent'
                                   WHEN notifications.body <> excluded.body
                                         OR notifications.subject <> excluded.subject
                                         OR notifications.to_address <> excluded.to_address
                                   OR (notifications.auto_send = 0 AND excluded.auto_send = 1)
                                   OR (notifications.delivery_state = 'Cancelled' AND excluded.auto_send = 1)
                                   THEN 'Queued' ELSE notifications.state END,
                auto_send = CASE WHEN notifications.state='Sent'
                                       THEN notifications.auto_send ELSE excluded.auto_send END,
                delivery_state = CASE WHEN notifications.state='Sent'
                                            THEN notifications.delivery_state
                                            WHEN notifications.body <> excluded.body
                                                 OR notifications.subject <> excluded.subject
                                                 OR notifications.to_address <> excluded.to_address
                                                 OR notifications.auto_send <> excluded.auto_send
                                                 OR (notifications.delivery_state = 'Cancelled' AND excluded.auto_send = 1)
                                            THEN excluded.delivery_state ELSE notifications.delivery_state END,
                delivery_attempts = CASE WHEN notifications.state='Sent'
                                                    THEN notifications.delivery_attempts
                                                    WHEN notifications.body <> excluded.body
                                                    OR notifications.subject <> excluded.subject
                                                    OR notifications.to_address <> excluded.to_address
                                                    OR notifications.auto_send <> excluded.auto_send
                                                    OR (notifications.delivery_state = 'Cancelled' AND excluded.auto_send = 1)
                                               THEN 0 ELSE notifications.delivery_attempts END,
                delivery_next_at = CASE WHEN notifications.state='Sent'
                                                   THEN notifications.delivery_next_at
                                                   WHEN notifications.body <> excluded.body
                                                   OR notifications.subject <> excluded.subject
                                                   OR notifications.to_address <> excluded.to_address
                                                   OR notifications.auto_send <> excluded.auto_send
                                                   OR (notifications.delivery_state = 'Cancelled' AND excluded.auto_send = 1)
                                              THEN NULL ELSE notifications.delivery_next_at END,
                delivery_error = CASE WHEN notifications.state='Sent'
                                                 THEN notifications.delivery_error
                                                 WHEN notifications.body <> excluded.body
                                                 OR notifications.subject <> excluded.subject
                                                 OR notifications.to_address <> excluded.to_address
                                                 OR notifications.auto_send <> excluded.auto_send
                                                 OR (notifications.delivery_state = 'Cancelled' AND excluded.auto_send = 1)
                                            THEN '' ELSE notifications.delivery_error END,
                provider_id = CASE WHEN notifications.state='Sent'
                                              THEN notifications.provider_id
                                              WHEN notifications.body <> excluded.body
                                              OR notifications.subject <> excluded.subject
                                              OR notifications.to_address <> excluded.to_address
                                              OR notifications.auto_send <> excluded.auto_send
                                              OR (notifications.delivery_state = 'Cancelled' AND excluded.auto_send = 1)
                                         THEN '' ELSE notifications.provider_id END,
                updated_at = CASE WHEN notifications.state='Sent' THEN notifications.updated_at
                                         ELSE datetime('now','localtime') END
            """, dict(job_id=job_id, client_id=job["client_id"], event=event, channel=channel,
                      to_address=address, subject=subject, body=body,
                      auto_send=1 if channel_auto else 0,
                      delivery_state="Pending" if channel_auto else "Manual",
                      delivery_next_at=time.time() + 3 if channel_auto else None))
            if old is None or (old["state"] != "Sent" and
                    (old["body"] != body or old["subject"] != subject
                     or old["to_address"] != address or old["auto_send"] != (1 if channel_auto else 0)
                     or (old["delivery_state"] == "Cancelled" and channel_auto))):
                fresh.append(channel)
        if fresh or skipped_consent:
            details = []
            if fresh:
                details.append("%s %s message queued for %s" % (
                    event, "automatic" if automatic else "manual", ", ".join(fresh)))
            if skipped_consent:
                details.append("%s automatic update not queued: no recorded consent for %s" % (
                    event, ", ".join(skipped_consent)))
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'message', ?)",
                        (job_id, "; ".join(details) + " (%s)" % job["client"]))
        _db.commit()
    if any(row["auto_send"] and row["state"] == "Queued" for row in notify_rows(job_id, event)):
        _notification_wakeup.set()
    return notify_rows(job_id, event)


def refresh_pending_status_notification(job_id):
    job = one("SELECT kind,status FROM jobs WHERE id=?", (job_id,))
    if job and job["kind"] == "Job" and job["status"] in AUTO_NOTIFY_EVENTS:
        queue_message(job_id, job["status"], automatic=True)


def notification_config_error(channel):
    if channel == "WhatsApp":
        missing = [name for name, value in (
            ("CHRISPHICS_WHATSAPP_TOKEN", WHATSAPP_TOKEN),
            ("CHRISPHICS_WHATSAPP_PHONE_NUMBER_ID", WHATSAPP_PHONE_NUMBER_ID),
            ("CHRISPHICS_WHATSAPP_TEMPLATE", WHATSAPP_TEMPLATE),
        ) if not value]
        if not re.fullmatch(r"v\d+\.\d+", WHATSAPP_API_VERSION):
            missing.append("CHRISPHICS_WHATSAPP_API_VERSION (for example v22.0)")
        if WHATSAPP_PHONE_NUMBER_ID and not WHATSAPP_PHONE_NUMBER_ID.isdigit():
            return "WhatsApp phone number ID must contain digits only."
        if missing:
            return "Configure " + ", ".join(missing) + " to enable WhatsApp delivery."
        return ""
    if channel == "Email":
        missing = [name for name, value in (
            ("CHRISPHICS_EMAIL_SMTP_HOST", EMAIL_SMTP_HOST),
            ("CHRISPHICS_EMAIL_SMTP_USERNAME", EMAIL_SMTP_USERNAME),
            ("CHRISPHICS_EMAIL_SMTP_PASSWORD", EMAIL_SMTP_PASSWORD),
            ("CHRISPHICS_EMAIL_FROM", EMAIL_FROM),
        ) if not value]
        try:
            port = int(EMAIL_SMTP_PORT)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            missing.append("a valid CHRISPHICS_EMAIL_SMTP_PORT")
        if EMAIL_SMTP_SECURITY not in ("starttls", "ssl"):
            missing.append("CHRISPHICS_EMAIL_SMTP_SECURITY=starttls or ssl")
        if EMAIL_FROM and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", EMAIL_FROM):
            return "CHRISPHICS_EMAIL_FROM must be a valid email address."
        if missing:
            return "Configure " + ", ".join(missing) + " to enable email delivery."
        return ""
    return "Unknown notification channel."


def _whatsapp_error_text(error):
    try:
        payload = json.loads(error.read(4096).decode("utf-8", errors="replace"))
        message = payload.get("error", {}).get("message")
        if message:
            return str(message)[:350]
    except (ValueError, AttributeError):
        pass
    return "Provider returned HTTP %s" % error.code


def send_whatsapp_notification(row):
    job = job_detail(row["job_id"])
    if not job:
        raise RuntimeError("The job or client no longer exists.")
    payload = {
        "messaging_product": "whatsapp",
        "to": row["to_address"],
        "type": "template",
        "template": {
            "name": WHATSAPP_TEMPLATE,
            "language": {"code": WHATSAPP_TEMPLATE_LANGUAGE},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": job["client"]},
                    {"type": "text", "text": job["ref"]},
                    {"type": "text", "text": job_what(job)},
                    {"type": "text", "text": row["event"]},
                ],
            }],
        },
    }
    url = "https://graph.facebook.com/%s/%s/messages" % (
        WHATSAPP_API_VERSION, WHATSAPP_PHONE_NUMBER_ID)
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + WHATSAPP_TOKEN,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError("WhatsApp delivery failed: " + _whatsapp_error_text(error)) from None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as error:
        raise RuntimeError("WhatsApp connection failed: %s" % str(error)[:300]) from None
    messages = result.get("messages") or []
    if not messages or not messages[0].get("id"):
        raise RuntimeError("WhatsApp accepted no message ID; check the provider response.")
    return messages[0]["id"]


def send_email_notification(row):
    message = EmailMessage()
    safe_name = EMAIL_FROM_NAME.replace("\r", " ").replace("\n", " ").strip()
    message["From"] = formataddr((safe_name, EMAIL_FROM))
    message["To"] = row["to_address"]
    message["Subject"] = row["subject"]
    message["Message-ID"] = make_msgid()
    message.set_content(row["body"])
    port = int(EMAIL_SMTP_PORT)
    context = ssl.create_default_context()
    if EMAIL_SMTP_SECURITY == "ssl":
        with smtplib.SMTP_SSL(EMAIL_SMTP_HOST, port, timeout=20, context=context) as smtp:
            smtp.login(EMAIL_SMTP_USERNAME, EMAIL_SMTP_PASSWORD)
            refused = smtp.send_message(message)
    else:
        with smtplib.SMTP(EMAIL_SMTP_HOST, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(EMAIL_SMTP_USERNAME, EMAIL_SMTP_PASSWORD)
            refused = smtp.send_message(message)
    if refused:
        raise RuntimeError("Email provider refused the recipient.")
    return str(message["Message-ID"])


def _claim_notification(note_id):
    with _lock:
        row = one("""
          SELECT n.*, c.whatsapp_updates, c.email_updates
          FROM notifications n JOIN clients c ON c.id=n.client_id WHERE n.id=?
        """, (note_id,))
        if not row or not row["auto_send"] or row["state"] != "Queued":
            return None
        if row["delivery_state"] not in ("Pending", "Sending"):
            return None
        now = time.time()
        if row["delivery_next_at"] is not None and row["delivery_next_at"] > now:
            return None
        consent = row["whatsapp_updates"] if row["channel"] == "WhatsApp" else row["email_updates"]
        if not consent:
            _db.execute("""
              UPDATE notifications SET delivery_state='Cancelled', delivery_next_at=NULL,
                delivery_error='Automatic delivery cancelled: client withdrew consent',
                updated_at=datetime('now','localtime') WHERE id=?
            """, (note_id,))
            _db.commit()
            return None
        attempts = row["delivery_attempts"] + 1
        _db.execute("""
          UPDATE notifications SET delivery_state='Sending', delivery_attempts=?,
            delivery_next_at=?, delivery_error='', updated_at=datetime('now','localtime')
          WHERE id=? AND delivery_state IN ('Pending','Sending')
        """, (attempts, now + 300, note_id))
        _db.commit()
        row["delivery_attempts"] = attempts
        return row


def _record_delivery_failure(row, reason):
    attempts = row["delivery_attempts"]
    if attempts >= DELIVERY_MAX_ATTEMPTS:
        state, next_at = "Failed", None
    else:
        delay = DELIVERY_RETRY_SECONDS[min(attempts - 1, len(DELIVERY_RETRY_SECONDS) - 1)]
        state, next_at = "Pending", time.time() + delay
    with _lock:
        _db.execute("""
          UPDATE notifications SET delivery_state=?, delivery_next_at=?, delivery_error=?,
            updated_at=datetime('now','localtime')
          WHERE id=? AND delivery_state='Sending'
        """, (state, next_at, str(reason)[:500], row["id"]))
        _db.commit()


def _consent_still_valid(row):
    column = "whatsapp_updates" if row["channel"] == "WhatsApp" else "email_updates"
    with _lock:
        client = one("SELECT %s AS allowed FROM clients WHERE id=?" % column, (row["client_id"],))
        if client and client["allowed"]:
            return True
        _db.execute("""
          UPDATE notifications SET delivery_state='Cancelled', delivery_next_at=NULL,
            delivery_error='Automatic delivery cancelled: client withdrew consent',
            updated_at=datetime('now','localtime') WHERE id=? AND delivery_state='Sending'
        """, (row["id"],))
        _db.commit()
        return False


def deliver_pending_notifications():
    now = time.time()
    candidates = q("""
      SELECT id, channel FROM notifications
      WHERE auto_send=1 AND state='Queued' AND delivery_state IN ('Pending','Sending')
        AND (delivery_next_at IS NULL OR delivery_next_at<=?)
      ORDER BY id LIMIT 50
    """, (now,))
    for item in candidates:
        config_error = notification_config_error(item["channel"])
        if config_error:
            with _lock:
                current = one("SELECT delivery_error FROM notifications WHERE id=?", (item["id"],))
                if current and current["delivery_error"] != config_error:
                    _db.execute("UPDATE notifications SET delivery_error=? WHERE id=?",
                                (config_error, item["id"]))
                    _db.commit()
            continue
        row = _claim_notification(item["id"])
        if not row or not _consent_still_valid(row):
            continue
        try:
            provider_id = (send_whatsapp_notification(row) if row["channel"] == "WhatsApp"
                           else send_email_notification(row))
        except Exception as error:  # Provider/transport failures are visible and retried.
            _record_delivery_failure(row, "%s: %s" % (type(error).__name__, error))
            continue
        with _lock:
            current = one("SELECT delivery_state FROM notifications WHERE id=?", (row["id"],))
            if current and current["delivery_state"] == "Sending":
                _db.execute("""
                  UPDATE notifications SET state='Sent', delivery_state='Sent',
                    delivery_next_at=NULL, delivery_error='', provider_id=?,
                    updated_at=datetime('now','localtime') WHERE id=?
                """, (provider_id, row["id"]))
                _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'message', ?)",
                            (row["job_id"], "%s update sent to %s on %s" % (
                                row["event"], row["to_address"], row["channel"])))
                _db.commit()


def watch_notifications(interval=15):
    """Retry the durable automatic-delivery queue while the shop server is running."""
    def loop():
        while True:
            _notification_wakeup.clear()
            try:
                deliver_pending_notifications()
            except Exception as error:  # Do not stop the server; retain an explicit log.
                sys.stderr.write("Notification delivery worker failed: %s: %s\n" %
                                 (type(error).__name__, error))
            due = one("""
              SELECT min(delivery_next_at) AS due FROM notifications
              WHERE auto_send=1 AND state='Queued'
                AND delivery_state IN ('Pending','Sending') AND delivery_next_at IS NOT NULL
            """)
            delay = max(0, min(interval, due["due"] - time.time())) if due and due["due"] else interval
            _notification_wakeup.wait(delay)
    threading.Thread(target=loop, daemon=True, name="notification-delivery").start()


def retry_notification(note_id):
    with _lock:
        row = one("SELECT auto_send,delivery_state,state FROM notifications WHERE id=?", (note_id,))
        if not row:
            raise LookupError("Message not found")
        if not row["auto_send"] or row["delivery_state"] != "Failed" or row["state"] != "Queued":
            raise ValueError("Only a failed automatic notification can be retried")
        _db.execute("""
          UPDATE notifications SET delivery_state='Pending', delivery_attempts=0,
            delivery_next_at=NULL, delivery_error='', updated_at=datetime('now','localtime')
          WHERE id=?
        """, (note_id,))
        _db.commit()
    _notification_wakeup.set()
    return notify_rows(one("SELECT job_id FROM notifications WHERE id=?", (note_id,))["job_id"])


def set_message_state(note_id, state):
    """The shop's own note of what has actually gone out."""
    if state not in NOTIFY_STATES:
        raise ValueError("A message can only be " + ", ".join(NOTIFY_STATES))
    row = one("SELECT * FROM notifications WHERE id = ?", (note_id,))
    if not row:
        raise LookupError("Message not found")
    automatic = bool(row["auto_send"])
    # An automatic message normally belongs to the delivery worker. While its channel has no
    # provider set up it cannot leave this Mac on its own, so the shop is allowed to record that
    # staff carried the news themselves; the delivery columns move with the answer, so the same
    # words are not sent twice if a provider is configured later.
    if automatic and not notification_config_error(row["channel"]):
        raise ValueError(row["channel"] + " updates go out on their own once the provider is set up")
    if automatic and row["delivery_state"] == "Cancelled":
        # The client asked not to be written to. Recording that a message reached them anyway
        # would be a note about a conversation that did not happen.
        raise ValueError("This update was called off when " + row["channel"] +
                         " permission was withdrawn, so there is nothing to mark")
    with _lock:
        if automatic:
            _db.execute("""UPDATE notifications SET state = ?, delivery_state = ?,
                                  delivery_next_at = ?, delivery_error = '',
                                  updated_at = datetime('now','localtime')
                           WHERE id = ?""",
                        (state, "Sent" if state == "Sent" else "Pending",
                         None if state == "Sent" else time.time() + 3, note_id))
        else:
            _db.execute("UPDATE notifications SET state = ?, "
                        "updated_at = datetime('now','localtime') WHERE id = ?", (state, note_id))
        if state == "Sent" and row["state"] != "Sent":
            client = one("SELECT name FROM clients WHERE id = ?", (row["client_id"],))
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'message', ?)",
                        (row["job_id"], "%s %s to %s on %s (%s)" % (
                            row["event"],
                            "update carried by the shop" if automatic else "message sent",
                            client["name"] if client else "the client",
                            row["channel"], row["to_address"])))
        _db.commit()
    return notify_rows(row["job_id"])


def delete_message(note_id):
    row = one("SELECT * FROM notifications WHERE id = ?", (note_id,))
    if not row:
        raise LookupError("Message not found")
    with _lock:
        _db.execute("DELETE FROM notifications WHERE id = ?", (note_id,))
        _db.commit()
    return notify_rows(row["job_id"])


def notify_payload(job_id):
    """Everything the drawer needs: the queued words, and where this client cannot be reached."""
    job = job_detail(job_id)
    if not job:
        raise LookupError("Job not found")
    rows = notify_rows(job_id)
    reachable = {c: message_target(job, c) for c in NOTIFY_CHANNELS}
    agreed = {c: bool(job.get("client_%s_updates" % c.lower())) for c in NOTIFY_CHANNELS}
    return {
        "messages": rows,
        "client": job["client"],
        "events": NOTIFY_EVENTS,
        "whatsapp_to": reachable["WhatsApp"],
        "email_to": reachable["Email"],
        "missing": [c for c in NOTIFY_CHANNELS if not reachable[c]],
        "to_send": len([r for r in rows
                        if r["state"] == "Queued" and r["delivery_state"] != "Cancelled"]),
        "not_consented": [c for c in NOTIFY_CHANNELS if reachable[c] and not agreed[c]],
        # Queued is not the same as sent. Where a client can be reached and has agreed, say what
        # still stops the message leaving this Mac on its own.
        "blocked": [{"channel": c, "reason": notification_config_error(c)}
                    for c in NOTIFY_CHANNELS
                    if reachable[c] and agreed[c] and notification_config_error(c)],
    }


# -------------------------------------------------------------- money-in notices
#
# When a client pays by MoMo into the shop's number, the network tells the shop in exactly
# one place: the SMS alert, which reaches Messages on this Mac. Reading that text is the only
# way money can enter the book without somebody typing it, and it stays inside the Mac — no
# bank API, no merchant account, no credentials kept here.
#
# The alert is read, matched to a client by number then by name, and booked with the client's
# own name as the reference, so the ledger says who paid without a second lookup. It lands on
# their job that still has a balance, or on their account as credit when nothing is open —
# which is what the book already does for money handed over early.
#
# What is deliberately not done is trusting a read amount blindly. An alert is a claim about
# money and a wrong read is a wrong ledger, so a notice waits for one click of confirmation
# until the shop has watched its own wording read back correctly; `momo_auto` turns that click
# off. Messages is opened read-only, a text that is not money news is dropped rather than
# stored, and the shop's own number is never allowed to match as the payer.

MOMO_NUMBER = "0506399641"      # what clients pay into; excluded when looking for a payer.

# Enough to call a text money news at all. Checked lowercased against the whole alert.
MONEY_WORDS = ("momo", "mobile money", "mtn", "telecel", "at money", "mtn money", "wallet",
               "deposit", "received", "credit", "paid", "payment", "ghs", "gh₵", "₵", "cedi",
               "top up", "trans_id", "reference")
# These mean money went out, which must never be booked as a payment in. "Balance after" is
# deliberately absent: a credit alert prints it too, so it says nothing about direction.
MONEY_OUT = ("withdraw", "you paid", "you have paid", "paid to", "sent to", "you sent",
             "debited", "transfer to", "sent for", "airtime", "payment to", "you transferred")
# A number sitting next to one of these is the amount paid, not a balance or a limit. The
# wording runs both ways: "received 1,250.00" and "1,250.00 received from Ama" are the same alert.
MONEY_CUE = ("received", "credited", "credit of", "credit alert", "deposit", "paid by",
             "sent by", "sent you", "top up", "payment received", "money in", "successfully",
             "you have")
# A number sitting after one of these is not the amount.
MONEY_SKIP = ("balance", "available", "limit", "since last", "total", "fee")

# A number inside "CH-2026-0001" or "Ref: TZ4521" is not an amount, so a candidate may not
# sit against a letter, a dash or another digit on either side.
AMOUNT_RE = re.compile(r"(?<![\dA-Za-z.,\-])(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\dA-Za-z\-])")
PHONE_RE = re.compile(r"[\d][\d\s-]{7,14}[\d]")
PAYER_RE = re.compile(r"\bfrom\s+([A-Za-z][A-Za-z .'\-]{1,30})(?!\w)", re.I)
REF_RE = re.compile(r"\b(?:ref|reference|name|customer|by)\s*[:#\- ]\s*([A-Za-z][A-Za-z .'\-]{2,29})(?!\w)", re.I)
# MTN signs a credit the other way round: "Kofi paid you GHS 450.00", with the name in front.
PAID_YOU_RE = re.compile(r"\b([A-Za-z][A-Za-z '\-]{2,29}?)\s+(?:paid|sent)\s+you\b", re.I)
# A send names whoever received it after "to", which is the only way to tell where it went.
PAYEE_RE = re.compile(r"\bto\s+([A-Za-z][A-Za-z .'\-]{2,29})(?!\w)", re.I)
# Text that is money news but is plainly not a client settling a bill.
MONEY_JUNK = ("won", "prize", "claim", "lottery", "congratul", "winner", "promo", "bonus offer")
# What the words after a payer name actually describe, so the name stops there.
PAYER_STOP = (" for ", " to ", " into ", " as ", " of ", " ref", " momo", " ghs", " payment",
              " order", " on ", " at ", " paid", " with", " using", " trans", " balance",
              " tel", " credited", " new", " excess")
MESSAGES_DB = os.path.expanduser("~/Library/Messages/chat.db")


def state_value(key, default=""):
    row = one("SELECT value FROM app_state WHERE key = ?", (key,))
    return row["value"] if row else default


def set_state(key, value):
    with _lock:
        _db.execute("INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, datetime('now','localtime'))"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at", (key, str(value)))
        _db.commit()


def momo_watching():
    return state_value("momo_watch", "1") == "1"


def momo_auto():
    """Writing money into the book with no human in the middle stays a choice the shop makes."""
    return state_value("momo_auto", "0") == "1"


def clean_name(value):
    name = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;-–—|")
    if not name or len(name) < 2 or name.lower() in ("momo", "mtn", "the", "you", "client"):
        return ""
    return name[:40]


def payer_name(value):
    """Cut the words that follow a payer's name, which describe the payment rather than them."""
    low = " " + re.sub(r"\s+", " ", str(value or "")).lower() + " "
    for word in PAYER_STOP:
        at = low.find(word)
        if at > 0:
            low = low[:at] + " "
    return clean_name(low)


def phones_in(body):
    """Every plausible Ghana mobile number in the text, normalised the way WhatsApp links are."""
    found = []
    for run in PHONE_RE.findall(body):
        number = wa_number(run)
        if len(number) == 12 and number.startswith(COUNTRY_DIAL) and number not in found:
            found.append(number)
    return found


def money_amount(body):
    """The figure that is the payment, not the balance that follows it."""
    low = body.lower()
    best = None
    for m in AMOUNT_RE.finditer(body):
        try:
            value = round(float(m.group(1).replace(",", "")), 2)
        except ValueError:
            continue
        if value <= 0 or value > MONEY_MAX:
            continue
        before = low[max(0, m.start() - 46):m.start()]
        if any(w in before for w in MONEY_SKIP):
            continue
        near = low[max(0, m.start() - 6):m.end() + 6]
        cue_before = any(w in low[max(0, m.start() - 34):m.start()] for w in MONEY_CUE)
        cue_after = any(w in low[m.end():m.end() + 34] for w in MONEY_CUE)
        score = 2 if cue_before or cue_after else 0
        if score == 0 and re.search(r"ghs|gh₵|₵|cedi", near):
            score = 1
        if score and (best is None or score > best[0]):
            best = (score, value, m.group(1))
    return best


def parse_money_alert(raw):
    """What the alert actually says: amount, payer, and which way the money moved."""
    body = re.sub(r"\s+", " ", str(raw or "")).strip()
    low = body.lower()
    read = {"amount": None, "amount_text": "", "payer": "", "payer_phone": "",
            "direction": "Unknown", "money": False, "reason": "Nothing was given to read."}
    if not body:
        return read
    read["money"] = any(w in low for w in MONEY_WORDS)
    if not read["money"]:
        read["reason"] = "This does not read as a money alert, so nothing was taken from it."
        return read

    numbers = [n for n in phones_in(body) if n != wa_number(MOMO_NUMBER)]
    read["payer_phone"] = numbers[0] if numbers else ""
    named = PAYER_RE.search(body) or PAID_YOU_RE.search(body)
    ref = None if named else REF_RE.search(body)
    hit = named or ref
    read["payer"] = payer_name(hit.group(1) if hit else "")

    amount = money_amount(body)
    if amount:
        read["amount"] = amount[1]
        read["amount_text"] = amount[2]

    if any(w in low for w in MONEY_OUT):
        # Money leaving the wallet is a movement the book still has to hold on to: it is either
        # back to a client or out to somebody else, and it names its receiver after "to".
        read["direction"] = "Out"
        owed = PAYEE_RE.search(body)
        if owed and (not read["payer"] or ref):
            # Who the money went to is the only name that matters on a send, and it beats a
            # word lifted out of the reference.
            read["payer"] = payer_name(owed.group(1))
        if read["amount"]:
            read["reason"] = "Read %s going out%s — record it against the client, or as money out." % (
                read["amount_text"], (" to " + read["payer"]) if read["payer"] else "")
        else:
            read["reason"] = "Money left the wallet, but no amount could be read from the alert."
        return read
    if any(w in low for w in MONEY_JUNK):
        # A prize or promo is not money news at all, so it never reaches the notices list.
        read["money"] = False
        read["reason"] = "This reads as a promotion or a prize, not a payment."
        return read
    read["direction"] = "Credit" if any(w in low for w in MONEY_CUE) else "Unknown"

    if not amount:
        read["reason"] = "No amount could be read from it — check the wording or type the figure."
        return read
    if read["direction"] == "Unknown":
        read["reason"] = "Read %s but cannot tell which way the money moved." % amount[2]
        return read
    read["reason"] = ""
    return read


def match_client(phone, payer):
    """A payer is a client when the number matches, or the name does. Never a guess beyond that."""
    rows = q("SELECT id, name, phone, whatsapp FROM clients WHERE archived = 0")
    if phone:
        for c in rows:
            if any(c[f] and wa_number(c[f]) == phone for f in ("phone", "whatsapp")):
                return c
    name = (payer or "").strip().lower()
    if len(name) >= 3:
        words = [w for w in name.split() if len(w) > 2]
        for c in rows:
            cn = c["name"].strip().lower()
            if not cn:
                continue
            if cn == name or cn.startswith(name) or name.startswith(cn):
                return c
            if words and words[0] == cn.split()[0]:
                return c
    return None


def pick_job(client_id, amount):
    """The job this money belongs to: one that settles exactly, else the newest still owing."""
    return one("""
      SELECT id, ref, balance FROM job_accounts
      WHERE client_id = ? AND kind = 'Job' AND status <> 'Cancelled' AND balance > 0.005
      ORDER BY (abs(balance - ?) < 0.005) DESC, abs(balance - ?), created_at DESC
      LIMIT 1
    """, (client_id, amount, amount))


def signal_row(row_id):
    return one("""SELECT s.*, c.name AS client, j.ref AS job_ref,
                         e.category AS expense_category, e.payee AS expense_payee
                  FROM money_signals s
                  LEFT JOIN clients c ON c.id = s.client_id
                  LEFT JOIN jobs j ON j.id = s.job_id
                  LEFT JOIN expenses e ON e.id = s.expense_id
                  WHERE s.id = ?""", (row_id,))


def ingest_alert(raw, source="Pasted", sender="", source_row=0):
    """Read one alert and hold it for booking. Returns the notice, or None if it is not money."""
    body = text(raw, 3000, name="alert text")
    read = parse_money_alert(body)
    if not read["money"]:
        return None
    key = ("msg:%d" % int(source_row)) if source == "Messages" else \
          ("paste:%s" % hashlib.sha1(body.lower().encode("utf-8")).hexdigest()[:16])
    with _lock:
        _db.execute("""INSERT OR IGNORE INTO money_signals
                       (source, source_row, dedupe, sender, raw, amount, payer, payer_phone,
                        direction, reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (source, int(source_row), key, text(sender, 80), body, read["amount"],
                     read["payer"], read["payer_phone"], read["direction"], read["reason"]))
        _db.commit()
    row = one("SELECT * FROM money_signals WHERE dedupe = ?", (key,))
    if not row:
        return None
    if row["payment_id"] or row["state"] != "Unreviewed":
        return signal_row(row["id"])
    client = match_client(row["payer_phone"], row["payer"])
    job = None
    note = row["reason"]
    going_out = row["direction"] == "Out"
    if client and row["amount"]:
        job = pick_job(client["id"], row["amount"])
        if going_out:
            note = "Money out to %s — record it as a refund to %s%s." % (
                client["name"], client["name"],
                (" against " + job["ref"]) if job else ", or as money out")
        else:
            note = "" if job else ("Matches %s, who has nothing outstanding — this would sit as "
                                   "credit on their account." % client["name"])
        with _lock:
            _db.execute("UPDATE money_signals SET client_id = ?, job_id = ?, reason = ? WHERE id = ?",
                        (client["id"], job["id"] if job else None, note, row["id"]))
            _db.commit()
    else:
        note = note or "No client could be matched to this payer."
        with _lock:
            _db.execute("UPDATE money_signals SET reason = ? WHERE id = ?", (note, row["id"]))
            _db.commit()
    # The same payment can reach the book twice — pasted once, texted once, or the network
    # sending two alerts for one deposit. Money this alike within two days waits for an eye.
    twin = None
    if row["amount"] and row["payer_phone"]:
        twin = one("""SELECT id FROM money_signals
                      WHERE state = 'Booked' AND payment_id IS NOT NULL AND id <> ?
                        AND amount = ? AND payer_phone = ?
                        AND booked_at >= datetime('now','localtime','-2 days')""",
                   (row["id"], row["amount"], row["payer_phone"]))
    if twin:
        with _lock:
            _db.execute("UPDATE money_signals SET reason = ? WHERE id = ?",
                        ("The same amount from this number is already booked — make sure this is "
                         "not the same payment twice.", row["id"]))
            _db.commit()
    if momo_auto() and client and row["amount"] and row["direction"] == "Credit" and not twin:
        book_signal(row["id"])
    return signal_row(row["id"])


def book_signal(row_id, payload=None):
    """Turn a notice into a payment, referenced by the client's own name."""
    payload = payload or {}
    row = one("SELECT * FROM money_signals WHERE id = ?", (row_id,))
    if not row:
        raise LookupError("There is no payment notice with that number")
    if row["payment_id"] or row["expense_id"]:
        raise ValueError("This notice is already booked")
    amount = num(payload.get("amount"), row["amount"] or 0, 0, MONEY_MAX)
    if amount >= MONEY_MAX:
        raise ValueError("That figure is too large for the book — check the amount")
    if amount <= 0:
        raise ValueError("No amount was read from this notice — type the figure in first")
    client_id = int(num(payload.get("client_id"), row["client_id"] or 0))
    client = one("SELECT id, name FROM clients WHERE id = ?", (client_id,)) if client_id > 0 else None
    if not client:
        raise ValueError("Say which client this came from before booking it")
    wanted = payload.get("job_id", None)
    if "job_id" in payload:
        # The panel said so deliberately — a number, or nothing at all to hold it as credit.
        job_id = int(wanted) if wanted not in (None, "", "0", 0, "none", "credit") else None
    else:
        # Changing the client in the panel invalidates the job guessed for the old one.
        job_id = row["job_id"] if row["client_id"] == client["id"] else None
        if not job_id:
            found = pick_job(client["id"], amount)
            job_id = found["id"] if found else None
    if job_id:
        owner = one("SELECT client_id FROM jobs WHERE id = ?", (job_id,))
        if not owner:
            raise LookupError("That job is not in the book")
        if owner["client_id"] != client["id"]:
            raise ValueError("That job belongs to another client")
    pay = create_payment({
        "client_id": client["id"], "job_id": job_id, "amount": amount, "kind": "Payment",
        "method": "MoMo", "reference": client["name"],
        "note": "MoMo alert %s%s" % (("#%d" % row["id"]),
                                     (": " + row["raw"][:120]) if row["raw"] else ""),
    })
    with _lock:
        _db.execute("""UPDATE money_signals SET state = 'Booked', client_id = ?, job_id = ?,
                       payment_id = ?, amount = ?, reason = '', booked_at = datetime('now','localtime')
                       WHERE id = ?""", (client["id"], job_id, pay["id"], round(amount, 2), row["id"]))
        _db.commit()
    return pay


def record_send(row_id, payload=None):
    """Record money the wallet sent out, so no MoMo movement leaves the book unread.

    A send that names one of our clients is money back to them, so it goes on their account as
    a refund and lowers what they have paid us. A send that names nobody in the book is the shop
    spending — it goes to the expenses ledger. Never done on its own: an outgoing alert could be
    airtime, rent or a supplier, and only the shop knows which."""
    payload = payload or {}
    row = one("SELECT * FROM money_signals WHERE id = ?", (row_id,))
    if not row:
        raise LookupError("There is no payment notice with that number")
    if row["payment_id"] or row["expense_id"]:
        raise ValueError("This notice is already recorded")
    amount = num(payload.get("amount"), row["amount"] or 0, 0, MONEY_MAX)
    if amount >= MONEY_MAX:
        raise ValueError("That figure is too large for the book — check the amount")
    if amount <= 0:
        raise ValueError("No amount was read from this notice — type the figure in first")
    note = "MoMo send %s%s" % (("#%d" % row["id"]),
                               (": " + row["raw"][:120]) if row["raw"] else "")
    client_id = int(num(payload.get("client_id"), row["client_id"] or 0))
    client = one("SELECT id, name FROM clients WHERE id = ?", (client_id,)) if client_id > 0 else None
    if "job_id" in payload:
        wanted = payload.get("job_id")
        job_id = int(wanted) if wanted not in (None, "", "0", 0, "none", "credit") else None
    else:
        job_id = row["job_id"] if row["client_id"] == client_id else None
    if job_id:
        owner = one("SELECT client_id FROM jobs WHERE id = ?", (job_id,))
        if not owner:
            raise LookupError("That job is not in the book")
        if client and owner["client_id"] != client["id"]:
            raise ValueError("That job belongs to another client")
    if client:
        rec = create_payment({"client_id": client["id"], "job_id": job_id, "amount": amount,
                              "kind": "Refund", "method": "MoMo", "reference": client["name"],
                              "note": note})
        with _lock:
            _db.execute("""UPDATE money_signals SET state = 'Booked', client_id = ?, job_id = ?,
                           payment_id = ?, amount = ?, reason = '', booked_at = datetime('now','localtime')
                           WHERE id = ?""", (client["id"], job_id, rec["id"], round(amount, 2), row_id))
            _db.commit()
        return dict(rec, recorded_as="refund")
    payee = text(payload.get("payee") or row["payer"], 160)
    exp = create_expense({
        "amount": amount, "category": payload.get("category") or "Other",
        "payee": payee or "MoMo send", "method": "MoMo", "reference": payee, "job_id": job_id,
        "note": note, "spent_on": text(payload.get("spent_on"), 10) or today(),
    })
    with _lock:
        _db.execute("""UPDATE money_signals SET state = 'Booked', job_id = ?, expense_id = ?, amount = ?,
                       reason = '', booked_at = datetime('now','localtime') WHERE id = ?""",
                    (job_id, exp["id"], round(amount, 2), row_id))
        _db.commit()
    return dict(exp, recorded_as="expense")


def ignore_signal(row_id):
    row = one("SELECT * FROM money_signals WHERE id = ?", (row_id,))
    if not row:
        raise LookupError("There is no payment notice with that number")
    if row["payment_id"] or row["expense_id"]:
        raise ValueError("Already booked — take it back off in Accounts or Expenses, not here")
    with _lock:
        _db.execute("UPDATE money_signals SET state = 'Ignored', reason = 'Put aside by the shop' WHERE id = ?",
                    (row_id,))
        _db.commit()
    return True


def read_messages(limit=60):
    """The newest texts Messages holds, or the plain reason it cannot be opened."""
    if not os.path.exists(MESSAGES_DB):
        return [], "Messages has no data on this Mac yet."
    try:
        con = sqlite3.connect("file:%s?mode=ro" % MESSAGES_DB, uri=True, timeout=3)
    except sqlite3.Error as exc:
        return [], ("Messages is shut to this app. Allow it under System Settings ▸ Privacy "
                    "& Security ▸ Full Disk Access, then press Check Messages. (%s)"
                    % type(exc).__name__)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(message)")}
        if "text" not in cols:
            return [], "This Messages file keeps text somewhere else — paste the alert instead."
        last = one("SELECT coalesce(max(source_row), 0) m FROM money_signals WHERE source = 'Messages'")["m"]
        mine = " AND is_from_me = 0" if "is_from_me" in cols else ""
        who = "sender" if "sender" in cols else ("handle_id" if "handle_id" in cols else None)
        sel = "ROWID AS rid, text" + (
            ", (SELECT id FROM handle WHERE ROWID = %s) AS from_id" % who if who else "")
        sql = "SELECT %s FROM message WHERE ROWID > ?%s ORDER BY rid DESC LIMIT ?" % (sel, mine)
        cur = con.execute(sql, (last, limit))
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur.fetchall()], ""
    except sqlite3.Error as exc:
        return [], "Messages could not be read this time (%s)." % type(exc).__name__
    finally:
        con.close()


def poll_messages(limit=60):
    """Take in whatever new money news Messages has. Safe to run on a timer or by button."""
    rows, problem = read_messages(limit)
    found = 0
    for row in reversed(rows):
        try:
            if ingest_alert(row.get("text"), source="Messages",
                            sender=str(row.get("from_id") or ""), source_row=row.get("rid") or 0):
                found += 1
        except Exception:  # noqa: BLE001 - one unreadable text must not stop the ledger
            continue
    set_state("momo_status", problem)
    set_state("momo_checked", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return found


def watch_messages(interval=20):
    """A quiet reader on Messages. It stops the moment the shop turns watching off."""
    def loop():
        while True:
            time.sleep(interval)
            try:
                if momo_watching():
                    poll_messages()
            except Exception:  # noqa: BLE001 - the book must keep serving while this spins
                continue
    threading.Thread(target=loop, daemon=True).start()


def paste_alert(raw):
    """The fallback that needs no permission at all: paste the alert, get it read back."""
    body = text(raw, 3000, name="alert text")
    row = ingest_alert(body, source="Pasted")
    if not row:
        raise ValueError("That text carries no money news — nothing was taken from it")
    return row


def momo_payload():
    rows = q("""SELECT s.*, c.name AS client, j.ref AS job_ref,
                      e.category AS expense_category, e.payee AS expense_payee
               FROM money_signals s
               LEFT JOIN clients c ON c.id = s.client_id
               LEFT JOIN jobs j ON j.id = s.job_id
               LEFT JOIN expenses e ON e.id = s.expense_id
               ORDER BY s.id DESC LIMIT 40""")
    waiting = [r for r in rows if r["state"] == "Unreviewed"]
    out = [r for r in rows if (r["direction"] or "") == "Out"]
    booked = [r for r in rows if r["payment_id"] and (r["direction"] or "") != "Out"]
    sent = [r for r in out if r["payment_id"] or r["expense_id"]]
    return {
        "signals": rows,
        "watching": momo_watching(),
        "auto": momo_auto(),
        "status": state_value("momo_status"),
        "checked": state_value("momo_checked"),
        "number": MOMO_NUMBER,
        "waiting": len(waiting),
        "booked": len(booked),
        "booked_total": round(sum(r["amount"] or 0 for r in booked), 2),
        "waiting_total": round(sum(r["amount"] or 0 for r in waiting), 2),
        # Money out is counted apart, so a send never looks like a missing payment.
        "waiting_out": len([r for r in out if r["state"] == "Unreviewed"]),
        "sent": len(sent),
        "sent_total": round(sum(r["amount"] or 0 for r in sent), 2),
        # So the panel can ask "whose money is this?" without another round trip.
        "clients": q("SELECT id, name FROM clients WHERE archived = 0 ORDER BY name"),
    }


# ----------------------------------------------------------------------- expenses

def clean_expense(payload):
    amount = num(payload.get("amount"), 0, 0, MONEY_MAX)
    if amount >= MONEY_MAX:
        raise ValueError("That figure is too large for the book — check the amount")
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
      SELECT e.*, j.ref, j.title AS job_title,
             EXISTS(SELECT 1 FROM spoiled_work s WHERE s.expense_id=e.id) AS is_spoilage
      FROM expenses e
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


def clean_spoilage(payload):
    try:
        job_id = int(payload.get("job_id"))
        quantity = int(payload.get("quantity"))
    except (TypeError, ValueError):
        raise ValueError("Choose a job and enter a whole-number quantity")
    job = one("SELECT id, ref, title, kind FROM jobs WHERE id=?", (job_id,))
    if not job or job["kind"] != "Job":
        raise LookupError("Choose an existing print job")
    if quantity < 1:
        raise ValueError("Quantity must be at least 1")
    quantity = min(quantity, QUANTITY_MAX)
    reason = text(payload.get("reason"), 500, True, "reason")
    amount = money(payload.get("amount"))
    spoiled_on = text(payload.get("spoiled_on"), 10) or today()
    if not DATE_ONLY.fullmatch(spoiled_on):
        raise ValueError("Enter a valid spoilage date")
    try:
        dt.date.fromisoformat(spoiled_on)
    except ValueError:
        raise ValueError("Enter a valid spoilage date")
    return {
        "job_id": job_id,
        "quantity": quantity,
        "reason": reason,
        "amount": round(amount, 2),
        "spoiled_on": spoiled_on,
        "job": job,
    }


def list_spoiled_work():
    return q("""
      SELECT s.id, s.job_id, s.quantity, s.reason, s.spoiled_on,
             e.id AS expense_id, e.amount, j.ref, j.title AS job_title,
             c.name AS client
      FROM spoiled_work s
      JOIN expenses e ON e.id = s.expense_id
      JOIN jobs j ON j.id = s.job_id
      JOIN clients c ON c.id = j.client_id
      ORDER BY s.spoiled_on DESC, s.id DESC
    """)


def create_spoilage(payload):
    data = clean_spoilage(payload)
    with _lock:
        try:
            cur = _db.execute("""
              INSERT INTO expenses (spent_on, category, amount, method, job_id, note)
              VALUES (?, 'Spoilage', ?, 'Other', ?, ?)
            """, (data["spoiled_on"], data["amount"], data["job_id"],
                  "%s spoiled · %s" % (data["quantity"], data["reason"])))
            expense_id = cur.lastrowid
            cur = _db.execute("""
              INSERT INTO spoiled_work (job_id, expense_id, quantity, reason, spoiled_on)
              VALUES (?, ?, ?, ?, ?)
            """, (data["job_id"], expense_id, data["quantity"], data["reason"], data["spoiled_on"]))
            spoilage_id = cur.lastrowid
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                        (data["job_id"], "%s spoiled on %s: %s; cost %s %.2f" % (
                            data["quantity"], data["spoiled_on"], data["reason"],
                            SHOP["currency_symbol"], data["amount"])))
            _db.commit()
        except Exception:
            _db.rollback()
            raise
    return next((row for row in list_spoiled_work() if row["id"] == spoilage_id), None)


def update_spoilage(spoilage_id, payload):
    data = clean_spoilage(payload)
    with _lock:
        record = one("SELECT expense_id, job_id FROM spoiled_work WHERE id=?", (spoilage_id,))
        if not record:
            raise LookupError("Spoilage record not found")
        try:
            _db.execute("""
              UPDATE spoiled_work SET job_id=?, quantity=?, reason=?, spoiled_on=?
              WHERE id=?
            """, (data["job_id"], data["quantity"], data["reason"], data["spoiled_on"], spoilage_id))
            _db.execute("""
              UPDATE expenses SET spent_on=?, category='Spoilage', amount=?, job_id=?,
                note=? WHERE id=?
            """, (data["spoiled_on"], data["amount"], data["job_id"],
                  "%s spoiled · %s" % (data["quantity"], data["reason"]), record["expense_id"]))
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                        (data["job_id"], "%s spoiled on %s: %s; cost %s %.2f" % (
                            data["quantity"], data["spoiled_on"], data["reason"],
                            SHOP["currency_symbol"], data["amount"])))
            _db.commit()
        except Exception:
            _db.rollback()
            raise
    return next((row for row in list_spoiled_work() if row["id"] == spoilage_id), None)


def delete_spoilage(spoilage_id):
    with _lock:
        row = one("SELECT expense_id, job_id FROM spoiled_work WHERE id=?", (spoilage_id,))
        if not row:
            raise LookupError("Spoilage record not found")
        try:
            _db.execute("DELETE FROM spoiled_work WHERE id=?", (spoilage_id,))
            _db.execute("DELETE FROM expenses WHERE id=?", (row["expense_id"],))
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'note', ?)",
                        (row["job_id"], "Spoilage record removed"))
            _db.commit()
        except Exception:
            _db.rollback()
            raise
    return {"ok": True}


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
    if one("SELECT id FROM spoiled_work WHERE expense_id=?", (eid,)):
        raise ValueError("Edit this entry on the Spoiled work screen")
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
    if one("SELECT id FROM spoiled_work WHERE expense_id=?", (eid,)):
        raise ValueError("Remove this entry on the Spoiled work screen")
    with _lock:
        # A send read out of Messages is not deleted with the entry: it goes back to the panel.
        _db.execute("""UPDATE money_signals SET state = 'Unreviewed', expense_id = NULL, booked_at = NULL,
                       reason = 'Taken back off the book — it waits to be recorded again.'
                       WHERE expense_id = ?""", (eid,))
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
    def opted_in(key):
        value = payload.get(key)
        # A record that says nothing is taken as yes: the shop's standing instruction is that
        # clients are told when their job moves. Only an explicit no turns a channel off.
        if value is None or value == "":
            return 1
        return 0 if value is False or str(value).strip().lower() in ("0", "false", "no", "off", "none") else 1

    data = {
        "name": text(payload.get("name"), 160, True, "client name"),
        "phone": text(payload.get("phone"), 40),
        "whatsapp": text(payload.get("whatsapp") or payload.get("phone"), 40),
        "email": text(payload.get("email"), 160),
        "whatsapp_updates": opted_in("whatsapp_updates"),
        "email_updates": opted_in("email_updates"),
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
          INSERT INTO clients (name, phone, whatsapp, email, whatsapp_updates, email_updates,
                               address, kind, notes)
          VALUES (:name, :phone, :whatsapp, :email, :whatsapp_updates, :email_updates,
                  :address, :kind, :notes)
        """, data)
        _db.commit()
        cid = cur.lastrowid
    return client_detail(cid)


def update_client(cid, payload):
    before = one("SELECT whatsapp_updates,email_updates FROM clients WHERE id=?", (cid,))
    if not before:
        raise LookupError("Client not found")
    data = clean_client(payload)
    # A PUT that does not mention a channel leaves the client's own answer alone: consent changes
    # only when the record screen actually says so.
    for key in ("whatsapp_updates", "email_updates"):
        if key not in payload:
            data[key] = before[key]
    with _lock:
        _db.execute("""
          UPDATE clients SET name=:name, phone=:phone, whatsapp=:whatsapp, email=:email,
            whatsapp_updates=:whatsapp_updates, email_updates=:email_updates,
            address=:address, kind=:kind, notes=:notes WHERE id=:id
        """, dict(data, id=cid))
        for channel, field in (("WhatsApp", "whatsapp_updates"), ("Email", "email_updates")):
            if before[field] and not data[field]:
                _db.execute("""
                  UPDATE notifications
                  SET delivery_state='Cancelled', delivery_next_at=NULL,
                      delivery_error='Automatic delivery cancelled: client withdrew consent',
                      updated_at=datetime('now','localtime')
                  WHERE client_id=? AND channel=? AND auto_send=1 AND state='Queued'
                    AND delivery_state IN ('Pending','Failed')
                """, (cid, channel))
        _db.commit()
    for job in q("SELECT id FROM jobs WHERE client_id=? AND kind='Job' "
                 "AND status IN ('Pending','Printing','Ready','Delivered')", (cid,)):
        refresh_pending_status_notification(job["id"])
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
    amount = num(payload.get("amount"), 0, 0, MONEY_MAX)
    if amount >= MONEY_MAX:
        raise ValueError("That figure is too large for the book — check the amount")
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
            # A refund is money handed back, so the job history must not claim it was received.
            _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'payment', ?)",
                        (job_id, ("%s %.2f refunded to the client (%s) - balance now %.2f"
                                  if kind == "Refund" else "%s %.2f received (%s) - balance now %.2f")
                         % (SHOP["currency_symbol"], amount, method,
                            balance["balance"] if balance else 0)))
        _db.commit()
        pid = cur.lastrowid
    if job_id:
        refresh_pending_status_notification(job_id)
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
    # The settled side of the book: billed work whose balance has been cleared, most recent first.
    settled = q("""
      SELECT ja.id, ja.ref, ja.client_id, ja.client, c.phone, ja.title, ja.category, ja.status,
             ja.total, ja.paid, ja.balance, ja.due_date,
             (SELECT max(p.paid_at) FROM payments p WHERE p.job_id = ja.id) AS settled_on
      FROM job_accounts ja JOIN clients c ON c.id = ja.client_id
      WHERE ja.kind='Job' AND ja.total > 0 AND ja.balance <= 0.005 AND ja.status <> 'Cancelled'
        AND c.archived = 0
      ORDER BY settled_on DESC, ja.id DESC LIMIT 80
    """)
    return {"ledger": ledger, "debtors": debtors, "credit": credit, "settled": settled,
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
take a deposit. To accept, reply to %(contact)s.</p>""" % {
            "sym": sym, "total": job["total"], "valid": esc(job["valid_until"] or "not set"),
            "contact": esc(SHOP["phone"] or SHOP["name"])}
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
<header><div><img class=logo src="/img/brand.png" alt="%(shop)s"><div class=muted>%(tagline)s &middot; %(contact)s</div></div>
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
        "contact": " · ".join(x for x in (SHOP["phone"], SHOP["address"]) if x),
        "status": esc(job["status"]),
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
            # The sample book follows the same default the app uses: a client is told about their
            # job unless the shop records that they did not agree.
            _db.execute("""INSERT INTO clients (name, phone, kind, address,
                          whatsapp_updates, email_updates) VALUES (?,?,?,?,1,1)""",
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
    server_version = "ChrisphicsHub/1.0"
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
        self.response_headers = {}
        try:
            with _lock:
                operation_id = text(self.headers.get("X-Operation-Id"), 120)
                protected = path.startswith("/api/") and path not in {
                    "/api/session", "/api/login",
                }
                if protected and not self.authenticated():
                    return self.send(401, {"error": "Sign in is required to access the shop book"})
                if method != "GET" and path.startswith("/api/") and path not in {
                        "/api/login", "/api/logout"} and operation_id:
                    replay = one("SELECT status, response FROM sync_requests WHERE operation_id=?",
                                 (operation_id,))
                    if replay:
                        return self.send(replay["status"], json.loads(replay["response"]))
                if method != "GET" and protected:
                    base_tag = self.headers.get("If-Match")
                    resource = self.sync_resource(path)
                    if base_tag and resource:
                        current = self.dispatch("GET", resource, {})
                        if current is None or isinstance(current[1], tuple) or current[0] != 200:
                            return self.send(409, {"error": "The record is no longer available.",
                                                   "current": None, "etag": None})
                        current_tag = self.etag(current[1])
                        if current_tag != base_tag:
                            return self.send(409, {"error": "This record changed on another device.",
                                                   "current": current[1], "etag": current_tag})
                result = self.dispatch(method, path, params)
            if result is not None:
                code, payload = result
                headers = dict(self.response_headers)
                if method == "GET" and path.startswith("/api/") and code == 200 and not isinstance(payload, tuple):
                    headers["ETag"] = self.etag(payload)
                if method != "GET" and path not in {"/api/login", "/api/logout"} and operation_id and code < 300 and not isinstance(payload, tuple):
                    response = json.dumps(payload, default=str, separators=(",", ":"))
                    with _lock:
                        _db.execute("INSERT OR IGNORE INTO sync_requests(operation_id,status,response) VALUES (?,?,?)",
                                    (operation_id, code, response))
                        _db.commit()
                self.emit(code, payload, path, params, headers)
        except ValueError as err:
            self.send(400, {"error": str(err)})
        except LookupError as err:
            self.send(404, {"error": str(err)})
        except sqlite3.Error as err:
            self.send(500, {"error": "Database error: %s" % err})
        except Exception as err:  # noqa: BLE001 - keep the shop running
            self.send(500, {"error": "%s: %s" % (type(err).__name__, err)})

    @staticmethod
    def etag(payload):
        raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        return '"' + hashlib.sha256(raw).hexdigest() + '"'

    @staticmethod
    def sync_resource(path):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] in {
                "jobs", "clients", "leads", "expenses", "spoiled"}:
            return "/" + "/".join(parts[:3])
        return None

    def session_token(self):
        cookies = self.headers.get("Cookie", "")
        for item in cookies.split(";"):
            key, _, value = item.strip().partition("=")
            if key == "crispprint_session":
                return value
        return ""

    def authenticated(self):
        if not AUTH_PASSWORD:
            return True
        token = self.session_token()
        expires = _sessions.get(token, 0)
        if expires <= time.time():
            _sessions.pop(token, None)
            return False
        return True

    def emit(self, code, payload, path, params, headers=None):
        if isinstance(payload, tuple) and payload[0] == "csv":
            return self.csv_response(payload[1], payload[2])
        if isinstance(payload, tuple) and payload[0] == "raw":
            return self.send(code, payload[1], payload[2], headers)
        if isinstance(payload, tuple) and payload[0] == "file":
            return self.send(200, open(payload[1], "rb").read(), payload[3],
                             dict(headers or {}, **{"Content-Disposition": 'attachment; filename="%s"' % payload[2]}))
        return self.send(code, payload, extra=headers)

    def dispatch(self, method, path, params):
        seg = [s for s in path.split("/") if s]
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True}
        if method == "GET" and path == "/api/session":
            return 200, {"required": bool(AUTH_PASSWORD), "authenticated": self.authenticated()}
        if path == "/api/login" and method == "POST":
            password = self.body().get("password", "")
            if not AUTH_PASSWORD:
                return 200, {"authenticated": True}
            if not isinstance(password, str) or not secrets.compare_digest(password, AUTH_PASSWORD):
                return 401, {"error": "Password is incorrect"}
            token = secrets.token_urlsafe(32)
            _sessions[token] = time.time() + 7 * 24 * 60 * 60
            self.response_headers["Set-Cookie"] = (
                "crispprint_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800%s"
                % (token, "; Secure" if getattr(self.server, "is_tls", False) else "")
            )
            return 200, {"authenticated": True}
        if path == "/api/logout" and method == "POST":
            _sessions.pop(self.session_token(), None)
            self.response_headers["Set-Cookie"] = (
                "crispprint_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0%s"
                % ("; Secure" if getattr(self.server, "is_tls", False) else "")
            )
            return 200, {"authenticated": False}
        if seg and seg[0] == "api" and not self.authenticated():
            return 401, {"error": "Sign in is required to access the shop book"}

        return self.dispatch_api(method, path, params, seg)

    def dispatch_api(self, method, path, params, seg):
        if method == "GET" and (not seg or seg[0] != "api"):
            if path.startswith("/print/"):
                return 200, ("raw", receipt_html(path_id(path.rsplit("/", 1)[1])), "text/html; charset=utf-8")
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
                             "SELECT status, count(*) c FROM jobs WHERE kind='Job' GROUP BY status")},
                         # Client news the shop still owes somebody.
                         "to_send": one("SELECT count(*) c FROM notifications WHERE state='Queued'"
                                        " AND delivery_state <> 'Cancelled'")["c"],
                         # Payment notices read off the network's alerts, waiting to be booked.
                         "to_check": one("SELECT count(*) c FROM money_signals WHERE state = 'Unreviewed'")["c"]}
        if method == "GET" and head == "dashboard":
            return 200, dashboard()
        if head == "clients":
            if len(rest) >= 2:
                cid = path_id(rest[1])
                if len(rest) == 3 and rest[2] == "restore" and method == "POST":
                    archive_client(cid, False)
                    return 200, {"ok": True}
                if len(rest) == 2:
                    if method == "GET":
                        detail = client_detail(cid)
                        return (200, detail) if detail else (404, {"error": "Client not found"})
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
                jid = path_id(rest[1])
                if len(rest) == 3 and method == "GET" and rest[2] == "notifications":
                    return 200, notify_payload(jid)
                if len(rest) == 2:
                    if method == "GET":
                        detail = job_detail(jid)
                        return (200, detail) if detail else (404, {"error": "Job not found"})
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
                    if action == "notify":
                        queue_message(jid, text(self.body().get("event"), 20), automatic=False)
                        return 200, notify_payload(jid)
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
                eid = path_id(rest[1])
                if method == "PUT":
                    return 200, update_expense(eid, self.body())
                if method == "DELETE":
                    return 200, delete_expense(eid)
                if method == "GET":
                    row = expense_detail(eid)
                    return (200, row) if row else (404, {"error": "Expense not found"})
            elif method == "GET":
                return 200, list_expenses(params)
            elif method == "POST":
                return 201, create_expense(self.body())
        if head == "spoiled":
            if len(rest) == 2:
                sid = path_id(rest[1])
                if method == "PUT":
                    return 200, update_spoilage(sid, self.body())
                if method == "DELETE":
                    return 200, delete_spoilage(sid)
                if method == "GET":
                    row = next((item for item in list_spoiled_work() if item["id"] == sid), None)
                    return (200, row) if row else (404, {"error": "Spoilage record not found"})
            elif method == "GET":
                return 200, list_spoiled_work()
            elif method == "POST":
                return 201, create_spoilage(self.body())
        if head == "leads":
            if len(rest) >= 2:
                lid = path_id(rest[1])
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
                        return (200, row) if row else (404, {"error": "Enquiry not found"})
                    if method == "PUT":
                        return 200, update_lead(lid, self.body())
                    if method == "DELETE":
                        return 200, delete_lead(lid)
            elif method == "GET":
                return 200, list_leads(params)
            elif method == "POST":
                return 201, create_lead(self.body())
        if head == "notifications":
            nid = path_id(rest[1]) if len(rest) >= 2 else 0
            if method == "POST" and len(rest) == 3 and rest[2] == "state":
                return 200, {"messages": set_message_state(nid, text(self.body().get("state"), 20))}
            if method == "POST" and len(rest) == 3 and rest[2] == "retry":
                return 200, {"messages": retry_notification(nid)}
            if method == "DELETE" and len(rest) == 2:
                return 200, {"messages": delete_message(nid)}
        if head == "momo":
            if method == "GET" and len(rest) == 1:
                return 200, momo_payload()
            if method == "POST" and len(rest) == 1:
                payload = self.body()
                if payload.get("check"):
                    return 200, dict(momo_payload(), found=poll_messages())
                if "watch" in payload:
                    set_state("momo_watch", "1" if payload.get("watch") else "0")
                    return 200, momo_payload()
                if "auto" in payload:
                    set_state("momo_auto", "1" if payload.get("auto") else "0")
                    return 200, momo_payload()
                notice = paste_alert(payload.get("text"))
                return 201, dict(momo_payload(), notice=notice)
            if method == "POST" and len(rest) == 3:
                sid = path_id(rest[1])
                if rest[2] == "book":
                    # The write happens first: a payload gathered before it would still show the
                    # notice as unbooked, exactly the thing the press was meant to change.
                    pay = book_signal(sid, self.body())
                    return 200, dict(momo_payload(), payment=pay)
                if rest[2] == "send":
                    rec = record_send(sid, self.body())
                    return 200, dict(momo_payload(), recorded=rec)
                if rest[2] == "ignore":
                    ignore_signal(sid)
                    return 200, momo_payload()
        if method == "GET" and head == "payments" and len(rest) == 1:
            return 200, list_payments(params)
        if method == "POST" and head == "payments":
            return 201, create_payment(self.body())
        if head == "payments" and len(rest) == 2 and method == "DELETE":
            row = one("SELECT * FROM payments WHERE id=?", (path_id(rest[1]),))
            if not row:
                raise LookupError("Payment not found")
            with _lock:
                # The alert the money came from is not deleted with the entry: it waits in the
                # panel to be booked again, so an undo never loses the notice itself.
                _db.execute("""UPDATE money_signals SET state = 'Unreviewed', payment_id = NULL,
                               booked_at = NULL,
                               reason = 'Taken back off the book — it waits to be booked again.'
                               WHERE payment_id = ?""", (row["id"],))
                _db.execute("DELETE FROM payments WHERE id=?", (row["id"],))
                if row["job_id"]:
                    _db.execute("INSERT INTO job_events (job_id, type, detail) VALUES (?, 'payment', ?)",
                                (row["job_id"], "Payment of %s %.2f removed" % (SHOP["currency_symbol"], row["amount"])))
                _db.commit()
            if row["job_id"]:
                refresh_pending_status_notification(row["job_id"])
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
    if os.environ.get("CHRISPHICS_WATCH_STDIN") != "1":
        return

    def loop():
        try:
            while sys.stdin.read(1):
                pass
        except Exception:  # noqa: BLE001 - any read failure means the pipe is gone
            pass
        os._exit(0)

    threading.Thread(target=loop, daemon=True).start()


class ShopHTTPServer(ThreadingHTTPServer):
    request_queue_size = 64


def serve(port, open_browser, seed_first, host="127.0.0.1", tls_cert=None, tls_key=None,
          trust_proxy=False, allow_unauthenticated_lan=False):
    global AUTH_PASSWORD
    if seed_first:
        seed()
    if allow_unauthenticated_lan:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as err:
            raise ValueError("--allow-unauthenticated-lan requires a private IPv4 address.") from err
        if (address.version != 4 or not address.is_private or address.is_loopback
                or address.is_link_local):
            raise ValueError("--allow-unauthenticated-lan requires a private Wi-Fi IPv4 address.")
        if trust_proxy:
            raise ValueError("--allow-unauthenticated-lan cannot be combined with a proxy.")
        AUTH_PASSWORD = ""
    if (host not in ("127.0.0.1", "localhost", "::1") and not AUTH_PASSWORD
            and not allow_unauthenticated_lan):
        raise RuntimeError("Set CHRISPHICS_AUTH_PASSWORD before listening beyond this computer.")
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("Both --tls-cert and --tls-key are required to enable HTTPS.")
    if (host not in ("127.0.0.1", "localhost", "::1") and not tls_cert and not trust_proxy
            and not allow_unauthenticated_lan):
        raise RuntimeError("HTTPS is required when listening beyond this computer.")
    if trust_proxy and tls_cert:
        raise ValueError("Use either direct HTTPS or --trust-proxy, not both.")
    watch_parent()
    watch_messages()
    watch_notifications()
    httpd = ShopHTTPServer((host, port), Handler)
    if tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_cert, tls_key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        httpd.is_tls = True
        scheme = "https"
    else:
        httpd.is_tls = bool(trust_proxy)
        scheme = "https" if trust_proxy else "http"
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = "%s://%s:%d/" % (scheme, display_host, port)
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
    ap = argparse.ArgumentParser(description="Chrisphics Hub — printing records and accounts")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CHRISPHICS_PORT", 8712)))
    ap.add_argument("--host", default=os.environ.get("CHRISPHICS_HOST", "127.0.0.1"))
    ap.add_argument("--tls-cert", default=os.environ.get("CHRISPHICS_TLS_CERT"))
    ap.add_argument("--tls-key", default=os.environ.get("CHRISPHICS_TLS_KEY"))
    ap.add_argument("--trust-proxy", action="store_true",
                    help="use only behind a trusted HTTPS-terminating reverse proxy")
    ap.add_argument("--allow-unauthenticated-lan", "--allow-insecure-lan",
                    dest="allow_unauthenticated_lan", action="store_true",
                    help="disable login only when bound to a private Wi-Fi IPv4 address")
    ap.add_argument("--db", default=None, help="alternative SQLite file")
    ap.add_argument("--seed", action="store_true", help="load sample records if the book is empty")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--backup", action="store_true", help="write data/backup-<timestamp>.db and exit")
    args = ap.parse_args()
    connect(args.db)
    if args.backup:
        print("Backup written to %s" % write_backup())
        return
    serve(args.port, not args.no_browser, args.seed, args.host, args.tls_cert, args.tls_key,
          args.trust_proxy, args.allow_unauthenticated_lan)


if __name__ == "__main__":
    main()
