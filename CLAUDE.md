# Indian Pharma IP System

Staff KPI, scoring and delivery app for the Indian Pharma warehouse. Flask +
SQLAlchemy + Jinja, one big `app.py`. Deployed on Render (free tier, one
eventlet worker); the database is Supabase PostgreSQL.

## Hard rules (never break)
- `DATABASE_URL` always points to Supabase. Never add a Render-managed
  database: `render.yaml` keeps `DATABASE_URL: sync: false` and has no
  `databases:` block. `tests_deploy_config.py` enforces this.
- Never commit secrets, keys, passwords or real staff data.
- No model names in commits, code or PRs.
- Work on the assigned `claude/...` branch, then fast-forward `main` and push
  both (the owner approved this flow). No pull requests unless asked.
- Every change ships with tests, and `./run_tests.sh` must be green first.

## Commands
- Python is `python3.13`. Unset `DATABASE_URL` for local work: the app falls
  back to SQLite (`instance/pharma_final.db`, untracked).
- Full suite: `./run_tests.sh` (about 900 checks). One suite:
  `python3.13 tests_picker_v2.py`. CI runs the same on every push
  (`.github/workflows/tests.yml`).
- The suite wipes local test accounts; re-create any you use for browser
  checks after a run.
- Local server: `python3.13 -c "import sys; sys.path.insert(0,'.'); from app import app; app.run(port=5077, use_reloader=False)"`.
  Templates are cached, so restart the server after editing a template.
- Playwright and Chromium are preinstalled (`require('playwright')`).

## Where things live
- `app.py`: models, routes, scheduler jobs, all scoring.
  `build_analytics()` is the ONE source of scores and point breakdowns; never
  compute a score anywhere else.
- `templates/`: one file per page. `login.html` is standalone (its own CSS);
  every other page loads `static/css/ip.css`.
- `static/js/email-suggest.js`: the @pharmaip.com pop-up. Any page with an
  email input must load it (`tests_email_suggest.py` checks).
- `utils.py`: the PDF report. `picking_pdf.py`: the picking report PDF.
- Picking (bill-based): models `PickBill`, `PickClaim`, `PickMissing`,
  `PickEvent`, `PickDay`; `picking_state()` builds every live screen in a
  fixed handful of queries; `templates/_picking.html` is the picker and Pick
  Assigner screen (also the admin Live floor and the multitask Picker box);
  `/pick/me` is the picker screen on its own page; admin pages are
  `/picking` (Live floor + item time, Reports, Leaderboard).
- `static/js/chart-values.js`: exact numbers on every chart (bar tops, pie
  slices, "Name — n (x%)" legend). Every page with a chart loads it.
- `webpush.py` + `static/sw.js`: phone alerts. `/admin/diagnostics` shows
  each database table, row count and whether the public API is closed.

## Owner's business rules (the intent; code follows these)
- Checker pending is a TEAM figure. Each checker enters the same day total;
  the day total is the MAX entered, not the sum.
  Pending = day total − all checkers' (normal + urgent) bills.
  Example: total 100, three checkers × 30 → pending 10.
- Urgent + normal can never exceed the day total, per checker or for the team.
- Urgent work earns more: ×1.5 (`URGENT_BILL_WEIGHT`), for items AND bills.
- A part can never exceed its whole, for every role (`entry_problems`).
- Level-up: a day scoring 72+ that beats the person's best becomes their new
  target, with a congratulations notification. Same for best week (Mon–Sat)
  and best month. Targets only go up, and old history never triggers level-ups.
- "Month" means the calendar month everywhere.
- Staff emails are `name@pharmaip.com`.
- Picking: the Pick Assigner adds bills (prefix IP2627/, IP2728/ from
  1 April; number never reused; `U` at the end = urgent). Pickers take items
  and a timer runs. Item time is set by the ADMIN only (Picking > Item time,
  stored in `app_keys`); defaults 35 s normal, 30 s urgent.
- Points per item: normal time 1.5, extra time 1, report time 0.5 (still
  allowed), late −1 per started minute, capped at −5. Missing items pay only
  when the assigner marks them Not available. Urgent pool 10 points.
- The assigner can Pause / Resume a bill (timers stop, time given back) and
  Pause all / Resume all / Close all. Close today fixes the checker day total.
- Bills show urgent first in their own box, then ascending bill number.
- Pause gives pickers a 7 s warning (`PICK_PAUSE_GRACE`) before the timer
  stops; once stopped they may "Work on another bill" (their items on the
  paused bill go back on it). Taking 4+ items asks "Sure?" first.
- One phone per staff login (`Employee.session_key`): a new sign-in signs the
  old phone out. Admins are exempt.
- No internet: picking taps queue and send when back; Done counts from the
  tap (at most 2 minutes back). Admin and assigner can pick via `/pick/me`.
- While picking, the picker sees only that bill, full screen: light green
  (normal), light orange (extra), light red (time over), flashing (late).
- Multitask: ONE extra role at a time. Picker as a multitask role is live
  picking (no typed numbers); grabs become the Picker multitask entry and
  never touch the main role's figures. "Other" is shown as Breakage & Expiry.

## Design direction
- The owner rejects anything that looks AI-made: no cream plus serif plus one
  coloured word, no glassmorphism, no purple gradients, no neon, no emoji as
  icons. Use specific, real details from the warehouse instead.
- Every page must fit phones (iPhone SE upward), phones held sideways,
  tablets and large monitors. Respect `prefers-reduced-motion`.
- Check UI changes in a real browser with screenshots. Skip the browser when
  the owner says "backend only" or "no screenshots".

## Speed (the owner notices every delay)
- One eventlet worker: `psycogreen` (in requirements) makes database waits
  cooperative. Never add a query per bill / per picker / per staff member:
  load once and group (`req_memo`, `_load_for_bills`, `picker_pick_stats`).
- Picking buttons change the screen at once; an action's reply carries the
  fresh state (`_pick_reply`). Refreshes send `?v=` and get `{"same": true}`
  when nothing changed (`PICK_VER`, bumped by an after_flush listener).
- Replies are gzip-compressed; static links carry `?v=APP_BUILD` (a hash of
  the code) and are cached a week. Phones reload when `APP_BUILD` changes.

## Friction log (mistakes made here before)
- Jinja: `period.items` resolves to dict.items(); use `period["items"]`.
- SQLite reuses deleted row ids, and tests can inherit orphan rows: clear
  child rows for a new employee id in test setup.
- Removing a feature once deleted unrelated code next to it; diff carefully.
- `checker_team_day_totals` per-person fallback applies only when the entered
  totals differ (otherwise it multiplied the day by head-count).
- Only one `#pickApp` per page; the assigner's multitask Picker links to
  `/pick/me` instead of embedding a second one.
- Chart.js: never write to `chart.options` in a plugin (it is a Proxy and
  loops); change `chart.config.options`. Pie and doughnut share one legend
  label function, so wrap it once.
- An `after_request` edit once stripped the decorator from the next function;
  check decorators after inserting code above a decorated function.
- `render.yaml` once forced 100 s per item; item time now lives in the admin
  setting, not an env var.
- Phones on an old page made "slow on one phone, fast on another"; the auto
  reload on a new `APP_BUILD` fixes that, but a deploy still needs one manual
  refresh on pages older than that feature.

## Talking to the owner
- The owner writes fast with typos and is often on a phone. Read for intent;
  ask only when a choice really changes the outcome.
- Keep replies short and plain, and lead with the result.
