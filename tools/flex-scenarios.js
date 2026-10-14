/* Scenarios for tools/check-flexible-entry.py, injected by the WebKit probe with
 * WKPROBE_SCRIPT. Nothing here belongs to the shop's own page: every name is hung off window so
 * it cannot collide with app.js, and each scenario returns plain facts the Python side asserts on.
 *
 * The point of these is the counter's minute: a walk-in arrives, one product is written down with
 * the figures typed the way they are said out loud ("1,200", "₵0.25"), the cash is taken on the same
 * screen, and the order is booked. Nothing in that path may lose a keystroke.
 */
window.__CTX = {};
window.__R = {};

window.__q = function (sel, root) { return (root || document).querySelector(sel); };
window.__qa = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
window.__sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
window.__until = function (fn, ms, label) {
  ms = ms || 5000;
  return new Promise(function (resolve, reject) {
    var waited = 0;
    (function tick() {
      var ok = false;
      try { ok = !!fn(); } catch (e) { ok = false; }
      if (ok) return resolve();
      waited += 40;
      if (waited > ms) return reject(new Error('waited ' + ms + 'ms for ' + (label || 'the page')));
      setTimeout(tick, 40);
    })();
  });
};
/* A figure is typed, not assigned: the same events the keyboard sends, so the page's own
   listeners (the live total, the client search, the row stepper) all run exactly as they do
   at the counter. */
window.__type = function (el, value) {
  el.focus();
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return el;
};
window.__press = function (el, key, mods) {
  var init = { key: key, bubbles: true, cancelable: true };
  if (mods) Object.keys(mods).forEach(function (k) { init[k] = mods[k]; });
  el.dispatchEvent(new KeyboardEvent('keydown', init));
};
window.__modal = function () { return window.__q('#modal'); };
window.__modalOpen = function () { return !window.__modal().hidden; };
window.__openBooking = function () {
  window.__q('[data-action="new-job"]').click();
  return window.__until(function () { return window.__modalOpen() && window.__q('#jobForm'); },
                         5000, 'the order sheet to open');
};
window.__leaveModal = function () {
  if (window.__modalOpen()) {
    var cancel = window.__q('#modalCard [data-action="close-modal"]');
    if (cancel) cancel.click();
    return window.__until(function () { return !window.__modalOpen(); }, 3000, 'the sheet to close');
  }
  return Promise.resolve();
};
/* Where the caret ended up once the sheet opened. The page hands focus a beat after the sheet
   appears, so "the sheet is on screen" is not yet "the caret is in a box" — and a wait that reads
   it in the same tick reports nowhere at all. A blink is generous; the counter does not look at the
   screen for longer than that before typing. */
window.__caret = function () {
  return window.__until(function () {
    var a = document.activeElement;
    return a && a !== document.body && (a.name || a.id);
  }, 1200, 'the caret to land in one of the sheet\'s boxes')
    .then(function () { return document.activeElement.name || document.activeElement.id; },
          function () { return 'no box took the caret'; });
};
/* Saving is the step that can be refused, and a refusal used to read as a hung page: the probe
   waited for a sheet that never closed and timed out holding nothing. This waits for either the
   sheet to close or the form's own error line to carry words, and says which one happened. The
   deadline stays short on purpose: the probe gives each step eight seconds of its own. */
window.__submit = function (form, errSel, out, key) {
  form.requestSubmit();
  var settled = function () {
    if (!window.__modalOpen()) return true;
    var e = window.__q(errSel);
    out[key] = (e && e.textContent) ||
      'the sheet stayed open with nothing written on it: the browser itself refused the submit';
    return false;
  };
  return window.__until(function () {
    var e = window.__q(errSel);
    return !window.__modalOpen() || (e && e.textContent);
  }, 4500).then(settled, settled);
};

/* 1a. open the order: where the caret lands, and a walk-in put on file by one Enter. The counter's
   minute is split into three probe steps because one step must stay inside the probe's own
   eight-second patience. */
