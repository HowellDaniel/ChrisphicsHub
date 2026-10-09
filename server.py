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
import socket
import subprocess
import sqlite3
import ssl
import smtplib
import sys
import tempfile
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
SUPPORT = os.environ.get("CHRISPHICS_SUPPORT") or os.path.expanduser(
    "~/Library/Application Support/Chrisphics Hub")
DB_PATH = os.environ.get("CHRISPHICS_DB") or os.path.join(SUPPORT, "chrisphics.db")
BACKUP_DIR = os.environ.get("CHRISPHICS_BACKUP_DIR") or os.path.join(SUPPORT, "Backups")
# Where the book lived while the shop's name was misspelled. adopt_legacy_book() copies it
# forward on the first run of the corrected build; the old file is never touched.
LEGACY_DB = os.path.expanduser("~/Library/Application Support/Chriphics Hub/chriphics.db")

SHOP = {
    # The shop's trading name, as it appears on a job sheet. The phone starts empty: these
    # details go out to customers, and a number that reaches nobody is worse than no number at
    # all. The shop sets any of them for good on its own Settings ▸ Profile screen, and what the
    # book holds then beats whatever this Mac's environment happens to say.
    "name": os.environ.get("CHRISPHICS_SHOP_NAME", "CRISPprint Ghana"),
    "tagline": os.environ.get("CHRISPHICS_SHOP_TAGLINE", "Printing & Design Services"),
    "phone": os.environ.get("CHRISPHICS_SHOP_PHONE", "").strip(),
    "address": os.environ.get("CHRISPHICS_SHOP_ADDRESS", "Accra, Ghana"),
    "currency": "GHS",
    "currency_symbol": "\u20b5",
}
# What the environment alone says, kept so a detail the shop clears can fall back to it.
DEFAULT_SHOP = dict(SHOP)

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
# The shop's own sign-in. Chosen inside the app and kept in the book as a salted hash, so an
# always-on server needs no secret in a plist and a copied backup gives away nothing.
AUTH_PASSWORD = os.environ.get("CHRISPHICS_AUTH_PASSWORD", "").strip()
SESSION_SECONDS = 30 * 24 * 60 * 60     # what a device gets until the shop shortens it
SESSION_DAYS_MIN, SESSION_DAYS_MAX = 1, 90
# Each stored hash carries the rounds it was made with, so raising this only affects the next
# password the shop chooses; a book signed in with 200,000 keeps opening at 200,000.
PBKDF2_ROUNDS = 600_000
LOGIN_ALLOWED_FAILS = 5
# Beyond the password screen the book still should not be buryable: one address may lean on it
# this often in a minute before it is told to wait. A busy counter never gets near it. A dev check
# lowers it so the waiting path can be proved without three hundred requests.
API_CALLS_PER_MINUTE = 300
try:
    API_CALLS_PER_MINUTE = max(1, int(os.environ.get("CHRISPHICS_CALLS_PER_MINUTE", "300")))
except ValueError:
    pass
_login_tries = {}       # address -> [wrong answers, quiet until]
_api_minute = {}        # address -> [window opened, calls since]
LAN_NO_LOGIN = False    # --allow-unauthenticated-lan
THROUGH_PROXY = False   # --trust-proxy: every visitor arrives as loopback, so loopback proves nothing
SERVE = {"host": "127.0.0.1", "port": 8712, "tls": False}
KEEP_AWAKE = False      # true while caffeinate is holding this Mac open for the shop Wi-Fi
_notification_wakeup = threading.Event()
_backup_wakeup = threading.Event()
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
    apply_book_profile()


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
    job["expenses"] = q("""
      SELECT e.*, EXISTS(SELECT 1 FROM spoiled_work s WHERE s.expense_id = e.id) AS is_spoilage
      FROM expenses e WHERE e.job_id = ? ORDER BY e.spent_on DESC, e.id DESC
    """, (job_id,))
    job["spoilage"] = q("""
      SELECT s.id, s.quantity, s.reason, s.spoiled_on, e.amount, e.category
      FROM spoiled_work s JOIN expenses e ON e.id = s.expense_id
      WHERE s.job_id = ? ORDER BY s.spoiled_on DESC, s.id DESC
    """, (job_id,))
    job["events"] = q("SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC", (job_id,))
    return job


# ---------------------------------------------------------------------------- collect

def ref_key(value):
    """A job number as it is typed at the counter: dashes, spaces and case fall away, so
    CH-2026-0001, ch20260001 and CH 2026 0001 are all the same question."""
    return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()


# The stored ref put through the same two shapes, so the comparison is between like and like.
REF_KEY_SQL = "upper(replace(replace(j.ref, '-', ''), ' ', ''))"
REF_BARE_SQL = "ltrim(%s, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')" % REF_KEY_SQL


def find_ref_job(raw):
    """Which jobs a loosely typed number could point at. Letters in what was typed mean the whole
    number was written down, so the ref must match it in full; a number typed without them is the
    part after the prefix, which is matched against that part of every ref. More than one job
    answering means the book holds two numbers that collapse together, so the counter is asked
    for the whole ref rather than the app picking one."""
    key = ref_key(raw)
    if not key:
        return []
    if key[0].isalpha():
        clause, args = REF_KEY_SQL + " = ?", (key,)
    else:
        clause, args = REF_BARE_SQL + " = ?", (key,)
    return q("""
      SELECT j.id, j.ref, j.title, j.status, c.name AS client
      FROM jobs j JOIN clients c ON c.id = j.client_id
      WHERE j.kind = 'Job' AND %s
      ORDER BY j.created_at DESC, j.id DESC LIMIT 5
    """ % clause, args)


def handover_job(job_id):
    """One job with the few columns the counter needs to check the right person is standing
    there, plus its money. No children, because nothing here writes them."""
    return one("""
      SELECT j.id, j.ref, j.kind, j.title, j.category, j.status, j.priority, j.due_date,
             j.closed_at, j.quantity, j.unit, j.size, j.created_at,
             j.client_id, c.name AS client, c.phone AS client_phone,
             c.whatsapp AS client_whatsapp, c.email AS client_email,
             ja.total, ja.paid, ja.balance
      FROM jobs j JOIN clients c ON c.id = j.client_id JOIN job_accounts ja ON ja.id = j.id
      WHERE j.id = ?
    """, (job_id,))


def handover_money_line(job):
    """The one line of money the card shows, so the counter reads it out while the client is
    still standing there."""
    sym = SHOP["currency_symbol"]
    balance = round(float(job["balance"] or 0), 2)
    if balance < -0.005:
        return "Settled, and %s%.2f in credit with us." % (sym, -balance)
    if balance <= 0.005:
        return "Paid in full — %s%.2f collected." % (sym, float(job["paid"] or 0))
    return "%s%.2f of the %s%.2f billed is still owed." % (sym, balance, sym,
                                                           float(job["total"] or 0))


def handover_card(job):
    """The verdict on one job: can it leave the counter, and if not, why, in the shop's own
    words, with the two doors that would clear it."""
    sym = SHOP["currency_symbol"]
    balance = round(float(job["balance"] or 0), 2)
    settled = balance <= 0.005
    status = job["status"]
    ready = status == "Ready"
    reasons = []
    doors = []
    # A called-off order is not a debt, so its money is never held up at the counter.
    if not settled and status != "Cancelled":
        reasons.append("%s%.2f still owed." % (sym, balance))
        doors.append({"action": "payment", "label": "Take %s%.2f now" % (sym, balance),
                      "amount": balance})
    if not ready:
        if status in ("Pending", "Printing"):
            reasons.append("Still on the press — it is not ready for collection yet.")
            doors.append({"action": "status", "label": "Move it to Ready", "status": "Ready"})
        elif status == "Delivered":
            closed = nice_date(job.get("closed_at"))
            reasons.append("Already handed over" + (" on %s." % closed if closed else "."))
        elif status == "Cancelled":
            reasons.append("This order was cancelled.")
        else:
            reasons.append("The job is at the %s stage, not Ready." % status)
    return {"job": job, "can_handover": bool(ready and settled),
            "reason": " ".join(reasons), "settled": settled, "ready": ready,
            "doors": doors, "balance": balance, "money_line": handover_money_line(job)}


