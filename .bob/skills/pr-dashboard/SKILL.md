---
name: pr-dashboard
description: >
  Build, update, or extend a GitHub PR dashboard for any repository.
  Use when the user wants to create a PR tracking dashboard, add a new
  squad/team filter, add a new tab or metric, deploy to GitHub Pages,
  or generate a Slack report for pull requests in a GitHub repo.
---

# PR Dashboard Skill

This skill guides you through building or modifying a GitHub PR dashboard —
a Python script + a static HTML shell + a GitHub Actions workflow that deploys
to GitHub Pages.

## Architecture: data/view split (live dashboard)

The dashboard uses a **JSON + static-shell** split so the page updates
frequently without rebuilding HTML:

- **`<squad>/dr_github_agent.py`** — run with `--json-out data.json`, it fetches
  and enriches the PRs and writes **only** `data.json` (metadata + raw PR
  records). This is the single source of truth.
- **`<squad>/index.html`** — a *static shell* committed to the repo. It
  `fetch()`es `./data.json`, renders all tabs client-side (cards, tables,
  metrics, scoring), and **auto-polls `data.json` every 5 min** (plus on tab
  re-focus). A live/stale/error pill shows freshness.
- **Workflow** — runs **hourly**, regenerates `data.json`, and deploys the
  static shell + fresh data. Only the small JSON changes between runs, so the
  HTML no longer churns in git.

All derived views (summary cards, metrics, merge-score) are computed in JS from
the `prs` array — keep the Python→JS logic in sync when changing either side.

The canonical reference implementation lives in `rdr/dr_github_agent.py` +
`rdr/index.html` (the RDR squad dashboard). Use it as the base for every new
dashboard.

> Legacy note: `dr_github_agent.py` still has a `--html` flag that bakes a
> self-contained static report (data embedded). It's kept for local one-off use
> only — the deployed dashboard uses `--json-out` + the static shell.

---

## Step 1 — Gather Requirements

Use `ask_followup_question` to collect:

1. **Target repo** — the `owner/repo` to track (e.g. `red-hat-storage/ocs-ci`)
2. **Squad / filter name** — short slug used for the subdirectory and workflow
   (e.g. `rdr`, `mcg`, `ceph`, `ui`)
3. **Keywords** — list of title/branch/label keywords that identify relevant PRs
4. **Must-match labels** — GitHub label names that unconditionally include a PR
   (e.g. `squad/turquoise`, `area/ceph`)
5. **Slack webhook** — optional; `None` to skip
6. **Pages subdirectory** — defaults to the squad slug (e.g. `rdr/`, `mcg/`)

If any answer is missing, use a sensible default and note it.

---

## Step 2 — Locate the Template

Read the reference agent to understand the current structure before touching it:

```
read_file path:<repo-root>/rdr/dr_github_agent.py range:1-100
```

Then skim the `export_html`, `_merge_score`, and `post_slack` symbols using
`GetSymbolsOverview` or `FindSymbol`.

---

## Step 3 — Create the New Agent File + Static Shell

Copy **both** reference files to a new subdirectory named after the squad slug:

```
<repo-root>/<squad-slug>/dr_github_agent.py   (e.g. mcg/dr_github_agent.py)
<repo-root>/<squad-slug>/index.html           (the static shell)
```

In `dr_github_agent.py`, make the following targeted substitutions using
`apply_diff` or `search_and_replace` — **never rewrite the whole file**:

| Constant | New value |
|---|---|
| `OWNER_REPO` | Target repo string |
| `RDR_KEYWORDS` | New keyword list (keep format — one string per line) |
| `RDR_LABELS` | New label set |
| Module docstring | Update squad name and repo references |

In `index.html`, update only the display strings:

| Field | Change to |
|---|---|
| `<title>` | `<Squad> Dashboard` |
| `<h1>` emoji + name | Match the new squad (e.g. `🟡 MCG Dashboard`) |

Leave all other logic (pagination, enrichment, the JS render/filter/sort/poll
code, CSS) untouched. `data.json` is squad-agnostic — the shell adapts to
whatever the agent emits.

