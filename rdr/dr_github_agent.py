"""
DR GitHub Agent — Local PR Tracker for Regional Disaster Recovery

Lists all RDR-related Pull Requests on red-hat-storage/ocs-ci and enriches
each one with:
  • Status (open / closed / merged)
  • Draft state
  • Review summary (approved / changes-requested / pending / dismissed)
  • All labels
  • Assignees & requested reviewers
  • Milestone
  • CI check-run status (latest commit)
  • Age (days since creation)
  • Comment count
  • Files changed count

Token resolution order (no ocs-ci framework required):
  1. GITHUB_TOKEN environment variable
  2. ~/.github_token file (plain text, first line)
  3. Unauthenticated (60 req/h rate limit — only good for small queries)

Usage:
    python ocs_ci/ocs/dr/dr_github_agent.py [--state open|closed|all] [--json] [--no-checks]
    python ocs_ci/ocs/dr/dr_github_agent.py --html rdr_prs.html
    python ocs_ci/ocs/dr/dr_github_agent.py --slack https://hooks.slack.com/...

Examples:
    # Show all open RDR PRs in a terminal table
    python ocs_ci/ocs/dr/dr_github_agent.py

    # Write a shareable HTML report (open in any browser, attach to email/Slack)
    python ocs_ci/ocs/dr/dr_github_agent.py --html

    # Post a summary to a Slack channel via incoming webhook
    python ocs_ci/ocs/dr/dr_github_agent.py --slack https://hooks.slack.com/services/T.../B.../xxx

    # Combine: HTML file + Slack post in one run
    python ocs_ci/ocs/dr/dr_github_agent.py --html --slack https://hooks.slack.com/...

    # Dump raw JSON for scripting
    python ocs_ci/ocs/dr/dr_github_agent.py --json

    # Include already-closed / merged PRs
    python ocs_ci/ocs/dr/dr_github_agent.py --state all
"""

import argparse
import html as _html_mod
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

OWNER_REPO = "red-hat-storage/ocs-ci"
API_BASE = "https://api.github.com"

# Keywords matched case-insensitively against: title, labels, milestone, and
# head branch.  Use space-padded " dr " so standalone "DR" in a title matches
# without also matching unrelated words like "address" or "dryrun".
RDR_KEYWORDS = [
    "rdr",
    "regional-dr",
    "regional dr",
    "disaster-recovery",
    "disaster recovery",
    " dr ",  # catches "Add OLS DR Recipe" style titles
    "dr-",  # catches "dr-policy", "dr-cluster" style prefixes
    "failover",
    "relocate",
    "drpolicy",
    "drcluster",
    "drplacementcontrol",
    "volsync",
    "odr",
    "recipe",  # DR Recipe generation / runbook content
]

# Labels that unconditionally mark a PR as RDR-related regardless of title/branch.
# Use exact case as it appears on GitHub (matching is case-insensitive below).
RDR_LABELS = {
    "squad/turquoise",
}

# Review states returned by the GitHub Reviews API
REVIEW_APPROVED = "APPROVED"
REVIEW_CHANGES = "CHANGES_REQUESTED"
REVIEW_DISMISSED = "DISMISSED"
REVIEW_COMMENTED = "COMMENTED"


# ── Token helpers ────────────────────────────────────────────────────────────


