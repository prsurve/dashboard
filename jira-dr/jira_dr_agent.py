"""
DR Jira Agent — Live tracker for DR bugs & features (RHSTOR + OCSQE)

Queries Red Hat's Atlassian **Cloud** Jira (https://redhat.atlassian.net) via the
REST API v3 and writes a compact ``data.json`` that the static dashboard shell
(``jira-dr/index.html``) fetches and polls every few minutes.

Two result sets are produced from two JQL queries:
  • bugs      — issuetype = Bug
  • features  — issuetype in (Epic, Story)

Each issue is flattened to a small record; all derived views (version chips,
status cards, tables) are computed client-side in JS, so this payload stays the
single source of truth and small.

Auth (basic auth — required; no interactive/MCP login works in CI):
  • JIRA_EMAIL      environment variable (e.g. prsurve@redhat.com)
  • JIRA_API_TOKEN  environment variable (Atlassian API token from
                    https://id.atlassian.com/manage-profile/security/api-tokens)

Usage:
    JIRA_EMAIL=... JIRA_API_TOKEN=... \
        python jira-dr/jira_dr_agent.py --json-out data.json

    # override the default JQL (e.g. after the team shares exact filters)
    python jira-dr/jira_dr_agent.py --json-out data.json \
        --bugs-jql 'project IN (OCSQE, RHSTOR) AND labels = "odf-qe-dr" AND issuetype = Bug' \
        --features-jql 'project IN (OCSQE, RHSTOR) AND labels = "odf-qe-dr" AND issuetype IN (Epic, Story)'
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

SITE = "https://redhat.atlassian.net"
API_BASE = f"{SITE}/rest/api/3"

PROJECTS = ["DFBUGS", "OCSQE"]

# The two releases the team is working on. fixVersion is named differently per
# project — DFBUGS uses "odf-5.0", OCSQE uses "ODF v5.0.0" — so we keep both
# conventions here and the dashboard normalizes them to "5.0" / "5.1" chips.
# Moving to the next release pair is a one-line edit of these three lists.
BUG_VERSIONS = ["odf-5.0", "odf-5.1"]            # DFBUGS fixVersion names
FEATURE_VERSIONS = ["ODF v5.0.0", "ODF v5.1.0"]  # OCSQE fixVersion names
ACTIVE_VERSIONS = ["5.1", "5.0"]                 # normalized chip keys, pre-selected

# DR team QA contacts (account IDs). DR bugs in DFBUGS carry no special label —
# they're identified by the QA contact being one of the DR team.
DR_QA_CONTACTS = [
    "70121:d3633ed2-b0cb-46b4-beea-7dbba32cfa0d",
    "70121:afabdfc0-9365-4e43-9634-0cd82e361085",
    "712020:271d797f-9183-4e71-8e4f-731fd0ffa60c",
    "712020:96fbb3ba-17c0-44d3-8ec0-635b343d46c3",
    "61d6d7bace3652006ad56263",
    "70121:0947089f-dfc8-4fb6-b91f-42e5919144cb",
]

QA_FIELD = '"QA Contact[User Picker (single user)]"'


def _bugs_jql() -> str:
    vers = ", ".join(f'"{v}"' for v in BUG_VERSIONS)
    qa = " OR ".join(f"{QA_FIELD} = {aid}" for aid in DR_QA_CONTACTS)
    return (
        f'project = "DFBUGS" AND status = ON_QA AND fixVersion in ({vers}) '
        f"AND ({qa}) ORDER BY updated DESC"
    )


def _features_jql() -> str:
    vers = ", ".join(f'"{v}"' for v in FEATURE_VERSIONS)
    return (
        f"project = OCSQE AND type = Story AND fixVersion in ({vers}) "
        f"AND labels = odf-qe-dr ORDER BY created DESC"
    )


BUGS_JQL = _bugs_jql()
FEATURES_JQL = _features_jql()

# Fields requested from Jira. Explicit list keeps the payload small and stable.
FIELDS = [
    "key",
    "summary",
    "status",
    "priority",
    "assignee",
    "reporter",
    "issuetype",
    "fixVersions",
    "components",
    "labels",
    "created",
    "updated",
    "resolution",
    "parent",
    "customfield_10470",  # QA Contact (single user picker)
]

# Cloud v3 search page size cap.
PAGE_SIZE = 100


# ── Auth / HTTP ─────────────────────────────────────────────────────────────────


def _resolve_auth() -> Tuple[str, str]:
    """Return (email, api_token) from the environment or exit with guidance."""
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not email or not token:
        logger.error(
            "Missing Jira credentials. Set JIRA_EMAIL and JIRA_API_TOKEN "
            "(create a token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens)."
        )
        sys.exit(2)
    return email, token


def _whoami(auth: Tuple[str, str]) -> None:
    """Log which account the token authenticates as (diagnostic)."""
    try:
        r = requests.get(
            f"{API_BASE}/myself",
            headers={"Accept": "application/json"},
            auth=auth,
            timeout=30,
        )
        if r.status_code == 200:
            me = r.json()
            logger.info(
                f"Authenticated as {me.get('displayName')} "
                f"<{me.get('emailAddress')}> (accountId {me.get('accountId')})"
            )
        else:
            logger.warning(f"/myself returned HTTP {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        logger.warning(f"/myself check failed: {exc}")


def _search(
    jql: str, auth: Tuple[str, str], fields: List[str]
) -> List[Dict[str, Any]]:
    """
    Run a JQL search against the Cloud v3 ``/search/jql`` endpoint and return all
    raw issue dicts, following ``nextPageToken`` pagination until ``isLast``.
    """
    url = f"{API_BASE}/search/jql"
    headers = {"Accept": "application/json"}
    issues: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    page = 0

    while True:
        params: Dict[str, Any] = {
            "jql": jql,
            "maxResults": PAGE_SIZE,
            "fields": ",".join(fields),
        }
        if next_token:
            params["nextPageToken"] = next_token

        resp = requests.get(url, headers=headers, params=params, auth=auth, timeout=60)
        if resp.status_code == 401:
            raise RuntimeError("Jira auth failed (401). Check JIRA_EMAIL / JIRA_API_TOKEN.")
        if resp.status_code == 400:
            raise RuntimeError(f"Jira rejected the JQL (400): {resp.text[:300]}")
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("issues", [])
        issues.extend(batch)
        page += 1
        logger.info(f"  page {page}: +{len(batch)} issues (total {len(issues)})")

        if data.get("isLast") or not data.get("nextPageToken"):
            break
        next_token = data["nextPageToken"]

    return issues


# ── Normalization ────────────────────────────────────────────────────────────────


def _age_days(iso: str) -> int:
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return (datetime.now(timezone.utc) - dt).days


def _user_name(user: Optional[Dict]) -> Optional[str]:
    if not user:
        return None
    return user.get("displayName") or user.get("emailAddress") or user.get("name")


def _normalize(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a raw Jira issue into a compact dashboard record."""
    f = issue.get("fields", {}) or {}
    key = issue.get("key", "")

    status = f.get("status") or {}
    status_cat = (status.get("statusCategory") or {}).get("key", "")  # new|indeterminate|done

    issuetype = (f.get("issuetype") or {}).get("name", "")
    priority = (f.get("priority") or {}).get("name", "")
    resolution = (f.get("resolution") or {}).get("name") if f.get("resolution") else None

    versions = [v.get("name", "") for v in (f.get("fixVersions") or []) if v.get("name")]
    components = [c.get("name", "") for c in (f.get("components") or []) if c.get("name")]
    labels = list(f.get("labels") or [])

    parent = f.get("parent") or {}
    parent_key = parent.get("key")
    parent_summary = (parent.get("fields") or {}).get("summary")

    return {
        "key": key,
        "url": f"{SITE}/browse/{key}",
        "summary": f.get("summary", ""),
        "type": issuetype,
        "status": status.get("name", ""),
        "status_category": status_cat,
        "priority": priority,
        "assignee": _user_name(f.get("assignee")),
        "reporter": _user_name(f.get("reporter")),
        "qa_contact": _user_name(f.get("customfield_10470")),
        "labels": labels,
        "components": components,
        "versions": versions,
        "created": f.get("created", ""),
        "updated": f.get("updated", ""),
        "resolution": resolution,
        "age_days": _age_days(f.get("created", "")),
        "updated_days": _age_days(f.get("updated", "")),
        "parent_key": parent_key,
        "parent_summary": parent_summary,
    }