window.__s1a = async function () {
  var out = {};
  await window.__openBooking();
  out.focusLandsOn = await window.__caret();
  var find = window.__q('#clientFind');
  window.__type(find, 'FLEX WALKIN');
  window.__press(find, 'Enter');
  await window.__sleep(150);
  var box = window.__q('#newClientBox');
  out.newClientBoxOpened = !box.hidden;
  out.nameCarriedAcross = box.querySelector('input').value;
  out.newClientFocused = document.activeElement.name === 'new_name';
  out.hintSaysNobody = window.__q('#clientHint').textContent;
  return out;
};

/* 1b. the line itself: figures typed the way they are said out loud, Enter that opens the next
   line instead of booking a half-written job, and a pasted list that lays itself out. */
window.__s1b = async function () {
  var out = {};
  var row = window.__q('.item-row');
  window.__type(row.querySelector('.it-title'), 'A5 FLYERS');
  window.__type(row.querySelector('.it-qty'), '1,200');
  window.__type(row.querySelector('.it-price'), '₵0.25');
  await window.__sleep(80);
  out.lineTotalShown = row.querySelector('.it-line').textContent;
  out.jobTotalShown = window.__q('#cTotal').textContent;

  window.__press(row.querySelector('.it-price'), 'Enter');
  await window.__sleep(40);
  out.enterInPriceMovesTo = document.activeElement.className;
  window.__press(document.activeElement, 'Enter');
  await window.__sleep(80);
  out.rowsAfterEnterOnLastCell = window.__qa('.item-row').length;
  out.formStillOpenAfterEnter = window.__modalOpen();
  out.priceBoxNormalised = row.querySelector('.it-price').value;

  var pasted = null;
  try {
    var dt = new DataTransfer();
    dt.setData('text/plain', 'VISITING CARDS\t300gsm\t100\t1.20\nROLLER BANNER\t3m wide\t1\t150');
    pasted = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
  } catch (e) {
    out.paste = 'UNSUPPORTED';
  }
  if (pasted) {
    var second = window.__qa('.item-row')[1];
    second.querySelector('.it-title').dispatchEvent(pasted);
    await window.__sleep(120);
    var rows = window.__qa('.item-row');
    out.rowsAfterPaste = rows.length;
    out.pastedSecondTitle = rows[2] ? rows[2].querySelector('.it-title').value : null;
    out.pastedSecondQty = rows[2] ? rows[2].querySelector('.it-qty').value : null;
    out.pastedSecondPrice = rows[2] ? rows[2].querySelector('.it-price').value : null;
  }

  /* Back to the one line the counter actually wrote, then the cash on the same screen. */
  return out;
};

/* 1c. the money taken with the order, then the booking itself. */
window.__s1c = async function () {
  var out = {};
  while (window.__qa('.item-row').length > 1) {
    var last = window.__qa('.item-row').pop();
    last.querySelector('[data-remove-item]').click();
    await window.__sleep(40);
  }
  out.rowsBeforeBook = window.__qa('.item-row').length;
  window.__q('[data-pay-all]').click();
  await window.__sleep(80);
  out.depositAfterChip = window.__q('#jobForm').deposit.value;
  out.hintAfterChip = window.__q('#depositHint').textContent;
  out.balanceAfterChip = window.__q('#cDue').textContent;

  if (!await window.__submit(window.__q('#jobForm'), '#jobErr', out, 'bookRefused')) return out;
  await window.__until(function () {
    return window.__q('#drawer:not([hidden])') && window.__q('[data-pay-job]');
  }, 2500, 'the booked job to open its own sheet');
  out.drawerOpened = true;
  /* The figures the sheet shows the second the order is booked are the counter's own answer to
     "is he paid?". Read them here, because a later check takes more money on this same job and
     the book's current balance would then describe that minute, not this one. */
  var amts = window.__qa('#drawer .amounts:not(.second) > div');
  out.sheetTotal = amts.length > 0 ? amts[0].querySelector('b').textContent : null;
  out.sheetPaid = amts.length > 1 ? amts[1].querySelector('b').textContent : null;
  out.sheetBalance = amts.length > 2 ? amts[2].querySelector('b').textContent : null;
  out.sheetBalanceWord = amts.length > 2 ? amts[2].querySelector('span').textContent : null;
  out.reorderButton = !!window.__q('[data-reorder-job]');
  window.__CTX.jobId = window.__q('[data-reorder-job]')
    ? window.__q('[data-reorder-job]').getAttribute('data-reorder-job') : null;
  var pay = window.__q('[data-pay-job]');
  out.payButtonWords = pay ? pay.textContent : null;
  out.payButtonPrefill = pay ? pay.getAttribute('data-pay-amount') : null;
  return out;
};