def _resolve_token() -> Optional[str]:
    """Return a GitHub personal access token from env or ~/.github_token."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        logger.debug("GitHub token loaded from GITHUB_TOKEN env var.")
        return token

    token_file = os.path.expanduser("~/.github_token")
    if os.path.isfile(token_file):
        with open(token_file) as fh:
            token = fh.readline().strip()
        if token:
            logger.debug("GitHub token loaded from ~/.github_token.")
            return token

    logger.warning(
        "No GitHub token found. Unauthenticated requests are rate-limited to "
        "60/hour. Set GITHUB_TOKEN or create ~/.github_token to avoid this."
    )
    return None


def _headers(token: Optional[str]) -> Dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ── GitHub API helpers ───────────────────────────────────────────────────────


def _get(url: str, token: Optional[str], params: Optional[Dict] = None) -> Any:
    """Single GET with basic error handling."""
    resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
    if resp.status_code == 403:
        raise RuntimeError(
            f"GitHub API rate limit or auth error (403). "
            f"Set GITHUB_TOKEN to avoid this. URL: {url}"
        )
    resp.raise_for_status()
    return resp.json()


def _paginate(
    url: str, token: Optional[str], params: Optional[Dict] = None
) -> List[Any]:
    """Follow GitHub pagination and return a flat list of all items."""
    items: List[Any] = []
    p = dict(params or {})
    p.setdefault("per_page", 100)
    page = 1
    while True:
        p["page"] = page
        resp = requests.get(url, headers=_headers(token), params=p, timeout=30)
        if resp.status_code == 403:
            raise RuntimeError(
                "GitHub API rate limit or auth error (403). Set GITHUB_TOKEN."
            )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        items.extend(batch)
        # Stop if this page was the last (fewer than per_page results)
        if len(batch) < p["per_page"]:
            break
        page += 1
    return items


# ── RDR keyword matching ─────────────────────────────────────────────────────


def _is_rdr_related(pr: Dict) -> bool:
    """Return True if the PR is RDR-related.

    Matches when ANY of the following are true:
    - A label exactly matches one of RDR_LABELS (e.g. Squad/Turquoise).
    - The title, any label name, milestone title, or head branch contains an
      RDR keyword from RDR_KEYWORDS (case-insensitive).
    """
    labels_lower = {lbl.get("name", "").lower() for lbl in pr.get("labels", [])}

    # Explicit label match — catches Squad/Turquoise PRs with no RDR keywords
    if labels_lower & RDR_LABELS:
        return True

    # Pad title with spaces so " dr " matches at word boundaries anywhere,
    # including at the start/end of the string (e.g. "DR Recipe" → " dr recipe").
    title_padded = " " + pr.get("title", "").lower() + " "

    haystack_parts = [title_padded]
    haystack_parts.extend(labels_lower)

    milestone = pr.get("milestone") or {}
    haystack_parts.append(milestone.get("title", "").lower())

    head = pr.get("head") or {}
    haystack_parts.append(head.get("ref", "").lower())

    haystack = " ".join(haystack_parts)
    return any(kw in haystack for kw in RDR_KEYWORDS)


# ── Per-PR enrichment ────────────────────────────────────────────────────────


def _age_days(created_at: str) -> int:
    """Return whole days since the PR was created."""
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


def _pr_status(pr: Dict) -> str:
    """Return human-readable status: open / draft / merged / closed."""
    if pr.get("draft"):
        return "draft"
    if pr.get("merged_at"):
        return "merged"
    state = pr.get("state", "open")
    return state  # 'open' or 'closed'


def _fetch_reviews(pr_number: int, token: Optional[str]) -> Dict[str, Any]:
    """
    Fetch all reviews for a PR and return a summary dict.

    Returns:
        {
            "decision": "APPROVED" | "CHANGES_REQUESTED" | "PENDING" | "COMMENTED",
            "approved_by": [...],
            "changes_by": [...],
            "review_count": int,
        }
    """
    url = f"{API_BASE}/repos/{OWNER_REPO}/pulls/{pr_number}/reviews"
    try:
        reviews = _paginate(url, token)
    except Exception as exc:
        logger.debug(f"Could not fetch reviews for PR #{pr_number}: {exc}")
        return {
            "decision": "unknown",
            "approved_by": [],
            "changes_by": [],
            "review_count": 0,
        }

    approved_by: List[str] = []
    changes_by: List[str] = []
    # Track latest state per reviewer (later reviews override earlier ones)
    latest: Dict[str, str] = {}
    for review in reviews:
        login = (review.get("user") or {}).get("login", "unknown")
        state = review.get("state", "")
        if state in (REVIEW_APPROVED, REVIEW_CHANGES, REVIEW_DISMISSED):
            latest[login] = state

    for login, state in latest.items():
        if state == REVIEW_APPROVED:
            approved_by.append(login)
        elif state == REVIEW_CHANGES:
            changes_by.append(login)

    if changes_by:
        decision = "CHANGES_REQUESTED"
    elif approved_by:
        decision = "APPROVED"
    elif reviews:
        decision = "COMMENTED"
    else:
        decision = "PENDING"

    return {
        "decision": decision,
        "approved_by": approved_by,
        "changes_by": changes_by,
        "review_count": len(reviews),
    }


def _fetch_check_status(pr: Dict, token: Optional[str], fetch_checks: bool) -> str:
    """
    Return the combined CI check-runs conclusion for the PR head SHA.
    Returns one of: success / failure / pending / skipped / unknown
    """
    if not fetch_checks:
        return "skipped"

    sha = (pr.get("head") or {}).get("sha", "")
    if not sha:
        return "unknown"

    url = f"{API_BASE}/repos/{OWNER_REPO}/commits/{sha}/check-runs"
    try:
        data = _get(url, token, params={"per_page": 100})
    except Exception as exc:
        logger.debug(f"Could not fetch checks for SHA {sha}: {exc}")
        return "unknown"

    runs = data.get("check_runs", [])
    if not runs:
        return "pending"

    conclusions = {r.get("conclusion") for r in runs}
    # If any run is still in-progress, overall is pending
    statuses = {r.get("status") for r in runs}
    if "in_progress" in statuses or "queued" in statuses:
        return "pending"
    if (
        "failure" in conclusions
        or "timed_out" in conclusions
        or "action_required" in conclusions
    ):
        return "failure"
    if "success" in conclusions or "neutral" in conclusions:
        return "success"
    return "unknown"


def _fetch_files_changed(pr_number: int, token: Optional[str]) -> int:
    """Return the number of files changed in the PR."""
    url = f"{API_BASE}/repos/{OWNER_REPO}/pulls/{pr_number}/files"
    try:
        files = _paginate(url, token)
        return len(files)
    except Exception:
        return -1


def _enrich_pr(pr: Dict, token: Optional[str], fetch_checks: bool) -> Dict[str, Any]:
    """Build the full enriched PR record."""
    number = pr["number"]
    labels = [lbl["name"] for lbl in pr.get("labels", [])]
    assignees = [u["login"] for u in pr.get("assignees", [])]
    requested_reviewers = [u["login"] for u in pr.get("requested_reviewers", [])]
    milestone = (pr.get("milestone") or {}).get("title", None)
    comments = pr.get("comments", 0) + pr.get("review_comments", 0)

    reviews = _fetch_reviews(number, token)
    ci_status = _fetch_check_status(pr, token, fetch_checks)
    files_changed = _fetch_files_changed(number, token)

    return {
        "number": number,
        "title": pr["title"],
        "url": pr["html_url"],
        "status": _pr_status(pr),
        "draft": pr.get("draft", False),
        "author": (pr.get("user") or {}).get("login", "unknown"),
        "created_at": pr.get("created_at", ""),
        "updated_at": pr.get("updated_at", ""),
        "merged_at": pr.get("merged_at"),
        "age_days": _age_days(
            pr.get("created_at", datetime.now(timezone.utc).isoformat())
        ),
        "labels": labels,
        "assignees": assignees,
        "requested_reviewers": requested_reviewers,
        "milestone": milestone,
        "review_decision": reviews["decision"],
        "approved_by": reviews["approved_by"],
        "changes_requested_by": reviews["changes_by"],
        "review_count": reviews["review_count"],
        "comments": comments,
        "files_changed": files_changed,
        "ci_status": ci_status,
        "base_branch": (pr.get("base") or {}).get("ref", ""),
        "head_branch": (pr.get("head") or {}).get("ref", ""),
    }


# ── Main agent class ─────────────────────────────────────────────────────────


class DRGitHubAgent:
    """
    Fetches and enriches all RDR-related Pull Requests from the ocs-ci repo.

    Usage::

        agent = DRGitHubAgent()
        prs   = agent.list_rdr_prs()          # enriched list
        agent.print_table(prs)                 # pretty console table
        agent.print_summary(prs)               # stats summary
    """

    def __init__(
        self,
        token: Optional[str] = None,
        repo: str = OWNER_REPO,
        fetch_checks: bool = True,
    ):
        """
        Args:
            token:         GitHub personal access token.  If None, auto-resolved
                           from GITHUB_TOKEN env var or ~/.github_token.
            repo:          GitHub repo in ``owner/repo`` format.
            fetch_checks:  If True, fetch CI check-run status per PR (costs one
                           extra API call per PR).  Set False to speed up queries
                           when you don't need CI status.
        """
        self.token = token or _resolve_token()
        self.repo = repo
        self.fetch_checks = fetch_checks
        # Override the module-level constant so all helpers use the right repo
        global OWNER_REPO
        OWNER_REPO = self.repo

    # ── Fetching ─────────────────────────────────────────────────────────────

    def _fetch_all_prs(self, state: str = "open") -> List[Dict]:
        """Return raw PR dicts from GitHub for the given state."""
        url = f"{API_BASE}/repos/{self.repo}/pulls"
        return _paginate(
            url,
            self.token,
            params={"state": state, "sort": "updated", "direction": "desc"},
        )

    def list_rdr_prs(self, state: str = "open") -> List[Dict[str, Any]]:
        """
        Return all RDR-related PRs enriched with review, label, CI and age data.

        Args:
            state: ``"open"``, ``"closed"``, or ``"all"``.

        Returns:
            List of enriched PR dicts sorted by PR number descending.
        """
        logger.info(f"Fetching {state} PRs from {self.repo} …")
        raw_prs = self._fetch_all_prs(state)
        logger.info(f"Total PRs fetched: {len(raw_prs)}.  Filtering for RDR …")

        rdr_prs = [pr for pr in raw_prs if _is_rdr_related(pr)]
        logger.info(f"RDR-related PRs found: {len(rdr_prs)}.  Enriching …")

        enriched: List[Dict[str, Any]] = []
        for i, pr in enumerate(rdr_prs, 1):
            logger.info(
                f"  [{i}/{len(rdr_prs)}] Enriching PR #{pr['number']}: {pr['title'][:60]}"
            )
            enriched.append(_enrich_pr(pr, self.token, self.fetch_checks))

        enriched.sort(key=lambda p: p["number"], reverse=True)
        return enriched

    # ── Output helpers ────────────────────────────────────────────────────────

    @staticmethod
    def print_table(prs: List[Dict[str, Any]]) -> None:
        """Print a compact human-readable table to stdout."""
        if not prs:
            print("No RDR-related PRs found.")
            return

        # Column widths
        W_NUM = 6
        W_TITLE = 55
        W_AUTH = 16
        W_STAT = 9
        W_REV = 20
        W_CI = 9
        W_AGE = 5
        W_LBLS = 30

        header = (
            f"{'#':<{W_NUM}} "
            f"{'Title':<{W_TITLE}} "
            f"{'Author':<{W_AUTH}} "
            f"{'Status':<{W_STAT}} "
            f"{'Review':<{W_REV}} "
            f"{'CI':<{W_CI}} "
            f"{'Age':>{W_AGE}} "
            f"{'Labels':<{W_LBLS}}"
        )
        sep = "-" * len(header)

        print(f"\n{'RDR Pull Requests — ' + OWNER_REPO:^{len(header)}}")
        print(sep)
        print(header)
        print(sep)

        for pr in prs:
            title = pr["title"]
            if len(title) > W_TITLE:
                title = title[: W_TITLE - 1] + "…"

            review_str = pr["review_decision"]
            if pr["approved_by"]:
                review_str += f" ({','.join(pr['approved_by'][:2])})"
            if len(review_str) > W_REV:
                review_str = review_str[: W_REV - 1] + "…"

            labels_str = ", ".join(pr["labels"]) if pr["labels"] else "—"
            if len(labels_str) > W_LBLS:
                labels_str = labels_str[: W_LBLS - 1] + "…"

            status = pr["status"]
            if pr["draft"]:
                status = "draft"

            print(
                f"#{pr['number']:<{W_NUM - 1}} "
                f"{title:<{W_TITLE}} "
                f"{pr['author']:<{W_AUTH}} "
                f"{status:<{W_STAT}} "
                f"{review_str:<{W_REV}} "
                f"{pr['ci_status']:<{W_CI}} "
                f"{pr['age_days']:>{W_AGE}}d "
                f"{labels_str:<{W_LBLS}}"
            )

        print(sep)
        print(f"Total: {len(prs)} PR(s)\n")

    @staticmethod
    def print_summary(prs: List[Dict[str, Any]]) -> None:
        """Print a short statistics block to stdout."""
        if not prs:
            return

        statuses = {}
        reviews = {}
        ci_results = {}
        label_counts: Dict[str, int] = {}
        total_age = 0

        for pr in prs:
            statuses[pr["status"]] = statuses.get(pr["status"], 0) + 1
            reviews[pr["review_decision"]] = reviews.get(pr["review_decision"], 0) + 1
            ci_results[pr["ci_status"]] = ci_results.get(pr["ci_status"], 0) + 1
            total_age += pr["age_days"]
            for lbl in pr["labels"]:
                label_counts[lbl] = label_counts.get(lbl, 0) + 1

        avg_age = total_age / len(prs) if prs else 0
        top_labels = sorted(label_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        print("── Summary ────────────────────────────────────────")
        print(f"  Total RDR PRs : {len(prs)}")
        print(f"  By status     : {statuses}")
        print(f"  By review     : {reviews}")
        print(f"  By CI         : {ci_results}")
        print(f"  Avg age       : {avg_age:.0f} days")
        print(f"  Top labels    : {top_labels}")
        print("───────────────────────────────────────────────────\n")

    @staticmethod
    def print_detail(pr: Dict[str, Any]) -> None:
        """Print full details for a single enriched PR."""
        print(f"\n{'─' * 60}")
        print(f"PR #{pr['number']}: {pr['title']}")
        print(f"  URL            : {pr['url']}")
        print(f"  Status         : {pr['status']}{'  [DRAFT]' if pr['draft'] else ''}")
        print(f"  Author         : {pr['author']}")
        print(f"  Base ← Head    : {pr['base_branch']} ← {pr['head_branch']}")
        print(f"  Created        : {pr['created_at']}  ({pr['age_days']}d ago)")
        print(f"  Updated        : {pr['updated_at']}")
        if pr["merged_at"]:
            print(f"  Merged         : {pr['merged_at']}")
        print(f"  Milestone      : {pr['milestone'] or '—'}")
        print(f"  Labels         : {', '.join(pr['labels']) or '—'}")
        print(f"  Assignees      : {', '.join(pr['assignees']) or '—'}")
        print(f"  Req. Reviewers : {', '.join(pr['requested_reviewers']) or '—'}")
        print(f"  Review decision: {pr['review_decision']}")
        if pr["approved_by"]:
            print(f"  Approved by    : {', '.join(pr['approved_by'])}")
        if pr["changes_requested_by"]:
            print(f"  Changes req by : {', '.join(pr['changes_requested_by'])}")
        print(f"  Reviews        : {pr['review_count']}")
        print(f"  Comments       : {pr['comments']}")
        print(f"  Files changed  : {pr['files_changed']}")
        print(f"  CI status      : {pr['ci_status']}")
        print(f"{'─' * 60}\n")

    # ── HTML export ───────────────────────────────────────────────────────────

    @staticmethod
    def export_html(prs: List[Dict[str, Any]], path: str = "rdr_prs.html") -> str:
        """
        Write a self-contained, sortable HTML report to *path* and return the path.

        The file has zero external dependencies — one file you can open in any
        browser, email, or attach to a Slack message.
        """
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # ── helpers ──────────────────────────────────────────────────────────
        def _e(v: Any) -> str:
            """HTML-escape a value."""
            return _html_mod.escape(str(v) if v is not None else "")

        STATUS_COLOR = {
            "open": "#1a7f37",
            "draft": "#9a6700",
            "merged": "#8250df",
            "closed": "#cf222e",
        }
        REVIEW_COLOR = {
            "APPROVED": "#1a7f37",
            "CHANGES_REQUESTED": "#cf222e",
            "PENDING": "#9a6700",
            "COMMENTED": "#57606a",
            "unknown": "#57606a",
        }
        CI_COLOR = {
            "success": "#1a7f37",
            "failure": "#cf222e",
            "pending": "#9a6700",
            "skipped": "#57606a",
            "unknown": "#57606a",
        }

        def _badge(text: str, color: str) -> str:
            return (
                f'<span style="display:inline-block;padding:1px 7px;border-radius:12px;'
                f'font-size:11px;font-weight:600;color:#fff;background:{color}">'
                f"{_e(text)}</span>"
            )

        def _label_pill(name: str) -> str:
            return (
                f'<span style="display:inline-block;margin:1px 2px;padding:1px 6px;'
                f"border-radius:10px;font-size:10px;border:1px solid #d0d7de;"
                f'color:#24292f;background:#f6f8fa">{_e(name)}</span>'
            )

        # ── stat counters for the summary bar ────────────────────────────────
        by_status: Dict[str, int] = {}
        by_review: Dict[str, int] = {}
        by_ci: Dict[str, int] = {}
        for pr in prs:
            by_status[pr["status"]] = by_status.get(pr["status"], 0) + 1
            by_review[pr["review_decision"]] = (
                by_review.get(pr["review_decision"], 0) + 1
            )
            by_ci[pr["ci_status"]] = by_ci.get(pr["ci_status"], 0) + 1

        def _stat_card(label: str, value: Any, color: str = "#24292f") -> str:
            return (
                f'<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;'
                f'padding:12px 20px;text-align:center;min-width:80px">'
                f'<div style="font-size:22px;font-weight:700;color:{color}">{_e(value)}</div>'
                f'<div style="font-size:11px;color:#57606a;margin-top:2px">{_e(label)}</div>'
                f"</div>"
            )

        summary_cards = _stat_card("Total PRs", len(prs), "#3b82d4")
        for st, cnt in sorted(by_status.items()):
            summary_cards += _stat_card(
                st.capitalize(), cnt, STATUS_COLOR.get(st, "#57606a")
            )
        approved_count = by_review.get("APPROVED", 0)
        pending_count = by_review.get("PENDING", 0) + by_review.get("COMMENTED", 0)
        changes_count = by_review.get("CHANGES_REQUESTED", 0)
        summary_cards += _stat_card("Approved", approved_count, "#1a7f37")
        summary_cards += _stat_card("Needs Review", pending_count, "#9a6700")
        summary_cards += _stat_card("Changes Req.", changes_count, "#cf222e")
        if by_ci.get("failure", 0):
            summary_cards += _stat_card("CI Failing", by_ci["failure"], "#cf222e")

        # ── table rows ────────────────────────────────────────────────────────
        rows_html = ""
        for pr in prs:
            status_badge = _badge(
                pr["status"], STATUS_COLOR.get(pr["status"], "#57606a")
            )
            if pr["draft"]:
                status_badge = _badge("draft", STATUS_COLOR["draft"])

            review_badge = _badge(
                pr["review_decision"],
                REVIEW_COLOR.get(pr["review_decision"], "#57606a"),
            )
            ci_badge = _badge(
                pr["ci_status"],
                CI_COLOR.get(pr["ci_status"], "#57606a"),
            )
            labels_html = "".join(_label_pill(lbl) for lbl in pr["labels"]) or "—"

            approved_str = ", ".join(pr["approved_by"]) if pr["approved_by"] else ""
            changes_str = (
                ", ".join(pr["changes_requested_by"])
                if pr["changes_requested_by"]
                else ""
            )
            reviewers_str = (
                ", ".join(pr["requested_reviewers"])
                if pr["requested_reviewers"]
                else "—"
            )
            assignees_str = ", ".join(pr["assignees"]) if pr["assignees"] else "—"

            review_detail = review_badge
            if approved_str:
                review_detail += f'<div style="font-size:10px;color:#57606a;margin-top:2px">✔ {_e(approved_str)}</div>'
            if changes_str:
                review_detail += f'<div style="font-size:10px;color:#cf222e;margin-top:2px">✘ {_e(changes_str)}</div>'

            rows_html += f"""
            <tr>
              <td style="white-space:nowrap">
                <a href="{_e(pr['url'])}" target="_blank" style="font-weight:600;color:#0969da;text-decoration:none">
                  #{_e(pr['number'])}
                </a>
              </td>
              <td>
                <a href="{_e(pr['url'])}" target="_blank"
                   style="color:#24292f;text-decoration:none;font-size:13px"
                   title="{_e(pr['title'])}">{_e(pr['title'])}</a>
              </td>
              <td style="white-space:nowrap;color:#57606a;font-size:12px">{_e(pr['author'])}</td>
              <td>{status_badge}</td>
              <td>{review_detail}</td>
              <td>{ci_badge}</td>
              <td style="white-space:nowrap;font-size:12px">{_e(pr['age_days'])}d</td>
              <td style="font-size:11px">{labels_html}</td>
              <td style="font-size:12px;color:#57606a;white-space:nowrap">{_e(assignees_str)}</td>
              <td style="font-size:12px;color:#57606a;white-space:nowrap">{_e(reviewers_str)}</td>
              <td style="font-size:12px;text-align:right">{_e(pr['comments'])}</td>
              <td style="font-size:12px;text-align:right">{_e(pr['files_changed'])}</td>
              <td style="font-size:11px;color:#57606a;white-space:nowrap">{_e(pr.get('milestone') or '—')}</td>
            </tr>"""

        # ── full HTML document ────────────────────────────────────────────────
        document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RDR Pull Requests — {_e(OWNER_REPO)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system,"Segoe UI",system-ui,sans-serif; font-size:14px;
          background:#ffffff; color:#24292f; padding:24px; }}
  h1   {{ font-size:20px; font-weight:700; margin-bottom:4px; }}
  .sub {{ font-size:12px; color:#57606a; margin-bottom:20px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:24px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th    {{ background:#f6f8fa; border:1px solid #d0d7de; padding:8px 10px;
           text-align:left; font-size:12px; font-weight:600; color:#57606a;
           cursor:pointer; white-space:nowrap; user-select:none; }}
  th:hover {{ background:#eaeef2; }}
  th.asc::after  {{ content:" ▲"; font-size:9px; }}
  th.desc::after {{ content:" ▼"; font-size:9px; }}
  td    {{ border:1px solid #d0d7de; padding:7px 10px; vertical-align:top; }}
  tr:hover td {{ background:#f6f8fa; }}
  input#search {{ width:100%; max-width:400px; padding:6px 10px; margin-bottom:14px;
                  border:1px solid #d0d7de; border-radius:6px; font-size:13px; }}
  .footer {{ margin-top:24px; font-size:11px; color:#57606a; text-align:center;
             border-top:1px solid #d0d7de; padding-top:12px; }}
</style>
</head>
<body>
<h1>🔵 RDR Pull Requests — {_e(OWNER_REPO)}</h1>
<div class="sub">Generated {_e(generated_at)} &nbsp;·&nbsp; {len(prs)} PR(s) matched</div>

<div class="cards">{summary_cards}</div>

<input id="search" type="search" placeholder="Filter by title, author, label…" oninput="filterTable()">

<table id="pr-table">
  <thead>
    <tr>
      <th onclick="sortTable(0)">#</th>
      <th onclick="sortTable(1)">Title</th>
      <th onclick="sortTable(2)">Author</th>
      <th onclick="sortTable(3)">Status</th>
      <th onclick="sortTable(4)">Review</th>
      <th onclick="sortTable(5)">CI</th>
      <th onclick="sortTable(6)">Age</th>
      <th>Labels</th>
      <th onclick="sortTable(8)">Assignees</th>
      <th>Req. Reviewers</th>
      <th onclick="sortTable(10)">💬</th>
      <th onclick="sortTable(11)">Files</th>
      <th onclick="sortTable(12)">Milestone</th>
    </tr>
  </thead>
  <tbody>{rows_html}
  </tbody>
</table>

<div class="footer">
  RDR PR Tracker &nbsp;·&nbsp; red-hat-storage/ocs-ci &nbsp;·&nbsp; {_e(generated_at)}
</div>

<script>
// ── sort ──────────────────────────────────────────────────────────────────────
let _sortCol = -1, _sortAsc = true;
function sortTable(col) {{
  const table = document.getElementById('pr-table');
  const ths   = table.querySelectorAll('th');
  const rows  = Array.from(table.tBodies[0].rows);
  if (_sortCol === col) {{ _sortAsc = !_sortAsc; }}
  else {{ _sortCol = col; _sortAsc = true; }}
  ths.forEach((th, i) => {{ th.classList.remove('asc','desc'); }});
  ths[col].classList.add(_sortAsc ? 'asc' : 'desc');
  rows.sort((a, b) => {{
    let av = a.cells[col].innerText.trim();
    let bv = b.cells[col].innerText.trim();
    // numeric sort for #, Age, 💬, Files columns
    if ([0,6,10,11].includes(col)) {{
      av = parseFloat(av.replace(/[^0-9.]/g, '')) || 0;
      bv = parseFloat(bv.replace(/[^0-9.]/g, '')) || 0;
      return _sortAsc ? av - bv : bv - av;
    }}
    return _sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(r => table.tBodies[0].appendChild(r));
}}

// ── filter ────────────────────────────────────────────────────────────────────
function filterTable() {{
  const q    = document.getElementById('search').value.toLowerCase();
  const rows = document.querySelectorAll('#pr-table tbody tr');
  rows.forEach(row => {{
    row.style.display = row.innerText.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(document)

        logger.info(f"HTML report written to: {path}")
        return path

    # ── Slack export ──────────────────────────────────────────────────────────

    @staticmethod
    def post_slack(
        prs: List[Dict[str, Any]],
        webhook_url: str,
        max_prs: int = 20,
    ) -> bool:
        """
        Post an RDR PR summary to a Slack channel via an Incoming Webhook.

        Args:
            prs:         Enriched PR list from list_rdr_prs().
            webhook_url: Slack Incoming Webhook URL.
            max_prs:     Cap on individual PR lines to avoid message size limits.

        Returns:
            True if Slack accepted the message (HTTP 200), False otherwise.

        Slack Incoming Webhook setup:
            https://api.slack.com/messaging/webhooks
        """
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        STATUS_EMOJI = {
            "open": "🟢",
            "draft": "🟡",
            "merged": "🟣",
            "closed": "🔴",
        }
        REVIEW_EMOJI = {
            "APPROVED": "✅",
            "CHANGES_REQUESTED": "🔴",
            "PENDING": "⏳",
            "COMMENTED": "💬",
            "unknown": "❓",
        }
        CI_EMOJI = {
            "success": "✅",
            "failure": "❌",
            "pending": "⏳",
            "skipped": "⏭️",
            "unknown": "❓",
        }

        # ── summary counts ────────────────────────────────────────────────────
        by_status: Dict[str, int] = {}
        by_review: Dict[str, int] = {}
        by_ci: Dict[str, int] = {}
        for pr in prs:
            by_status[pr["status"]] = by_status.get(pr["status"], 0) + 1
            by_review[pr["review_decision"]] = (
                by_review.get(pr["review_decision"], 0) + 1
            )
            by_ci[pr["ci_status"]] = by_ci.get(pr["ci_status"], 0) + 1

        status_parts = " · ".join(
            f"{STATUS_EMOJI.get(s,'🔵')} {s}: *{n}*"
            for s, n in sorted(by_status.items())
        )
        review_parts = " · ".join(
            f"{REVIEW_EMOJI.get(r,'❓')} {r}: *{n}*"
            for r, n in sorted(by_review.items())
        )
        ci_parts = " · ".join(
            f"{CI_EMOJI.get(c,'❓')} {c}: *{n}*" for c, n in sorted(by_ci.items())
        )

        header_block = {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔵 RDR Pull Requests — {OWNER_REPO}",
                "emoji": True,
            },
        }
        meta_block = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{len(prs)} PR(s) matched* · generated {generated_at}\n"
                    f"{status_parts}\n"
                    f"Review → {review_parts}\n"
                    f"CI → {ci_parts}"
                ),
            },
        }
        divider = {"type": "divider"}

        # ── per-PR lines ──────────────────────────────────────────────────────
        pr_blocks = []
        for pr in prs[:max_prs]:
            status_icon = STATUS_EMOJI.get(pr["status"], "🔵")
            if pr["draft"]:
                status_icon = STATUS_EMOJI["draft"]
            review_icon = REVIEW_EMOJI.get(pr["review_decision"], "❓")
            ci_icon = CI_EMOJI.get(pr["ci_status"], "❓")

            labels_str = (
                " ".join(f"`{lbl}`" for lbl in pr["labels"][:4]) if pr["labels"] else ""
            )
            assignees_str = ", ".join(pr["assignees"]) if pr["assignees"] else ""
            approved_str = ", ".join(pr["approved_by"]) if pr["approved_by"] else ""

            detail_parts = [f"{pr['age_days']}d old"]
            if assignees_str:
                detail_parts.append(f"assigned: {assignees_str}")
            if approved_str:
                detail_parts.append(f"approved by: {approved_str}")
            if labels_str:
                detail_parts.append(labels_str)

            pr_blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{status_icon} *<{pr['url']}|#{pr['number']}>* "
                            f"{review_icon} {ci_icon}  "
                            f"_{pr['author']}_\n"
                            f"{pr['title']}\n"
                            f"<{pr['url']}|view PR>  ·  {' · '.join(detail_parts)}"
                        ),
                    },
                }
            )

        if len(prs) > max_prs:
            pr_blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f"_… and {len(prs) - max_prs} more PRs not shown."
                                " Run with --html for the full report._"
                            ),
                        }
                    ],
                }
            )

        payload = {
            "blocks": [header_block, meta_block, divider] + pr_blocks,
        }

        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("Slack message posted successfully.")
            return True
        else:
            logger.error(f"Slack post failed: HTTP {resp.status_code} — {resp.text}")
            return False


# ── CLI entrypoint ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="List RDR-related Pull Requests for red-hat-storage/ocs-ci",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--state",
        choices=["open", "closed", "all"],
        default="open",
        help="PR state to query (default: open)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Dump raw enriched data as JSON",
    )
    p.add_argument(
        "--no-checks",
        action="store_true",
        help="Skip fetching CI check-run status (faster, but no CI column)",
    )
    p.add_argument(
        "--repo",
        default=OWNER_REPO,
        help=f"GitHub repo (default: {OWNER_REPO})",
    )
    p.add_argument(
        "--detail",
        type=int,
        metavar="PR_NUMBER",
        help="Print full detail for a single PR number",
    )
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only the statistics summary, no table",
    )
    p.add_argument(
        "--html",
        nargs="?",
        const="rdr_prs.html",
        metavar="FILE",
        help="Write a self-contained HTML report (default filename: rdr_prs.html)",
    )
    p.add_argument(
        "--slack",
        metavar="WEBHOOK_URL",
        help="Post the summary to a Slack channel via Incoming Webhook URL",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    agent = DRGitHubAgent(
        repo=args.repo,
        fetch_checks=not args.no_checks,
    )

    prs = agent.list_rdr_prs(state=args.state)

    if args.as_json:
        print(json.dumps(prs, indent=2, default=str))
        return

    if args.detail:
        matches = [p for p in prs if p["number"] == args.detail]
        if not matches:
            print(f"PR #{args.detail} not found in {args.state} PRs.")
            sys.exit(1)
        agent.print_detail(matches[0])
        return

    # ── HTML output ───────────────────────────────────────────────────────────
    if args.html:
        out_path = agent.export_html(prs, path=args.html)
        print(f"HTML report written → {out_path}")

    # ── Slack output ──────────────────────────────────────────────────────────
    if args.slack:
        ok = agent.post_slack(prs, webhook_url=args.slack)
        if not ok:
            sys.exit(1)

    # ── terminal output (always shown unless --html/--slack only flags used) ──
    if not args.html and not args.slack:
        if not args.summary_only:
            agent.print_table(prs)
        agent.print_summary(prs)
    elif not args.summary_only and not args.slack:
        # HTML was written; also print a quick terminal summary for confirmation
        agent.print_summary(prs)


if __name__ == "__main__":
    main()
