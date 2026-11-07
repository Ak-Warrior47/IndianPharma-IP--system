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
  back to SQLite (`pharma_final.db`).
- Full suite: `./run_tests.sh` (about 600 checks). One suite:
  `python3.13 tests_levels.py`. CI runs the same on every push
  (`.github/workflows/tests.yml`).
- Tests write to `pharma_final.db`; restore it before committing:
  `git checkout pharma_final.db`.
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
- `utils.py`: the PDF report.

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

## Design direction
- The owner rejects anything that looks AI-made: no cream plus serif plus one
  coloured word, no glassmorphism, no purple gradients, no neon, no emoji as
  icons. Use specific, real details from the warehouse instead.
- Every page must fit phones (iPhone SE upward), phones held sideways,
  tablets and large monitors. Respect `prefers-reduced-motion`.
- Check UI changes in a real browser with screenshots. Skip the browser when
  the owner says "backend only" or "no screenshots".

## Friction log (mistakes made here before)
- Jinja: `period.items` resolves to dict.items(); use `period["items"]`.
- SQLite reuses deleted row ids, and tests can inherit orphan rows: clear
  child rows for a new employee id in test setup.
- Removing a feature once deleted unrelated code next to it; diff carefully.
- `checker_team_day_totals` per-person fallback applies only when the entered
  totals differ (otherwise it multiplied the day by head-count).

## Talking to the owner
- The owner writes fast with typos and is often on a phone. Read for intent;
  ask only when a choice really changes the outcome.
- Keep replies short and plain, and lead with the result.