/* 2. an advance on tomorrow's job: no item lines at all, quantity and price typed below,
   money that does not cover the total, so the booking must still leave a balance owing. */
window.__s2 = async function () {
  var out = {};
  await window.__openBooking();
  window.__type(window.__q('#clientFind'), 'walkin');
  await window.__sleep(120);
  out.matchHint = window.__q('#clientHint').textContent;
  out.oneMatchSelected = window.__q('#clientSelect').value !== '';
  out.matchedName = (window.__q('#clientSelect').selectedOptions[0] || {}).textContent || '';

  var row = window.__q('.item-row');
  row.querySelector('[data-remove-item]').click();
  await window.__sleep(80);
  out.singleBlockShown = !window.__q('#singleBlock').hidden;
  window.__type(window.__q('#singleBlock [name=quantity]'), '2');
  window.__type(window.__q('#singleBlock [name=unit_price]'), '1,500');
  window.__type(window.__q('#jobForm [name=title]'), 'CANOPY PRINT');
  window.__type(window.__q('#jobForm [name=deposit]'), '1,000');
  await window.__sleep(80);
  out.totalShown = window.__q('#cTotal').textContent;
  out.balanceShown = window.__q('#cDue').textContent;
  out.paidLabel = window.__q('#cPaidLabel').textContent;
  if (!await window.__submit(window.__q('#jobForm'), '#jobErr', out, 'bookRefused')) return out;
  return out;
};

/* 3. money taken against a job the book already says is settled — an advance, a second
   instalment, a refund. The old screen only ever listed jobs that still owed. */
window.__s3 = async function () {
  var out = {};
  var id = window.__CTX.jobId;
  location.hash = '#/jobs/' + id;
  /* The booking before this one left a job's own sheet on screen, so "a sheet is open" is not yet
     "this job's sheet": the wait is on the number, or the scenario reads the money off the wrong job. */
  await window.__until(function () {
    var p = window.__q('[data-pay-job]');
    return window.__q('#drawer:not([hidden])') && p && p.getAttribute('data-pay-job') === id;
  }, 5000, 'job ' + id + ' to open its own sheet');
  var pay = window.__q('[data-pay-job]');
  if (!pay) throw new Error('the settled job offers no way to take money on it');
  out.jobIsTheSettledOne = pay.getAttribute('data-pay-job') === id;
  out.prefilledForAnOwingJob = pay.getAttribute('data-pay-amount');
  pay.click();
  await window.__until(function () { return window.__modalOpen() && window.__q('#payForm'); },
                        4000, 'the money sheet');
  out.focusLandsOn = await window.__caret();
  out.amountBoxEmpty = window.__q('#payForm').amount.value === '';
  window.__type(window.__q('#payForm [name=amount]'), '₵500');
  await window.__sleep(60);
  out.afterShown = window.__q('#payAfter').textContent;
  out.hintShown = window.__q('#payHint').textContent;
  if (!await window.__submit(window.__q('#payForm'), '#payErr', out, 'payRefused')) return out;
  return out;
};