# ── Agent ────────────────────────────────────────────────────────────────────────


class JiraDRAgent:
    def __init__(self, bugs_jql: str = BUGS_JQL, features_jql: str = FEATURES_JQL):
        self.auth = _resolve_auth()
        self.bugs_jql = bugs_jql
        self.features_jql = features_jql

    def fetch(self) -> Tuple[List[Dict], List[Dict]]:
        _whoami(self.auth)
        logger.info(f"Fetching bugs … [{self.bugs_jql}]")
        bugs = [_normalize(i) for i in _search(self.bugs_jql, self.auth, FIELDS)]
        logger.info(f"Fetching features … [{self.features_jql}]")
        features = [_normalize(i) for i in _search(self.features_jql, self.auth, FIELDS)]
        logger.info(f"Done: {len(bugs)} bugs, {len(features)} features.")
        return bugs, features

    def export_json(self, path: str) -> str:
        bugs, features = self.fetch()
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "site": SITE,
            "projects": PROJECTS,
            "active_versions": ACTIVE_VERSIONS,
            "bug_count": len(bugs),
            "feature_count": len(features),
            "bugs": bugs,
            "features": features,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        logger.info(f"JSON data written to: {path}")
        return path


# ── CLI ──────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate data.json of DR bugs & features (RHSTOR + OCSQE) from Jira Cloud",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--json-out",
        dest="json_out",
        required=True,
        metavar="FILE",
        help="Write the dashboard data.json to this path",
    )
    p.add_argument("--bugs-jql", default=BUGS_JQL, help="Override the bugs JQL")
    p.add_argument(
        "--features-jql", default=FEATURES_JQL, help="Override the features JQL"
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    agent = JiraDRAgent(bugs_jql=args.bugs_jql, features_jql=args.features_jql)
    out = agent.export_json(args.json_out)
    print(f"JSON data written → {out}")


if __name__ == "__main__":
    main()
