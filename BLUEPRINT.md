# Indian Pharma IP System — Blueprint

The whole software in plain words. This file is rewritten (not added to) at
the end of each working day, so it always describes the software as it is
today. Old descriptions are replaced, not kept.

Last checked: 10 November 2026

---

## 1. What it is

A website for the Indian Pharma warehouse. Staff open it on their phones.
It records the work each person does, turns it into points and a score out
of 100, and shows the owner (admin) everything: live picking, reports,
leaderboards, deliveries and staff profiles.

- Address: the Render website (one server, free plan).
- Data: kept in Supabase (a PostgreSQL database). Nothing is kept on Render.
- Code: this GitHub repository. Render rebuilds the site when code on `main`
  changes.

## 2. Who uses it (roles)

| Role | What they do in the app |
|---|---|
| Admin (owner) | Sees and controls everything. Sets item time. Adds/edits staff. |
| Pick Assigner | Adds bills to pick, watches pickers live, pauses / closes bills, sees missing items (the purchaser finishes them). |
| Picker | Takes items from bills, a timer runs, presses Done. Scored bill by bill. |
| Checker | Types the day's checking figures. Pending is a team figure. |
| Purchaser | Types purchase-order figures. Sorts items pickers could not find (Done / Not available). |
| Delivery | Gets delivery jobs with a map, starts/ends trips (GPS). |
| Assigner (biller) | Assigns deliveries, writes daily notes. |
| Packer | Writes daily notes / quantity. |
| Supervisor | Console to enter bill counts that check staff numbers; can multitask. |

Staff emails are `name@pharmaip.com`. Only the admin sets someone's role.

## 3. Picking (the main daily flow)

1. **Pick Assigner adds a bill**: bill number + number of items.
   - The prefix is added by itself: IP2627/ (it becomes IP2728/ on 1 April).
   - Up to 6 digits. A number can never be used twice.
   - Put **U** at the end for an urgent bill (e.g. 4521U).
   - Free pickers get a phone alert.
2. **Picker takes items**: types how many items they take from a bill and
   taps Pick. Several pickers can share one bill.
   - Urgent bills must be picked first. A pick already started is never
     cut by an urgent bill: if the urgent bill arrived a moment before the
     picker's screen showed it (up to 60 s), their normal pick goes ahead.
   - **Find a bill** for pickers, checkers and purchasers: type part of a
     bill number. Pickers see their matching open bills to pick, and every
     role sees matching bills of any date as a short summary (date, time,
     items, how many left / complete / missing with purchaser). Who picked
     what stays with the assigner and admin, who can tap a bill for its full
     detail.
   - Every bill shows its date with the time, e.g. "added 10 Nov, 2:05 PM"
     (picker, assigner, admin, bill details).
   - You can never take more than the items still left on the bill: if a
     bill has 10 and 5 are taken, you can take at most 5. Typing more shows
     "Only 5 items left" at once and nothing starts. Missing items can't add
     up to more than the items you took. The server checks the same.
   - One bill at a time per picker.
   - A picker sees each open bill's number, items and items left, and on
     "My day" only their own totals (bills, items, points, on time, not
     found). **Bill details** (who took what, item names not found, bill
     timelines, bill-by-bill lists, picking PDFs) are shown only to the Pick
     Assigner and the admin.
3. **Timer** (per pick): items × item time.
   - Item time is set by the ADMIN only (Picking > Live floor > Item time).
     Defaults: 35 s per item on a normal bill, 30 s on an urgent bill.
   - After normal time: 62.5 s **extra time** to find, then 62.5 s
     **report time**, then **late**.
4. **Full screen while picking**: the phone shows only that bill: name, bill
   number, items and a big clock. The whole screen changes colour:
   light green (normal), light orange (extra), light red (time over),
   flashing red (late), grey (paused).
5. **Done** (or "Item not found" with the item name and quantity).
   - Done counts from the moment it was tapped. If the internet is down, the
     tap is kept and sent when it is back (counted from the tap, up to
     2 minutes back).
6. **Bill complete** when all items are picked; it moves to the Completed tab.

**Points per item picked**

| When Done is tapped | Points per item |
|---|---|
| Normal time | 1.5 |
| Extra time | 1 |
| Report time | 0.5 |
| Late | −1 per started minute late, never more than −5 |

- **Item not found**: the picker writes the item and quantity and taps
  Done; their part is finished. The **Purchaser** gets a phone alert and a
  "Missing items to sort" box on their dashboard (the admin sees it on the
  Today tab too). Only the purchaser (or admin) finishes it: **Done** (sorted)
  or **Not available**. Either way the picker is paid for it at the rate of
  when they reported it, and the bill completes. The assigner sees "with
  purchaser" and has no buttons for it.