/* 3b. the money screen lists every job, and choosing a person narrows it to theirs. */
window.__s3b = async function () {
  var out = {};
  location.hash = '#/accounts';
  await window.__until(function () { return window.__q('[data-action="receive-payment"]'); });
  window.__q('[data-action="receive-payment"]').click();
  await window.__until(function () { return window.__modalOpen() && window.__q('#payJob'); });
  var jobs = window.__q('#payJob');
  out.jobOptions = window.__qa('#payJob option').length;
  out.settledJobsListed = /settled/.test(jobs.textContent);
  out.owingJobsListed = /owes/.test(jobs.textContent);
  out.quotesLeftOut = !/Quote/.test(jobs.textContent);
  var who = window.__q('#payClient');
  out.clientOptions = window.__qa('#payClient option').length;
  who.selectedIndex = 1;
  var chosen = who.selectedOptions[0].textContent.split(' ·')[0].split(' (')[0];
  who.dispatchEvent(new Event('change', { bubbles: true }));
  await window.__sleep(120);
  var after = window.__qa('#payJob option').slice(1);
  out.narrowedOptions = after.length;
  out.everyJobIsThatClients = after.every(function (o) {
    return o.textContent.indexOf(chosen) >= 0;
  });
  out.chosenClient = chosen;
  await window.__leaveModal();
  return out;
};

/* 4. the same order again: last week's ticket becomes this week's, with the client, the line,
   the quantity and the price already written. */
window.__s4 = async function () {
  var out = {};
  var id = window.__CTX.jobId;
  location.hash = '#/jobs/' + id;
  /* The money taken on this job just re-rendered a different job's sheet on top of the page. Waiting
     only for "a sheet is open" read the repeat button off that one instead. */
  await window.__until(function () {
    var r = window.__q('[data-reorder-job]');
    return window.__q('#drawer:not([hidden])') && r && r.getAttribute('data-reorder-job') === id;
  }, 5000, 'job ' + id + ' to open its own sheet');
  var again = window.__q('[data-reorder-job]');
  if (!again) throw new Error('the job sheet offers no way to repeat the order');
  again.click();
  await window.__until(function () { return window.__modalOpen() && window.__q('#jobForm'); });
  out.headerWords = (window.__q('#modalCard header h2') || {}).textContent || '';
  out.clientCarriedAcross = window.__q('#clientSelect').value !== '';
  var row = window.__q('.item-row');
  out.lineCarriedAcross = row.querySelector('.it-title').value;
  out.qtyCarriedAcross = row.querySelector('.it-qty').value;
  out.priceCarriedAcross = row.querySelector('.it-price').value;
  out.isEditingOldJob = window.__q('#jobForm').getAttribute('data-job-id') !== '';
  out.totalShown = window.__q('#cTotal').textContent;
  window.__type(window.__q('#jobForm [name=title]'), 'A5 FLYERS (SECOND RUN)');
  if (!await window.__submit(window.__q('#jobForm'), '#jobErr', out, 'bookRefused')) return out;
  return out;
};

/* 5. a figure typed into a quick cell survives a refusal. The counter renames, re-dates and re-prices
   without opening the form; when the book says no, what he typed must still be there. An enquiry's
   money is changed on the dashboard's chase list — that is where the figure is actually clicked — so
   the enquiry is written with a follow-up date to put it on that list.
   Two probe steps: the write, then the refusal. One step is the page's own re-read of the book plus
   a keyboard round trip, and the probe gives each step eight seconds of its own. */
