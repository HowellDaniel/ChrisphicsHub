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
| `tools/` | `shop-boot.sh` keeps the shop online on whatever address the Wi-Fi gives this Mac, `shop-server.sh` installs that service and its certificates, `nightly-backup.sh` copies the book each night, `carry-book.py` hands a copy to a hosted book, `make-qr.swift` and `make-web-icons.swift` draw the QR and the install icons; the rest are dev checks |
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
  of its own); Reload `⇧⌘R` after changing `public/`. There is no **Appearance** menu and no look to
  pick: the shop's clock decides it, a machine that slept through 5:00pm corrects itself, and the
  app's own window chrome turns over with the page.
- `Data` — Back Up Book Now `⌘B`, Show Data File in Finder, Open Backup Folder.
- A job's **Job sheet** opens in its own window. Print Job Sheet `⌘P` goes through the normal
  macOS print dialog (the sheet's own print button is wired to it); **Export Job Sheet as
  PDF** saves it as a file.

### Settings `⌘,`

`Chrisphics Hub ▸ Settings…` (`⌘,`) opens the app's own settings window — three panes in a toolbar,
and the window resizes to whichever pane you are in.

| Pane | What it holds |
| --- | --- |
| **General** | open at login; which screen the app shows first (any of the ten, or "Where I left off"); text size 90–140%; keep this Mac awake while the shop is open; which records engine the window is attached to; where the book and the backups live, with Back up now / Show book in Finder / Open backups |
| **Profile** | the shop's name, tagline, phone and address, kept *in the book*; the MoMo number, the address devices install from, and the currency are read out but not editable here |
| **Updates** | this build's version, commit and build date, the repository, **Check for update**, and a button to open the repository |

General describes one machine, so it lives in that Mac's own preferences
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
| `./run.sh --host 0.0.0.0 --port 8834 --tls-cert /path/shop-cert.pem --tls-key /path/shop-key.pem` | serve the installable app over the shop Wi-Fi with HTTPS; the book needs a shop password first, unless the shop deliberately runs open — see [The shape the shop runs today](#run-the-shop-all-the-time) |
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
tools/shop-server.sh wifi-ip    # the address this Mac holds on the shop network (the keeper asks here)
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
by `tools/shop-boot.sh` (below), and `tools/nightly-backup.sh` at 22:30 each night.
The server itself keeps a copying clock too, so a book that is overdue gets copied while the
shop is trading rather than waiting for 22:30 — see [Your data](#your-data).
Neither file holds a secret: the password lives in the book as a hash, the private key stays
in `~/Library/Application Support/CRISPprint TLS/shop-key.pem` at mode 600, and the logs land
beside the book as `shop-server.log` and `shop-server.error.log`.

Every line in `shop-server.error.log` starts with the time and then the **address of the machine
that asked**, so when the counter wants to know who came calling — `19:11:47  192.168.100.14
"GET /api/momo HTTP/1.1" 200 -` — it is there. Only the address is written: no machine code, no
sign-in word, nothing a reader of that file could use to become someone else.

**The keeper: `tools/shop-boot.sh`.** This is what makes the counter open by itself when the Mac
is switched on. `server.py` has to be told which address to hold, and the router decides that
number, so a service handed a fixed one either dies when the Wi-Fi has not joined yet at login or
— worse — keeps running bound to an address this Mac no longer holds, which looks perfectly
healthy to `launchd` while every device in the shop finds nothing. The keeper is one small
supervisor in front of the engine: it reads the address from `tools/shop-server.sh wifi-ip` (one
rule, kept in one place), waits out a Wi-Fi that has not arrived, issues a leaf that names the
number the devices dial, starts the engine on it, and re-reads the address every fifteen seconds
so a move at 3am is a re-bind and a fresh certificate rather than a dark counter. It starts at
login, again every five minutes if it is not running, and whenever it dies badly; a stop is passed
straight to the engine so nothing orphans on the port; and a second keeper seeing the shop already
answering exits without starting a second engine over one book. It asks the kernel who holds the
port (`lsof -sTCP:LISTEN`) rather than dialling it — a connection to a number nothing answers on
only lets go when macOS decides, tens of seconds later, and a keeper stuck mid-probe is a counter
that stays dark and cannot even be stopped.

**The shape the shop runs today.** Since 2026-10-14 the service on this Mac is *not* the one above:
the owner was shown what an open Wi-Fi means and answered **"Open the Wi-Fi, no word"**, so the
LaunchAgent holds `--allow-unauthenticated-lan` and no `--host` at all — the keeper supplies the
address — and the log says `Sign-in: not required (this run was told to skip it)`. Two consequences
are live, not hypothetical: anyone who joins that Wi-Fi reads the whole accounts without a password
(see [One shop password](#one-shop-password) for the door that is standing open); and the engine
holds one address at a time, so `127.0.0.1:8834` is closed and `Chrisphics Hub.app` cannot attach to
it and starts its own engine beside it — which is why the app is quit, or rebuilt, while this shape
runs. `--allow-unauthenticated-lan` also demands a private, non-loopback IPv4, so the keeper will not
start the engine until this Mac genuinely has a shop address to bind.

**When the Wi-Fi address moves.** Routers hand out new addresses, and the shop's is one of
them. The keeper now takes that by itself: it notices the number it was holding is gone, lets go of
the stale bind, asks `tools/shop-server.sh certs` for a leaf naming the new one, and serves again
without anyone being told. With a password and `0.0.0.0` the bind never names a number, so only the
certificate is the thing to renew; on the no-word shape both move together, and the keeper does
both. `tools/shop-server.sh status` still says what is running, and `certs` followed by
`install` remains the manual cure if the keeper is ever missing. Reserving the Mac's address in the
router still avoids the question, and the *devices*' bookmarks type the number, so a moved address
means the counter reprints its QR and tells phones the new one.

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
the device you are holding. The page reads the device's own browser and puts that device's
steps first, with the other kinds folded underneath, so an Android phone is never told to
open Keychain Access. Print it, mail it, or open it from **Shop & devices ▸ Install
page**. Nothing leaves the building — the page is served by the same Mac that holds the book,
and the address only resolves on the shop Wi-Fi.

**Android and Windows trust it differently, and both look broken.** An iPhone completes the
path from the root it already has, so it was happy being handed the leaf certificate alone.
Chrome on Android and Edge on Windows won't: the authority has to travel with the leaf, which
is why `certs` now appends the root to `shop-cert.pem` and the server answers with both. Their
certificate installer also reads the *file name* and opens only `.0`, `.pem`, `.crt` or
`.cacert.cer` — so the same bytes are handed out under three addresses, and `/setup` links the
one each device will accept:

| device | file it is given | why |
| --- | --- | --- |
| Android | `/CRISPprint-Shop-Root-CA.cacert.cer` | its installer refuses a plain `.cer` name |
| iPhone, Windows, Mac | `/shop-root-ca.cer` | the one a device can name and tap |
| anywhere with a text editor | `/shop-root-ca.pem` | same authority, plain base64 |

On Windows the steps say **Current User**, not the local machine store — that path needs no
administrator password, and Edge and Chrome both honour it. Re-running `certs` for a new
address keeps the same authority, so a device that already trusts the shop keeps trusting it;
only the file on the leaf changes.

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
copy and where it is kept, with signing out and the backup button. A job's
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
`"$TLS/device-download/"` as both `shop-root-ca.cer` and `shop-root-ca.pem`, plus a copy named
`CRISPprint-Shop-Root-CA.cacert.cer` for Android's installer, and the addresses the leaf
answers to in `valid-for.txt` — which the `/setup` page then serves itself at
`/shop-root-ca.cer` and friends on the HTTPS port. There is no second server on a second port
any more. `certs` also chains the root onto `shop-cert.pem` so Android and Windows can build a
path, and records the addresses the new leaf answers to, so `/setup` can name the mismatch out
loud when a device dials a number the certificate does not cover instead of leaving whoever is
holding it with a padlock warning and no explanation.

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
The first thing on it is the shop's own **day**: the month laid out the way an iPhone's medium
calendar widget lays it out, beside a clock the size of a phone's lock screen, the date in full
underneath, the time running to the second, and the hour the look
turns over — see [Colour and appearance](#colour-and-appearance).
Below the tiles: quotes still waiting for an answer, enquiries due for a chase, and where the
month's money went.
The *MoMo money in and out* card that used to sit above the tiles is gone — the owner asked for it
off the Dashboard on 2026-10-15 ([see below](#money-in-and-out-by-momo)).

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
The **Profit on jobs** card at the top deliberately shows a dash instead of a sum: the
counter asked for the figure off that card, because a job's cost is typed in by hand and he
does not accept a profit total built from it as a headline. Nothing else moved — the Profit
columns in every table below still do the arithmetic, job by job, and the same is true of
the dashboard's own profit card, so the number is one scroll away for anyone who wants it.

**Shop & devices** — the book's own back office, and the only screen about the shop rather
than its work. Four figures along the top: the address devices use, whether a password is
required, how many devices hold the book open, and how many copies exist. Below them: **Put
the book on a device** (the `/setup` install page), **Kept awake for the shop**, **Signed in
on these devices** — each with its browser, the day it signed in and when it was last heard
from, and a button that throws it out — **The shop password** (choose, change, or turn
sign-in off, all from this Mac only), **Copies of the book**, which says when the last copy
was made, whether it was checked against the original, where the second folder got to, how
many are kept, how much room is left and when the next one is due — and lets the shop set the
cadence, the nights kept and the second folder, or copy it now; **Email accounts**, which lists
the mailboxes the shop sends client notices from, their server, when one last sent and what the
mail server said the last time it refused, and lets this Mac add one, change one, forget one,
choose which one sends, and press **Test this account** to hand one real letter to that server;
and **Security of this book**,
which states the rules a device is signing in under (rounds, days signed in, this Mac asked or
not, HTTPS, the wrong-password wait and the per-minute ceiling) and lists the last forty
things that happened to the book's guard, twelve shown at a time: sign-ins, turn-aways,
lockouts, devices thrown out, password changes, cross-origin refusals, mailboxes added, chosen,
tested and forgotten, and every copy — the
book keeps four hundred of those lines, and no password, no partial password, no mail account
word and no cookie ever goes into one. Those last three cards can be *read* by a
signed-in phone but only changed on the shop's own Mac, which says so plainly rather than
greying itself out. *The shop's own Mac* is this machine by either of its numbers: loopback, and
the Wi-Fi address the counter's own browser and the app reach the book at — a call from
`192.168.100.11` to `192.168.100.11` is this Mac talking to itself, and a device out on the
network arrives as its own address, which can never equal it. While no password is set the screen opens with a warning: the book is open
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

That credit is why **Owed to the shop** and **Owed on jobs** are not the sum of the job balances:
each client's held credit is set against their own open jobs, biggest first, and the figure is
floored at zero per client — one client's overpayment never hides another client's debt. The
Outstanding table shows the arithmetic job by job, and its rows add back up to the headline.
Because a page copied into `public/` goes live without a restart, the screen can be a build ahead
of the server answering it; when that server has no netting to report, the page says plain
"job balances" and asks for each job's own balance rather than calling anything covered.
`tools/check-owed-net.py` renders the screen against both builds to keep that honest.

Profit is only as good as the costs you enter. A job with nothing costed shows its whole
price as profit in pale type, with the margin reading **not costed** — record the item
costs or an expense and it turns into a real figure.

Every change is written to the job's record: status moves, edits (shown as
`Unit price: ₵ 45.00 -> ₵ 50.00`), notes and payments. Nothing is silently overwritten.

## Taking an order at the counter

The book is used one-handed, with a client standing at the desk, so the order screen is written for
that minute rather than for a form. Twelve hundred flyers at twenty-five pese is typed the way it is
said out loud — `1,200` and `₵0.25` — and it books **₵ 300.00**. Every money and quantity box takes a
comma, a space or the `₵` sign, settles to plain digits the moment he leaves it, and stores plain
digits; nothing is kept in the book with punctuation in it.

- **The caret starts in a box you can type into.** In the client box when an order opens, in the
  **amount** box when the money sheet opens. The first keystroke is never lost to a heading.
- **Enter steps through the ticket instead of saving it.** From the price box it moves to the next
  cell; from the last cell it opens another line. The order is booked by **Book it** or by
  ⌘/Ctrl+Enter — and a booking that cannot be made stays open with the reason written on it.
- **A pasted list lays itself out.** Product, specification, quantity and price, separated by tabs or
  newlines, become one line each: the way a list arrives over WhatsApp.
- **A name that matches one client picks them; a name that matches nobody puts them on file.** Enter
  opens the new-client box with the typing already carried into it. Four walk-ins typed at this desk
  are already sitting in the book as four separate clients, so the search answers to one.
- **The cash belongs on the order screen.** A box under the total, a chip that fills it to the whole
  figure, and one line saying which it became. Money that covers the job is recorded as a **Payment**
  and the balance reads `₵ 0.00`, so the accounts never claim a paid client still owes; money short of
  the total is a **Deposit** and leaves the rest owing.
- **A settled job still takes money.** Any figure handed over after it was cleared is a Payment that
  sits as credit on that job, shown as a negative balance — an advance on the next run, a second
  instalment, money handed back. The money screen lists **every** job and choosing a person narrows it
  to theirs, so a paid ticket is never a dead end.
- **Yesterday's ticket is tomorrow's order in one press.** **Same order again** on the job sheet opens
  a fresh booking with the client, the lines, the quantities and the prices already written, leaving
  the date, the money and the status for him to set. It books a new row and the old one is untouched.
- **A figure the book refuses comes back typed.** Click-to-change on the dashboard and the enquiry
  list writes straight to the book; if the record moved on another device, or is gone, the typing is
  put back in the field instead of wiped, and the page takes the record's current state so the next
  attempt lands rather than being refused again.

On the phone the ticket stops pretending to be a spreadsheet: each line becomes a small card with its
own field names printed above the boxes, every input stays at 16px so the browser does not zoom away
from the rest of the order while he types, and nothing on the screen has to be scrolled sideways.

## Money in and out by MoMo

Clients pay into **0506399641**. When a payment alert arrives as an SMS in **Messages** on
this Mac, the shop's own Python reads it — but since 2026-10-15 nothing on the Dashboard shows
it: the owner's word was *"remover the momo in and out on the dashboard"*, and the *MoMo money in
and out* card, its buttons and its styling came off `public/app.js` and `public/styles.css` with
it. The reading, the storage and the money-marking below all still work; only the screen that
presented them is gone. Money that came through the number now enters the book the way any other
payment does — **at the Collect screen or on the client's page, with `MoMo` chosen as the method**.

The reader itself is a server-side poller, not part of the card. While `momo_watch` is on — and it
defaults to on — every twenty seconds it opens `~/Library/Messages/chat.db` **read-only**, never
writes to it, and never sends anything, and stores what it recognises in `money_signals`. macOS
protects that file, so the engine that runs it needs the door:
*System Settings ▸ Privacy & Security ▸ Full Disk Access*, aimed at whichever program is doing the
reading — the Mac app and the Wi-Fi server that starts itself at boot are two different programs to
macOS, and granting one does not grant the other. Either one reading is enough, and the one that
cannot get in keeps out of the way: while any read is under ninety seconds old — the shared
`momo_read_ok` stamp, or a blank refusal with a fresh poll behind it, which is all an app built
before that stamp existed leaves — the shut engine writes nothing at all, so it cannot stamp its
refusal over the working reader, and equally cannot renew that proof with its own silence once the
reader has gone home. `tools/check-momo-door.py` proves both halves.

**The consequence of the card being gone**: notices the reader stores have no screen any more, so
they sit in `money_signals` as `Unreviewed` and nothing can be booked from them. Turning the reader
off is a one-line write to `app_state` (`momo_watch` = `0`) on the shop's own Mac, and it needs the
same care as any book write — snapshot the file first. **Both were done on 2026-10-15**: this shop's
book now carries `momo_watch='0'`, so no engine reads Messages at all, and the 43 stored notices (all
MTN and Telecel housekeeping, none a client paying in) were deleted after a `.backup` of the book.
The card is recoverable from git: it lived in `public/app.js` before this pass, and the
`POST /api/momo` endpoints it used are untouched.

**The book is the authority for what came through the number.** Whatever the reader does, every
payment recorded with **MoMo** as its method is summed by `momo_ledger()` — this month and all the
time — and the clients who sent it are listed with their totals and last payment day; that payload
still answers on `GET /api/momo` even though no card reads it. Marking a payment MoMo changes its
label and nothing else: the day's, week's and month's takings read the same whichever method is
ticked, which is the half that `tools/check-momo-ledger.py` is strictest about.

What the reader takes from a text:

```
amount        the figure the alert carries, in cedis
payer         the name the alert gives, and their number
direction     money coming in, or money going out of the wallet
```

Number and name are what tie an alert to a client: the payer's number is matched against each
client's phone and WhatsApp (normalised to the same 233 form), and if no number matches, the
name is tried. That is as far as it goes — nothing is guessed, and an alert that cannot be placed
is stored with the reason written out in its row. **Money going out** (`you sent`, `sent to`,
`paid to`, `withdraw`, `debited`…) is flagged `Out` and never booked as a payment in. Airtime
messages, prizes and promotions are dropped and never appear as a notice at all.

Booking an alert used to be one press on the card: `MoMo` as the method, **the client's own name as
the transaction reference**, the amount, the job chosen (or none, leaving it as credit on their
account), and the raw alert kept in the note as the proof. That press is gone with the card; the
same entry now happens at the counter — a payment added on the Collect screen or the client's page,
method `MoMo`, the client's name in the reference box, the alert text in the note.

**A send out belongs in the book as well, and it has no screen either.** Money leaving the wallet is
either the shop paying for something or money handed back to a client, and only the person at the
counter knows which, so `POST /api/momo/<id>/send` never fires on its own — an `Out` notice waits.
The two answers the card used to ask for are recorded differently:

* against a client it is a **Refund** on their account: the amount comes off what they had paid,
  the job's balance grows back, and the job timeline says *refunded to the client* — never
  *received*;
* with no client chosen it enters the Expenses ledger against the payee named in the alert, with
  the raw text kept as the proof.

Sends are counted separately from money in the payload (`sent` / `sent_total` against
`booked` / `booked_total`), so the two never add up to the wrong total. Delete that payment or
expense later and the notice returns to `Unreviewed` with a line saying why, instead of vanishing
from both places at once.

**Book without asking** (`momo_auto`, off by default) is the one path that still writes money with
nobody pressing anything: an alert whose amount, direction and client all read cleanly books itself
the moment it is seen. The card's confirmation dialog went with the card, so switching it on is now
a write to `app_state` on the shop's own Mac — and it should stay off unless the reading below has
been checked against a real alert, because it puts money in the book with no human between the SMS
and the ledger. Anything it cannot place with certainty still waits.

Only texts that actually read as money news are stored, and only in the `money_signals` table
of the same SQLite file, next to everything else in the book. Nothing is uploaded, and the
shop's wallet number (the `MOMO_NUMBER` at the top of the MoMo block in `server.py`) is
excluded when the reader looks for the payer, so your own number is never mistaken for a
client's.

Alert wording differs by network and changes over time. Before trusting **Book without
asking**, run `poll_messages()` from the shop's own server code in a throwaway copy of the book and
check the amount, the name and the direction of one real alert come back as they should — the card
that used to show that reading is gone, so this is the only way to see it.

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

### Email accounts

The environment route above needs a terminal, a restart, and a mail password sitting in a shell.
**Shop & devices ▸ Email accounts** is the same thing done at the counter: register the mailboxes
the shop sends from — a name for the drawer, the address the client sees, the display name, the
SMTP host, port and security, the sign-in name, and the app password — and the notices go out
through them from that moment, with no restart.

A fresh form arrives already aimed at the shop's own Gmail (`smtp.gmail.com`, port `587`, STARTTLS,
`chriphicshubgh@gmail.com`), because that is the mailbox the letters actually leave from. Only the
app password is left in the empty box, and only the counter may type it: Gmail wants a 16-character
app password from the account's own security settings, not the word that opens the mailbox on a
phone. Ask for it, and nobody — including this repository — ever sees it.

- **One mailbox sends.** The first one registered becomes the sender; **Make this the sender**
  hands the place to another. The rest sit ready, so a mailbox the provider has locked can be
  stood aside without the shop losing email altogether.
- **The password does not live in the book.** Every other detail of a mailbox is ledger material
  and is kept in `chrisphics.db`; the word is kept in `email-secrets.json`, one small file beside
  the book at mode `600` — readable only by this Mac's own user. The book is copied every night,
  mirrored to a second folder if you set one, downloaded from the page footer, and this repository
  is public: a mail password travelling with the ledger is a mail password leaked. The screen can
  therefore say *word saved* and never show it back. A password box left empty on an edit keeps the
  word that is already there; **Forget** takes the mailbox and its word out together.
- **Test this account** hands one real letter to that mail server, addressed to the mailbox's own
  address, and reports what the server said. A refusal comes back in the shop's own words naming
  the mailbox (*The mail server refused the word saved for shop@example.com.*) and is written on
  that row as its **Last error**, so the counter can see which mailbox is the problem without
  opening a job. A mail server saying no is the shop's details being wrong, not the counter's
  machine breaking down, so it is answered as a 400 about the mail rather than a 500 about the
  shop's Mac. A successful test clears the error and stamps **Last sent**.
- **Only this Mac changes them.** The shop's own computer registers, edits, forgets, chooses the
  sender and tests. A phone or a second machine out on the Wi-Fi can read which mailbox letters
  go out through and when one last failed, and is turned away from every write — a mailbox and its
  password are added where somebody can see it happen.
- **A registered mailbox wins over the environment.** With nothing registered, email still falls
  back to `CHRISPHICS_EMAIL_*`, so the Render box and any machine set up the older way keeps
  working. With one registered, the environment is not consulted for that channel.

`python3 tools/check-email-accounts.py` proves all of this against a mail server that is real but
worth nothing — a throwaway SMTP listener on this Mac, its own CA and certificate, holding the
conversations the shop's own `smtplib` believes.

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
- **The one thing that is not in the book**: if you register mailboxes on **Shop & devices ▸
  Email accounts**, their passwords live in `email-secrets.json` beside `chrisphics.db`, at mode
  `600`. A restored book with no file beside it comes back with its mailboxes listed and each one
  saying *no word saved*; type the word once on that mailbox and it sends again. Take both files
  off the Mac if you move the shop to another machine.
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
under `public/` — the engine inside the app reads those files off disk on every request, so a
front-end edit can be copied straight into the bundle and shows on the next reload with no restart.
**State of this bundle on 2026-10-15:** its `public/` was copied in by hand that way, which leaves
the ad-hoc signature's resource seal stale (the executable is untouched, so its CDHash did not
move). The next `./desktop/build-app.sh` restores a valid seal — and any re-sign, even one with no
code recompiled, moves the CDHash and so costs a fresh Full Disk Access grant.

**Colour and appearance.** The palette comes off the logo itself: crimson `#AB1F23` and the
grey `#808085` it pairs with, measured from the artwork rather than eyeballed. Both are
darkened for text (`--brand-ink`, `--link`) so they hold up as type on paper, and the mark's
own inks (`#C93A3F` / `#9A9AA0`) are what the Dock icon and the tab icon are drawn with.

There is **one appearance, and it is the clock's**: the light look between **8:00am and 5:00pm**, the
dark one outside those hours, turning over by itself with nobody pressing anything (a machine that
slept through 5:00pm corrects itself when it wakes). Nothing anywhere offers a choice of look — the
page footer's buttons, `View ▸ Appearance` and **Settings ▸ Appearance** were all taken out on the
owner's word (2026-10-13: "Remove Light, Dark or System because By clock is already there"), and the
day/night checks now *fail if any of them comes back*. The dashboard
card that carries the date and the running time says which of the two the clock is asking for.
That card is **three widgets**, because the three versions before it went unseen; the last of them drew
"let the calendar widget be the medium type with all the days on it, not small". So the calendar is
the iPhone's **medium** form: the month and its year as a header, seven columns of initials running
Monday to Sunday, and all forty-two cells under them — every day of the month, plus the weeks of the
months either side in the quiet ink — with today set in a crimson roundel. It is the medium *content*
at a landscape shape; see the "reduce the two widget" note below. Beside it the clock is
drawn at **lock-screen size** (56px on a desktop, 32px on a phone), the seconds and am/pm kept small.
Both wear this shop's own tokens, not somebody else's red: the roundel is crimson by day and the
amber of an evening notice after, and the inks are measured rather than asserted — today's type
7.11:1 on crimson, the day numerals 16.62:1 on the page, the month name, the neighbouring days and the
weekday initials all past AA. Two of them had to change with the palette: the evening amber is a light
colour on the dark palette, so its roundel ink turns *dark* there (white on it reads 2.46:1), and the
month name cannot wear that palette's brand, which is only 3.22:1 on the lifted face, so it takes the
brightened link red at 6.74:1. The face always holds six rows, so a month of four or five Fridays
never changes the card's height next to the clock, and on a phone the month takes the whole row with
the clock dropping under it rather than its cells shrinking to nothing. The whole date is still written
out underneath, so nothing a widget leaves out is lost to anyone reading the screen aloud — and the
widget is announced as "October 2026. Today is Tuesday 13." rather than forty-two numbers.
There is nothing to overrule the clock with: the Mac app's own window chrome goes dark at 5:00pm and
light at 8:00am alongside the page rather than waiting for the next opening, and it watches the hour
after the window is drawn so a shop left open across 5:00pm turns over by itself. A machine that met
the shop before the choice existed may still carry an old stored word; the page ignores it.
The dark palette is a real second
palette, not a filter: surfaces lift off black, the crimson brightens so it survives on a
dark counter, and the status pills go from pale washes to deep ones with lightened type.
Native date pickers and scrollbars follow the palette too. Text clears WCAG AA in both
palettes on all seven screens, the job drawer and the new-job form.

Both hours live in one place per file — `DAY_START` / `DAY_END` in `public/app.js`, repeated in the
pre-paint script of `public/index.html` because that has to decide the palette before `app.js`
exists — and `tools/check-daylight-theme.py` fails if the two ever disagree.

The third widget on that card is the **weather**, added the same evening: "Add the weather after the
calendar widget in the same size like the calendar". Same size is a fact about two painted boxes, so
it is measured as one — the month and the weather both come out of a desktop at **302×185**, wearing
the same `--cal-face`, the same `22px` corner and the same counter shadow, the weather tile stretched
to the month's height rather than guessing at it. It sits *after* the calendar, and on a phone it takes
the row underneath it at the month's own width (329px there), all six hours still in view. What is on
it is the iPhone's medium weather form: the town and a drawn glyph along the top, the temperature at
**40px**, the condition as a word (`Partly cloudy`, not `2`), the day's high and low, what it feels
like and the damp, and six hours along the bottom.

He then looked at the pair and said **"reduce the two widget and make them landscape"**. They were two
**340×275** blocks that filled the card, and in his own window — a content column near 630px — they
could not share a row, so they stacked one over the other and the clock fell off the screen. Both are
now **302×185** on a desktop: the same month, the same six hours, a third less of the card, and each
tile at least a half again wider than it is tall. What shrank is the row and the padding, never the
month — all forty-two cells are still on the face, at 15px numerals in 20px rows with a 20px roundel.
At the counter's width the two now sit side by side on one band, which is the part a full-desktop
screenshot cannot show, so `tools/check-daylight-theme.py` measures that window too: one run holds the
page at 860×800 and fails if the weather drops under the month, if the two boxes stop agreeing, or if
either stops lying down.

The figures come from **Open-Meteo** for Accra — no key, plain HTTPS, and the shop's own server stays
local rather than becoming a weather station, which is why this shipped as a `public/` change with no
restart. The six hours are laid out on the *service's* own clock, not the machine's, so a laptop with
the wrong time cannot move the forecast. The glyph is drawn in the shop's slate (`--cool`, the same
token the card's own left border wears) and turns to amber only for the sun — never a borrowed sky
blue — and the weather is carried by the glyph's **shape** (sun, cloud, rain, snow, fog, storm, and a
crescent when the service says it is night), so a machine that cannot tell the two inks still reads
the right weather. Every ink on the tile is the calendar's token, so every pair is one the shop already
measures: the temperature and condition 16.62:1 by day and 13.26:1 by night, the town 7.11:1 and
6.74:1, the quiet lines 5.43:1 and 5.94:1.

And when the sky does not answer, the tile does not make anything up. It asks once when the dashboard
opens, then every twenty minutes while it has a real reading to show — but a screen with **nothing** to
show is chased again after a **minute**, because the shop's counter is left open all day and one slow
answer at 8:00am must not leave it saying "No weather" until 8:20. Nor does it say that while the ask is
still in the air — that tile reads *"Getting the weather…"* until an answer actually comes back refused,
because a machine reaching the service at that second is not a machine that cannot reach it. A machine
that is refused says so in plain words —
*"No weather — this machine cannot reach the forecast."* — and draws **no figure at all** rather than a
zero. A machine that has an old reading in its pocket keeps showing it and marks it `read at 7:50 pm`,
because a figure from yesterday on a screen people trust is worse than none. The whole tile is
announced as one sentence — "Accra: Overcast, 27 degrees, feels like 30, high 32, low 25." — or as
"Accra, no reading yet." when there is nothing to say. `tools/check-daylight-theme.py` runs all of it
against a frozen clock and a **forecast of the check's own choosing**, because a test that passes or
fails depending on the weather outside the counter is not a test; it then runs once more with the real
service left alone, to prove the page can actually read what Accra answers.

On the dashboard only, the panels are **glass**. Each card and each money tile is a translucent
sheet that samples what is behind it (`backdrop-filter: blur(20px) saturate(130%)`), lifted on a
soft shadow, with a hairline of light running round its rim where it catches the day; a money tile
gives a little under a press and settles back. For there to be anything to see through, the screen
carries an ambient wash of the shop's own three colours — the crimson, the pipeline blue, the amber
of a notice — laid in soft blobs behind the panels and never on top of the type, and a second
arrangement of the same three colours crossfades over it every half-minute, so a tile's tint drifts
while the figure on it holds still. It is the same material in both appearances, just tinted for the
counter it sits on: a brighter sheet by day, a deeper one at night with the rim still shining.
**This is the material, not Apple's lens** — real refraction of the content behind is not something
this app's rendering engines can be asked for, so nothing bends or magnifies at the edges.

The first ship of it went unseen, and the reason is worth keeping: a panel that is 72% white lets
through 28% of what is under it, and what was under it was crimson at `.16` alpha — dark pigment,
already nearly grey. Measured through the glass it came out at chroma 6 out of 255, which is not a
colour anybody can see. So the wash is built from bright tints at high alpha instead: the hue rides
on lightness rather than eating it, which is the only way to get colour through a white sheet and
still keep the money figures readable. The same arithmetic says a blob edge has to be crisp for the
blur to have anything to smear, which is what makes the glass read as a material at all. Through the
glass the panel now sits at chroma 23-29 by day and 28-42 at night, against the 1-6 it used to be.

It is deliberately narrow: `render()` takes the glass class off on every route and only
`viewDashboard()` puts it back, so the other seven screens, the sign-in door and the printed job
sheet are untouched. It went wider for an hour on 2026-10-14 — every screen, a second tier inside the
panels, the top bar too — and the counter sent it back the same evening ("this doesn't look good"),
which is what one class gate is for. The two alarm cards — a job running late and the shop being
shut — stay solid opaque, because a warning the counter has to read at a glance is not allowed to be
see-through. An engine with no `backdrop-filter` at all gets a solid panel instead of a washed-out
one, and a machine asking for reduced motion gets no transitions and a wash that sits still at
half-strength rather than drifting. `tools/check-liquid-glass.py` measures the contrast of every ink
through **every** stop of both wash layers, with the panel's own `saturate()` applied before the
compositing rather than after (worst case 4.60:1 light, 4.89:1 dark, both past WCAG AA), fails if the
wash stops being colourful enough to see, loads all eight other screens to prove they did not change,
and reads the values back off a page laid out in the same WebKit the Mac app ships.

The printed job sheet is the one thing that stays black on white in either appearance, so a
photocopy and a customer's copy look the same.

To retune a colour, change the token in `public/styles.css` — the light values are in the
`:root` block at the top, the dark ones in `:root[data-theme="dark"]` just under it. The glass has
its own set in those same two blocks (`--glass`, `--glass-rim`, `--glass-spec`, `--glass-shadow`,
`--wash-*`); its corner radius (`--r-glass`) is written once, above them both, because the panels
are the same shape at midnight as at noon. Every
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

The dev checks live in `tools/`, and none of them needs anything installed:

| Command | What it answers |
| --- | --- |
| `python3 tools/js-check.py` | does `public/app.js` still parse? (one stray bracket empties every screen) |
| `python3 tools/check-profile-settings.py` | can the shop's own details be saved and read back out of the book, do bad ones get refused, and does a cleared one fall back to its default? Runs against a **copy** of the book on a throwaway port. |
| `python3 tools/check-backups-security.py` | does the book get copied by itself, checked, mirrored, retained and reported; and is it guarded — password rules, salted 600,000-round hash, session length, devices thrown out, wrong-password waits, the per-minute ceiling, cross-origin refusals, the security trail, HTTPS headers? Runs four throwaway servers on ports 8897–8899 against a **copy** of the book, in its own backup folders, and closes by proving no job, client, payment, expense or message moved and that the shop's own file was not written to. |
| `python3 tools/check-daylight-theme.py` | does the dashboard carry the shop's date and a clock that runs, and does the look follow the hour — light 8:00am–5:00pm, dark outside, turning over by itself with nobody touching it, and that no surface offering a hand-picked look survives — the page carries no appearance buttons, and a `crispprint-theme` word left in a browser by an older build is proved ignored? It also measures the two widgets as painted: that the calendar is the **medium** type with **all forty-two cells of the month on it, in order**, today marked exactly once and in the right row and column, the grid seven columns wide, the numerals and the month name large enough to be seen from the counter, and all five inks (roundel, month name, numerals, neighbouring months, weekday initials) re-measured against AA out of the palette the page is actually wearing. It walks one page held open across a midnight and across the last minute of a month, and closes by proving nothing hangs off a 390px screen and the shop's own file was not written to. Every page is run in the same WebKit the app uses with its clock frozen by an injected script, so the hour under test is the hour the page believes from its pre-paint script onwards. |
| `python3 tools/check-app-daylight.py` | does the Mac app's own **By clock** keep the same day as the page? `desktop/src/Theme.swift` is compiled on its own into a throwaway command that answers for all twenty-four hours, so the window chrome's rule is run rather than read; it also fails if the shell's 8/17 ever disagrees with the page's, and refuses any appearance mode, menu or Settings pane coming back. |
| `python3 tools/check-flexible-entry.py` | can the counter take an order and its money without fighting the screen? A real WebKit view drives the shipped page against a **copy** of the book: the caret landing in a box, `1,200` and `₵0.25` booking ₵ 300.00, Enter stepping through the lines instead of half-booking, a pasted list laying itself out, cash on the order screen recorded as a Payment so the balance reads zero, money taken on a job the book already calls settled, one press repeating yesterday's ticket, a refused figure coming back typed, and the whole ticket readable at 390px. Runs one at a time — the view's browser data is per address, so a second concurrent run wipes the first one's mid-scenario. |
| `python3 tools/check-shop-boot.py` | does the counter open by itself? The real keeper is run against a **copy** of the book, a throwaway support folder, a throwaway certificate and a stand-in helper, and watched through three scenes: switched on before the Wi-Fi has joined (it waits, says so in the log, and serves the moment the lease appears — nobody restarts anything), the address moving under a shop that is already serving (it lets go of the dead bind, re-certifies for the new number, and is serving again by itself when the lease comes back), and a second keeper arriving to find the shop answering (it refuses to start another engine over one book). It then reads the live service file for the keys that make the rest true — login start, restart on a bad exit, the five-minute re-arm, the short throttle, and no address of the day pinned in it — proves the served copies of both tools match this repository, that the address rule lives in one place, and closes by showing the live book is exactly the bytes it started with. Ports 8911–8913 are claimed first, so a number somebody else is holding cannot fake a pass. |
| `python3 tools/check-email-accounts.py` | can the shop register the mailboxes it sends from, and does the mail actually go? A **throwaway SMTP server** is run on this Mac — real sockets, real STARTTLS, real `AUTH`, on a certificate minted for this one run from its own CA — so *Test this account* is proved against a server rather than a mock. Runs two throwaway shop servers on free ports it asks the kernel for, against a **copy** of the book, in its own support and backup folders: registration and its refusals, the first mailbox becoming the sender and a second waiting, an edit with the password box left empty keeping the saved word, a mailbox that is no longer in the book answered honestly, and every path gated so a phone out on the Wi-Fi can read the list but cannot add, change, forget, choose or test. It then proves the two rules the feature exists for: that no copy of the book, no server log, no API response and no stored provider error ever carries a word, while the secrets file sits beside the book at mode `600` and not among the copies; and that a client's **Ready** notice goes out by itself through the mailbox the shop chose, over TLS, with the provider's own receipt kept against the message and the job timeline saying so. A refusal is checked to be a 400 about the mail rather than a 500 about the Mac, an untrusted certificate is checked to be refused, a provider that repeats the word back is checked to come back `[hidden]`, and the registered mailbox is checked to win over `CHRISPHICS_EMAIL_*`. Closes by proving only the throwaway job and client moved, in the copy, and that the shop's own file is the bytes it started with. |
| `swift tools/make-qr.swift "<address>" /tmp/shop-qr.png 520` | the counter's QR as a printable PNG — and every code is read back before the file is trusted (`--verify <file.png>` checks one already drawn) |
| `swiftc -O tools/wk-probe.swift -o /tmp/wk-probe` then `/tmp/wk-probe <url> <dir> "open\|/#/jobs" "shot\|jobs"` | what a screen actually rendered, in the same WebKit the app uses, as text plus a PNG. `WKPROBE_HANDLERS=external` registers a handler the app's shell registers, so a page can be read as the app sees it — what it posts there comes back as `posted external: …`. `WKPROBE_SCRIPT=<file>` injects a script before the page's own, and `WKPROBE_FRESH_DATA=1` starts the browser with none of the history it kept from the last run |

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