def handover_payload(params):
    """GET /api/handover — read-only, the counter's lookup. A job number in `q` answers that one
    job; with no number it lists everything ready and paid that is still on the shelf."""
    out = {"query": text(params.get("q", [""])[0], 120),
           "waiting": list_jobs({"kind": ["Job"], "status": ["Ready"], "paid": ["full"],
                                 "sort": ["due"]}),
           "found": None, "ambiguous": [], "card": None}
    if not out["query"]:
        return out
    matches = find_ref_job(out["query"])
    if not matches:
        return out
    out["found"] = matches[0]
    if len(matches) > 1:
        out["ambiguous"] = matches
        return out
    out["card"] = handover_card(handover_job(matches[0]["id"]))
    return out


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
    "Pending": "Great news — your order {ref} for {what} is confirmed and scheduled for "
               "production. {due}We appreciate your business and will keep you updated!",
    "Printing": "Great news! Your order {ref} for {what} is officially on the press and "
                "printing now.\n\n{schedule}We will send you another update as soon as it is "
                "finished and ready for pickup!",
    "Ready": "Good news: your order {ref} for {what} is ready for collection at {address}. {balance}"
             "Let us know when you are coming.",
    "Delivered": "Your order {ref} has been delivered. {balance}"
                 "Thank you for your business — we appreciate it.",
    "Cancelled": "Your order {ref} has been cancelled. {balance}"
                 "Call {phone} if you would like us to run it again.",
}

# The short phrase an email subject is built from.
NOTIF_HEADLINE = {
    "Quote": "quote ready", "Booked": "order booked", "Pending": "order confirmed",
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
        "due": ("We are on track to have everything ready for you by %s. " % due) if due else "",
        # The press-side phrasing, kept separate because the shop dictates each stage's words.
        "schedule": ("We are right on schedule to have it ready by %s. " % due) if due else "",
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
        return "Hi %s, %s is here.\n\n%s%s" % (
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


# ---------------------------------------------------------------- the shop's own identity
#
# The trading name, tagline, phone and address go out on every job sheet and every client
# message, so they belong to the book rather than to whoever last set an environment variable.
# A stored value wins over the default; nothing stored yet means the default still speaks.

PROFILE_FIELDS = (("name", "shop_name", 60, True), ("tagline", "shop_tagline", 60, False),
                  ("phone", "shop_phone", 24, False), ("address", "shop_address", 120, False))
PROFILE_KEYS = dict((field, key) for field, key, _limit, _required in PROFILE_FIELDS)
PHONE_CHARS = set("0123456789 +()-")


def apply_book_profile():
    """Lay the book's own shop details over the defaults. Run once at start-up, before any
    sheet, message or screen reads SHOP."""
    for field, key, _limit, _required in PROFILE_FIELDS:
        stored = state_value(key).strip()
        if stored:
            SHOP[field] = stored


def set_shop_profile(body):
    """Take the shop's details from the counter's own screen: check each one, keep it in the
    book, and put it straight into the values every later sheet is written from."""
    if not isinstance(body, dict):
        raise ValueError("Send the shop details as a JSON object")
    cleaned = {}
    for field, key, limit, required in PROFILE_FIELDS:
        if field not in body:
            continue
        value = body.get(field)
        value = "" if value is None else str(value).strip()
        if len(value) > limit:
            raise ValueError("The shop's %s is too long — %d characters at most" % (field, limit))
        if required and not value:
            raise ValueError("The shop needs a name to print on a job sheet")
        if field == "phone" and value and set(value) - PHONE_CHARS:
            raise ValueError("A phone number holds digits, spaces and + ( ) - only")
        cleaned[field] = value
    if not cleaned:
        raise ValueError("Nothing to change — send a name, tagline, phone or address")
    for field, value in cleaned.items():
        key = PROFILE_KEYS[field]
        if value:
            set_state(key, value)
        else:
            with _lock:
                _db.execute("DELETE FROM app_state WHERE key = ?", (key,))
                _db.commit()
        SHOP[field] = value or DEFAULT_SHOP[field]
    return {"shop": dict(SHOP), "saved": sorted(cleaned)}


# ----------------------------------------------------------------------- sign-in

def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    """Salted and deliberately slow: the book keeps this, never the words themselves. Hex, not
    the digest's repr — a raw byte can print as a dollar sign, and the parts are split on those."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds)
    return "pbkdf2_sha256$%d$%s$%s" % (rounds, salt, digest.hex())


def book_password():
    return state_value("shop_password")


def auth_required():
    """True once a password exists — typed into the app on the shop Mac, or handed over in the
    environment by a hosted service such as Render."""
    return bool(AUTH_PASSWORD or book_password())


def shop_mac_locks_itself():
    """Off unless the shop turns it on: loopback normally means the counter's own Mac and is
    never asked for the password. Switched on, this Mac signs in like every other device — which
    is worth doing only while the password is written down somewhere safe nearby."""
    return bool(auth_required() and state_value("shop_mac_login") == "1")


def session_days():
    """How many days a device may go before it is asked for the password again. Kept in the book
    so a shop that hands a phone to casual staff can shorten it without touching a plist."""
    try:
        days = int(state_value("session_days", "") or SESSION_SECONDS // 86400)
    except ValueError:
        return SESSION_SECONDS // 86400
    return max(SESSION_DAYS_MIN, min(SESSION_DAYS_MAX, days))


def session_seconds():
    return session_days() * 24 * 60 * 60


# The words people pick when they are asked for a password and want it over with. Not a
# security promise — a shop password guards the whole ledger, so these are refused outright.
WORN_OUT_PASSWORDS = frozenset("""
password password1 password123 passw0rd p@ssword 123456 1234567 12345678 123456789 1234567890
qwerty qwerty123 asdfgh zxcvb letmein welcome welcome1 welcome123 admin administrator root
iloveyou abc123 abcd1234 a1b2c3d1 111111 000000 007007 123123 654321 sunshine princess warrior
trustno1 dragon monkey football baseball business company shop shop123 chrisphics chrisphicshub
crispprint crispprint1 accra ghana gabz0000 money money123 cash cash123 print printing
""".split())


def weak_password_reason(password):
    """Why this word would let a stranger in. The shop's own name and trading name are refused
    too: they are printed on the job sheet that goes out of the door with every order."""
    if len(password) < 8:
        return "A shop password wants at least 8 characters — long beats clever here"
    low = password.lower()
    if low in WORN_OUT_PASSWORDS:
        return "That word is on every list a guesser starts from — choose one the shop owns alone"
    for filler in (SHOP.get("name", ""), SHOP.get("tagline", "")):
        if len(filler) > 4 and filler.lower() in low:
            return "The shop's own name is printed on the job sheets, so it cannot be the password"
    if len(set(password)) < 5:
        return "That repeats the same few characters; anyone who watches the counter can type it"
    if password.isdigit():
        return "Digits alone get guessed by the thousand — mix in letters"
    classes = sum(1 for group in (lambda c: c.islower(), lambda c: c.isupper(),
                                  lambda c: c.isdigit(), lambda c: not c.isalnum())
                  if any(group(c) for c in password))
    if len(password) < 12 and classes < 3:
        return "Under 12 characters has to mix upper, lower and digits or a symbol — or be longer"
    return ""


def password_matches(candidate):
    if not isinstance(candidate, str):
        return False
    if AUTH_PASSWORD:
        return secrets.compare_digest(candidate, AUTH_PASSWORD)
    record = book_password()
    parts = record.split("$") if record else []
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        rounds = int(parts[1])
    except ValueError:
        return False
    if not 1000 <= rounds <= 5_000_000:
        return False
    return secrets.compare_digest(record, hash_password(candidate, parts[2], rounds))


def quiet_seconds(address):
    """Wrong passwords get answered more slowly, so a neighbour cannot run a list at the shop."""
    _wrong, until = _login_tries.get(address, (0, 0.0))
    return max(0, int(round(until - time.time()))) if until > time.time() else 0


def note_bad_login(address):
    wrong, until = _login_tries.get(address, (0, 0.0))
    wrong += 1
    if wrong >= LOGIN_ALLOWED_FAILS:
        until = time.time() + min(900, 30 * 2 ** (wrong - LOGIN_ALLOWED_FAILS))
    _login_tries[address] = (wrong, until)
    return max(0, int(round(until - time.time()))) if until > time.time() else 0


def throttle_calls(address):
    """Which numbered call this is from that address inside its current minute. A quiet window is
    opened again every minute, and addresses that have gone quiet are dropped, so a busy day on the
    Wi-Fi cannot grow this memory forever."""
    now = time.time()
    opened, calls = _api_minute.get(address, (now, 0))
    if now - opened >= 60:
        opened, calls = now, 0
    calls += 1
    _api_minute[address] = (opened, calls)
    if len(_api_minute) > 400:
        for stale in [a for a, (start, _c) in _api_minute.items() if now - start > 300]:
            _api_minute.pop(stale, None)
    return calls


SECURITY_TRAIL_KEEP = 400
_last_denied = {}       # address -> when its "turned away" line was last written


def note_security(kind, address="", device="", detail=""):
    """Write one line of the book's own security memory. Nothing that was typed goes in here:
    no password, no partial password, no cookie — only what happened and from where.
    A signed-out phone that polls the book every second is one story, not one line a second, so
    the same address is only written again about being turned away after five minutes."""
    if kind == "turned_away":
        now = time.time()
        if now - _last_denied.get(address or "?", 0.0) < 300:
            return
        _last_denied[address or "?"] = now
    try:
        with _lock:
            _db.execute("INSERT INTO security_log (kind, address, device, detail) VALUES (?,?,?,?)",
                        (text(kind, 32), text(address, 60), text(device, 60), text(detail, 200)))
            _db.execute("DELETE FROM security_log WHERE id NOT IN"
                        " (SELECT id FROM security_log ORDER BY id DESC LIMIT ?)",
                        (SECURITY_TRAIL_KEEP,))
            _db.commit()
    except sqlite3.Error:
        # A shop still being able to open its ledger matters more than the note about it.
        pass


def security_trail(limit=40):
    rows = q("SELECT id, at, kind, address, device, detail FROM security_log"
             " ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), SECURITY_TRAIL_KEEP)),))
    return [dict(r) for r in rows]


def security_view(trail_limit=40):
    """What the counter's Security card answers without any of it being typed anywhere."""
    return {"required": auth_required(),
            "from_environment": bool(AUTH_PASSWORD),
            "chosen": bool(book_password()),
            "session_days": session_days(),
            "rounds": PBKDF2_ROUNDS,
            "shop_mac_signin": shop_mac_locks_itself(),
            "loopback_open": not shop_mac_locks_itself(),
            "https": bool(SERVE.get("tls")),
            "reachable_from_wifi": SERVE["host"] not in ("127.0.0.1", "localhost", "::1"),
            "allowed_fails": LOGIN_ALLOWED_FAILS,
            "calls_per_minute": API_CALLS_PER_MINUTE,
            "devices": len(signed_in_devices()),
            "trail": security_trail(trail_limit)}


def set_security_settings(patch, where=""):
    """Only the shop's own Mac moves these, and only inside the bounds the book will honour."""
    if not isinstance(patch, dict):
        raise ValueError("Nothing to change was sent")
    changed = []
    if patch.get("session_days") not in (None, ""):
        try:
            days = int(patch["session_days"])
        except (TypeError, ValueError):
            raise ValueError("Days signed in has to be a whole number")
        if not SESSION_DAYS_MIN <= days <= SESSION_DAYS_MAX:
            raise ValueError("A device can stay signed in for %d to %d days"
                             % (SESSION_DAYS_MIN, SESSION_DAYS_MAX))
        set_state("session_days", days)
        changed.append("session_days")
    if patch.get("shop_mac_signin") is not None:
        want = bool(patch["shop_mac_signin"])
        if want and not book_password():
            raise ValueError("This Mac only gets asked for a password once the shop has chosen"
                             " one — choose it first, then switch this on")
        set_state("shop_mac_login", "1" if want else "0")
        changed.append("shop_mac_signin")
        note_security("shop_mac_signin", where, detail="the shop's own Mac must now sign in" if want
                      else "the shop's own Mac is let through on its own")
    return security_view()


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def device_name(user_agent):
    """A short label for the list of signed-in devices, so a lost phone can be recognised and
    thrown out. It names the machine, never the person."""
    low = (user_agent or "").lower()
    browser = ("Safari" if "safari" in low and "chrome" not in low else
               "Edge" if "edg" in low else
               "Chrome" if "chrome" in low else
               "Firefox" if "firefox" in low else
               "the app" if "chrisphicshub" in low or "crispprint" in low else "a browser")
    if "iphone" in low or "ipod" in low:
        machine = "iPhone"
    elif "ipad" in low:
        machine = "iPad"
    elif "android" in low:
        machine = "Android"
    elif "windows" in low:
        machine = "Windows"
    elif "macintosh" in low or "mac os" in low:
        machine = "Mac"
    elif "crispprint" in low or "chrisphicshub" in low:
        machine = "this Mac"
    else:
        machine = "a device"
    return "%s · %s" % (machine, browser)


def open_session(user_agent):
    token = secrets.token_urlsafe(32)
    with _lock:
        _db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        _db.execute("INSERT INTO sessions (token_hash, device, expires_at) VALUES (?, ?, ?)",
                    (token_digest(token), device_name(user_agent)[:60],
                     time.time() + session_seconds()))
        _db.commit()
    return token


def touch_session(token):
    """A device that keeps being used never has to sign in again; one idle past the shop's chosen
    number of days does."""
    if not token:
        return False
    seconds = session_seconds()
    with _lock:
        row = one("SELECT id, expires_at FROM sessions WHERE token_hash = ?", (token_digest(token),))
        if not row:
            return False
        now = time.time()
        remaining = row["expires_at"] - now
        if remaining <= 0:
            _db.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            _db.commit()
            return False
        if remaining > seconds:
            # The shop asked for a shorter life than this session was originally given, so the
            # next time it is picked up it is pulled inside the new limit.
            _db.execute("UPDATE sessions SET expires_at = ?, last_seen = datetime('now','localtime')"
                        " WHERE id = ?", (now + seconds, row["id"]))
        elif remaining < seconds - 24 * 3600:
            _db.execute("UPDATE sessions SET expires_at = ?, last_seen = datetime('now','localtime')"
                        " WHERE id = ?", (now + seconds, row["id"]))
        else:
            _db.execute("UPDATE sessions SET last_seen = datetime('now','localtime') WHERE id = ?",
                        (row["id"],))
        _db.commit()
    return True


def close_session(token):
    with _lock:
        _db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_digest(token),))
        _db.commit()


