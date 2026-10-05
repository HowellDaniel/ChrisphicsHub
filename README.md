# Chrisphics Hub — CRISPprint Ghana printing records & account book

A records and accounts system for a print shop, running as a Mac app and installable web app. Every printing job gets
a line, every payment gets a line, and every client keeps a running balance. It runs on the
shop's own computer and local network: normal record keeping works offline; configured
outbound WhatsApp/email updates need internet when they are sent.

The app wears the shop's own artwork: the CRISPprint Ghana lockup is in the sidebar, on the
printed job sheet and in the browser tab, and the Dock icon is the four-diamond mark. The
colours are lifted from the same artwork — see [Colour and appearance](#colour-and-appearance).
The bundle on disk is `Chrisphics Hub.app` and your records live in
`~/Library/Application Support/Chrisphics Hub/`. The first time the renamed app starts it copies
the book out of the old misspelled folder (`Chriphics Hub`) and leaves that file where it was, so
there is never a moment when the shop's records only exist in one place.

## What is in this folder

| Path | What it is |
| --- | --- |
| `Chrisphics Hub.app` | the desktop app — this is what you open |
| `Chrisphics Hub.command` | the same thing in a browser tab, if you prefer |
| `server.py`, `schema.sql`, `public/` | the records engine and the screens it serves |
| `desktop/` | the native shell's Swift source and `build-app.sh` |
| `render.yaml` | Render web service, HTTPS proxy, password and persistent-disk setup |
| `tools/` | `make-web-icons.swift` redraws the install icons; the other two are dev checks |
| `~/Library/Application Support/Chrisphics Hub/` | your actual records — not in this folder |

## Start it

**The desktop app (single-computer mode).** Double-click **`Chrisphics Hub.app`**. It opens in
its own window with its own Dock icon and menus, and starts a private copy of the engine on
a port only it uses. There is nothing to keep open and nothing to install — quit the app and
the engine stops with it. For one shared book across the shop Wi-Fi, use the LAN server
instructions below instead; do not run this private app at the same time as that server.

**Switching over from the old build.** Until the shop quits the app that is open right now, the
misspelled `Chriphics Hub.app` and its folder are still the live book, and the Wi-Fi server must
not be restarted in that window: a restart would copy the book forward and the running app would
keep writing to the old one. Quit the old app, then open **`Chrisphics Hub.app`** — it copies the
book into `~/Library/Application Support/Chrisphics Hub/` on its first run and the old file stays
untouched as the safety copy. From then on only the new name is used.

- `File` — New Job `⌘N`, New Quote `⇧⌘N`, New Enquiry `⌘I`, New Client `⌘K`,
  Receive Payment `⌘R`, Record Money Out `⌘E`.
- `View` — `⌘1`…`⌘9` jump between the nine screens; Reload `⇧⌘R` after changing `public/`;
  **Appearance** picks Light, Dark or "Follow the Mac" (the same three buttons sit at the
  bottom of the sidebar).
- `Data` — Back Up Book Now `⌘B`, Show Data File in Finder, Open Backup Folder.
- A job's **Job sheet** opens in its own window. Print Job Sheet `⌘P` goes through the normal
  macOS print dialog (the sheet's own print button is wired to it); **Export Job Sheet as
  PDF** saves it as a file.

**In a browser instead.** Double-click **`Chrisphics Hub.command`**. A Terminal window opens and
your browser goes to <http://127.0.0.1:8712/>. It reads and writes the same data file as the
desktop app. Use one at a time; keep the Terminal window open while you work, and press
`Ctrl+C` in it (or close it) to stop.

From the command line, the browser route is `./run.sh`. Useful flags:

| Command | What it does |
| --- | --- |
| `./run.sh` | start and open the browser |
| `./run.sh --no-browser` | start without opening a browser tab |
| `./run.sh --port 8080` | use a different port if 8712 is taken |
| `./run.sh --host 0.0.0.0 --port 8712 --tls-cert /path/server.crt --tls-key /path/server.key` | serve the installable app over shop Wi-Fi with HTTPS; requires `CHRISPHICS_AUTH_PASSWORD` |
| `./run.sh --seed` | add sample clients, jobs, a quote, expenses and enquiries — only into an empty book |
| `python3 server.py --backup` | write a backup into the Backups folder and exit |
| `./desktop/build-app.sh` | rebuild `Chrisphics Hub.app` after editing the code |

### Install on Windows, Android and iPhone

The same responsive web app can be installed from Microsoft Edge on Windows and from a
mobile browser. First configure the shop computer as the local server, give it a stable
Wi-Fi address/name, and use a TLS certificate trusted by the devices. HTTPS is required for
browser installation and offline app-shell storage on phones; a plain `http://` LAN address
is not sufficient. Set `CHRISPHICS_AUTH_PASSWORD` on the server before listening on the
network. Do not forward the server port to the public internet.

The certificate must be trusted by the devices and include the stable shop hostname (or IP)
in its Subject Alternative Name. A self-signed certificate warning is not enough for
service-worker installation. Keep the certificate's private key on the server; only install
the CA's public certificate on client devices.

On the shop computer, configure a database path and start the server. For example, in
PowerShell on Windows:

```powershell
Set-Location C:\path\to\CRISPprint-Ghana
$env:CHRISPHICS_AUTH_PASSWORD = Read-Host "Shop password"
$env:CHRISPHICS_DB = "$env:LOCALAPPDATA\CRISPprint\chrisphics.db"
New-Item -ItemType Directory -Force (Split-Path $env:CHRISPHICS_DB) | Out-Null
python server.py --host 0.0.0.0 --port 8712 --tls-cert C:\shop\server.crt --tls-key C:\shop\server.key --no-browser
```

On macOS/Linux, set the same environment variables and run the matching `./run.sh` command
from the project folder. Keep this server computer on while devices are syncing. Open the
HTTPS shop address on the server computer too; it is the shared book for every device.

Open the HTTPS shop address once on each device while connected to the shop Wi-Fi:

- Use the app's **Install app** button to open the browser's install prompt where supported,
  or show device-specific steps.
- **Windows / Edge:** use the browser's **Install this site as an app** command.
- **Android / Chrome:** use **Install app** or **Add to Home screen**.
- **iPhone / iPad:** in Safari, choose **Share → Add to Home Screen**.

Under 720px the book becomes a phone app rather than a shrunken desktop. The side rail is
gone; **Desk · Jobs · Money · Clients · More** sit in a tab bar at thumb height, and **More**
opens a sheet holding the remaining screens (spoiled work, pending sync, enquiries, expenses,
reports) plus the theme switch and the backup button. A job's record and every form rise from
the bottom of the screen, the first column of a wide ledger pins itself so a scrolled row is
still identifiable, and everything you press is at least 44px tall. The status strip and the
tab bar sit inside the notch and home-indicator areas, so nothing hides under them. Once
installed, long-press the home-screen icon for **New job** and **Receive payment** shortcuts,
which open straight to that form.

Icons are drawn, not photographed: `tools/make-web-icons.swift` renders the shop's four-diamond
mark at 192px, 512px, a 512px maskable version with the mark pulled inside the safe circle,
and a 180px `apple-touch-icon`, all on the same ink plate as the Dock icon.

The app shell and screens already opened are cached on each device. While offline, new
changes are saved in that device's **Pending sync** queue. Reconnect to the shop Wi-Fi to
send them to the shared book; the queue retries safely if a connection drops. If another
device changed the same record first, the app pauses and shows both the saved local change
and current shared record for review. Data stays on the server and on devices' browser
storage; protect each device with its own screen lock. Offline copies may be stale, and a
device must reconnect to the shop server to see updates entered elsewhere.

The native Mac desktop app is a separate single-computer mode, not a client for the shared
LAN server. When using shared mode on a Mac, use the HTTPS shop address in its browser rather
than opening `Chrisphics Hub.app`.

### Open and install over HTTPS on the same Wi-Fi

The no-login Wi-Fi mode can use HTTPS too. On the shop Mac, install a local certificate
authority tool and create a certificate for the Mac's current Wi-Fi address:

```sh
brew install mkcert
mkcert -install || security add-trusted-cert -r trustRoot -p ssl \
  -k "$HOME/Library/Keychains/login.keychain-db" "$(mkcert -CAROOT)/rootCA.pem"
WIFI_IP="$(ipconfig getifaddr en0)"
mkdir -p "$HOME/Library/Application Support/CRISPprint TLS"
mkcert -cert-file "$HOME/Library/Application Support/CRISPprint TLS/shop-cert.pem" \
  -key-file "$HOME/Library/Application Support/CRISPprint TLS/shop-key.pem" \
  "$WIFI_IP" localhost 127.0.0.1 ::1
chmod 600 "$HOME/Library/Application Support/CRISPprint TLS/shop-key.pem"
```

The HTTPS listener requires that certificate and private key. It still has no sign-in; bind it
only to the private Wi-Fi address, and do not forward its port or run it on a guest/public
network:

```sh
./run.sh --host "$WIFI_IP" --port 8834 \
  --tls-cert "$HOME/Library/Application Support/CRISPprint TLS/shop-cert.pem" \
  --tls-key "$HOME/Library/Application Support/CRISPprint TLS/shop-key.pem" \
  --allow-unauthenticated-lan --no-browser
```

Run the certificate-only download server in a second Terminal window so other devices can
obtain the public CA certificate before trusting the HTTPS site:

```sh
CERT_DIR="$HOME/Library/Application Support/CRISPprint TLS/device-download"
mkdir -p "$CERT_DIR"
openssl x509 -in "$(mkcert -CAROOT)/rootCA.pem" -outform DER \
  -out "$CERT_DIR/shop-root-ca.cer"
ruby -run -e httpd -- --bind-address="$WIFI_IP" --port=8835 "$CERT_DIR"
```

On each other device, download `http://<WIFI_IP>:8835/shop-root-ca.cer` or transfer the public
CA certificate from `"$(mkcert -CAROOT)/rootCA.pem"`. Verify the certificate's SHA-256
fingerprint is:

```text
ED:4D:60:46:1B:FA:5F:E8:3C:EA:A0:84:03:F3:42:EE:A0:D1:F9:D8:7F:62:C3:B8:02:98:BF:E2:E3:7D:1A:CB
```

Install that CA certificate as a trusted root on the device: Windows uses Certificate Manager
under **Trusted Root Certification Authorities**; Android uses Security settings to install a
CA certificate; on iPhone/iPad, install the downloaded profile in Settings and enable full
trust under Certificate Trust Settings. Then open `https://<WIFI_IP>:8834/` and use **Install
app**: supported browsers can install it and the service worker can cache the app shell. The
CA **private key** (`rootCA-key.pem`) must remain on the shop Mac and must never be shared. The
shop certificate's private key must also stay on the Mac. Renew the shop certificate and repeat
the device trust steps if the local CA is replaced.

### Open on the same Wi-Fi without HTTPS

For a quick, no-login connection on the shop Wi-Fi only, bind the server to the Mac's private
Wi-Fi IPv4 address instead of `0.0.0.0`. For example:

```sh
WIFI_IP="$(ipconfig getifaddr en0)"
./run.sh --host "$WIFI_IP" --port 8834 --allow-insecure-lan --no-browser
```

On another device connected to that same Wi-Fi, open `http://<WIFI_IP>:8834/`, replacing
`<WIFI_IP>` with the address printed by `ipconfig getifaddr en0` (for this setup, currently
`192.168.100.29`). Reserve that address in the router if it should stay the same. Keep the Mac
awake and the server running.

This explicit mode has **no login and no HTTPS**. Anyone who can join or reach that Wi-Fi can
read and change the whole shop book. Do not use it on public/shared guest Wi-Fi or expose the
port to the internet; stop the server to close access. Browsers also require trusted HTTPS for
service-worker installation on other devices, so this HTTP address opens the app but does not
enable PWA installation/offline caching there.

### Host on Render

`render.yaml` defines a single-instance paid web service with a persistent disk for the
SQLite book and backups. Render terminates public HTTPS; the app's `--trust-proxy` option is
only for this trusted proxy deployment. A shop password is required at setup. Choose a strong
password in Render's setup prompt and keep it private; the sign-in cookie is marked Secure.

To deploy, connect the GitHub repository containing this project to Render, then create a
new Blueprint from that repository and review the paid service and disk before confirming.
The source repository is public; use a private repository if you do not want the app source
to be public.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/HowellDaniel/ChrisphicsHub)

Open the deployment link, sign in to Render, provide a strong value for
`CHRISPHICS_AUTH_PASSWORD`, review the paid Starter service and persistent disk, then confirm
**Deploy Blueprint**. Render will show the app's public HTTPS URL after provisioning finishes.
Render builds the Python server, provisions its persistent disk, and serves the app over
HTTPS. The service exposes `/healthz` for health checks and is intentionally limited to one
instance because SQLite is stored on that disk. Keep a current backup outside Render as
well; the app's backup action writes to the mounted disk, which is not an independent backup.

The hosted book starts empty. It does not import or change the records on the shop computer.
Configure WhatsApp and email provider secrets separately in the Render service environment
if automatic customer messages are wanted. Record each client's channel consent in the app;
without provider settings, those messages cannot be delivered.

## The nine screens

**Dashboard** — money in today / this week / month to date, what customers still owe,
what is late, what is ready for pickup, a 15-day cash chart, and what you print most.
Above the tiles sits **Money in by MoMo**: payment alerts the shop did not type, read out of
Messages or pasted in, waiting to be booked ([see below](#money-in-by-momo)).
Below that: quotes still waiting for an answer, enquiries due for a chase, and where the
month's money went.

The dashboard is the one screen you work *in*, not just read. Every number tile is a way
into the screen behind it, and the things the shop changes during a day — a job's title,
status, priority or due date, a quote's validity, an enquiry's stage, what they want, its
value and when to chase — are underlined where they sit. Click one, type or pick, and it
saves the moment you press Enter: the cell flashes green, the screen settles back into
place without jumping to the top, and only that one field is written, so nothing else on
the row can be caught in the change. A value the engine refuses (an empty job name, a date
that is not a date) shakes red and stays as it was.

**Print jobs** — the order book, with a Quotes tab beside it. Filter by status, service,
date booked or client, and by **Money: any / Still owing / Paid in full**; search by job,
item, client name or phone. Each row shows the balance and the profit, how many client
messages are still to send, and a green **Paid in full** pill on work whose balance has been
cleared. Open a row for the
full record: every item line on the order, the costs booked against it, payments, a status
stepper (Pending → Printing → Ready → Delivered), a **Tell the client** card with the
message written for that stage ([see below](#telling-the-client)), a notes timeline, and a
**Job sheet** you can hand over or print.

**Clients** — everyone you have printed for, with their phone, WhatsApp, area, job count,
total billed, total received and balance due. Click a client for their whole history.

**Enquiries** — the pipeline: people who asked about printing but have not booked. Each
one carries a source, what they want, a rough value, a stage (Prospect → Meeting →
Proposal → Won / Lost) and a follow-up date, so nothing asked in the shop is forgotten.
A yes turns into a client and a booked job in one click.

**Accounts** — who owes what, with a "Get ₵…" button on each outstanding job; a **Paid in
full — settled jobs** table holding the work whose balance has been cleared, with what was
billed, what was collected and the date it was cleared; the payment ledger (filter by date,
method, client or transaction reference); what was paid out in the same period; and credit
held on client accounts that has not been applied to a job yet.

In the **Receive money** window, typing the whole balance turns the foot of the form into
"That is the whole balance — this job will read Paid in full", and a partial figure offers
**Cover the rest · ₵…** which fills the amount in, sets the type to `Payment` and notes the
entry as *Paid in full*. Money taken after a job is cleared is not refused: it sits as credit
on the client's account, which the next job draws against.

**Expenses** — money out: paper, ink, finishing, rent, airtime, transport, and anything
outsourced. Each entry can hang off a job, which is what makes that job's profit real;
the rest is shop overhead. Totals by category and month, and a CSV for the accountant.

**Spoiled work** — record spoiled quantity, reason, date and extra cost against an existing
job. The cost is added to that job's expenses and reduces its profit; entries can be edited
or removed from this screen. The same record sits inside the job it belongs to: open a job and
**Spoiled on this job** lists every incident on that order with its cost, and **+ Log spoiled
work** there opens the form with the job already chosen — so a batch that goes wrong is
recorded against the job, never against the client's price.

**Pending sync** — shows offline changes saved on this device, whether they are waiting,
synced or need conflict review.

**Reports** — billed, what it cost, profit, collected, still owed and delivered for any
date range, grouped by day, week or month; broken down by service, by payment method, by
client and by expense category. Every table exports to CSV for Excel or the accountant.

## How the money works

```
job total   = sum of the item lines, or quantity × unit price when there are none
              + materials/finishing − discount
job cost    = what each item line costs + expenses booked against the job
job profit  = job total − job cost
job balance = job total − payments recorded against that job
client due  = sum of that client's job balances − credit held on their account
```

A quote (`Q-…`) is priced, not billed: it stays out of money-in, receivables and reports
until you press **Book it as a job**, which gives it a `CH-…` reference, keeps its item
lines and starts counting. Quotes take no payments — book the job first.

A payment typed without choosing a job becomes **credit on the client's account** — useful
when somebody hands over money before the work is booked. Refunds are entered as
`Refund` and count against what was received.

Profit is only as good as the costs you enter. A job with nothing costed shows its whole
price as profit in pale type, with the margin reading **not costed** — record the item
costs or an expense and it turns into a real figure.

Every change is written to the job's record: status moves, edits (shown as
`Unit price: ₵ 45.00 -> ₵ 50.00`), notes and payments. Nothing is silently overwritten.

## Money in by MoMo

Clients pay into **0506399641**. When a payment alert arrives as an SMS in **Messages** on
this Mac, the dashboard should already know about it — the *Money in by MoMo* card at the top
of the Dashboard is how that happens.

**Watch Messages** turns on a reader that looks at Messages every twenty seconds. It opens
`~/Library/Messages/chat.db` **read-only**, never writes to it, and never sends anything.
macOS protects that file, so the first time you switch it on the app needs permission:
*System Settings ▸ Privacy & Security ▸ Full Disk Access*, tick **Chrisphics Hub** (or the
terminal you started `server.py` from), then press **Check Messages now**. Without it the card
says so in one line instead of failing quietly, and the paste box still works.

**Paste an alert instead — this needs no permission**: drop the SMS text into the box and press
**Read it**. Same reading, no disk access.

What the reader takes from a text:

```
amount        the figure the alert carries, in cedis
payer         the name the alert gives, and their number
direction     money coming in, or money going out of the wallet
```

Number and name are what tie an alert to a client: the payer's number is matched against each
client's phone and WhatsApp (normalised to the same 233 form), and if no number matches, the
name is tried. That is as far as it goes — nothing is guessed, and an alert that cannot be
placed waits in the panel with the reason written out, so a person can name the client and
pick the job. **Money going out** (`you paid`, `withdraw`, `debited`…) is flagged and never
booked as a payment in; airtime messages, prizes and promotions are dropped and never appear
as a notice at all.

Press **Book it** and the money enters the book like any other payment: `MoMo` as the method,
**the client's own name as the transaction reference**, the amount, the job you chose (or left
as *Kept on their account (credit)* when nothing is owed), and the raw alert kept in the note
as the proof. The collected and owed figures on the Dashboard move at once, and while the
Dashboard is open the card refreshes on its own every twenty seconds, so an alert that arrives
while you are working shows itself without a click.

**Book without asking** takes the last click away: an alert whose amount, direction and client
all read cleanly books itself the moment it is seen. It is off by default and asks you to
confirm before it turns on, because it writes money into the book with no human between the
SMS and the ledger. Anything it cannot place with certainty still waits for you.

Only texts that actually read as money news are stored, and only in the `money_signals` table
of the same SQLite file, next to everything else in the book. Nothing is uploaded, and the
shop's wallet number (the `MOMO_NUMBER` at the top of the MoMo block in `server.py`) is
excluded when the reader looks for the payer, so your own number is never mistaken for a
client's.

Alert wording differs by network and changes over time. Before trusting **Book without
asking**, read one real alert from your own SMS through the panel and check the amount, the
name and the direction come back as they should.

## Telling the client

When a job is first booked and when its status changes to **Pending**, **Printing** or
**Ready**, the shop server automatically sends an update to each channel the client has
agreed to use. **Quotes, Delivered and Cancelled** messages remain available as drafts for
staff to open and send. Both channels are optional; edit a client and tick the matching
permission box only after they have agreed to receive job updates on that channel. Existing
clients are opted out until the shop records that permission.

Automatic messages are written to the job's durable queue first. If the provider is
unavailable or the shop server has no internet, it retries later; after eight unsuccessful
attempts the message is marked failed and the job screen offers **Retry now**. The queue and
provider error remain visible for review. Successful deliveries appear in the job timeline.
Provider acceptance is recorded, but email/WhatsApp cannot guarantee that the recipient's
device displayed or read a message.

Before automatic delivery can work on a channel, configure that provider in the environment
of the computer running `server.py` (never paste credentials into the app, source files or
chat). WhatsApp requires a Meta WhatsApp Business Cloud API token, phone-number ID, current
Graph API version, and an approved template in the configured language. Its body must be:

```text
Hello {{1}}, your print job {{2}} ({{3}}) is now {{4}}. We will keep you updated.
```

The four template parameters are the client's name, job reference, a concise work
description and status.
Configure `CHRISPHICS_WHATSAPP_TOKEN`, `CHRISPHICS_WHATSAPP_PHONE_NUMBER_ID`,
`CHRISPHICS_WHATSAPP_TEMPLATE`, and optionally `CHRISPHICS_WHATSAPP_API_VERSION` (defaults to
`v22.0`) and `CHRISPHICS_WHATSAPP_TEMPLATE_LANGUAGE` (defaults to `en`). Use Meta's current
supported API version and the exact language code approved for the template.

Email requires an SMTP host, username, password and sender address. For example, in the same
PowerShell window before starting the server:

```powershell
$env:CHRISPHICS_WHATSAPP_TOKEN = Read-Host "WhatsApp Cloud API token"
$env:CHRISPHICS_WHATSAPP_PHONE_NUMBER_ID = "your-phone-number-id"
$env:CHRISPHICS_WHATSAPP_TEMPLATE = "crispprint_job_status"
$env:CHRISPHICS_EMAIL_SMTP_HOST = "smtp.example.com"
$env:CHRISPHICS_EMAIL_SMTP_PORT = "587"
$env:CHRISPHICS_EMAIL_SMTP_USERNAME = "shop@example.com"
$env:CHRISPHICS_EMAIL_SMTP_PASSWORD = Read-Host "Email SMTP password"
$env:CHRISPHICS_EMAIL_FROM = "shop@example.com"
$env:CHRISPHICS_EMAIL_SMTP_SECURITY = "starttls"
```

Port 587 uses STARTTLS by default; set `CHRISPHICS_EMAIL_SMTP_SECURITY=ssl` for an SSL-wrapped
SMTP service (commonly port 465). Environment changes take effect after restarting the
server. If a provider is not configured, messages stay queued and show the missing settings;
the server never reports them as sent.

Email drafts and messages include the job reference, work, total, due date and balance.
WhatsApp auto updates use the approved short status template above. A client with no
WhatsApp number or email, or without recorded consent for a channel, is not automatically
contacted on that channel.

There is one message per stage per channel, so retrying or repeating a status does not
double-send it. If job details change before a queued notice is sent, the message is refreshed
and returned to the queue. A message already accepted by a provider keeps its sent wording.

## Your data

Everything lives in one file: **`~/Library/Application Support/Chrisphics Hub/chrisphics.db`**.
The sidebar footer shows the full path, and `Data ▸ Show Data File in Finder` reveals it.
Both the app and the browser launcher use this same file, so rebuilding or moving the `.app`
never touches your records.

- **Back up**: `Data ▸ Back Up Book Now`, or *Download data backup* in the sidebar, or
  `python3 server.py --backup`. Each one lands in
  `~/Library/Application Support/Chrisphics Hub/Backups/backup-<date>.db` as a complete,
  self-contained copy — one file, nothing else needed to restore it. Do this at the end of
  each trading day, and keep a copy on a USB stick, Google Drive or Time Machine.
- **Restore**: quit the app, move your backup file into that folder renamed to
  `chrisphics.db`, and start again.
- **Move to another Mac**: copy the `.app` and the `chrisphics.db` file. Only Python 3
  (already on macOS) is needed to run it.
- **Start with an empty book**: quit, then delete `chrisphics.db` from that folder.
- **Bring an old book up to date**: just open the rebuilt app. Missing tables (item lines,
  expenses, enquiries, queued client messages) and columns are added on start-up; every
  record you already wrote stays where it is, and anything booked before quotes existed
  counts as a job.

Want to look inside it directly:

```bash
sqlite3 "$HOME/Library/Application Support/Chrisphics Hub/chrisphics.db" \
  "SELECT ref, title, total, paid, balance FROM job_accounts WHERE balance > 0;"
```

## Making it yours

Open `server.py` and edit the `SHOP` block near the top — shop name, tagline, phone,
address and the currency symbol that appears on job sheets. The lists of services,
units, payment methods and client types are the `CATEGORIES`, `UNITS`, `PAY_METHODS`
and `KINDS` lines just below it; add or rename entries as you like.

The phone (`+233 000 000 000`) and address (`Accra, Ghana`) in that block are still
placeholders. They print at the top of every job sheet, and since the client messages sign
off with them ("Call or WhatsApp +233 000 000 000…", "ready for collection at Accra, Ghana"),
they land in front of your customers either way. Set them to the shop's real details before
handing a sheet — or a message — to anyone.

The `.app` carries its own copy of the program, so after editing anything run
`./desktop/build-app.sh` and reopen the app. That only rebuilds the program — your data
file is never touched. If the app is already open, `View ▸ Reload` is enough for changes
under `public/`.

**Colour and appearance.** The palette comes off the logo itself: crimson `#AB1F23` and the
grey `#808085` it pairs with, measured from the artwork rather than eyeballed. Both are
darkened for text (`--brand-ink`, `--link`) so they hold up as type on paper, and the mark's
own inks (`#C93A3F` / `#9A9AA0`) are what the Dock icon and the tab icon are drawn with.

There are three appearances — **Light**, **Dark** and **System**. Pick one in the sidebar or
under `View ▸ Appearance`; the choice is remembered on this Mac, and System follows the
Mac's own Light/Dark setting the moment you change it. The dark palette is a real second
palette, not a filter: surfaces lift off black, the crimson brightens so it survives on a
dark counter, and the status pills go from pale washes to deep ones with lightened type.
Native date pickers and scrollbars follow the appearance too. Text clears WCAG AA in both
palettes on all seven screens, the job drawer and the new-job form.

The printed job sheet is the one thing that stays black on white in either appearance, so a
photocopy and a customer's copy look the same.

To retune a colour, change the token in `public/styles.css` — the light values are in the
`:root` block at the top, the dark ones in `:root[data-theme="dark"]` just under it. Every
screen reads those variables, so one line moves the whole app. The logo itself is
`public/img/brand.png`; the favicon is drawn inline in `public/index.html` and the Dock icon
by `desktop/make-icon.swift`.

**Type.** Every screen, and the printed job sheet, is set in one face: **Poppins**
(`--face` at the top of `public/styles.css`), taken from the copy installed on this Mac in
`~/Library/Fonts` — nothing is downloaded, and the system face is used if Poppins is ever
removed. Money and dates are right-aligned rather than forced into tabular figures, because
the Poppins on this Mac does not carry that feature. To change the look, change `--face` in
one place; weights used anywhere in the CSS are snapped to the four Poppins ships
(400 / 500 / 600 / 700), so an in-between weight silently becomes its nearest one.

Two dev checks live in `tools/`, and neither needs anything installed:

| Command | What it answers |
| --- | --- |
| `python3 tools/js-check.py` | does `public/app.js` still parse? (one stray bracket empties every screen) |
| `swiftc -O tools/wk-probe.swift -o /tmp/wk-probe` then `/tmp/wk-probe <url> <dir> "open\|/#/jobs" "shot\|jobs"` | what a screen actually rendered, in the same WebKit the app uses, as text plus a PNG |

## Notes

- The app listens on `127.0.0.1` only, and picks its own free port each time it starts, so
  nothing outside this Mac can reach it and two copies never collide. The browser launcher
  uses 8712; if that is busy, start it with `--port 8080`.
- Data is never sent anywhere. There is no analytics, no login, no cloud. The one place the
  app reaches outward is a message you chose to send: pressing **WhatsApp** or **Email** on
  the **Tell the client** card asks macOS to open `wa.me` or your mail app with the words
  already typed. Nothing is sent, uploaded or contacted while you work, and the app will not
  load any outside page inside its own window.
- Job sheets print black on white for a clean photocopy.
- Run one at a time — the app and `Chrisphics Hub.command` open the same book, so close one
  before starting the other.
- The `.app` is built and signed for this Mac. If a copy on another Mac says it is from an
  unidentified developer, right-click it and choose Open once.
- Rebuilding needs the free Xcode Command Line Tools (already here, for `swiftc`); running it
  needs nothing but macOS and the Python 3 that ships with it.
