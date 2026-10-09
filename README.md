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
| `tools/` | `shop-server.sh` runs the shop's always-on server, `nightly-backup.sh` copies the book each night, `carry-book.py` hands a copy to a hosted book, `make-qr.swift` and `make-web-icons.swift` draw the QR and the install icons; the rest are dev checks |
| `~/Library/Application Support/Chrisphics Hub/` | your actual records — not in this folder |

## Start it

**The desktop app (single-computer mode).** Double-click **`Chrisphics Hub.app`**. It opens in
its own window with its own Dock icon and menus, and starts a private copy of the engine on
a port only it uses. There is nothing to keep open and nothing to install — quit the app and
the engine stops with it. For one shared book across the shop Wi-Fi, use
[Run the shop all the time](#run-the-shop-all-the-time) instead; do not run this private app at
the same time as that server.

**Switching over from the old build.** Until the shop quits the app that is open right now, the
misspelled `Chriphics Hub.app` and its folder are still the live book, and the Wi-Fi server must
not be restarted in that window: a restart would copy the book forward and the running app would
keep writing to the old one. Quit the old app, then open **`Chrisphics Hub.app`** — it copies the
book into `~/Library/Application Support/Chrisphics Hub/` on its first run and the old file stays
untouched as the safety copy. From then on only the new name is used.

- `File` — New Job `⌘N`, New Quote `⇧⌘N`, New Enquiry `⌘I`, New Client `⌘K`,
  Receive Payment `⌘R`, Record Money Out `⌘E`.
- `View` — `⌘1`…`⌘9` and `⌘0` jump between the screens (Collect sits under Print Jobs with no key
  of its own); Reload `⇧⌘R` after changing `public/`;
  **Appearance** picks Light, Dark or "Follow the Mac". Inside the app this is where the look is
  chosen, so the same three buttons on the page footer hide themselves here; they show only in a
  browser tab or on a phone, which have no Settings window.
- `Data` — Back Up Book Now `⌘B`, Show Data File in Finder, Open Backup Folder.
- A job's **Job sheet** opens in its own window. Print Job Sheet `⌘P` goes through the normal
  macOS print dialog (the sheet's own print button is wired to it); **Export Job Sheet as
  PDF** saves it as a file.

### Settings `⌘,`

`Chrisphics Hub ▸ Settings…` (`⌘,`) opens the app's own settings window — four panes in a toolbar,
and the window resizes to whichever pane you are in.

| Pane | What it holds |
| --- | --- |
| **General** | open at login; which screen the app shows first (any of the ten, or "Where I left off"); text size 90–140%; keep this Mac awake while the shop is open; which records engine the window is attached to; where the book and the backups live, with Back up now / Show book in Finder / Open backups |
| **Appearance** | **Light**, **Dark**, **Follow the Mac** — the same three choices as `View ▸ Appearance` and as the buttons on the page footer in a browser or on a phone; change one anywhere and the others re-tick |
| **Profile** | the shop's name, tagline, phone and address, kept *in the book*; the MoMo number, the address devices install from, and the currency are read out but not editable here |
| **Updates** | this build's version, commit and build date, the repository, **Check for update**, and a button to open the repository |

General and Appearance describe one machine, so they live in that Mac's own preferences
(`UserDefaults`) and never in the book. Profile is the opposite: **Save** posts through the shop's
API into the book, so every later job sheet, quote and queued client message is written from those
words, and a phone signing in to the same server sees them too. That is why the server takes
profile writes only from the shop computer itself — and why the pane says so when it is refused.
Clearing a field and saving drops it back to the default `server.py` (or its environment) seeded.
**Check for update** is the only outward call in the window; see [Notes](#notes).

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
| `./run.sh --host 0.0.0.0 --port 8834 --tls-cert /path/shop-cert.pem --tls-key /path/shop-key.pem` | serve the installable app over the shop Wi-Fi with HTTPS; the book needs a shop password first |
| `./run.sh --seed` | add sample clients, jobs, a quote, expenses and enquiries — only into an empty book |
| `python3 server.py --backup` | write one checked, tidied copy into the Backups folder and exit |
| `python3 server.py --allow-sleep` | serve the network without asking the Mac to stay awake |
| `./desktop/build-app.sh` | rebuild `Chrisphics Hub.app` after editing the code |

### Run the shop all the time

One tool makes this Mac the shop's server: it starts when the Mac starts, comes back by
itself if it falls over, keeps the Mac awake while devices are connected, and stays on the
shop's own network.

```sh
tools/shop-server.sh check      # how this Mac looks to the shop — reads only, changes nothing
tools/shop-server.sh certs      # a certificate for this Mac's current Wi-Fi address and name
tools/shop-server.sh ca         # the public certificate file a phone has to be given once
tools/shop-server.sh install    # the service, and the nightly copy of the book
tools/shop-server.sh status     # is it up, what did it last say, when is the next copy
tools/shop-server.sh remove     # stop both, and take back only the two files this tool made
```

`install` refuses until the book has a shop password — choose one on **Shop & devices** on
this Mac first — and it will not put a second server over a book something else is holding:
quit `Chrisphics Hub.app`, or press `Ctrl+C` in the Terminal window running the hand-started
server, then run it again. `install --take-over` stops that server for you after asking once
and typing `yes`.

What it installs is two ordinary launch agents in `~/Library/LaunchAgents/`: the book served
by `/usr/bin/python3 server.py --host 0.0.0.0 --port 8834 --tls-cert … --tls-key …`, restarted
by `launchd` whenever it exits badly, and `tools/nightly-backup.sh` at 22:30 each night.
The server itself keeps a copying clock too, so a book that is overdue gets copied while the
shop is trading rather than waiting for 22:30 — see [Your data](#your-data).
Neither file holds a secret: the password lives in the book as a hash, the private key stays
in `~/Library/Application Support/CRISPprint TLS/shop-key.pem` at mode 600, and the logs land
beside the book as `shop-server.log` and `shop-server.error.log`.

**When the Wi-Fi address moves.** Routers hand out new addresses, and the shop's is one of
them. The server answers on every address the Mac holds, so the shop does not go quiet; the
*certificate* is the part that names one number, so phones show a padlock warning until it is
renewed. `tools/shop-server.sh status` says when that has happened, and `certs` followed by
`install` puts it right in two commands — devices keep trusting the same authority throughout.
Reserving the Mac's address in the router avoids the question altogether.

### One shop password

A phone left on the counter should not be the whole ledger, so every device other than this
Mac signs in. **Shop & devices ▸ The shop password** on the shop computer sets it; the
book stores only a PBKDF2-SHA256 hash (600,000 rounds, salted per password), never the word
itself, so a copied `.db` gives nothing away. A password chosen while the number was 200,000
keeps opening at 200,000 — the rounds travel in the record — and new ones are made at 600,000.

- The shop's own Mac is the machine the records sit on, so it is never asked to sign in —
  unless you tick **Ask this Mac for the password too** on **Security of this book**, which is
  what you want on a Mac the counter staff also use. It can only be ticked once a password
  exists, and switching it off again needs the password itself.
- Choosing the word is checked before it is kept: at least 8 characters, not one of the words a
  guesser starts from, not digits alone, and under 12 characters it has to mix upper case, lower
  case and a digit or symbol. It also refuses the shop's own trading name and a word that just
  repeats the same few characters, because the people at the counter can read both off a job
  sheet. Longer still beats clever: a whole phrase passes easily.
- Each device gets its own session token, stored only as a hash, and stays signed in for
  thirty days by default — **Days a device stays signed in** sets 1 to 90 days. Signing one
  out takes effect the moment it next asks for something, and changing the password signs every
  device out at once, including the one that asked.
- **Signed in on these devices** lists what is holding the book open — a device, a browser, and
  when it last asked for something — with **Sign out** on each row and **Throw out every other
  one** for the lot. Names are kinds, not people; nothing typed is kept.
- Five wrong tries from one address and it is held out for a while, doubling each time (30
  seconds, then 60, 120 …) up to fifteen minutes, answered with `429` and a `Retry-After`. The
  right password is not even looked at while a wait is running. This Mac and devices already
  signed in are never locked out.
- On top of that, one address may make at most 300 API calls in a minute (`429` and
  `Retry-After: 5` past it), so a script cannot lean on the book. `CHRISPHICS_CALLS_PER_MINUTE`
  sets a different ceiling — the Render box uses a lower one, and `tools/check-backups-security.py`
  runs at 20 so the wait can be proved without three hundred requests.
- Every request that asks for the book without a sign-in, every wrong password, every lockout,
  a sign-in, a device thrown out, a password changed, a schedule settled, a cross-origin request,
  a throttled burst and each copy taken — with the address it came from — is written into the
  book's own `security_log`, which keeps the last 400 lines; **Security of this book** shows the
  twelve most recent of them. No password,
  no partial password and no cookie ever goes into that trail. Repeated turn-aways from one
  address are noted once every five minutes rather than every second.
- The settings that change how the book is guarded and copied — the sign-in length, whether this
  Mac is asked, the copy cadence and the second folder, and the password itself — are settled on
  the shop's own Mac or by a signed-in device changing only the password; a phone is told so in
  words rather than being silently ignored. `GET /api/security` and `GET /api/backups` are readable
  from a signed-in device, so it can show the rules it is signing in under.
- A page on another website cannot make the book do things: a request that arrives with an
  `Origin` that is not the shop's own address is refused with `403` and written into the trail.
- `CHRISPHICS_AUTH_PASSWORD` sets the password from the environment instead (that is how the
  Render deployment does it); while it is set, the in-app change is refused so the two cannot
  disagree.
- `--allow-unauthenticated-lan` turns sign-in off for a temporary demo on a private address.
  The screen says so in warning type while it is off. A phone cannot turn it on — only the
  shop's own Mac can, and the attempt is written down. Turn it back on from **Shop & devices**.
- Over `--trust-proxy` (Render terminates HTTPS there) a named address is told to stay on HTTPS
  for six months, cookies carry `Secure`, and the `Server` header says `ChrisphicsHub/1.0`
  instead of this Mac's Python version. A bare IP host gets no HSTS line, because a browser
  ignores it there anyway.

### Put the book on a phone

The shop's address ends in `/setup`: one page with the QR to point a camera at, the
certificate to download, its SHA-256 fingerprint to read out loud, and the trust steps for
the device you are holding. Print it, mail it, or open it from **Shop & devices ▸ Install
page**. Nothing leaves the building — the page is served by the same Mac that holds the book,
and the address only resolves on the shop Wi-Fi.

**Which address to hand over.** The server prints both at start-up, and `tools/shop-server.sh
check` prints them too. This Mac's name — the one `check` shows, ending in `.local` —
is the one worth writing on a card: it survived the router handing this shop a new number on
2026-10-09, when `192.168.100.29` became `192.168.100.43` and every device holding the old
number fell to a padlock warning. Names resolve on Apple devices out of the box; on Windows
and Android use the current number, and renew the certificate when it changes — `certs`
followed by `install`, which keeps the same authority so no device has to be re-trusted.

Trust the certificate once per device (the `/setup` page walks each one through it), then
install from the browser itself:

- **Windows / Edge:** use the browser's **Install this site as an app** command.
- **Android / Chrome:** use **Install app** or **Add to Home screen**.
- **iPhone / iPad:** in Safari, choose **Share → Add to Home Screen**.
- Any device: the app's own **Install app** button opens the browser's prompt where it
  supports one, and shows the steps where it does not.

Under 720px the book becomes a phone app rather than a shrunken desktop. The side rail is
gone; **Desk · Jobs · Money · Clients · More** sit in a tab bar at thumb height, and **More**
opens a sheet holding the remaining screens (spoiled work, pending sync, enquiries, expenses,
reports, Shop & devices) and, at its end, the page footer itself — the book's counts, its last
copy and where it is kept, with the theme switch, signing out and the backup button. A job's
record and every form rise from the bottom of the screen, the first column of a wide ledger
pins itself so a scrolled row is still identifiable, and everything you press is at least 44px
tall. The status strip and the
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

### By hand, or on another machine

`tools/shop-server.sh` is only a wrapper around `server.py` flags, so the same thing runs by
hand wherever Python 3 lives — a shop laptop, a Mac without Homebrew, a start you would
rather type yourself:

```sh
python3 server.py --host 0.0.0.0 --port 8834 \
  --tls-cert "$HOME/Library/Application Support/CRISPprint TLS/shop-cert.pem" \
  --tls-key  "$HOME/Library/Application Support/CRISPprint TLS/shop-key.pem" \
  --no-browser
```

It refuses to serve an address the whole network can reach while the book has no password,
and it refuses HTTPS without both certificate files. The certificate has to name the address
devices type, which is why a local authority signs it:

```sh
brew install mkcert && mkcert -install
TLS="$HOME/Library/Application Support/CRISPprint TLS"; mkdir -p "$TLS"
mkcert -cert-file "$TLS/shop-cert.pem" -key-file "$TLS/shop-key.pem" \
  "$(ipconfig getifaddr en0)" "$(scutil --get LocalHostName).local" localhost 127.0.0.1 ::1
chmod 600 "$TLS/shop-key.pem"
```

`tools/shop-server.sh certs` is exactly that, and `ca` writes the public half to
`"$TLS/device-download/shop-root-ca.cer"` — which the `/setup` page then serves itself at
`/shop-root-ca.cer` on the HTTPS port. There is no second server on a second port any more.

**A plain-HTTP address opens the book but installs nothing**: a browser will only let a device
keep an app offline over HTTPS. `/setup` says so on an HTTP address instead of sending a phone
down a road that cannot work. `--allow-unauthenticated-lan` drops the password as well, on a
private address only, which leaves the whole book open to anyone who joins that Wi-Fi; treat it
as a demo switch. Do not forward the port to the internet either: a bare Mac with a hole punched
in the router has no HTTPS and no password in front of the accounts. If the book should be
reachable away from the shop, [host it](#host-on-render) instead.

### Host on Render

The shop's hosted book answers at **`https://chrisphicshub.onrender.com`**, and the service in
`render.yaml` is named `ChrisphicsHub` so it matches. A blueprint that invents a second name would
stand up a second, empty ledger beside the shop's.

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

The hosted book starts empty until you carry yours into it, and it never reaches back into the
records on the shop computer by itself.
Configure WhatsApp and email provider secrets separately in the Render service environment
if automatic customer messages are wanted. New clients start opted in on both channels, which
their record can turn off; without provider settings the messages queue and wait for staff to
carry them from the job screen instead.

### Carry the shop's records into the hosted book

```sh
tools/carry-book.py https://chrisphicshub.onrender.com            # the newest checked copy
tools/carry-book.py https://chrisphicshub.onrender.com my-book.db # a copy you name
```

It says out loud what the copy holds and how many of each record is in it, asks for the shop
password at the terminal — never on the command line, so it stays out of the shell's history —
and hands the file to the hosted server. The far side says no unless its own book holds
no records at all, so a carry-over can never overwrite work that has already started there; a
copy that fails its own integrity check, or that is not a Chrisphics book, is refused before
anything is written. Devices signed in on the shop Wi-Fi do not travel with the book — every
screen has to be told the password again, which is what you want once the address is public.

Then say which book is the live one, out loud and in writing. Two ledgers that are both being
written to will disagree within a day, and nothing joins them back together:

1. Carry the records over, and open the hosted address on the counter's phone to see them.
2. Work in the hosted book from that moment — the address, on every device including this Mac.
3. Keep the Mac's own copy as the shop's independent backup: stop entering records into it, and
   let `tools/nightly-backup.sh` go on copying it each night. A copy that is no longer being
   written to is exactly what a backup should be.

The always-on Wi-Fi server and the hosted book are alternatives, not partners: leave the
LaunchAgents installed only while the shop's records live on this Mac
(`tools/shop-server.sh remove` takes them back).

## The eleven screens

**Dashboard** — money in today / this week / month to date, what customers still owe,
what is late, what is ready for pickup, a 15-day cash chart, and what you print most.
Above the tiles sits **Money in and out by MoMo**: wallet alerts the shop did not type, read
out of Messages or pasted in, waiting to be booked ([see below](#money-in-and-out-by-momo)).
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

**Collect** — the counter's screen, with one box big enough to read a job number into from the
sheet or the WhatsApp message. It is forgiving about how the number is typed: `CH-2026-0001`,
`ch20260001` and `2026-0001` all land on the same job. One card then answers the only question
that matters while the client stands there — whose it is, what was billed, what has been paid —
and either offers **Hand it over**, or says plainly why the box cannot leave yet: *₵180.00 still
owed*, *Still on the press*, *Already handed over on 08 Oct 2026*, *This order was cancelled*.
Under a refusal sit the only two things that clear it, **Take ₵180.00 now** and **Move it to
Ready**, which open the same payment form and stage write the job record uses; nothing at all is
written until you press something. Handing over is exactly the drawer's Delivered step — the time
is stamped into the job's history and the Delivered message to the client queues with the rest.
With the box empty the screen lists what is already both ready and paid, so the shelf of work
waiting for someone to collect it is one click away on a phone as much as on the counter.

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
job. The record belongs to the job; the loss belongs to the shop. Spoilage never enters that
job's cost and never reduces its profit, and it never touches what the client pays — but it does
still count as money out in the month-by-month table, because the paper was really bought and
really was ruined. The client is never told about it either: a spoiled row is left off the
printed job sheet's **Costs booked against this job** table, and out of every queued message.
Entries can be edited or removed from this screen. The same record sits inside
the job it belongs to: open a job and
**Spoiled on this job** lists every incident on that order with its cost, and **+ Log spoiled
work** there opens the form with the job already chosen — so a batch that goes wrong is
recorded against the job, never against the client's price.

**Pending sync** — shows offline changes saved on this device, whether they are waiting,
synced or need conflict review.

**Reports** — billed, what it cost, profit, collected, still owed and delivered for any
date range, grouped by day, week or month; broken down by service, by payment method, by
client and by expense category. Every table exports to CSV for Excel or the accountant.

**Shop & devices** — the book's own back office, and the only screen about the shop rather
than its work. Four figures along the top: the address devices use, whether a password is
required, how many devices hold the book open, and how many copies exist. Below them: **Put
the book on a device** (the `/setup` install page), **Kept awake for the shop**, **Signed in
on these devices** — each with its browser, the day it signed in and when it was last heard
from, and a button that throws it out — **The shop password** (choose, change, or turn
sign-in off, all from this Mac only), **Copies of the book**, which says when the last copy
was made, whether it was checked against the original, where the second folder got to, how
many are kept, how much room is left and when the next one is due — and lets the shop set the
cadence, the nights kept and the second folder, or copy it now; and **Security of this book**,
which states the rules a device is signing in under (rounds, days signed in, this Mac asked or
not, HTTPS, the wrong-password wait and the per-minute ceiling) and lists the last forty
things that happened to the book's guard, twelve shown at a time: sign-ins, turn-aways,
lockouts, devices thrown out, password changes, cross-origin refusals and every copy — the
book keeps four hundred of those lines, and no password, no partial password and no cookie
ever goes into one. The last two cards can be *read* by a
signed-in phone but only changed on the shop's own Mac, which says so plainly rather than
greying itself out. While no password is set the screen opens with a warning: the book is open
to anything on the Wi-Fi.

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

## Money in and out by MoMo

Clients pay into **0506399641**. When a payment alert arrives as an SMS in **Messages** on
this Mac, the dashboard should already know about it — the *MoMo money in and out* card at the
top of the Dashboard is how that happens.

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
pick the job. **Money going out** (`you sent`, `sent to`, `paid to`, `withdraw`, `debited`…)
is flagged and never booked as a payment in — it has its own recording path below. Airtime
messages, prizes and promotions are dropped and never appear as a notice at all.

Press **Book it** and the money enters the book like any other payment: `MoMo` as the method,
**the client's own name as the transaction reference**, the amount, the job you chose (or left
as *Kept on their account (credit)* when nothing is owed), and the raw alert kept in the note
as the proof. The collected and owed figures on the Dashboard move at once, and while the
Dashboard is open the card refreshes on its own every twenty seconds, so an alert that arrives
while you are working shows itself without a click.

**A send out belongs in the book as well.** Money leaving the wallet is either the shop paying
for something or money handed back to a client, and only the person at the counter knows which
— so a send never books itself, whatever **Book without asking** is set to. The row asks
**Who this went to**, and the two answers are recorded differently:

* choose the client and **Record send** writes a **Refund** on their account. The amount comes
  off what they had paid, the job's balance grows back, and the job timeline says
  *refunded to the client* — never *received*;
* leave it on **Nobody in the book — money out**, choose a **Money out for** category, and it
  enters the Expenses ledger against the payee named in the alert, with the raw text kept as
  the proof.

The panel counts sends separately from money in, so the two never add up to the wrong total.
Delete that payment or expense later and the notice comes back to the waiting list with a line
saying why, instead of vanishing from both places at once.

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

When a job is first booked and when its status changes to **Pending**, **Printing**, **Ready**
or **Delivered**, the shop server automatically writes an update for each channel the client
has agreed to use. **Quotes and Cancelled** messages remain available as drafts for staff to
open and send, because those are the shop's judgement rather than a stage the client is
waiting on. Both channels are optional. A client added from now on is opted in on both, and
the first launch of this build opts in every client already in the book once — untick the
matching permission box on a client's record to stop one channel, which also calls off
anything still queued on it.

An automatic message whose channel the client has not agreed to is never written, and the job
screen says which channel was skipped and why. A client with no WhatsApp number or no email on
file cannot be reached on that channel either; the job screen shows the gap and links to their
record so it can be filled in.

Automatic messages are written to the job's durable queue first. If the provider is
unavailable or the shop server has no internet, it retries later; after eight unsuccessful
attempts the message is marked failed and the job screen offers **Retry now**. The queue and
provider error remain visible for review. Successful deliveries appear in the job timeline.
Provider acceptance is recorded, but email/WhatsApp cannot guarantee that the recipient's
device displayed or read a message.

**Until a provider is set up, the shop carries the news itself and the book keeps the record.**
A queued message with nothing behind it still shows **Open in WhatsApp** or **Open in Mail**,
**Copy** and **Mark as sent** on the job screen, and the card names the exact settings that are
missing. Marking one sent closes it and writes *update carried by the shop* into the job
timeline, and the message is not sent a second time if a provider is configured afterwards —
press **Back to queued** on it if you want the server to take it from there. A message the
client asked not to receive is called off and gets no buttons at all.

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
the server never reports them as sent, and the job screen hands them to the shop to send by
hand instead.

Email drafts and messages include the job reference, work, total, due date and balance. Once
WhatsApp is configured, automatic updates use the approved short status template above; until
then the queued message carries the shop's full wording, because it leaves through the shop's
own WhatsApp. Either way the words are written before you press anything, and **Copy** always
gives you the exact text in the book.

Set `CHRISPHICS_SHOP_PHONE` (and optionally `CHRISPHICS_SHOP_NAME`, `CHRISPHICS_SHOP_TAGLINE`,
`CHRISPHICS_SHOP_ADDRESS`) in the same environment: the number is what the client is told to
call. With no number set, the message simply leaves that line out rather than hand the client a
placeholder that reaches nobody.

There is one message per stage per channel, so retrying or repeating a status does not
double-send it. If job details change before a queued notice is sent, the message is refreshed
and returned to the queue. A message already accepted by a provider keeps its sent wording.

## Your data

Everything lives in one file: **`~/Library/Application Support/Chrisphics Hub/chrisphics.db`**.
The page footer — the slim strip under every screen, below the work rather than pinned over it —
says how much is in the book, when it was last copied and where it lives; `Data ▸ Show Data File in
Finder` reveals the file itself.
Both the app and the browser launcher use this same file, so rebuilding or moving the `.app`
never touches your records.

- **Back up**: `Data ▸ Back Up Book Now` (⌘B), or *Download data backup* on the page footer, or
  **Shop & devices ▸ Copies of the book ▸ Copy it now**. Each one writes a complete,
  self-contained copy — one file, nothing else needed to restore it — into
  `~/Library/Application Support/Chrisphics Hub/Backups/` and is named `manual-<day>-<time>.db`
  because you asked for it. The ⌘B and footer routes also hand the file to you as a download
  (so you can put one on a USB stick or in Drive), and that is written into the book's security
  trail as a copy that left the Mac. `python3 server.py --backup` from a terminal is the same act
  named `backup-<day>-<time>.db`.
- **Copied by itself, while the server runs**: the always-on server keeps its own clock. Every
  five minutes it looks at the schedule and, when a copy is due, makes one, opens it again, puts it
  through `PRAGMA integrity_check`, counts it against the book table by table (jobs, clients,
  payments, expenses, messages) and only then writes the time down — so a silently truncated copy
  is recorded as a failure rather than a quiet night. **Copies of the book** is where the shop
  rules that: **Copy the book by itself while the server is running** switches it on and off,
  **Every (hours)** sets 1–168 hours (24 by default), **Keep night copies for (days)** sets 3–120
  nights (14 by default), and **A second folder (optional)** mirrors every copy somewhere else —
  an external disk, a network folder. Save the form and the clock is looked at again straight away;
  a book that was overdue a day gets its copy the moment you say so, not when the old gap ends.
  Retention is capped before a short cadence could fill the disk: inside the nights you keep, every
  copy except more than twelve from one day; past them, one copy for each of the last twelve
  months; and the twelve most recent copies asked for by hand, always. The card lists the newest
  copies with their size and says which were made by itself and
  which were asked for, the folder's free space, when the next one is due, and whether the second
  folder actually took it. `CHRISPHICS_BACKUP_CHECK` (seconds) sets how often the clock is looked
  at; `CHRISPHICS_BACKUP_DIR` moves the folder, which is how the Render box puts copies on its
  persistent disk.
- **Backed up by the calendar too**: with the always-on server installed, `tools/nightly-backup.sh`
  still runs at 22:30 every night and does the same thing through the same checks. It is the second
  way a copy happens, so a server that was never running in the daytime still gets its night copy —
  and if either path misses, the page footer turns red and **Copies of the book** says when the
  last one really was. The server's own clock needs the server running; the calendar job does not
  ask permission of anything.
  A copy on this Mac is not a backup of the shop: carry one off to a USB stick, Google Drive or
  Time Machine now and then — or set the second folder and let the copying do it.
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

The phone in that block is empty and the address (`Accra, Ghana`) is still a placeholder. Both
print at the top of every job sheet, and the client messages sign off with them, so they land in
front of your customers either way. You do not have to edit the program for any of them: set the
name, tagline, phone and address in **`Chrisphics Hub ▸ Settings… ▸ Profile`** and **Save**, and
they are kept in the book — the server lays them over these defaults on every start-up, and every
later sheet, quote and client message is written from them. Clearing one there and saving falls
back to the default below. For a server you cannot open Settings on, set them in its environment
instead — `CHRISPHICS_SHOP_PHONE`, `CHRISPHICS_SHOP_ADDRESS`, `CHRISPHICS_SHOP_NAME`,
`CHRISPHICS_SHOP_TAGLINE` — and restart; those are what a book with nothing saved in it falls back
to, and a value already saved in the book wins over them. Until a number is set, messages
simply leave the "Call or WhatsApp…" line out; a customer is never handed a number that reaches
nobody.

The `.app` carries its own copy of the program, so after editing anything run
`./desktop/build-app.sh` and reopen the app. That only rebuilds the program — your data
file is never touched. If the app is already open, `View ▸ Reload` is enough for changes
under `public/`.

**Colour and appearance.** The palette comes off the logo itself: crimson `#AB1F23` and the
grey `#808085` it pairs with, measured from the artwork rather than eyeballed. Both are
darkened for text (`--brand-ink`, `--link`) so they hold up as type on paper, and the mark's
own inks (`#C93A3F` / `#9A9AA0`) are what the Dock icon and the tab icon are drawn with.

There are three appearances — **Light**, **Dark** and **System**. In the Mac app pick one under
`View ▸ Appearance` or in **Settings ▸ Appearance**, and the footer's own three buttons stay out of
the way; in a browser tab or on a phone, where there is no Settings window, they sit at the end of
the page footer. The choice is remembered on this Mac, and System follows the
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

Five dev checks live in `tools/`, and none of them needs anything installed:

| Command | What it answers |
| --- | --- |
| `python3 tools/js-check.py` | does `public/app.js` still parse? (one stray bracket empties every screen) |
| `python3 tools/check-profile-settings.py` | can the shop's own details be saved and read back out of the book, do bad ones get refused, and does a cleared one fall back to its default? Runs against a **copy** of the book on a throwaway port. |
| `python3 tools/check-backups-security.py` | does the book get copied by itself, checked, mirrored, retained and reported; and is it guarded — password rules, salted 600,000-round hash, session length, devices thrown out, wrong-password waits, the per-minute ceiling, cross-origin refusals, the security trail, HTTPS headers? Runs four throwaway servers on ports 8897–8899 against a **copy** of the book, in its own backup folders, and closes by proving no job, client, payment, expense or message moved and that the shop's own file was not written to. |
| `swift tools/make-qr.swift "<address>" /tmp/shop-qr.png 520` | the counter's QR as a printable PNG — and every code is read back before the file is trusted (`--verify <file.png>` checks one already drawn) |
| `swiftc -O tools/wk-probe.swift -o /tmp/wk-probe` then `/tmp/wk-probe <url> <dir> "open\|/#/jobs" "shot\|jobs"` | what a screen actually rendered, in the same WebKit the app uses, as text plus a PNG |

## Notes

- The desktop app and `Chrisphics Hub.command` listen on `127.0.0.1` only — the app picks its
  own free port each time it starts, the launcher uses 8712 — so nothing outside this Mac can
  reach either, and two copies never collide. Only the always-on shop server opens the book to
  the Wi-Fi, and it does so behind the shop password.
- Data is never sent anywhere. There is no analytics and no cloud. Devices on the shop Wi-Fi
  sign in to this Mac with one password; nothing reaches an outside account, and no account
  reaches in. The app looks outward only where you start it: pressing **WhatsApp** or **Email**
  on the **Tell the client** card asks macOS to open `wa.me` or your mail app with the words
  already typed, and pressing **Check for update** in `Settings ▸ Updates` asks GitHub for the
  newest commit in the shop's own repository and compares that hash with the stamp baked into
  this build — a plain read of a public repository, with no shop name, no records and no sign-in
  detail in it. Nothing else is sent, uploaded or contacted while you work, and the app will not
  load any outside page inside its own window.
- Job sheets print black on white for a clean photocopy.
- Run one engine at a time. The app looks for the always-on shop server before it starts anything:
  if something answers on `https://127.0.0.1:8834/healthz`, the app opens *that* server instead of
  launching its own, so the app and the service can both be left running and every device sees the
  same book. `Chrisphics Hub.command` still starts its own engine, so close the app before running
  it beside the service. `tools/shop-server.sh` refuses to install over a book the app is holding.
- The `.app` is built and signed for this Mac. If a copy on another Mac says it is from an
  unidentified developer, right-click it and choose Open once.
- Rebuilding needs the free Xcode Command Line Tools (already here, for `swiftc`); running it
  needs nothing but macOS and the Python 3 that ships with it.