def signed_in_devices():
    return [dict(r) for r in q("SELECT id, device, created_at, last_seen FROM sessions"
                               " WHERE expires_at > ? ORDER BY last_seen DESC", (time.time(),))]


def sign_out_every_device(keep_token=""):
    """The one button for a phone that is missing: nobody stays inside the book, and the Mac the
    password was just changed from can keep its own window open."""
    with _lock:
        if keep_token:
            _db.execute("DELETE FROM sessions WHERE token_hash <> ?", (token_digest(keep_token),))
        else:
            _db.execute("DELETE FROM sessions")
        _db.commit()
    return {"devices": len(signed_in_devices()), "signed_out": True}


def revoke_device(device_id):
    """Throw one device out of the book. Its cookie stays on the phone but no longer opens
    anything, so the next time it is picked up it is asked for the password."""
    with _lock:
        cursor = _db.execute("DELETE FROM sessions WHERE id = ?", (int(device_id),))
        _db.commit()
    if not cursor.rowcount:
        raise LookupError("That device is not signed in any more")
    return {"revoked": int(device_id), "devices": len(signed_in_devices())}


def set_shop_password(password, current):
    """Replacing a password means knowing the one before it, and every device signs out when it
    changes — a password lost with a stolen phone has to stay lost."""
    password = "" if not isinstance(password, str) else password
    if len(password) > 200:
        raise ValueError("That password is too long to keep")
    reason = weak_password_reason(password)
    if reason:
        raise ValueError(reason)
    if AUTH_PASSWORD:
        raise ValueError("Sign-in is being set by CHRISPHICS_AUTH_PASSWORD in this server's"
                         " environment. Take it out of the service settings to choose a"
                         " password here instead.")
    if book_password() and not password_matches(current):
        raise ValueError("That is not the password the shop uses now")
    set_state("shop_password", hash_password(password))
    with _lock:
        _db.execute("DELETE FROM sessions")
        _db.commit()
    return {"password_set": True, "devices": 0}


def clear_shop_password(current):
    if AUTH_PASSWORD:
        raise ValueError("This server takes its sign-in from CHRISPHICS_AUTH_PASSWORD;"
                         " clear it in the service settings to switch sign-in off")
    if not book_password():
        return {"password_set": False}
    if not password_matches(current):
        raise ValueError("That is not the password the shop uses now")
    set_state("shop_password", "")
    # Without a word there is nothing for this Mac to be asked for, so the demand is dropped
    # rather than left switched on to bite the next password the shop chooses.
    set_state("shop_mac_login", "0")
    return {"password_set": False}


