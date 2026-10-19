#!/usr/bin/env python3
"""Weekly Jira report.

Counts the issues created, resolved and still open in one Jira Cloud project
and sends the result by email. It uses only the Jira REST API (no Jira Automation).
"""
import argparse
import html
import os
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

FIELDS = "summary,status,priority,issuetype"


def need(name):
    """Read a required setting from the environment."""
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"ERROR: environment variable {name} is missing")
    return value


def jira_get(session, base, path, params):
    """GET with a few retries for network errors, 429 and 5xx."""
    for attempt in range(1, 4):
        try:
            r = session.get(base + path, params=params, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as err:
            print(f"Network problem ({err.__class__.__name__}), try {attempt}/3")
            time.sleep(2 * attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = int(r.headers.get("Retry-After", 2 * attempt))
            print(f"Jira answered {r.status_code}, waiting {wait}s, try {attempt}/3")
            time.sleep(wait)
            continue
        if r.status_code == 401:
            sys.exit("ERROR 401: JIRA_EMAIL or JIRA_API_TOKEN is wrong")
        if r.status_code == 403:
            sys.exit("ERROR 403: this user may not browse the project")
        if r.status_code == 400:
            sys.exit(f"ERROR 400: Jira rejected the query: {r.text[:300]}")
        r.raise_for_status()
        return r.json()
    sys.exit("ERROR: Jira did not answer after 3 tries")


def search(session, base, jql):
    """Return every issue of a JQL query. Jira sends them in pages."""
    issues, token = [], None
    while True:
        params = {"jql": jql, "fields": FIELDS, "maxResults": 100}
        if token:
            params["nextPageToken"] = token
        data = jira_get(session, base, "/rest/api/3/search/jql", params)
        issues += data.get("issues", [])
        token = data.get("nextPageToken")
        if not token:
            return issues


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_queries(project, start, end, types, priorities):
    extra = ""
    if types:
        extra += " AND issuetype in (" + ", ".join(quote(t) for t in types) + ")"
    if priorities:
        extra += " AND priority in (" + ", ".join(quote(p) for p in priorities) + ")"
    first = start.isoformat()
    after = (end + timedelta(days=1)).isoformat()  # the end day counts too
    base = f"project = {quote(project)}"
    return {
        "Created": f'{base} AND created >= "{first}" AND created < "{after}"{extra} ORDER BY created DESC',
        "Resolved": f'{base} AND resolved >= "{first}" AND resolved < "{after}"{extra} ORDER BY resolved DESC',
        "Open now": f"{base} AND statusCategory != Done{extra} ORDER BY created ASC",
    }


def to_rows(issues, site):
    rows = []
    for item in issues:
        f = item.get("fields") or {}
        rows.append({
            "key": item.get("key", "?"),
            "summary": f.get("summary") or "(no summary)",
            "type": (f.get("issuetype") or {}).get("name", "-"),
            "priority": (f.get("priority") or {}).get("name", "-"),
            "status": (f.get("status") or {}).get("name", "-"),
            "url": f"{site}/browse/{item.get('key', '')}",
        })
    return rows


def make_report(project, start, end, sections, filters):
    title = f"Jira weekly report {project}: {start} to {end}"
    text = [title, filters, ""]
    page = [f"<h2>{html.escape(title)}</h2>", f"<p>{html.escape(filters)}</p>",
            "<table cellpadding='8' style='border-collapse:collapse'><tr>"]
    for name, rows in sections:
        page.append(f"<td style='border:1px solid #bbb;text-align:center'>"
                    f"<b style='font-size:24px'>{len(rows)}</b><br>{name}</td>")
        text.append(f"{name}: {len(rows)}")
    page.append("</tr></table>")
    for name, rows in sections:
        text += ["", f"{name} ({len(rows)})"]
        page.append(f"<h3>{name} ({len(rows)})</h3>")
        if not rows:
            text.append("  none")
            page.append("<p>None</p>")
            continue
        page.append("<table cellpadding='4' style='border-collapse:collapse'>"
                    "<tr><th align='left'>Key</th><th align='left'>Summary</th>"
                    "<th>Type</th><th>Priority</th><th>Status</th></tr>")
        for r in rows:
            text.append(f"  {r['key']}  {r['summary']}  [{r['type']}, {r['priority']}, {r['status']}]  {r['url']}")
            page.append(f"<tr><td><a href='{r['url']}'>{r['key']}</a></td>"
                        f"<td>{html.escape(r['summary'])}</td><td>{html.escape(r['type'])}</td>"
                        f"<td>{html.escape(r['priority'])}</td><td>{html.escape(r['status'])}</td></tr>")
        page.append("</table>")
    return title, "\n".join(text), "".join(page)


def send_email(subject, text, page):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = need("MAIL_FROM")
    msg["To"] = need("MAIL_TO")
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    host = need("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(need("SMTP_USER"), need("SMTP_PASSWORD"))
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        sys.exit(f"ERROR: the email could not be sent: {err}")


def main():
    ap = argparse.ArgumentParser(description="Weekly Jira report by email")
    ap.add_argument("--project", required=True, help="Jira project key, for example KAN")
    ap.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    ap.add_argument("--from", dest="start", help="first day, YYYY-MM-DD (replaces --days)")
    ap.add_argument("--to", dest="end", help="last day, YYYY-MM-DD (default: today)")
    ap.add_argument("--types", help="only these issue types, comma separated")
    ap.add_argument("--priorities", help="only these priorities, comma separated")
    ap.add_argument("--no-email", action="store_true", help="print the report, send nothing")
    a = ap.parse_args()

    today = datetime.now(ZoneInfo("Europe/Berlin")).date()  # "today" in Berlin, not in the runner's UTC
    end = date.fromisoformat(a.end) if a.end else today
    start = date.fromisoformat(a.start) if a.start else end - timedelta(days=a.days)
    if start > end:
        sys.exit("ERROR: the start date is after the end date")
    types = [t.strip() for t in a.types.split(",")] if a.types else []
    priorities = [p.strip() for p in a.priorities.split(",")] if a.priorities else []

    site = need("JIRA_BASE_URL").rstrip("/")
    session = requests.Session()
    session.auth = (need("JIRA_EMAIL"), need("JIRA_API_TOKEN"))
    session.headers["Accept"] = "application/json"

    sections = []
    for name, jql in build_queries(a.project, start, end, types, priorities).items():
        print(f"{name}: {jql}")
        sections.append((name, to_rows(search(session, site, jql), site)))

    filters = f"Filters: types={', '.join(types) or 'all'}; priorities={', '.join(priorities) or 'all'}"
    subject, text, page = make_report(a.project, start, end, sections, filters)

    os.makedirs("reports", exist_ok=True)
    with open(f"reports/{a.project}_{end}.html", "w", encoding="utf-8") as fh:
        fh.write(page)

    print()
    print(text)
    if not a.no_email:
        send_email(subject, text, page)
        print("\nEmail sent.")


if __name__ == "__main__":
    main()