window.__s5a = async function () {
  var out = {};
  var box = function () { return '[data-qe="leads/' + window.__CTX.leadId + '/value"]'; };
  var made = await fetch('/api/leads', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'FLEX LEAD', interest: 'flyers', value: 400,
                           follow_up: new Date().toISOString().slice(0, 10) })
  });
  var lead = await made.json();
  window.__CTX.leadId = lead.id;
  location.hash = '#/';
  /* The enquiry was written by this page but not through its own save path, so the numbers already
     held on screen do not know about it. The shop's quiet refresh is the same one a quick edit runs
     when the book accepts a figure — without it the chase list would stay as it was loaded. */
  if (window.refreshQuiet) await window.refreshQuiet();
  await window.__until(function () { return window.__q(box()); }, 4000,
                        'the enquiry on the dashboard with its money box');
  window.__q(box()).click();
  await window.__until(function () { return window.__q('.qe-in'); }, 3000, 'the click-to-change box');
  var typed = window.__q('.qe-in');
  out.typedIntoIsText = typed.type;
  window.__type(typed, '1,200');
  window.__press(typed, 'Enter');
  /* The page re-reads the book after a write, so the figure it lands with is the answer, and it is
     worth waiting for rather than sleeping and hoping — but a failure to show it is recorded, not
     thrown, so the scenario still reports. */
  try {
    await window.__until(function () {
      var b = window.__q(box());
      return b && /200/.test(b.textContent);
    }, 2500, 'the chase card to show the new figure');
  } catch (e) { out.pageShowsTheFigure = 'never showed: ' + e.message; }
  out.pageShowsTheFigure = (window.__q(box()) || {}).textContent || out.pageShowsTheFigure;
  out.writtenToTheBook = (await (await fetch('/api/leads/' + window.__CTX.leadId)).json()).value;
  return out;
};

/* The refusal: the enquiry is deleted off another device while this one is still showing it, so the
   write cannot land. The typed figure must come back, not vanish. */
window.__s5b = async function () {
  var out = {};
  var again = window.__q('[data-qe="leads/' + window.__CTX.leadId + '/value"]');
  if (!again) throw new Error('the chase list lost the enquiry after one write');
  again.click();
  await window.__until(function () { return window.__q('.qe-in'); }, 2500, 'the box reopened');
  await fetch('/api/leads/' + window.__CTX.leadId, { method: 'DELETE' });
  var box2 = window.__q('.qe-in');
  window.__type(box2, '3,500');
  window.__press(box2, 'Enter');
  /* The page puts the typing back a beat after the book says no. */
  var reopened = null;
  try {
    await window.__until(function () {
      reopened = window.__q('.qe-in');
      return !!reopened;
    }, 2500, 'the refused figure to be put back in the box');
  } catch (e) { reopened = null; }
  out.refusedValueStillThere = reopened ? reopened.value : null;
  if (reopened) window.__press(reopened, 'Escape');
  return out;
};

/* 6. the keys the counter lives on: n for a new order, ⌘/Ctrl+Enter to save the one open. */
window.__s6 = async function () {
  var out = {};
  await window.__leaveModal();
  document.body.focus();
  window.__press(document.body, 'n');
  await window.__sleep(250);
  out.nOpensABooking = window.__modalOpen() && !!window.__q('#jobForm');
  window.__type(window.__q('#jobForm [name=title]'), 'KEYBOARD ONLY');
  window.__press(window.__q('#jobForm [name=title]'), 'Enter', { metaKey: true });
  await window.__sleep(400);
  out.reachedTheHandler = window.__q('#jobErr').textContent;
  out.modalStillOpen = window.__modalOpen();
  await window.__leaveModal();
  return out;
};

/* 7. the same form on the phone the counter carries. An order line is a card here, not a
   column of a spreadsheet you scroll sideways through, and no field is small enough to make
   the browser zoom away from the rest of the ticket. */
window.__s7 = async function () {
  var out = {};
  await window.__openBooking();
  out.viewport = window.innerWidth;
  out.itemInputFont = getComputedStyle(window.__q('.items-t input')).fontSize;
  out.moneyAlignsRight = getComputedStyle(window.__q('.it-price')).textAlign;
  out.rowDisplay = getComputedStyle(window.__q('.item-row')).display;
  out.cellLabel = getComputedStyle(window.__q('.item-row td[data-l="Qty"]'), '::before').content;
  out.sidewaysScroll = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  out.tableOverflow = window.__q('.items .tablewrap').scrollWidth -
    window.__q('.items .tablewrap').clientWidth;
  await window.__leaveModal();
  return out;
};

window.__go = function (name) {  window['__' + name]().then(function (r) {
    r.errors = (window.__probeErrors || []).slice(0, 6);
    window.__R[name] = r;
  }, function (e) {
    window.__R[name] = { failed: String((e && e.message) || e),
                         errors: (window.__probeErrors || []).slice(0, 6) };
  });
};