- Urgent bill bonus: 10 points per urgent bill, shared by items picked.
- After a bill is added, free pickers have **5 minutes** to take it (they may
  finish other work first). A free picker who leaves it waiting longer gets a warning
  (no points cut yet). Late picks alert the assigner.

**Assigner controls**

- **Today strip** at the top: bills added today, completed, pending (with
  items still to pick) and the closed total. When the assigner taps
  **Close today**, that day's bill count becomes the checkers' fixed day
  total (shown as "Closed total (checkers)").
- Pause / Resume one bill. After Pause the picker gets **7 seconds** of
  warning before the timer stops, so they can finish. Once stopped the
  picker may tap "Work on another bill"; their items on the paused bill go
  back on it. Resume gives the paused time back. No pause time is shown
  anywhere: a paused bill just says "Paused · timers stopped", the picker's
  clock is blank while stopped, and reports show time given as items × item
  time (pause never added).
- Pause all / Resume all / Close all (Close all asks to tap again).
- Reschedule a bill to a later time; Bring back now.
- Close today: counts the day's bills and fixes it as the checkers' day total;
  unfinished bills move to next morning.
- Order of bills: urgent bills in their own box on top, then every bill by
  number, smallest first. Completed bills are on the Completed tab.
- Search box above the pending bills: type part of a bill number to show
  only matching bills (pending and Completed) and, below, matching bills of
  **any date**. Tap one to open everything about it: who picked, items,
  times, items not found. A newly added bill drops into its place in number
  order by itself.

## 4. Scores (out of 100) and grades

Scores are worked out in one place in the code (`build_analytics`). Grades:
88+ ELITE, 72+ PROFICIENT, 52+ SATISFACTORY, below that RE-TRAINING.

- **Picker (bill picking)**: On time 40 · Pick speed 30 (items/hour against
  target, default 50) · Volume 20 (items/day against 200) · Consistency 10.
- **Checker**: Weighted speed 22 · Team clearance 22 · VWCR 20 · Cleaner rate
  8 · Efficiency 8 · Consistency 8 · Potential 5 · Workspace 4 · Sweep 3.
  Pending is a team figure: day total (the highest entered) minus all
  checkers' bills. Urgent work counts ×1.5.
- **Purchaser**: Bill processing 25 · CS fulfilment 20 · Racking 15 · Speed
  10 · Bill entry 8 · VWCR 8 · Pending 5 · Efficiency 4 · Workspace 3 ·
  Consistency 2.
- Delivery, assigner (biller) and packer are not scored with points.
- Then: complaints deduct, admin adjustment adds/subtracts, multitask adds.
- A part can never be bigger than its whole (checked on every entry).

**Levels**: a day scoring 72+ that beats the person's best becomes their new
target, with a congratulations message. Same for best week (Mon–Sat) and best
month (calendar month). Targets only go up.

## 5. Multitask (a second role on the same day)

- One extra role at a time. Ticking a role unticks the others.
- Roles: Picker, Checker, Purchaser, Delivery, Billing, Packing,
  Breakage & Expiry (used to be called "Other").
- Picker as a second role = live bill picking (no typed numbers). The picks
  become the Picker multitask entry by themselves and never change the
  person's main-role figures.
- Pick Assigner and admin pick through "Pick bills yourself" (/pick/me).
- Extra-role points: +10 per extra role day, capped.

## 6. What the admin sees

- **Admin dashboard**, kept simple:
  - Big buttons at the top for the daily jobs: Live picking, Pick bills
    yourself, Staff today, Picking reports, Multitask report, Add staff.
  - **Find a bill**: type a bill number, matching bills of any date appear;
    tap one for all its details (staff, items, times).
  - Four tabs: Staff · Today (picking today, multitask reports, notes) ·
    Scores · Setup.
  - **Go to** menu (top bar): every other admin page, in one place.
  - On phones the staff list is cards, no sideways scrolling.
- **Staff today** (/admin/staff-today; Today tab and Go to menu): everyone
  who signed in or worked on a day, with sign-in / sign-out / last seen
  times and their work. Pickers are shown pick by pick: bill, items taken,
  picked, not found (with item names), took at, normal time ends, done at,
  time given, time taken, result (normal / extra / report / late by m:ss /
  picking now) and points, plus first pick, last Done, picking time, free
  time, items per hour and the late / waiting log. Other staff show only
  their sign-in times (no typed figures, no bill counts). Any date, one person or
  everyone; **Download PDF** for the whole day or one person.
  Sign-ins are recorded from 9 Nov 2026 (admins are not tracked).
- **Picking** (/picking): Live floor (with Item time), Reports for any dates
  or one picker (PDF and CSV), Leaderboard with charts. Every chart shows
  exact numbers (on bars, in pie slices, "Name — 9 (60%)" beside pies).