# ------------------------------------------------------------------- shop Wi-Fi
# One page a device opens before it installs the app: the address, a QR to point a camera at,
# the shop certificate to trust, and the steps for that particular kind of device.

TLS_DIR = os.path.expanduser("~/Library/Application Support/CRISPprint TLS")
CA_DOWNLOAD = os.path.join(TLS_DIR, "device-download", "shop-root-ca.cer")
QR_HELPER = os.path.join(SUPPORT, "qr-encode")


def private_address(host):
    try:
        address = ip_address_of(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def wifi_address():
    """The number this machine holds on the network it is actually using. A UDP socket is opened
    and pointed at an address but never written to — the operating system answers the route lookup
    with the source address it would have used, and no packet leaves the building."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""


def ip_address_of(host):
    """An IP address out of a Host header, port taken off when there is one. A bare IPv6 address
    keeps its colons, so it is only split when exactly one colon separates it from a port."""
    value = (host or "").strip()
    if value.startswith("["):                      # [::1]:8834 — the address is inside the brackets
        value = value[1:].partition("]")[0]
    elif value.count(":") == 1:                    # 192.168.100.29:8834
        value = value.split(":")[0]
    return ipaddress.ip_address(value)


def host_is_address(host):
    """True when a visitor typed an IP rather than a name. A browser honours Strict-Transport-
    Security only for a name, so the shop's Wi-Fi address is told in words instead of being given
    a header that would do nothing at all."""
    try:
        ip_address_of(host)
    except ValueError:
        return False
    return True


def ca_fingerprint():
    """Printed on the install page so a device can check the certificate it is about to trust
    against the one written in the README, rather than taking a stranger's word for it."""
    try:
        with open(CA_DOWNLOAD, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return ":".join("%02X" % b for b in hashlib.sha256(raw).digest())


def shop_url(fallback_host=""):
    scheme = "https" if SERVE["tls"] else "http"
    host, port = SERVE["host"], SERVE["port"]
    if host in ("0.0.0.0", "::", ""):
        # Bound to every address, so this device's own Host header is the only thing that says
        # which of them it reached. Only a shape that cannot carry markup is allowed through
        # (\Z, not $: a trailing newline would still match $).
        named = re.match(r"^([A-Za-z0-9.\-]{1,120})(?::(\d{1,5}))?\Z", fallback_host or "")
        if named:
            host = named.group(1)
            if named.group(2):
                port = int(named.group(2))
    return "%s://%s:%d/" % (scheme, host, port)


def qr_png(words, pixels=560):
    """The address as a QR, drawn by the small helper `tools/shop-server.sh` compiles into the
    support folder. Without it the page still shows the address in type, so nothing is lost."""
    if not (os.path.isfile(QR_HELPER) and os.access(QR_HELPER, os.X_OK)):
        return None
    out = os.path.join(SUPPORT, "qr-%s.png" % hashlib.sha1(
        ("%s|%d" % (words, pixels)).encode("utf-8")).hexdigest()[:16])
    if not os.path.isfile(out):
        try:
            subprocess.run([QR_HELPER, words, out, str(pixels)], timeout=20, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            return None
    try:
        with open(out, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def setup_html(request_host=""):
    url = shop_url(request_host)
    fingerprint = ca_fingerprint()
    secure = SERVE["tls"]

    def esc(value):
        return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    trust = ("" if secure else
             '<p class="warn">This address is plain HTTP. A browser will open the book but will'
             ' not let it be installed as an app or keep it offline. Ask the shop to run'
             ' <code>tools/shop-server.sh certs</code> and <code>install</code> on the shop Mac,'
             ' then use the HTTPS address instead.</p>')
    cert = ("" if not fingerprint else
            '<h2>One certificate to trust first</h2>'
            '<p>Phones and laptops will not open a shop address that they cannot check.'
            ' Download the shop\'s own certificate once per device, then trust it in that'
            ' device\'s settings.</p>'
            '<p><a class="big" href="/shop-root-ca.cer">Download CRISPprint-Shop-Root-CA.cer</a></p>'
            '<p class="fingerprint">It must read<br><code>%s</code></p>' % esc(fingerprint))
    steps = [
        ("iPhone or iPad", "Settings ▸ General ▸ VPN &amp; Device Management ▸ Download the"
         " certificate, then Settings ▸ General ▸ About ▸ Certificate Trust Settings and turn"
         " full trust on for it. Then open the address in Safari and use Share ▸ Add to Home Screen."),
        ("Android", "Open the downloaded certificate in Settings ▸ Security ▸"
         " Encryption &amp; credentials ▸ Install a certificate ▸ CA certificate. Then open the"
         " address in Chrome and use the menu ▸ Install app."),
        ("Windows", "Double-click the certificate ▸ Install Certificate ▸ Local Machine ▸ place"
         " it in Trusted Root Certification Authorities. Then open the address in Edge and use the"
         " install icon in the address bar."),
        ("Another Mac", "Open the certificate and trust it in Keychain Access, then open the"
         " address in Safari. On this Mac the desktop app already holds the same book."),
    ]
    return """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex">
<title>Put %(shop)s on this device</title>
<style>
:root{color-scheme:light dark}
body{font:16px/1.55 -apple-system,"Poppins",Segoe UI,Roboto,sans-serif;margin:0 auto;padding:28px 20px 60px;
max-width:660px;color:#211a1c;background:#faf7f6}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:30px 0 8px}
.sub{color:#6d6165;margin:0 0 22px}
.url{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:#fff;border:1px solid #e2d8d6;
border-radius:12px;padding:14px 16px}
.url a{font-weight:600;color:#a8232c;word-break:break-all;text-decoration:none}
.qr{margin:14px 0 0}.qr img{width:184px;height:184px;image-rendering:pixelated;border-radius:10px;
background:#fff;padding:8px;border:1px solid #e2d8d6}
.big{display:inline-block;background:#c93a3f;color:#fff;padding:11px 16px;border-radius:10px;
text-decoration:none;font-weight:600}
ol{padding-left:22px}li{margin:14px 0}li b{display:block}
code{font:13px ui-monospace,Menlo,Consolas,monospace}
/* Only the fingerprint is one unbroken token that has to be allowed to split mid-run. */
.fingerprint code{word-break:break-all}
.fingerprint{background:#fff;border:1px solid #e2d8d6;border-radius:10px;padding:10px 14px;font-size:13px}
.warn{background:#fdf0ee;border:1px solid #e9b7b3;border-radius:10px;padding:12px 14px}
.note{color:#6d6165;font-size:14px}
@media (prefers-color-scheme:dark){body{color:#eceaea;background:#1c1517}
.url,.fingerprint,.qr img{background:#241d1f;border-color:#3a2f32}.warn{background:#2d1d1e;border-color:#5a2f31}}
</style>
<h1>Put %(shop)s on this device</h1>
<p class=sub>Install the shop's records as an app. Nothing is copied to this device to keep:
the book stays on the shop's own computer, and this is a window onto it.</p>
<div class=url><span>The shop address</span><a href="%(url)s">%(url)s</a></div>
%(qr)s%(trust)s
<h2>Sign in</h2>
<p>The shop keeps one password for its devices. Ask whoever runs the counter — it was chosen on
the shop computer, and every device is signed out whenever it changes.</p>
%(cert)s
<h2>Install it</h2>
<ol>%(steps)s</ol>
<p class=note><a href="%(url)s">Open the shop book now</a> · Setup for the person at the counter is
in the README under “Open and install over HTTPS on the same Wi-Fi”.</p>
</html>""" % {
        "shop": esc(SHOP["name"]), "url": esc(url), "cert": cert, "trust": trust,
        "qr": ('<p class=qr><img src="/setup-qr.png" width="184" height="184" alt="QR code for'
               ' the shop address"><br><span class=note>Camera on this text, or type the address.</span></p>'
               if qr_png(url) else ""),
        "steps": "".join("<li><b>%s</b>%s</li>" % (esc(t), d) for t, d in steps),
    }


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
    # A sheet a client signs must not carry the shop's waste. Spoilage is booked against the job
    # so the shop knows which batch went wrong, but it is the shop's loss: it never appears here,
    # in a job's cost or profit, or in anything sent to the client.
    spend = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td class=r>%s %.2f</td></tr>" % (
            esc(e["spent_on"]), esc(e["category"]), esc(e["payee"] or "-"), sym, e["amount"])
        for e in job["expenses"] if not e["is_spoilage"])

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
    # The Python version this Mac happens to run is not information the shop needs to hand out on
    # every response; a stranger can only use it to look for a known fault in that version.
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % args))

    # helpers
    def drain_body(self):
        """A browser keeps one connection and sends the next request down it, so a body nobody
        read would be taken for the next request line — which is how a refused POST used to come
        back as 501 Unsupported method ('{}GET')."""
        if getattr(self, "_body_taken", False):
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return
        if length > 8 * 1024 * 1024:
            self.close_connection = True   # too much to throw away on a reused connection
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if self.command not in ("GET", "HEAD"):
            self.drain_body()
            self._body_taken = True
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Nothing on this server may be framed, sniffed as another type, or pulled at by a
        # stranger's page. Inline style and script stay allowed because the pages here use them.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "base-uri 'none'; object-src 'none'; frame-ancestors 'self';"
                         " form-action 'self'")
        if getattr(self.server, "is_tls", False) and not host_is_address(self.headers.get("Host", "")):
            # A browser that came here over HTTPS should not be able to be pushed back to plain
            # HTTP afterwards. Browsers ignore this for a bare IP address, so the Wi-Fi Mac where
            # the shop's own number is an address is left out rather than given a header that
            # silently does nothing; a hosted name gets it for six months.
            self.send_header("Strict-Transport-Security", "max-age=15552000")
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
            self._body_taken = True
            return {}
        raw = self.rfile.read(length)
        self._body_taken = True
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("Request body is not valid JSON")
        return parsed if isinstance(parsed, dict) else {}

    def raw_body(self, limit):
        """The bytes of an upload, or nothing honest about why they could not be taken."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            self.close_connection = True   # the rest of the body is never read
            raise ValueError("That file is bigger than this book will ever need")
        if not length:
            self._body_taken = True
            return b""
        raw = self.rfile.read(length)
        self._body_taken = True
        return raw

    def csv_response(self, csv_text, filename):
        self.send(200, csv_text.encode("utf-8-sig"), "text/csv; charset=utf-8",
                  {"Content-Disposition": 'attachment; filename="%s"' % filename})

    def handle_one_request(self):
        # One handler serves every request that arrives down a kept-open connection, so the note
        # about whose body has been read starts again with each one.
        self._body_taken = False
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
        # A page on somebody else's website must not be able to make this book do things with the
        # shop's saved sign-in. Browsers send Origin on every request that changes data, so a
        # missing header is ordinary (curl, the app's own window) and only a wrong one is refused.
        origin = self.headers.get("Origin", "")
        if (origin and method not in ("GET", "HEAD") and path.startswith("/api/")
                and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower()):
            note_security("cross_origin", self.client_ip(),
                          self.headers.get("User-Agent", ""), "%s %s from %s" % (method, path, origin))
            return self.send(403, {"error": "That request did not come from the shop's own page"})
        # Beyond the password screen the book is still one file on one Mac, and a script that hammers
        # it would stop the counter's own screen from opening a job.
        calls = throttle_calls(self.client_ip())
        if calls > API_CALLS_PER_MINUTE:
            if calls == API_CALLS_PER_MINUTE + 1:
                note_security("throttled", self.client_ip(), self.headers.get("User-Agent", ""),
                              "more than %d calls in a minute" % API_CALLS_PER_MINUTE)
            return self.send(429, {"error": "Too many requests from here in one minute."
                                            " Wait a few seconds."},
                             extra={"Retry-After": "5"})
        try:
            with _lock:
                operation_id = text(self.headers.get("X-Operation-Id"), 120)
                protected = path.startswith("/api/") and path not in {
                    "/api/session", "/api/login", "/api/logout",
                }
                if protected and not self.authenticated():
                    note_security("turned_away", self.client_ip(), self.headers.get("User-Agent", ""),
                                  "asked for %s with no sign-in" % path[:120])
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

    def client_ip(self):
        if THROUGH_PROXY:
            forwarded = self.headers.get("X-Forwarded-For", "")
            first = forwarded.split(",")[0].strip()
            if first:
                return first[:60]
        return self.client_address[0] if self.client_address else "?"

    def is_shop_computer(self):
        """Loopback means this Mac — the counter's own screen, which the shop already controls.
        Behind a proxy every visitor arrives from the proxy's address, so loopback proves nothing."""
        if THROUGH_PROXY:
            return False
        ip = self.client_ip()
        return ip.startswith("127.") or ip == "::1"

    def authenticated(self):
        if not auth_required() or LAN_NO_LOGIN:
            return True
        if self.is_shop_computer() and not shop_mac_locks_itself():
            return True
        return touch_session(self.session_token())

    def cookie(self, token, max_age):
        secure = "; Secure" if getattr(self.server, "is_tls", False) else ""
        self.response_headers["Set-Cookie"] = (
            "crispprint_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s"
            % (token, max_age, secure))

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
            signed_in = self.authenticated()
            return 200, {"required": auth_required(), "authenticated": signed_in,
                         "from_environment": bool(AUTH_PASSWORD),
                         "this_is_the_shop_computer": self.is_shop_computer()}
        if path == "/api/login" and method == "POST":
            if not auth_required():
                return 200, {"authenticated": True}
            wait = quiet_seconds(self.client_ip())
            if wait:
                self.response_headers["Retry-After"] = str(wait)
                note_security("locked_out", self.client_ip(), self.headers.get("User-Agent", ""),
                              "%d seconds still to wait" % wait)
                return 429, {"error": "Too many wrong passwords from here. Wait %d seconds." % wait}
            if not password_matches(self.body().get("password", "")):
                wait = note_bad_login(self.client_ip())
                wrong = _login_tries.get(self.client_ip(), (0, 0))[0]
                note_security("wrong_password" if not wait else "locked_out", self.client_ip(),
                              self.headers.get("User-Agent", ""),
                              "%d wrong from here" % wrong)
                if wait:
                    self.response_headers["Retry-After"] = str(wait)
                    return 429, {"error": "Password is incorrect. Wait %d seconds before"
                                          " trying again." % wait}
                return 401, {"error": "Password is incorrect"}
            _login_tries.pop(self.client_ip(), None)
            seconds = session_seconds()
            self.cookie(open_session(self.headers.get("User-Agent", "")), seconds)
            note_security("signed_in", self.client_ip(), self.headers.get("User-Agent", ""),
                          "signed in for %d day%s" % (session_days(), "" if session_days() == 1 else "s"))
            return 200, {"authenticated": True}
        if path == "/api/logout" and method == "POST":
            token = self.session_token()
            if token:
                close_session(token)
                note_security("signed_out", self.client_ip(), self.headers.get("User-Agent", ""))
            self.cookie("", 0)
            return 200, {"authenticated": False}

        return self.dispatch_api(method, path, params, seg)

    def dispatch_api(self, method, path, params, seg):
        if method == "GET" and (not seg or seg[0] != "api"):
            if path.startswith("/print/"):
                return 200, ("raw", receipt_html(path_id(path.rsplit("/", 1)[1])), "text/html; charset=utf-8")
            if path == "/setup":
                return 200, ("raw", setup_html(self.headers.get("Host", "")), "text/html; charset=utf-8")
            if path == "/setup-qr.png":
                png = qr_png(shop_url(self.headers.get("Host", "")))
                if not png:
                    self.send(404, "No QR helper on this Mac", "text/plain")
                    return None
                return 200, ("raw", png, "image/png")
            if path == "/shop-root-ca.cer":
                # The shop's root certificate, handed to a device standing on the same Wi-Fi. A
                # hosted deployment has no business distributing a Mac's trust root.
                if not private_address(self.client_ip()):
                    self.send(404, "The shop certificate is only handed out on the shop Wi-Fi",
                              "text/plain")
                    return None
                if not os.path.isfile(CA_DOWNLOAD):
                    self.send(404, "No certificate has been made for this shop", "text/plain")
                    return None
                return 200, ("file", CA_DOWNLOAD, "CRISPprint-Shop-Root-CA.cer",
                             "application/x-x509-ca-cert")
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
                         "to_check": one("SELECT count(*) c FROM money_signals WHERE state = 'Unreviewed'")["c"],
                         # What the shop screen and the sidebar need to know about the sign-in,
                         # the address devices install from, and the book's own copies.
                         "login": {"required": auth_required(), "from_environment": bool(AUTH_PASSWORD),
                                   "chosen": bool(book_password())},
                         "address": shop_url(self.headers.get("Host", "")),
                         "secure": bool(getattr(self.server, "is_tls", False)),
                         "backup": backup_report()}
        if method == "GET" and head == "dashboard":
            return 200, dashboard()
        if method == "GET" and head == "shop":
            return 200, {"shop": SHOP,
                         "address": shop_url(self.headers.get("Host", "")),
                         "setup": "/setup",
                         "momo_number": MOMO_NUMBER,
                         "kept_in_book": {field: bool(state_value(key))
                                          for field, key, _limit, _req in PROFILE_FIELDS},
                         "secure": bool(getattr(self.server, "is_tls", False)),
                         "reachable_from_wifi": SERVE["host"] not in ("127.0.0.1", "localhost", "::1"),
                         "awake": KEEP_AWAKE,
                         "qr": os.path.isfile(QR_HELPER) and os.access(QR_HELPER, os.X_OK),
                         "certificate": bool(ca_fingerprint()),
                         "login": {"required": auth_required(),
                                   "from_environment": bool(AUTH_PASSWORD),
                                   "chosen_on_this_mac": bool(book_password())},
                         "backup": backup_report()}
        if method == "PUT" and head == "shop":
            # The shop's own details are the counter's business: the name and number printed on
            # every sheet go out to customers, so only this Mac may change them.
            if not self.is_shop_computer():
                return 403, {"error": "The shop's own details are changed on the shop's computer"}
            return 200, set_shop_profile(self.body())
        if head == "shop-password":
            if method == "POST":
                if not self.is_shop_computer() and not book_password():
                    return 403, {"error": "The first shop password has to be chosen on the shop's"
                                          " own computer, not over the Wi-Fi"}
                body = self.body()
                try:
                    answer = set_shop_password(body.get("password"), body.get("current"))
                except ValueError as err:
                    if "not the password the shop uses now" in str(err):
                        note_security("wrong_current", self.client_ip(),
                                      self.headers.get("User-Agent", ""),
                                      "tried to change the password without the one before it")
                    raise
                note_security("password_set", self.client_ip(), self.headers.get("User-Agent", ""),
                              "every device was signed out")
                return 200, answer
            if method == "DELETE":
                if not self.is_shop_computer():
                    return 403, {"error": "Sign-in is switched off on the shop's own computer,"
                                          " so that this device cannot leave the book open"}
                try:
                    answer = clear_shop_password(self.body().get("current"))
                except ValueError as err:
                    if "not the password the shop uses now" in str(err):
                        note_security("wrong_current", self.client_ip(),
                                      self.headers.get("User-Agent", ""),
                                      "tried to switch sign-in off without the password")
                    raise
                note_security("sign_in_off", self.client_ip(), self.headers.get("User-Agent", ""),
                              "the book is open to anything that can reach this address")
                return 200, answer
            return 405, {"error": "Use POST to set the shop password, DELETE to switch it off"}
        if head == "devices":
            if method == "GET":
                return 200, {"devices": signed_in_devices(), "session_days": session_days()}
            if method == "DELETE" and len(rest) == 2:
                answer = revoke_device(path_id(rest[1]))
                note_security("device_revoked", self.client_ip(),
                              self.headers.get("User-Agent", ""),
                              "device %s signed out, %d left" % (answer["revoked"], answer["devices"]))
                return 200, answer
            if method == "DELETE" and len(rest) == 1:
                # The one button for a phone that is missing: nobody stays inside the book. This
                # device keeps its own window, otherwise the shop would lock itself out mid-call.
                answer = sign_out_every_device(self.session_token())
                note_security("signed_out_all", self.client_ip(),
                              self.headers.get("User-Agent", ""),
                              "%d other device(s) thrown out" % answer["devices"])
                return 200, answer
            return 405, {"error": "Use DELETE /api/devices/<number> to sign one device out,"
                                  " or /api/devices to sign every other one out"}
        if head == "backups":
            if method == "GET":
                return 200, backup_report()
            if method == "PUT":
                if not self.is_shop_computer():
                    return 403, {"error": "How often the book is copied, and to where, is settled"
                                          " on the shop's own computer"}
                return 200, set_backup_settings(self.body(), self.client_ip())
            if method == "POST":
                # A copy asked for right now, checked and tidied the same way the automatic ones
                # are. Nothing is handed back to download here — that is /api/backup.
                path, counts, tidy = run_backup("manual")
                note_security("book_copied", self.client_ip(), self.headers.get("User-Agent", ""),
                              "%s asked for a copy, checked against %d jobs"
                              % ("the shop's Mac" if self.is_shop_computer() else "a device",
                                 counts["jobs"]))
                return 200, backup_report()
            return 405, {"error": "Use GET for the copies, PUT to settle how often they happen,"
                                  " POST to make one now"}
        if head == "security":
            if method == "GET":
                return 200, security_view()
            if method == "PUT":
                if not self.is_shop_computer():
                    return 403, {"error": "How long a device stays signed in, and whether this Mac"
                                          " is asked too, is settled on the shop's own computer"}
                return 200, set_security_settings(self.body(), self.client_ip())
            return 405, {"error": "Use GET to read the security, PUT to change it"}
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
        if method == "GET" and head == "handover":
            return 200, handover_payload(params)
        if method == "GET" and head == "accounts":
            return 200, accounts_view(params)
        if method == "GET" and head == "report":
            return 200, report(params)
        if method == "GET" and head == "export" and len(rest) == 2:
            name = rest[1].replace(".csv", "")
            rows, filename, _count = export_rows(name, params)
            return 200, ("csv", rows, filename)
        if method == "GET" and head == "backup":
            tmp = write_backup("manual")
            # Whole ledgers leave through this door, on purpose. Which device took one, and from
            # where, is the kind of thing the shop is entitled to look up afterwards.
            note_security("book_taken", self.client_ip(), self.headers.get("User-Agent", ""),
                          "a copy of every record was downloaded")
            return 200, ("file", tmp, os.path.basename(tmp), "application/x-sqlite3")
        if head == "import-book":
            if method != "POST":
                return 405, {"error": "Use POST with the book copy itself as the body"}
            if not book_is_empty():
                return 409, {"error": "This book already has records of its own — a copy is only"
                                       " ever carried into an empty one"}
            raw = self.raw_body(IMPORT_MAX_BYTES)
            if len(raw) < 4096 or raw[:15] != b"SQLite format 3":
                return 400, {"error": "What arrived is not a copy of the book"}
            handle, temp = tempfile.mkstemp(prefix="chrisphics-carry-", suffix=".db")
            try:
                with os.fdopen(handle, "wb") as fh:
                    fh.write(raw)
                return 200, carry_book_over(temp)
            finally:
                try:
                    os.remove(temp)
                except OSError:
                    pass
        raise LookupError("Unknown route " + method + " " + path)