---

## Step 4 — Create the GitHub Actions Workflow

Create `.github/workflows/<squad-slug>_dashboard.yml` by adapting the
reference workflow at `.github/workflows/rdr_dashboard.yml`.

Key substitutions:

| Field | Change to |
|---|---|
| `name:` | `<Squad> PR Dashboard` |
| script path in `run:` | `<squad-slug>/dr_github_agent.py` |
| `--repo` flag | New target repo |
| `--json-out` output | `/tmp/data.json` |
| Pages prep | `mkdir -p /tmp/pages/<squad-slug>`, then copy **both** `<squad-slug>/index.html` and `/tmp/data.json` into it |
| Workflow file name | `<squad-slug>_dashboard.yml` |

Keep `--no-checks --workers 10 --state all` — these are always the right
defaults for the scheduled job. Use `--json-out /tmp/data.json` (not `--html`):
the deployed page is the static shell, which fetches `data.json`.

The schedule is **hourly** (`cron: "0 * * * *"`). The published page auto-polls
`data.json` every 5 min, so viewers see fresh data within minutes of each build
without a manual refresh.

---

## Step 5 — Validate

Syntax-check the agent, then smoke-test the shell + data locally:

```
python3 -m py_compile <squad-slug>/dr_github_agent.py && echo OK

# generate a real data.json, stage it next to the shell, serve over HTTP
python3 <squad-slug>/dr_github_agent.py --repo <owner/repo> --no-checks \
  --json-out /tmp/preview/<squad-slug>/data.json
cp <squad-slug>/index.html /tmp/preview/<squad-slug>/index.html
cd /tmp/preview && python3 -m http.server 8777
# open http://localhost:8777/<squad-slug>/
```

`file://` will not work — the shell uses `fetch()`, which needs HTTP.
Do not proceed until `OK` prints and the page renders all tabs.

---

## Step 6 — Commit and Push

```bash
cd <dashboard-repo-clone>
git add <squad-slug>/ .github/workflows/<squad-slug>_dashboard.yml .bob/
git commit -m "feat(<squad-slug>): add PR dashboard for <owner/repo>"
git push origin main
```

---

## Step 7 — Confirm GitHub Pages is Enabled

Pages must be set to deploy from the `gh-pages` branch. Check once per repo:

```bash
gh api repos/<github-user>/dashboard/pages --jq '.source'
```

If Pages is not enabled or the source is wrong, instruct the user to:
> Settings → Pages → Source → Deploy from branch → `gh-pages` → `/ (root)`

---

## Step 8 — Tell the User

Report:
- New agent + static shell paths in the repo
- Workflow file name and cron schedule (`0 * * * *` = hourly)
- That the page auto-polls `data.json` every 5 min (live without manual refresh)
- Dashboard URL: `https://<github-user>.github.io/dashboard/<squad-slug>/`
- How to trigger a manual run: Actions → `<Squad> PR Dashboard` → Run workflow

---

## Modifying an Existing Dashboard

Most presentation changes now live in **`<squad>/index.html`** (the static
shell), not in the Python. Only touch the agent when you need a new *data*
field in `data.json`.

When the user asks to **add a tab, column, filter card, or metric**:

1. Read the relevant section of `index.html`.
2. Identify the exact insertion point.
3. Use `apply_diff` — one `SEARCH/REPLACE` block per change.
4. If the change needs a field not already in `data.json`, add it to
   `_enrich_pr()` in the agent too (and `python3 -m py_compile` it).
5. Commit, push.

### Adding a new filter card
- Add a `statCard(...)` call inside `renderCards()` in `index.html`.
- Ensure the `data-filter` value and `_applyFilters()` handle the new dimension
  (`_dim()` / `_raw()` prefixes, and a matching `data-*` attr on the rows in
  `renderPrTable()`).

### Adding a new tab
1. Add a tab button in the `.tabs` div.
2. Add `<div id="tab-<name>" class="tab-panel">` with empty containers.
3. Add a `renderNewTab(prs)` function and call it from `render()`.
4. If the tab needs sorting, add a `sortNewTable(col)` mirroring
   `sortReadyTable`.

