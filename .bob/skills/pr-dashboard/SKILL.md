---
name: pr-dashboard
description: >
  Build, update, or extend a GitHub PR dashboard for any repository.
  Use when the user wants to create a PR tracking dashboard, add a new
  squad/team filter, add a new tab or metric, deploy to GitHub Pages,
  or generate a Slack report for pull requests in a GitHub repo.
---

# PR Dashboard Skill

This skill guides you through building or modifying a self-contained GitHub
PR dashboard — a single Python script + a GitHub Actions workflow that
generates a multi-tab HTML report and deploys it to GitHub Pages daily.

The canonical reference implementation lives in `rdr/dr_github_agent.py`
(the RDR squad dashboard). Use it as the base for every new dashboard.

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

## Step 3 — Create the New Agent File

Copy the reference implementation to a new subdirectory named after the squad slug:

```
<repo-root>/<squad-slug>/dr_github_agent.py   (e.g. mcg/dr_github_agent.py)
```

Make the following targeted substitutions using `apply_diff` or
`search_and_replace` — **never rewrite the whole file**:

| Constant | New value |
|---|---|
| `OWNER_REPO` | Target repo string |
| `RDR_KEYWORDS` | New keyword list (keep format — one string per line) |
| `RDR_LABELS` | New label set |
| Module docstring | Update squad name and repo references |
| HTML title / `<h1>` emoji+name | Match the new squad (e.g. `🟡 MCG Dashboard`) |

Leave all other logic (pagination, enrichment, scoring, JS, CSS) untouched.

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
| `publish_dir` Pages prep | `mkdir -p /tmp/pages/<squad-slug>` + copy |
| Workflow file name | `<squad-slug>_dashboard.yml` |

Keep `--no-checks --workers 10 --state all` — these are always the right
defaults for the scheduled job.

---

## Step 5 — Validate

Run a syntax check before committing:

```
python3 -m py_compile <squad-slug>/dr_github_agent.py && echo OK
```

If it fails, fix the error. Do not proceed until `OK` is printed.

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
- New agent path in the repo
- Workflow file name and cron schedule (`0 3 * * *` = 03:00 UTC daily)
- Dashboard URL: `https://<github-user>.github.io/dashboard/<squad-slug>/`
- How to trigger a manual run: Actions → `<Squad> PR Dashboard` → Run workflow

---

## Modifying an Existing Dashboard

When the user asks to **add a tab, column, filter card, or metric** to an
existing dashboard:

1. Read the relevant section of the agent with `read_file` (line range).
2. Identify the exact insertion point.
3. Use `apply_diff` — one `SEARCH/REPLACE` block per change.
4. Syntax-check, copy to the dashboard repo clone, commit, push.

### Adding a new filter card
- Add a `_stat_card(...)` call after the line that builds `summary_cards`.
- Ensure the `data-filter` value and `_applyFilters()` JS handle the new dimension.

### Adding a new tab
1. Add a tab button in the `.tabs` div.
2. Add `<div id="tab-<name>" class="tab-panel">` after the last existing panel.
3. Populate `{new_rows}` by building the rows string in `export_html`.
4. If the tab needs sorting, add a `sortNewTable(col)` JS function mirroring
   the existing `sortReadyTable`.

### Adding a column to the PR table
1. Add `<th onclick="sortTable(N)">Column Name</th>` at the correct index N
   in the `<thead>`.
2. Add `<td>...</td>` at the same index N in the `rows_html` loop.
3. If N is a numeric sort column, add it to the `[0,6,10,11,...]` array in
   `sortTable()`.

---

## CSS / UI Standards

All dashboards in this repo share the same CSS block. When fixing or adding
styles, follow these conventions:

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

The merge-readiness score is always **0–5**:

| Condition | Points |
|---|---|
| PR is not a draft | +1 |
| Review decision = APPROVED | +2 |
| CI status = success | +1 |
| No changes-requested reviewers | +1 |

Labels: `5 = 🟢 Ready`, `4 = 🟡 Almost`, `3 = 🟠 Needs work`, `≤2 = 🔴 Blocked`

Do not change the scoring formula without updating this skill.

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
│   └── dr_github_agent.py              ← RDR agent (canonical reference)
└── <squad>/
    └── dr_github_agent.py              ← new squad agent
```

Dashboard URLs follow the pattern:
```
https://<github-user>.github.io/dashboard/<squad-slug>/
```

---

## Checklist for a New Dashboard

- [ ] Requirements gathered (repo, slug, keywords, labels)
- [ ] New agent file created under `<slug>/dr_github_agent.py`
- [ ] Constants updated (`OWNER_REPO`, `RDR_KEYWORDS`, `RDR_LABELS`, title)
- [ ] Syntax check passes (`python3 -m py_compile`)
- [ ] Workflow file created under `.github/workflows/<slug>_dashboard.yml`
- [ ] Committed and pushed to `main`
- [ ] GitHub Pages enabled on `gh-pages` branch
- [ ] Manual workflow run triggered to verify end-to-end
- [ ] Dashboard URL shared with the team
