-- Chrisphics Hub — printing shop records & account book
-- SQLite 3.37+ (ships with macOS). Money is stored in Ghana Cedi (GHS) as decimals.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clients (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  phone       TEXT,
  whatsapp    TEXT,
  email       TEXT,
  whatsapp_updates INTEGER NOT NULL DEFAULT 0,
  email_updates    INTEGER NOT NULL DEFAULT 0,
  address     TEXT,
  kind        TEXT NOT NULL DEFAULT 'Individual',   -- Individual | Business | School | Church | NGO
  notes       TEXT,
  archived    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS jobs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ref           TEXT NOT NULL UNIQUE,               -- CH-2026-0001 for jobs, Q-2026-0001 for quotes
  client_id     INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL DEFAULT 'Job',        -- Job | Quote  (a quote is a job not booked yet)
  title         TEXT NOT NULL,
  category      TEXT NOT NULL DEFAULT 'Other',      -- Business Cards | Banner | Flyer | ...
  description   TEXT,
  size          TEXT,                               -- 'A4', '2m x 1m', '360gsm art paper'
  quantity      INTEGER NOT NULL DEFAULT 1,
  unit          TEXT NOT NULL DEFAULT 'pcs',
  unit_price    REAL NOT NULL DEFAULT 0,
  extras        REAL NOT NULL DEFAULT 0,            -- material/lamination/binding cost
  discount      REAL NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'Pending',    -- Pending | Printing | Ready | Delivered | Cancelled
  priority      TEXT NOT NULL DEFAULT 'Normal',     -- Normal | Urgent
  due_date      TEXT,
  valid_until   TEXT,                               -- quotes only: how long the price holds
  converted_at  TEXT,                               -- quotes only: when it became a job
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  closed_at     TEXT,
  total         REAL GENERATED ALWAYS AS ( max(quantity * unit_price + extras - discount, 0) ) VIRTUAL
);

-- One order, many products: 500 cards + 2 roll-ups on a single job sheet.
-- A job with no lines here falls back to the quantity/unit_price columns above,
-- so every job booked before lines existed keeps its original total.
CREATE TABLE IF NOT EXISTS job_items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  title       TEXT NOT NULL,
  category    TEXT NOT NULL DEFAULT 'Other',
  size        TEXT,
  quantity    INTEGER NOT NULL DEFAULT 1,
  unit        TEXT NOT NULL DEFAULT 'pcs',
  unit_price  REAL NOT NULL DEFAULT 0,              -- charged to the client
  unit_cost   REAL NOT NULL DEFAULT 0,              -- what it costs the shop
  position    INTEGER NOT NULL DEFAULT 0,
  line_total  REAL GENERATED ALWAYS AS ( max(quantity * unit_price, 0) ) VIRTUAL,
  line_cost   REAL GENERATED ALWAYS AS ( max(quantity * unit_cost, 0) ) VIRTUAL
);

-- Money going out. Linked to a job when it was for that job, otherwise shop overhead.
CREATE TABLE IF NOT EXISTS expenses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  spent_on    TEXT NOT NULL DEFAULT (date('now','localtime')),
  category    TEXT NOT NULL DEFAULT 'Other',        -- Paper / Ink / Rent / Labour …
  payee       TEXT,                                 -- who was paid
  amount      REAL NOT NULL,
  method      TEXT NOT NULL DEFAULT 'Cash',
  reference   TEXT,
  job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  note        TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Spoilage records pair a production incident with its job-cost expense.
