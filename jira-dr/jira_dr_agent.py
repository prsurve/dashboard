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
CLOUD_ID = "2b9e35e3-6bd3-4cec-b838-f4249ee02432"
SITE_API = f"{SITE}/rest/api/3"
CLOUD_API = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"
# API_BASE is used for building issue browse URLs only; the live client base is
# resolved at runtime (basic vs bearer) — see _resolve_client().
API_BASE = SITE_API

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


class Client:
    """A resolved, authenticated Jira client (base URL + auth scheme)."""

    def __init__(self, name: str, base: str, headers: Dict[str, str], auth):
        self.name = name
        self.base = base
        self.headers = {"Accept": "application/json", **headers}
        self.auth = auth


def _resolve_client() -> Client:
    """
    Resolve a working Jira client by probing /myself with several auth schemes:
      1. Basic auth (email + token) against the site  — classic API tokens
      2. Bearer token against api.atlassian.com/ex/jira — scoped API tokens
      3. Bearer token against the site                 — fallback
    Returns the first scheme that authenticates (HTTP 200), else exits.
    """
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not token:
        logger.error(
            "Missing JIRA_API_TOKEN. Create a token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens "
            "and set JIRA_API_TOKEN (+ JIRA_EMAIL for a classic token)."
        )
        sys.exit(2)

    candidates = []
    if email:
        candidates.append(Client("basic-site", SITE_API, {}, (email, token)))
    candidates.append(Client("bearer-cloud", CLOUD_API, {"Authorization": f"Bearer {token}"}, None))
    candidates.append(Client("bearer-site", SITE_API, {"Authorization": f"Bearer {token}"}, None))

    for c in candidates:
        try:
            r = requests.get(f"{c.base}/myself", headers=c.headers, auth=c.auth, timeout=30)
        except Exception as exc:
            logger.warning(f"auth probe {c.name} failed: {exc}")
            continue
        if r.status_code == 200:
            me = r.json()
            logger.info(
                f"Authenticated via {c.name} as {me.get('displayName')} "
                f"<{me.get('emailAddress')}> (accountId {me.get('accountId')})"
            )
            return c
        logger.warning(f"auth probe {c.name} -> HTTP {r.status_code}: {r.text[:120]}")

    logger.error(
        "Could not authenticate to Jira with any scheme. Check that JIRA_API_TOKEN "
        "is valid and (for a classic token) JIRA_EMAIL matches the token's account."
    )
    sys.exit(2)


def _issue_parent(key: str, client: Client) -> Tuple[Optional[str], Optional[str]]:
    """Return (parent_key, parent_summary) for an issue, or (None, None)."""
    try:
        r = requests.get(
            f"{client.base}/issue/{key}",
            headers=client.headers,
            auth=client.auth,
            params={"fields": "parent,summary"},
            timeout=30,
        )
        if r.ok:
            p = (r.json().get("fields", {}) or {}).get("parent") or {}
            return p.get("key"), (p.get("fields") or {}).get("summary")
    except Exception as exc:
        logger.debug(f"parent lookup for {key} failed: {exc}")
    return None, None


def _resolve_features(features: List[Dict[str, Any]], client: Client) -> None:
    """
    Stamp each feature with its top-level RHSTOR feature epic (feature_key/
    feature_summary), resolving through an intermediate OCSQE epic when present:
      • story parent is RHSTOR-*  → that is the feature
      • story parent is OCSQE-*   → look up that epic's parent; use it if RHSTOR-*
    OCSQE epic lookups are cached so each distinct epic is fetched at most once.
    """
    cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    def feature_of(pkey: Optional[str], psummary: Optional[str]):
        if not pkey:
            return None, None
        if pkey.startswith("RHSTOR"):
            return pkey, psummary
        if pkey in cache:
            return cache[pkey]
        gk, gs = _issue_parent(pkey, client)           # OCSQE epic → its parent
        res = (gk, gs) if (gk and gk.startswith("RHSTOR")) else (None, None)
        cache[pkey] = res
        return res

    for f in features:
        fk, fs = feature_of(f.get("parent_key"), f.get("parent_summary"))
        f["feature_key"] = fk
        f["feature_summary"] = fs
    logger.info(f"Resolved RHSTOR features via {len(cache)} OCSQE-epic lookup(s).")


def _search(jql: str, client: Client, fields: List[str]) -> List[Dict[str, Any]]:
    """
    Run a JQL search against the Cloud v3 ``/search/jql`` endpoint and return all
    raw issue dicts, following ``nextPageToken`` pagination until ``isLast``.
    """
    url = f"{client.base}/search/jql"
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

        resp = requests.get(
            url, headers=client.headers, params=params, auth=client.auth, timeout=60
        )
        if resp.status_code == 401:
            raise RuntimeError("Jira auth failed (401) mid-run.")
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
        self.client = _resolve_client()
        self.bugs_jql = bugs_jql
        self.features_jql = features_jql

    def fetch(self) -> Tuple[List[Dict], List[Dict]]:
        logger.info(f"Fetching bugs … [{self.bugs_jql}]")
        bugs = [_normalize(i) for i in _search(self.bugs_jql, self.client, FIELDS)]
        logger.info(f"Fetching features … [{self.features_jql}]")
        features = [_normalize(i) for i in _search(self.features_jql, self.client, FIELDS)]
        _resolve_features(features, self.client)
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