- Staff profiles: score breakdown, picking bill by bill, urgent bills, late
  report, PDFs.
- Diagnostics: every database table, its row count and whether it is locked
  from the public internet.
- Staff Management: add, edit (Edit button), delete, notes.

## 7. Phone behaviour

- Works on small phones, phones held sideways, tablets and big screens.
- **One phone per login** (staff): signing in on another phone signs the old
  one out. A **Pick Assigner** login can be open on **3 phones** at once; a
  4th sign-in signs out the oldest. Admins can stay signed in on several
  devices.
- **Alerts**: while the app is open the phone vibrates and beeps for new
  bills, pauses, late and missing items. A "Phone alerts: off — Turn on"
  button turns on alerts that arrive even when the app is closed.
  (On iPhone: add the app to the Home Screen first.)
- **No internet**: taps are kept and sent when it is back; buttons never hang.
- **New version**: open phones reload by themselves.
- **Saving the free server**: a screen nobody touches for 10 minutes checks
  every 30 s; after 30 minutes it stops until someone taps it. At night
  (10 PM - 7 AM) it stops after 5 minutes. This lets Render's free server
  sleep when nobody is working.

## 8. Speed

- Buttons change the screen at once; the server's answer arrives after.
- Other phones see changes within about 3 seconds.
- Answers are compressed; script files are kept by phones for a week.
- Every page loads in a small fixed number of database questions, however
  many staff or bills there are.
- The database wait no longer blocks other phones (psycogreen).
- Slow phones: charts are drawn only when their tab is open; no hidden
  animations run; no blur or fixed backgrounds on phones (costly to redraw
  while scrolling).
- What code cannot fix: Render's free plan sleeps after 15 minutes with no
  visitors (first visit then takes 30–50 s), and the distance between the
  Render and Supabase regions.

## 9. Running costs and limits (Render free plan)

- Free plan: about 750 server hours a month (enough for one site running
  all month, not two), limited build minutes and bandwidth.
- Builds happen only when files the live app runs change (`render.yaml`
  buildFilter). Tests, this blueprint and notes do not use build minutes.
- Render's health check uses `/health`, which answers at once without the
  database, so a slow database moment no longer gets the site restarted.
- Keep only ONE Render service. A second one (e.g. an old trial copy)
  shares the same free hours and makes them run out early.
- `DATABASE_URL` always points to Supabase. Never add a Render database.

## 10. Safety and privacy

- No passwords, keys or real staff data are kept in the code. Secrets live
  in Render's settings (names only are listed in `render.yaml`).
- Supabase's public internet access to the app's tables is switched off by
  the app on every start (shown on Diagnostics).
- Forms are protected against forged posts (CSRF); logins are throttled
  after failed attempts.
- This repository is **public** on GitHub today. To keep this blueprint and
  the code private, change it to private in GitHub: Settings > General >
  Danger Zone > Change visibility. Render keeps working with a private repo.

## 11. Where things are (files)

| File | What it is |
|---|---|
| `app.py` | The whole server: data tables, pages, scoring, picking, alerts. |
| `templates/` | One file per page. `_picking.html` is the picking screen. |
| `static/` | Styles, scripts, icons, the phone-alert worker (`sw.js`). |
| `utils.py`, `picking_pdf.py` | The PDF reports (`picking_pdf.py`: picking report and staff day report). |
| `webpush.py` | Sends phone alerts. |
| `render.yaml` | How Render runs the site and which settings exist. |
| `requirements.txt` | The Python packages the site needs. |
| `tests_*.py`, `run_tests.sh` | About 1,050 automatic checks, run on every push. |
| `CLAUDE.md` | Working rules for the AI that maintains the code. |
| `BLUEPRINT.md` | This file. |
| `tools_recommend_targets.py` | Optional helper to suggest hourly targets from history. |

## 12. Settings (names only; values are in Render, never in code)

SECRET_KEY, DATABASE_URL, ADMIN_EMAIL, ADMIN_PASSWORD, mail settings
(MAIL_*, REPORT_EMAIL_TO, EMAIL_PROVIDER, EMAIL_FROM, BREVO_API_KEY,
RESEND_API_KEY), map and SMS (TOMTOM_API_KEY, TWILIO_*, ADMIN_PHONE,
STORE_LAT, STORE_LNG), targets (PICKER_TARGET_HR, CHECKER_TARGET_HR,
URGENT_BILL_WEIGHT), phone alerts (VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY),
GPS (GPS_*), TRIAL_DEMO (keep 0 on the live site).

## 13. Daily upkeep

At the end of each day this file is checked against the code and rewritten
where anything changed, and old wording is replaced. Every code change also
updates the matching section here.