CREATE TABLE IF NOT EXISTS spoiled_work (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  expense_id  INTEGER NOT NULL UNIQUE REFERENCES expenses(id) ON DELETE CASCADE,
  quantity    INTEGER NOT NULL CHECK (quantity > 0),
  reason      TEXT NOT NULL,
  spoiled_on  TEXT NOT NULL DEFAULT (date('now','localtime')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Work that has not become a job yet: someone asked, nothing is booked.
CREATE TABLE IF NOT EXISTS leads (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  phone       TEXT,
  whatsapp    TEXT,
  email       TEXT,
  source      TEXT NOT NULL DEFAULT 'Walk-in',      -- Walk-in | WhatsApp | Referral …
  interest    TEXT,                                 -- what they want printed
  value       REAL NOT NULL DEFAULT 0,              -- rough worth of the work
  stage       TEXT NOT NULL DEFAULT 'Prospect',     -- Prospect | Meeting | Proposal | Won | Lost
  follow_up   TEXT,                                 -- when to chase again
  client_id   INTEGER REFERENCES clients(id) ON DELETE SET NULL,
  job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  note        TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  closed_at   TEXT
);


CREATE TABLE IF NOT EXISTS payments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  job_id       INTEGER REFERENCES jobs(id) ON DELETE CASCADE,  -- NULL = credit held on the client account
  amount       REAL NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'Payment',     -- Deposit | Payment | Refund
  method       TEXT NOT NULL DEFAULT 'Cash',        -- Cash | MoMo | Bank | Cheque | Change
  reference    TEXT,                                -- MoMo / bank transaction id
  note         TEXT,
  paid_at      TEXT NOT NULL DEFAULT (date('now','localtime')),
  recorded_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Timeline of everything that happened to a job: the "records being taken" part.
CREATE TABLE IF NOT EXISTS job_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  type        TEXT NOT NULL DEFAULT 'status',       -- status | note | payment | file | message
  detail      TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- One status message per job, event and channel. Automatic deliveries retry from this
-- durable queue; manual messages still open in WhatsApp or Mail for staff to send.
CREATE TABLE IF NOT EXISTS notifications (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  event       TEXT NOT NULL,                        -- Quote | Booked | Pending | Printing | Ready | Delivered | Cancelled
  channel     TEXT NOT NULL,                        -- WhatsApp | Email
  to_address  TEXT NOT NULL,                        -- wa.me number (no +) or email address
  subject     TEXT NOT NULL DEFAULT '',             -- email only; '' for WhatsApp
  body        TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'Queued',       -- Queued | Opened | Sent
  auto_send   INTEGER NOT NULL DEFAULT 0,
  delivery_state TEXT NOT NULL DEFAULT 'Manual',    -- Manual | Pending | Sending | Failed | Sent
  delivery_attempts INTEGER NOT NULL DEFAULT 0,
  delivery_next_at REAL,
  delivery_error TEXT NOT NULL DEFAULT '',
  provider_id TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- A payment notice the shop did not type: read out of Messages, or pasted in.
-- The alert text is money news, not a bill, so it waits here until it is matched to a
-- client and booked — `payment_id` is what makes re-reading the same text harmless.
CREATE TABLE IF NOT EXISTS money_signals (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source       TEXT NOT NULL DEFAULT 'Messages',     -- Messages | Pasted
  source_row   INTEGER NOT NULL DEFAULT 0,           -- chat.db ROWID; 0 for a pasted alert
  dedupe       TEXT NOT NULL UNIQUE,                 -- 'msg:<rowid>' or 'paste:<sha1>'
  sender       TEXT,                                 -- who the alert came from, as it appeared
  raw          TEXT NOT NULL,                        -- the alert itself, kept as the proof
  amount       REAL,
  payer        TEXT,                                 -- the name the alert carries, if any
  payer_phone  TEXT,                                 -- normalised to 233…
  direction    TEXT NOT NULL DEFAULT 'Unknown',      -- Credit | Out | Unknown
  state        TEXT NOT NULL DEFAULT 'Unreviewed',   -- Unreviewed | Booked | Ignored
  client_id    INTEGER REFERENCES clients(id) ON DELETE SET NULL,
  job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  payment_id   INTEGER REFERENCES payments(id) ON DELETE SET NULL,   -- money in, or a refund out
  expense_id   INTEGER REFERENCES expenses(id) ON DELETE SET NULL,   -- a send to someone who is not a client
  reason       TEXT,                                 -- why it is unsure, for the human to read
  seen_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  booked_at    TEXT
);

-- Two switches the shop sets and the app must remember across launches: whether to watch
-- Messages, and whether a matched alert books itself or waits for a click. `momo_status`
-- carries what the last read actually returned, so a missing disk permission is said out
-- loud instead of the panel just looking idle.
CREATE TABLE IF NOT EXISTS app_state (
  key         TEXT PRIMARY KEY,                      -- momo_watch | momo_auto | momo_status
  value       TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sync_requests (
  operation_id TEXT PRIMARY KEY,
  status       INTEGER NOT NULL,
  response     TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- A job's money. `total` prefers the item lines and only falls back to the header
-- quantity x price when the job has no lines, so old jobs are untouched.
-- cost = what the lines cost the shop + any expense booked against the job.
DROP VIEW IF EXISTS job_accounts;
CREATE VIEW job_accounts AS
WITH lines AS (
  SELECT job_id,
         count(*)                      AS item_count,
         coalesce(sum(line_total), 0)  AS line_gross,
         coalesce(sum(line_cost), 0)   AS line_cost
  FROM job_items
  GROUP BY job_id
),
priced AS (
  SELECT j.id AS job_id,
         round(max(CASE WHEN coalesce(l.item_count, 0) > 0
                        THEN l.line_gross + j.extras - j.discount
                        ELSE j.quantity * j.unit_price + j.extras - j.discount END, 0), 2) AS total,
         round(coalesce(l.line_cost, 0)
               + coalesce((SELECT sum(e.amount) FROM expenses e WHERE e.job_id = j.id), 0), 2) AS cost
  FROM jobs j
  LEFT JOIN lines l ON l.job_id = j.id
)
SELECT j.id, j.ref, j.kind, j.client_id, c.name AS client, j.title, j.category,
       j.status, j.priority, j.due_date, j.valid_until, j.converted_at,
       j.quantity, j.unit, j.size, j.unit_price, j.extras, j.discount,
       j.created_at, j.updated_at, j.closed_at,
       coalesce(l.item_count, 0) AS item_count,
       pr.total,
       pr.cost,
       round(pr.total - pr.cost, 2) AS profit,
       round(coalesce(sum(CASE WHEN p.kind = 'Refund' THEN -p.amount ELSE p.amount END), 0), 2) AS paid,
       round(pr.total - coalesce(sum(CASE WHEN p.kind = 'Refund' THEN -p.amount ELSE p.amount END), 0), 2) AS balance
FROM jobs j
JOIN clients c        ON c.id = j.client_id
JOIN priced pr        ON pr.job_id = j.id
LEFT JOIN lines l     ON l.job_id = j.id
LEFT JOIN payments p  ON p.job_id = j.id
GROUP BY j.id;

-- A client's running balance. Quotes are an ask, not a bill, so they are left out here.
DROP VIEW IF EXISTS client_accounts;
CREATE VIEW client_accounts AS
SELECT c.id,
       round(coalesce((SELECT sum(total) FROM job_accounts ja
                       WHERE ja.client_id = c.id AND ja.kind <> 'Quote' AND ja.status <> 'Cancelled'), 0), 2) AS billed,
       round(coalesce((SELECT sum(CASE WHEN p.kind = 'Refund' THEN -p.amount ELSE p.amount END)
                       FROM payments p WHERE p.client_id = c.id), 0), 2) AS received,
       round(coalesce((SELECT sum(balance) FROM job_accounts ja WHERE ja.client_id = c.id
                       AND ja.kind <> 'Quote' AND ja.status <> 'Cancelled'), 0), 2) AS open_job_balance,
       round(coalesce((SELECT sum(CASE WHEN p.kind = 'Refund' THEN -p.amount ELSE p.amount END)
                       FROM payments p WHERE p.client_id = c.id AND p.job_id IS NULL), 0), 2) AS account_credit,
       coalesce((SELECT count(*) FROM job_accounts ja WHERE ja.client_id = c.id
                 AND ja.kind = 'Quote' AND ja.converted_at IS NULL AND ja.status <> 'Cancelled'), 0) AS open_quotes
FROM clients c;

-- Month-by-month trading position: money in, money out, the difference.
DROP VIEW IF EXISTS monthly_pnl;
CREATE VIEW monthly_pnl AS
SELECT m.month,
       round(coalesce(m.income, 0), 2)  AS income,
       round(coalesce(x.spent, 0), 2)   AS spent,
       round(coalesce(m.income, 0) - coalesce(x.spent, 0), 2) AS net
FROM (
  SELECT substr(p.paid_at, 1, 7) AS month,
         sum(CASE WHEN p.kind = 'Refund' THEN -p.amount ELSE p.amount END) AS income
  FROM payments p GROUP BY month
) m
LEFT JOIN (
  SELECT substr(e.spent_on, 1, 7) AS month, sum(e.amount) AS spent
  FROM expenses e GROUP BY month
) x ON x.month = m.month
UNION ALL
SELECT substr(e.spent_on, 1, 7) AS month, 0 AS income, round(sum(e.amount), 2) AS spent,
       round(-sum(e.amount), 2) AS net
FROM expenses e
WHERE substr(e.spent_on, 1, 7) NOT IN (SELECT substr(p.paid_at, 1, 7) FROM payments p)
GROUP BY month
ORDER BY month DESC;

CREATE INDEX IF NOT EXISTS idx_jobs_client   ON jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_due      ON jobs(due_date);
CREATE INDEX IF NOT EXISTS idx_jobs_kind     ON jobs(kind);
CREATE INDEX IF NOT EXISTS idx_items_job     ON job_items(job_id);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(spent_on);
CREATE INDEX IF NOT EXISTS idx_expenses_job  ON expenses(job_id);
CREATE INDEX IF NOT EXISTS idx_leads_stage   ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_follow  ON leads(follow_up);
CREATE INDEX IF NOT EXISTS idx_payments_job  ON payments(job_id);
CREATE INDEX IF NOT EXISTS idx_payments_cust ON payments(client_id);
CREATE INDEX IF NOT EXISTS idx_events_job    ON job_events(job_id);
CREATE INDEX IF NOT EXISTS idx_signals_state ON money_signals(state);
CREATE INDEX IF NOT EXISTS idx_signals_row   ON money_signals(source, source_row);
CREATE UNIQUE INDEX IF NOT EXISTS ux_notify_slot ON notifications(job_id, event, channel);
CREATE INDEX IF NOT EXISTS idx_notify_job    ON notifications(job_id);
CREATE INDEX IF NOT EXISTS idx_notify_state  ON notifications(state);
