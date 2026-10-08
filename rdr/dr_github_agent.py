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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    def list_rdr_prs(
        self, state: str = "open", workers: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Return all RDR-related PRs enriched with review, label, CI and age data.

        API calls for each PR (reviews + files) are fetched concurrently using a
        thread pool, cutting wall-clock time from O(N) serial requests to roughly
        O(N/workers).  Default is 10 workers which stays well inside GitHub's
        per-token rate limit of 5000 req/h.

        Args:
            state:   ``"open"``, ``"closed"``, or ``"all"``.
            workers: Max concurrent threads for the enrichment phase (default 10).

        Returns:
            List of enriched PR dicts sorted by PR number descending.
        """
        logger.info(f"Fetching {state} PRs from {self.repo} …")
        raw_prs = self._fetch_all_prs(state)
        logger.info(f"Total PRs fetched: {len(raw_prs)}.  Filtering for RDR …")

        rdr_prs = [pr for pr in raw_prs if _is_rdr_related(pr)]
        total = len(rdr_prs)
        logger.info(f"RDR-related PRs found: {total}.  Enriching with {workers} workers …")

        enriched: List[Dict[str, Any]] = [None] * total  # type: ignore[list-item]

        def _enrich_indexed(idx: int, pr: Dict) -> tuple:
            logger.info(
                f"  [{idx + 1}/{total}] Enriching PR #{pr['number']}: {pr['title'][:60]}"
            )
            return idx, _enrich_pr(pr, self.token, self.fetch_checks)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_enrich_indexed, i, pr): i
                for i, pr in enumerate(rdr_prs)
            }
            for future in as_completed(futures):
                idx, result = future.result()
                enriched[idx] = result

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
        Write a self-contained two-tab HTML report to *path* and return the path.

        Tab 1 — PR Table  : sortable, filterable list of all RDR pull requests.
        Tab 2 — Metrics   : time-to-first-review, time-to-merge, age buckets,
                            merge velocity (weekly/monthly), longest-open PRs.

        The file has zero external dependencies — one file you can open in any
        browser, email, or attach to a Slack message.
        """
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        now_utc = datetime.now(timezone.utc)

        # ── helpers ──────────────────────────────────────────────────────────
        def _e(v: Any) -> str:
            """HTML-escape a value."""
            return _html_mod.escape(str(v) if v is not None else "")

        def _parse_dt(iso: str) -> Optional[datetime]:
            """Parse an ISO-8601 string (Z or +00:00) into an aware datetime."""
            if not iso:
                return None
            try:
                return datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except ValueError:
                return None

        def _hours_between(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
            if a and b:
                return abs((b - a).total_seconds()) / 3600
            return None

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

        def _stat_card(
            label: str,
            value: Any,
            color: str = "#24292f",
            filter_val: str = "",
        ) -> str:
            """Render a stat card.  If filter_val is set the card is clickable
            and calls filterByStatus(filter_val) on click."""
            if filter_val:
                onclick = f' onclick="filterByStatus(\'{filter_val}\',this)"'
            else:
                onclick = ' onclick="filterByStatus(\'all\',this)"'
            return (
                f'<div class="stat-card" data-filter="{_e(filter_val or "all")}"'
                f' {onclick}'
                f' style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;'
                f'padding:12px 20px;text-align:center;min-width:80px;transition:box-shadow .15s">'
                f'<div style="font-size:22px;font-weight:700;color:{color}">{_e(value)}</div>'
                f'<div style="font-size:11px;color:#57606a;margin-top:2px">{_e(label)}</div>'
                f"</div>"
            )

        summary_cards = _stat_card("Total PRs", len(prs), "#3b82d4", "all")
        for st, cnt in sorted(by_status.items()):
            summary_cards += _stat_card(
                st.capitalize(), cnt, STATUS_COLOR.get(st, "#57606a"), st
            )
        approved_count = by_review.get("APPROVED", 0)
        pending_count = by_review.get("PENDING", 0) + by_review.get("COMMENTED", 0)
        changes_count = by_review.get("CHANGES_REQUESTED", 0)
        summary_cards += _stat_card("Approved", approved_count, "#1a7f37", "review:APPROVED")
        summary_cards += _stat_card("Needs Review", pending_count, "#9a6700", "review:PENDING")
        summary_cards += _stat_card("Changes Req.", changes_count, "#cf222e", "review:CHANGES_REQUESTED")
        if by_ci.get("failure", 0):
            summary_cards += _stat_card("CI Failing", by_ci["failure"], "#cf222e", "ci:failure")

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

            pr_status_val = "draft" if pr["draft"] else pr["status"]
            pr_review_val = pr["review_decision"]
            pr_ci_val = pr["ci_status"]
            # open+draft visible by default; closed/merged hidden until filter click
            default_hidden = (
                ' style="display:none"'
                if pr_status_val in ("closed", "merged")
                else ""
            )

            rows_html += f"""
            <tr data-status="{_e(pr_status_val)}" data-review="{_e(pr_review_val)}" data-ci="{_e(pr_ci_val)}"{default_hidden}>
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

        # ── metrics computations ──────────────────────────────────────────────
        merge_times_h: List[float] = []     # hours from open → merge
        merged_this_week: List[Dict] = []
        merged_this_month: List[Dict] = []
        age_buckets = {"<1d": 0, "1–7d": 0, "7–30d": 0, "30–90d": 0, ">90d": 0}
        longest_open: List[Dict] = []

        week_cutoff  = now_utc.timestamp() - 7 * 86400
        month_cutoff = now_utc.timestamp() - 30 * 86400

        for pr in prs:
            created = _parse_dt(pr.get("created_at", ""))
            merged  = _parse_dt(pr.get("merged_at", ""))

            # merge time
            mt = _hours_between(created, merged)
            if mt is not None:
                merge_times_h.append(mt)

            # merged this week / month
            if merged:
                if merged.timestamp() >= week_cutoff:
                    merged_this_week.append(pr)
                if merged.timestamp() >= month_cutoff:
                    merged_this_month.append(pr)

            # age buckets (open PRs only)
            if pr["status"] in ("open", "draft") and created:
                age = pr["age_days"]
                if age < 1:
                    age_buckets["<1d"] += 1
                elif age <= 7:
                    age_buckets["1–7d"] += 1
                elif age <= 30:
                    age_buckets["7–30d"] += 1
                elif age <= 90:
                    age_buckets["30–90d"] += 1
                else:
                    age_buckets[">90d"] += 1

            # longest open
            if pr["status"] in ("open", "draft"):
                longest_open.append(pr)

        longest_open.sort(key=lambda p: p["age_days"], reverse=True)
        longest_open = longest_open[:10]

        def _avg(lst: List[float]) -> str:
            if not lst:
                return "n/a"
            v = sum(lst) / len(lst)
            if v >= 24:
                return f"{v/24:.1f}d"
            return f"{v:.1f}h"

        def _med(lst: List[float]) -> str:
            if not lst:
                return "n/a"
            s = sorted(lst)
            mid = len(s) // 2
            v = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
            if v >= 24:
                return f"{v/24:.1f}d"
            return f"{v:.1f}h"

        # ── metrics cards ─────────────────────────────────────────────────────
        metrics_cards = (
            _stat_card("Avg Merge Time", _avg(merge_times_h), "#3b82d4")
            + _stat_card("Median Merge Time", _med(merge_times_h), "#3b82d4")
            + _stat_card("Merged This Week", len(merged_this_week), "#1a7f37")
            + _stat_card("Merged This Month", len(merged_this_month), "#1a7f37")
            + _stat_card("Still Open", sum(age_buckets.values()), "#9a6700")
        )

        # ── age bar chart data ─────────────────────────────────────────────────
        age_labels = list(age_buckets.keys())
        age_values = list(age_buckets.values())
        age_max    = max(age_values) if any(age_values) else 1

        def _bar(label: str, value: int, max_val: int) -> str:
            pct = int(value / max_val * 100) if max_val else 0
            color = "#3b82d4" if pct < 60 else ("#9a6700" if pct < 85 else "#cf222e")
            return (
                f'<div style="display:flex;align-items:center;gap:8px;margin:6px 0">'
                f'<div style="width:70px;font-size:12px;color:#57606a;text-align:right">{_e(label)}</div>'
                f'<div style="flex:1;background:#eaeef2;border-radius:4px;height:18px">'
                f'<div style="width:{pct}%;background:{color};height:18px;border-radius:4px"></div></div>'
                f'<div style="width:28px;font-size:12px;font-weight:600">{value}</div>'
                f'</div>'
            )

        age_bars_html = "".join(
            _bar(label, val, age_max) for label, val in zip(age_labels, age_values)
        )

        # ── longest open table ─────────────────────────────────────────────────
        longest_rows = ""
        for pr in longest_open:
            longest_rows += (
                f'<tr>'
                f'<td><a href="{_e(pr["url"])}" target="_blank" '
                f'style="color:#0969da;font-weight:600">#{_e(pr["number"])}</a></td>'
                f'<td style="font-size:12px">'
                f'<a href="{_e(pr["url"])}" target="_blank" style="color:#24292f">'
                f'{_e(pr["title"][:70])}{"…" if len(pr["title"])>70 else ""}</a></td>'
                f'<td style="font-size:12px;color:#57606a">{_e(pr["author"])}</td>'
                f'<td style="font-weight:700;color:#cf222e;white-space:nowrap">'
                f'{_e(pr["age_days"])}d</td>'
                f'<td>{_badge(pr["review_decision"], REVIEW_COLOR.get(pr["review_decision"],"#57606a"))}</td>'
                f'</tr>'
            )

        # ── merge velocity table (merged this month) ───────────────────────────
        velocity_rows = ""
        for pr in sorted(merged_this_month, key=lambda p: p.get("merged_at") or "", reverse=True):
            created = _parse_dt(pr.get("created_at", ""))
            merged  = _parse_dt(pr.get("merged_at", ""))
            mt      = _hours_between(created, merged)
            mt_str  = (f"{mt/24:.1f}d" if mt and mt >= 24 else f"{mt:.1f}h") if mt else "—"
            velocity_rows += (
                f'<tr>'
                f'<td><a href="{_e(pr["url"])}" target="_blank" '
                f'style="color:#0969da;font-weight:600">#{_e(pr["number"])}</a></td>'
                f'<td style="font-size:12px">'
                f'<a href="{_e(pr["url"])}" target="_blank" style="color:#24292f">'
                f'{_e(pr["title"][:65])}{"…" if len(pr["title"])>65 else ""}</a></td>'
                f'<td style="font-size:12px;color:#57606a">{_e(pr["author"])}</td>'
                f'<td style="font-size:12px;color:#57606a;white-space:nowrap">'
                f'{(_e(pr.get("merged_at","")[:10]))}</td>'
                f'<td style="font-weight:600;color:#1a7f37;white-space:nowrap">{mt_str}</td>'
                f'</tr>'
            )
        if not velocity_rows:
            velocity_rows = '<tr><td colspan="5" style="color:#57606a;text-align:center;padding:16px">No merged PRs in the last 30 days</td></tr>'

        # ── full HTML document (two tabs) ─────────────────────────────────────
        document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RDR Dashboard — {_e(OWNER_REPO)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system,"Segoe UI",system-ui,sans-serif; font-size:14px;
          background:#ffffff; color:#24292f; padding:24px; }}
  h1   {{ font-size:20px; font-weight:700; margin-bottom:4px; }}
  h2   {{ font-size:15px; font-weight:600; margin:24px 0 10px; color:#24292f; }}
  .sub {{ font-size:12px; color:#57606a; margin-bottom:20px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:24px; }}
  .stat-card {{ cursor:pointer; }}
  .stat-card:hover {{ box-shadow:0 0 0 2px #3b82d4; border-color:#3b82d4 !important; }}
  .stat-card.active {{ box-shadow:0 0 0 2px currentColor; outline:2px solid #3b82d4;
                       outline-offset:-1px; background:#eaf3ff !important; }}
  /* ── tabs ── */
  .tabs     {{ display:flex; gap:0; border-bottom:2px solid #d0d7de; margin-bottom:20px; }}
  .tab-btn  {{ padding:8px 20px; font-size:13px; font-weight:600; color:#57606a;
               background:none; border:none; border-bottom:3px solid transparent;
               cursor:pointer; margin-bottom:-2px; }}
  .tab-btn:hover  {{ color:#24292f; }}
  .tab-btn.active {{ color:#0969da; border-bottom-color:#0969da; }}
  .tab-panel      {{ display:none; }}
  .tab-panel.active {{ display:block; }}
  /* ── table ── */
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
  .section-box {{ background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px;
                  padding:16px 20px; margin-bottom:20px; }}
  .auto-note {{ font-size:11px; color:#57606a; background:#f6f8fa; border:1px solid #d0d7de;
                border-radius:6px; padding:8px 12px; margin-bottom:16px; display:inline-block; }}
</style>
</head>
<body>
<h1>🔵 RDR Dashboard — {_e(OWNER_REPO)}</h1>
<div class="sub">
  Generated {_e(generated_at)} &nbsp;·&nbsp; {len(prs)} PR(s) matched
  &nbsp;·&nbsp; <span style="color:#1a7f37">⏰ auto-refreshes daily at 03:00 UTC</span>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="showTab('prs',this)">📋 Pull Requests</button>
  <button class="tab-btn" onclick="showTab('metrics',this)">📊 Metrics</button>
</div>

<!-- ═══════════════════════ TAB 1 : PR TABLE ════════════════════════════════ -->
<div id="tab-prs" class="tab-panel active">
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
</div>

<!-- ═══════════════════════ TAB 2 : METRICS ════════════════════════════════ -->
<div id="tab-metrics" class="tab-panel">

  <div class="auto-note">
    ⏰ This page is <strong>auto-generated daily at 03:00 UTC</strong> by GitHub Actions
    — no manual run needed. Bookmark the URL and share with your team.
  </div>

  <h2>⏱ Time to Merge</h2>
  <div class="cards">{metrics_cards}</div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px">

    <div class="section-box">
      <h2 style="margin-top:0">📦 Open PR Age Distribution</h2>
      <div style="margin-top:8px">{age_bars_html}</div>
    </div>

    <div class="section-box">
      <h2 style="margin-top:0">📈 Merge Velocity</h2>
      <table style="margin-top:8px">
        <tr>
          <td style="border:none;padding:6px 8px;font-size:13px">PRs merged <strong>this week</strong></td>
          <td style="border:none;padding:6px 8px;font-size:18px;font-weight:700;color:#1a7f37">{len(merged_this_week)}</td>
        </tr>
        <tr>
          <td style="border:none;padding:6px 8px;font-size:13px">PRs merged <strong>this month</strong></td>
          <td style="border:none;padding:6px 8px;font-size:18px;font-weight:700;color:#1a7f37">{len(merged_this_month)}</td>
        </tr>
        <tr>
          <td style="border:none;padding:6px 8px;font-size:13px">Avg time to merge</td>
          <td style="border:none;padding:6px 8px;font-size:18px;font-weight:700;color:#3b82d4">{_avg(merge_times_h)}</td>
        </tr>
        <tr>
          <td style="border:none;padding:6px 8px;font-size:13px">Median time to merge</td>
          <td style="border:none;padding:6px 8px;font-size:18px;font-weight:700;color:#3b82d4">{_med(merge_times_h)}</td>
        </tr>
      </table>
    </div>

  </div>

  <h2>🐢 Longest Open PRs (top 10)</h2>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Title</th><th>Author</th><th>Age</th><th>Review</th>
      </tr>
    </thead>
    <tbody>{longest_rows if longest_rows else
      '<tr><td colspan="5" style="color:#57606a;text-align:center;padding:16px">No open PRs</td></tr>'}
    </tbody>
  </table>

  <h2>✅ Recently Merged (last 30 days)</h2>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Title</th><th>Author</th><th>Merged</th><th>Time to Merge</th>
      </tr>
    </thead>
    <tbody>{velocity_rows}
    </tbody>
  </table>

</div>

<div class="footer">
  RDR Dashboard &nbsp;·&nbsp; {_e(OWNER_REPO)} &nbsp;·&nbsp; {_e(generated_at)}
  &nbsp;·&nbsp; auto-refreshes daily at 03:00 UTC
</div>

<script>
// ── tabs ──────────────────────────────────────────────────────────────────────
function showTab(id, btn) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  btn.classList.add('active');
}}

// ── active filter state ───────────────────────────────────────────────────────
// Tracks what the card buttons have selected so text search can combine with it.
let _activeFilter = 'open';   // default: show only open PRs on load

// ── card filter ───────────────────────────────────────────────────────────────
function filterByStatus(val, cardEl) {{
  _activeFilter = val;
  // highlight the clicked card
  document.querySelectorAll('.stat-card').forEach(c => c.classList.remove('active'));
  if (cardEl) cardEl.classList.add('active');
  _applyFilters();
}}

// ── text search ───────────────────────────────────────────────────────────────
function filterTable() {{
  _applyFilters();
}}

// ── combined filter engine ────────────────────────────────────────────────────
function _applyFilters() {{
  const q = document.getElementById('search').value.toLowerCase();
  document.querySelectorAll('#pr-table tbody tr').forEach(row => {{
    const status = row.dataset.status || '';
    const review = row.dataset.review || '';
    const ci     = row.dataset.ci     || '';

    // card filter
    let cardMatch = true;
    if (_activeFilter === 'all') {{
      cardMatch = true;
    }} else if (_activeFilter.startsWith('review:')) {{
      cardMatch = review === _activeFilter.slice(7);
    }} else if (_activeFilter.startsWith('ci:')) {{
      cardMatch = ci === _activeFilter.slice(3);
    }} else {{
      cardMatch = status === _activeFilter;
    }}

    // text filter
    const textMatch = !q || row.innerText.toLowerCase().includes(q);

    row.style.display = (cardMatch && textMatch) ? '' : 'none';
  }});
}}

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
    if ([0,6,10,11].includes(col)) {{
      av = parseFloat(av.replace(/[^0-9.]/g, '')) || 0;
      bv = parseFloat(bv.replace(/[^0-9.]/g, '')) || 0;
      return _sortAsc ? av - bv : bv - av;
    }}
    return _sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(r => table.tBodies[0].appendChild(r));
}}

// ── init: activate the Open card on load ─────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {{
  const openCard = document.querySelector('.stat-card[data-filter="open"]');
  if (openCard) {{ openCard.classList.add('active'); }}
  else {{
    // no open PRs — fall back to showing all
    _activeFilter = 'all';
    const allCard = document.querySelector('.stat-card[data-filter="all"]');
    if (allCard) allCard.classList.add('active');
    _applyFilters();
  }}
}});
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
        "--workers",
        type=int,
        default=10,
        metavar="N",
        help="Parallel threads for PR enrichment API calls (default: 10)",
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

    prs = agent.list_rdr_prs(state=args.state, workers=args.workers)

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