### Adding a column to the PR table
1. Add `<th onclick="sortTable(N)">Column Name</th>` at the correct index N
   in the `<thead>`.
2. Add the matching `<td>…</td>` at the same index N in `renderPrTable()`.
3. If N is a numeric sort column, add it to the `[0,6,10,11]` arrays passed to
   `_sortTableBy()`.
4. If the column needs new data, add the field in `_enrich_pr()` + `export_json`.

---

## CSS / UI Standards

All dashboards share the same CSS block, which lives in the `<style>` section of
each `<squad>/index.html` shell. When fixing or adding styles, follow these
conventions:

| Element | Rule |
|---|---|
| Stat cards | `.stat-card` — rounded border, `#f6f8fa` bg, `10px 18px` padding, `border-radius:10px` |
| Active card | `border-color:#0969da`, `background:#dbeafe`, `box-shadow:0 0 0 2px #0969da` |
| Hover card | `border-color:#3b82d4`, `background:#eaf3ff`, soft drop shadow |
| Badges | `border-radius:12px`, `font-size:11px`, white text, colored bg |
| Label pills | `border-radius:10px`, `font-size:10px`, `border:1px solid #d0d7de`, `#f6f8fa` bg |
| Tables | `border-collapse:collapse`, `font-size:13px`, `#f6f8fa` header bg |
| Fonts | `-apple-system,"Segoe UI",system-ui,sans-serif` |
| Accent | `#3b82d4` (blue), `#1a7f37` (green), `#cf222e` (red), `#9a6700` (amber) |

Never add `!important` except to override third-party styles that cannot be
reached any other way.

---

## Scoring (Ready to Merge tab)

The merge-readiness score is always **0–5**, computed in JS by `mergeScore()` in
`index.html`:

| Condition | Points |
|---|---|
| PR is not a draft | +1 |
| Review decision = APPROVED | +2 |
| CI status = success | +1 |
| No changes-requested reviewers | +1 |

Labels: `5 = 🟢 Ready`, `4 = 🟡 Almost`, `3 = 🟠 Needs work`, `≤2 = 🔴 Blocked`

Do not change the scoring formula without updating this skill. (A legacy copy of
the same formula lives in `_merge_score()` in the Python, used only by `--html`.)

---

## File Layout Reference

```
<dashboard-repo>/
├── .bob/
│   └── skills/
│       └── pr-dashboard/
│           └── SKILL.md                 ← this file
├── .github/
│   └── workflows/
│       ├── rdr_dashboard.yml            ← RDR squad workflow (reference)
│       └── <squad>_dashboard.yml        ← new squad workflow
├── rdr/
│   ├── dr_github_agent.py              ← RDR agent (canonical reference)
│   └── index.html                     ← static shell (fetches data.json, polls)
└── <squad>/
    ├── dr_github_agent.py              ← new squad agent
    └── index.html                     ← new squad static shell
```

At deploy time the workflow also writes `<squad>/data.json` onto the `gh-pages`
branch — it is generated, not committed to `main`.

Dashboard URLs follow the pattern:
```
https://<github-user>.github.io/dashboard/<squad-slug>/
```

---

## Checklist for a New Dashboard

- [ ] Requirements gathered (repo, slug, keywords, labels)
- [ ] New agent file created under `<slug>/dr_github_agent.py`
- [ ] Constants updated (`OWNER_REPO`, `RDR_KEYWORDS`, `RDR_LABELS`)
- [ ] Static shell created under `<slug>/index.html` (title + `<h1>` updated)
- [ ] Syntax check passes (`python3 -m py_compile`)
- [ ] Shell smoke-tested locally over HTTP (all tabs render from data.json)
- [ ] Workflow file created under `.github/workflows/<slug>_dashboard.yml`
      (hourly cron, `--json-out`, copies shell + data.json to Pages)
- [ ] Committed and pushed to `main`
- [ ] GitHub Pages enabled on `gh-pages` branch
- [ ] Manual workflow run triggered to verify end-to-end
- [ ] Dashboard URL shared with the team
