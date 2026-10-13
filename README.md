# 🔵 Dashboard

GitHub Pages dashboards for Red Hat Storage projects.  
Each sub-folder is an independent dashboard — its own workflow, its own Pages path.

| Dashboard | Description | Link |
|---|---|---|
| [rdr/](rdr/) | Regional Disaster Recovery — PR tracker for `red-hat-storage/ocs-ci` | [View →](https://prsurve.github.io/dashboard/rdr/) |
| [jira-dr/](jira-dr/) | DR bugs & features from Jira (RHSTOR + OCSQE), with version chips | [View →](https://prsurve.github.io/dashboard/jira-dr/) |

## Adding a new dashboard

1. Create a folder: `mkdir <project>/`
2. Copy `rdr/dr_github_agent.py` (or write your own agent)
3. Copy `rdr/.github/workflows/rdr_dashboard.yml` and adapt the `--repo` flag
4. Push — the workflow runs automatically the next morning (07:00 UTC) or trigger manually

## Setup (one-time per repo)

1. **Settings → Pages** → Source: `gh-pages` branch → `/` root → Save
2. **Settings → Actions → Workflow permissions** → `Read and write permissions` → Save
3. Trigger any workflow manually from the **Actions** tab to generate the first report

### Jira dashboard (`jira-dr/`) extra setup

The Jira dashboard reads from Jira Cloud and needs an API token (no `GITHUB_TOKEN`
equivalent). One-time: create an Atlassian API token at
<https://id.atlassian.com/manage-profile/security/api-tokens>, then add two repo
secrets under **Settings → Secrets and variables → Actions**:

- `JIRA_EMAIL` — your Red Hat Atlassian account email
- `JIRA_API_TOKEN` — the API token

## License

Apache 2.0