def write_backup(kind="backup"):
    """Snapshot the book into BACKUP_DIR and return the new file's path. `manual` marks a copy
    the shop asked for, which retention keeps for longer than the nightly ones."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # The name only carries to the second, and a copy that quietly overwrote an earlier one is a
    # copy lost — so if this second is already taken, wait for the next name that is free.
    for attempt in range(60):
        path = os.path.join(BACKUP_DIR,
                            "%s-%s.db" % (kind, dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
        if not os.path.exists(path):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("The backups folder already holds a copy from this second, again and again")
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


# A copy that was never opened again is only a copy of a guess, and a folder of copies nobody
# prunes ends full. So every run checks its own work and tidies behind itself, and the server that
# is running makes the copies itself instead of trusting one calendar moment: a Mac that was asleep
# at 22:30 still has two copies of its ledger by the next morning.
BACKUP_KEEP_DAYS = 14
BACKUP_KEEP_MONTHS = 12
BACKUP_KEEP_MANUAL = 12
BACKUP_KEEP_PER_DAY = 12
BACKUP_HOURS_DEFAULT = 24
BACKUP_HOURS_MIN, BACKUP_HOURS_MAX = 1, 168
BACKUP_DAYS_MIN, BACKUP_DAYS_MAX = 3, 120
STAMPED_BACKUP = re.compile(r"^(backup|manual)-(\d{8})-(\d{6})\.db$")


def backup_time(name):
    match = STAMPED_BACKUP.match(name)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(2) + match.group(3), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _state_number(key, default):
    try:
        return int(state_value(key, "") or default)
    except ValueError:
        return int(default)


def backup_auto():
    """Off only when the shop switches it off; a book with no copies is one failed disk away
    from being nothing."""
    return state_value("backup_auto", "1") != "0"


def backup_hours():
    return max(BACKUP_HOURS_MIN, min(BACKUP_HOURS_MAX, _state_number("backup_every_hours",
                                                                      BACKUP_HOURS_DEFAULT)))


def backup_kept_days():
    return max(BACKUP_DAYS_MIN, min(BACKUP_DAYS_MAX, _state_number("backup_keep_days",
                                                                   BACKUP_KEEP_DAYS)))


def backup_mirror():
    """The shop's chosen second destination — an external disk or a folder its own sync tool
    watches. Empty means the book is kept in one place only, and says so."""
    return state_value("backup_mirror")


def check_backup(path):
    """Read the new copy back: is it a database, and does it hold the same records as the book
    it came from? Returns a short note, or raises when the copy is not trustworthy."""
    check = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            raise RuntimeError("The copy of the book does not pass its own check: %s" % verdict)
        counts = {}
        for table in ("jobs", "clients", "payments", "expenses", "notifications"):
            mine = q("SELECT count(*) c FROM %s" % table)[0]["c"]
            theirs = check.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            if theirs != mine:
                raise RuntimeError("The copy holds %d %s but the book holds %d"
                                  % (theirs, table, mine))
            counts[table] = theirs
        return counts
    finally:
        check.close()


def tend_backup_folder(now=None, folder=None, keep_days=None):
    """Keep one night's copies for as long as the shop asked, one copy for each month before that
    for a year, the last dozen copies asked for by hand, and no more than a dozen from the same
    day — otherwise a short cadence fills the disk with near-identical files."""
    now = now or dt.datetime.now()
    folder = folder or BACKUP_DIR
    keep_days = backup_kept_days() if keep_days is None else keep_days
    try:
        names = [n for n in os.listdir(folder) if STAMPED_BACKUP.match(n)]
    except OSError:
        return {"kept": 0, "gone": 0}
    stamped = sorted(((n, backup_time(n)) for n in names), key=lambda pair: pair[1], reverse=True)
    keep, months, manual, per_day = set(), {}, 0, {}
    for name, when in stamped:
        if name.startswith("manual"):
            manual += 1
            if manual <= BACKUP_KEEP_MANUAL:
                keep.add(name)
            continue
        day = when.strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + 1
        if per_day[day] > BACKUP_KEEP_PER_DAY:
            continue
        if (now - when).days <= keep_days:
            keep.add(name)
            continue
        key = when.strftime("%Y-%m")
        if key not in months and len(months) < BACKUP_KEEP_MONTHS:
            months[key] = name
            keep.add(name)
    gone = 0
    for name, _when in stamped:
        if name in keep:
            continue
        try:
            os.remove(os.path.join(folder, name))
            gone += 1
        except OSError:
            pass
    return {"kept": len(keep), "gone": gone}


def mirror_copy(source):
    """Best-effort second home for the newest copy. Returns None when the shop has chosen no
    second folder, (False, why) when it could not be written, (True, where) when it was. A mirror
    that is unplugged must never look like a night the book was not copied."""
    target = backup_mirror()
    if not target:
        return None
    if not os.path.isdir(target):
        return False, "the second folder is not there — is that disk plugged in?"
    dest = os.path.join(target, os.path.basename(source))
    try:
        with open(source, "rb") as fh:
            data = fh.read()
        with open(dest, "wb") as fh:
            fh.write(data)
    except OSError as err:
        return False, "could not be written there (%s)" % type(err).__name__
    tend_backup_folder(folder=target)
    return True, dest


def short_path(path):
    return (path or "").replace(os.path.expanduser("~"), "~")


def run_backup(kind="backup"):
    """One run, end to end, with the result written into the book so any screen can say when the
    records were last copied, what the copy was checked against, and where it went."""
    path = write_backup(kind)
    counts = check_backup(path)
    tidy = tend_backup_folder()
    mirrored = mirror_copy(path)
    note = "checked, %d jobs · kept %d, cleared %d" % (counts["jobs"], tidy["kept"], tidy["gone"])
    if mirrored is not None:
        note += " · mirror " + ("done" if mirrored[0] else "missing: %s" % mirrored[1])
    set_state("last_backup_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    set_state("last_backup_file", os.path.basename(path))
    set_state("last_backup_note", note)
    set_state("last_backup_error", "")
    set_state("last_backup_kind", kind)
    return path, counts, tidy


def backup_due():
    """(is a copy overdue, hours until the next one). A book that has never been copied is overdue
    the moment the server starts."""
    hours = backup_hours()
    at = state_value("last_backup_at")
    if not at:
        return True, 0.0
    try:
        age = (dt.datetime.now() - dt.datetime.strptime(at, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except ValueError:
        return True, 0.0
    return age >= hours, max(0.0, hours - age)


def watch_backups(check=None):
    """The server keeps the book copied on its own clock while it runs, so the nightly calendar
    moment is a second way a copy happens rather than the only one. The clock is looked at every
    five minutes; a test or a small hosted box may want it looked at more often."""
    if check is None:
        try:
            check = max(5, int(os.environ.get("CHRISPHICS_BACKUP_CHECK", "300")))
        except ValueError:
            check = 300

    def loop():
        while True:
            _backup_wakeup.wait(check)
            _backup_wakeup.clear()      # a new schedule is looked at the moment it is settled
            if not backup_auto():
                continue
            try:
                if backup_due()[0]:
                    run_backup()
            except Exception as error:  # The shop must keep trading even if copying cannot.
                sys.stderr.write("Automatic copy of the book failed: %s: %s\n"
                                 % (type(error).__name__, error))
                set_state("last_backup_error", "%s: %s" % (type(error).__name__, str(error)[:180]))
                set_state("last_backup_error_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                note_security("backup_failed", detail=str(error)[:200])
    threading.Thread(target=loop, daemon=True, name="book-backup").start()


def recent_backups(limit=6, folder=None):
    folder = folder or BACKUP_DIR
    try:
        names = [n for n in os.listdir(folder) if STAMPED_BACKUP.match(n)]
    except OSError:
        return []
    rows = []
    # The name only carries whole seconds. A copy the shop asked for and one the clock made can land
    # inside the same second, so the moment the file was actually written settles which is newest.
    stamped = []
    for name in names:
        when = backup_time(name)
        if when is None:
            continue
        try:
            touched = os.path.getmtime(os.path.join(folder, name))
        except OSError:
            touched = 0.0
        stamped.append((when, touched, name))
    for when, _touched, name in sorted(stamped, key=lambda row: (row[0], row[1]),
                                       reverse=True)[:limit]:
        try:
            size = os.path.getsize(os.path.join(folder, name))
        except OSError:
            size = 0
        rows.append({"name": name, "kind": name.split("-")[0],
                     "at": when.strftime("%Y-%m-%d %H:%M:%S"), "bytes": size})
    return rows


def backup_report():
    """What the shop screen and the Settings window say about the book's copies."""
    at = state_value("last_backup_at")
    age = None
    if at:
        try:
            age = (dt.datetime.now() - dt.datetime.strptime(at, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
        except ValueError:
            age = None
    try:
        copies = len([n for n in os.listdir(BACKUP_DIR) if n.endswith(".db")])
    except OSError:
        copies = 0
    auto = backup_auto()
    overdue, next_in = backup_due()
    mirror = backup_mirror()
    try:
        free = os.statvfs(BACKUP_DIR)
    except OSError:
        # Before the first copy the folder does not exist yet; the drive it will be made on is
        # the same drive, so the shop still gets a real number rather than a blank.
        free = os.statvfs(os.path.dirname(BACKUP_DIR) or ".")
    free_gb = round(free.f_bavail * free.f_frsize / 1024 ** 3, 1)
    return {"at": at, "file": state_value("last_backup_file"), "note": state_value("last_backup_note"),
            "age_hours": round(age, 1) if age is not None else None,
            "stale": age is None or age > max(26, backup_hours() + 2), "copies": copies,
            "folder": short_path(BACKUP_DIR),
            "auto": auto, "every_hours": backup_hours(), "keep_days": backup_kept_days(),
            "due": bool(overdue and auto), "next_in_hours": round(next_in, 1) if auto else None,
            "mirror": short_path(mirror), "mirror_set": bool(mirror),
            "mirror_dir": bool(mirror) and os.path.isdir(mirror),
            "error": state_value("last_backup_error"),
            "error_at": state_value("last_backup_error_at"),
            "free_gb": free_gb, "recent": recent_backups()}


def set_backup_settings(patch, where=""):
    """The shop's own Mac decides how often the book is copied, how long the copies are kept and
    whether a second one goes somewhere else. Only the schedule and the folders are reachable."""
    if not isinstance(patch, dict):
        raise ValueError("Nothing to change was sent")
    changed = []
    if patch.get("auto") is not None:
        want = bool(patch["auto"])
        set_state("backup_auto", "1" if want else "0")
        changed.append("auto")
    if patch.get("every_hours") not in (None, ""):
        try:
            hours = int(patch["every_hours"])
        except (TypeError, ValueError):
            raise ValueError("How often has to be a whole number of hours")
        if not BACKUP_HOURS_MIN <= hours <= BACKUP_HOURS_MAX:
            raise ValueError("The book can be copied every %d to %d hours"
                             % (BACKUP_HOURS_MIN, BACKUP_HOURS_MAX))
        set_state("backup_every_hours", hours)
        changed.append("every_hours")
    if patch.get("keep_days") not in (None, ""):
        try:
            days = int(patch["keep_days"])
        except (TypeError, ValueError):
            raise ValueError("How many nights has to be a whole number")
        if not BACKUP_DAYS_MIN <= days <= BACKUP_DAYS_MAX:
            raise ValueError("Copies can be kept for %d to %d nights"
                             % (BACKUP_DAYS_MIN, BACKUP_DAYS_MAX))
        set_state("backup_keep_days", days)
        changed.append("keep_days")
    if "mirror" in patch:
        raw = text(patch.get("mirror"), 300).strip()
        if raw:
            target = os.path.abspath(os.path.expanduser(raw))
            if target == os.path.abspath(BACKUP_DIR) or \
                    os.path.realpath(target) == os.path.realpath(BACKUP_DIR):
                raise ValueError("That is the folder the copies already go to; choose a second,"
                                 " different place")
            if not os.path.isdir(target):
                raise ValueError("That second folder is not there yet — make it, or plug in the"
                                 " disk, then save again")
            set_state("backup_mirror", target)
        else:
            set_state("backup_mirror", "")
        changed.append("mirror")
    note_security("backup_settings", where,
                  detail="changed: " + ", ".join(changed) if changed else "nothing changed")
    if changed:
        # The watcher is asleep until the cadence it last read comes round. A shop that just said
        # "every hour" should not wait out an old night-long gap before the first copy lands.
        _backup_wakeup.set()
    answer = backup_report()
    answer["saved"] = changed
    if changed:
        # The watcher is asleep until the cadence it last read comes round. A shop that just said
        # "every hour" should not wait out an old night-long gap before the first copy lands. The
        # answer above is built first, so the shop is told the schedule it settled, not a copy that
        # landed a moment later.
        _backup_wakeup.set()
    return answer


# ------------------------------------------------------------------ carrying the book over
# A hosted deployment starts as an empty book, which is no use to a shop with years of records.
# So the shop hands its own copy to the empty server, once, and that copy becomes the book.

RECORD_TABLES = ("clients", "jobs", "job_items", "expenses", "spoiled_work", "leads",
                 "payments", "job_events", "notifications", "money_signals")
IMPORT_MAX_BYTES = 64 * 1024 * 1024


def book_is_empty():
    """The gate a carry-over is judged by: a book that already holds records of its own is
    never written over, so the worst a wrong file can do here is be refused."""
    tables = {r["name"] for r in _db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table in RECORD_TABLES:
        if table in tables and one("SELECT count(*) c FROM %s" % table)["c"]:
            return False
    return True


def carry_book_over(source_path):
    """Make the shop's copy of the book this server's book, whole. Only an empty book takes one;
    devices signed in on the old server do not travel, and a copy from an older build is brought
    forward by the same migrations a shop Mac runs."""
    source = sqlite3.connect("file:%s?mode=ro" % source_path, uri=True)
    try:
        verdict = source.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            raise ValueError("That copy does not pass its own check: %s" % verdict)
        tables = {r[0] for r in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "jobs" not in tables or "clients" not in tables:
            raise ValueError("That file is not a Chrisphics book — it holds no jobs or clients")
        counts = {table: source.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
                  for table in RECORD_TABLES if table in tables}
        if not sum(counts.values()):
            raise ValueError("That copy holds no records, so there is nothing to carry over")
        with _lock:
            source.backup(_db)
            migrate()
            with open(os.path.join(ROOT, "schema.sql"), "r", encoding="utf-8") as fh:
                _db.executescript(fh.read())
            # A copy from the shop's Wi-Fi brings its own signed-in devices; none of them are
            # signed in here.
            _db.execute("DELETE FROM sessions")
            _db.execute("DELETE FROM sync_requests")
            _db.commit()
        return {"carried": True, "records": counts}
    finally:
        source.close()


CAFFEINATE = "/usr/bin/caffeinate"


def keep_mac_awake():
    """A phone can only reach the book while this Mac is awake. While the shop server listens
    beyond this computer, ask the Mac not to idle away; caffeinate stops on its own when we do."""
    if not os.path.exists(CAFFEINATE):
        return False
    try:
        subprocess.Popen([CAFFEINATE, "-i", "-s", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False



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
          trust_proxy=False, allow_unauthenticated_lan=False, keep_awake=None):
    global AUTH_PASSWORD, LAN_NO_LOGIN, THROUGH_PROXY, KEEP_AWAKE
    local_only = host in ("127.0.0.1", "localhost", "::1")
    THROUGH_PROXY = bool(trust_proxy)
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
        LAN_NO_LOGIN = True
    # A password chosen inside the book counts just as much as one handed over in the
    # environment; either way the Wi-Fi is not left looking straight into the accounts.
    if not local_only and not allow_unauthenticated_lan and not auth_required():
        raise RuntimeError("Other devices can reach this book, so it needs a password. Open"
                           " Shop & devices on this Mac and choose one (or set"
                           " CHRISPHICS_AUTH_PASSWORD).")
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("Both --tls-cert and --tls-key are required to enable HTTPS.")
    if not local_only and not tls_cert and not trust_proxy and not allow_unauthenticated_lan:
        raise RuntimeError("HTTPS is required when listening beyond this computer.")
    if trust_proxy and tls_cert:
        raise ValueError("Use either direct HTTPS or --trust-proxy, not both.")
    SERVE.update({"host": host, "port": port, "tls": bool(tls_cert) or bool(trust_proxy)})
    if keep_awake is None:
        keep_awake = not local_only
    if keep_awake:
        KEEP_AWAKE = keep_mac_awake()
    watch_parent()
    watch_messages()
    watch_notifications()
    watch_backups()
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
    if not local_only:
        # `0.0.0.0` is where the server listens, not a number anyone can type. Without the Mac's own
        # Wi-Fi address this line reads `…://127.0.0.1:8834/setup` — which every machine on the
        # network other than this one fails to open, and the counter has nothing else to go by.
        peer = display_host
        named = ""
        if host in ("0.0.0.0", "::", ""):
            peer = wifi_address() or display_host
            named = socket.gethostname()
        print("Devices on this Wi-Fi install from %s://%s:%d/setup" % (scheme, peer, port))
        if named.endswith(".local"):
            print("  Apple devices can also use the name, which outlives a new number:"
                  " %s://%s:%d/" % (scheme, named, port))
        print("Sign-in: %s" % ("one shop password" if auth_required() and not LAN_NO_LOGIN
                               else "not required (this run was told to skip it)"))
        if not KEEP_AWAKE:
            print("Note: this Mac may still sleep, and the book goes quiet with it.")
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
    ap.add_argument("--keep-awake", action="store_true", default=None,
                    help="hold this Mac awake while the book is served (automatic beyond loopback)")
    ap.add_argument("--allow-sleep", dest="keep_awake", action="store_false",
                    help="let the Mac sleep even when other devices can reach the book")
    ap.add_argument("--backup", action="store_true",
                    help="write a checked, tidied copy of the book into the backups folder and exit")
    args = ap.parse_args()
    connect(args.db)
    if args.backup:
        path, counts, tidy = run_backup()
        print("Backup written to %s" % path)
        print("Checked: %s" % ", ".join("%s %d" % kv for kv in sorted(counts.items())))
        print("Backups folder: kept %d, cleared %d older copies" % (tidy["kept"], tidy["gone"]))
        return
    serve(args.port, not args.no_browser, args.seed, args.host, args.tls_cert, args.tls_key,
          args.trust_proxy, args.allow_unauthenticated_lan, args.keep_awake)


if __name__ == "__main__":
    main()
