"""Small tests for reportScript.py. They need no network and no Jira.

Run:  python test_report.py
"""
from datetime import date

import requests

import reportScript as rs

rs.time.sleep = lambda seconds: None  # do not really wait in the retry tests


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Gives back the prepared answers one by one."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def exit_text(function, *args):
    """Run a function that should stop the program and return its message."""
    try:
        function(*args)
    except SystemExit as stop:
        return str(stop)
    raise AssertionError("the program did not stop")


def test_retry_then_success():
    session = FakeSession([requests.Timeout(), FakeResponse(500), FakeResponse(200, {"ok": 1})])
    assert rs.jira_get(session, "https://x", "/p", {}) == {"ok": 1}
    assert session.calls == 3


def test_retry_gives_up():
    session = FakeSession([requests.ConnectionError()] * 3)
    assert "3 tries" in exit_text(rs.jira_get, session, "https://x", "/p", {})


def test_rate_limit_uses_retry_after():
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, {"ok": 1})])
    assert rs.jira_get(session, "https://x", "/p", {}) == {"ok": 1}


def test_clear_errors():
    assert "401" in exit_text(rs.jira_get, FakeSession([FakeResponse(401)]), "https://x", "/p", {})
    assert "403" in exit_text(rs.jira_get, FakeSession([FakeResponse(403)]), "https://x", "/p", {})
    assert "400" in exit_text(rs.jira_get, FakeSession([FakeResponse(400, text="bad jql")]), "https://x", "/p", {})
    assert "404" in exit_text(rs.jira_get, FakeSession([FakeResponse(404, text="gone")]), "https://x", "/p", {})
    assert "valid JSON" in exit_text(rs.jira_get, FakeSession([FakeResponse(200)]), "https://x", "/p", {})


def test_paging_follows_the_token():
    session = FakeSession([
        FakeResponse(200, {"issues": [{"key": "A-1"}], "nextPageToken": "t2"}),
        FakeResponse(200, {"issues": [{"key": "A-2"}]}),
    ])
    assert [i["key"] for i in rs.search(session, "https://x", "jql")] == ["A-1", "A-2"]
    assert session.calls == 2


def test_queries():
    q = rs.build_queries("KAN", date(2026, 10, 12), date(2026, 10, 19), ["Task"], ["High"])
    assert 'created >= "2026-10-12" AND created < "2026-10-20"' in q["Created"]
    assert 'issuetype in ("Task")' in q["Open now"]
    assert 'priority in ("High")' in q["Resolved"]
    assert rs.quote('a"b') == '"a\\"b"'


def issue(key="A-1", created="2026-10-19T10:00:00.000+0200", resolved=None):
    return {"key": key, "fields": {"created": created, "resolutiondate": resolved}}


def test_data_warnings():
    assert rs.check_issue(issue(), "Created") == []
    assert rs.check_issue(issue(resolved="2026-10-19T12:00:00.000+0200"), "Resolved") == []
    assert "no resolution date" in rs.check_issue(issue(), "Resolved")[0]
    assert "open but has a resolution date" in rs.check_issue(
        issue(resolved="2026-10-19T12:00:00.000+0200"), "Open now")[0]
    assert "before it was created" in rs.check_issue(
        issue(resolved="2026-10-19T08:00:00.000+0200"), "Resolved")[0]
    # the same moment written with two different offsets is not an error
    assert rs.check_issue(issue(resolved="2026-10-19T08:30:00.000+0000"), "Resolved") == []
    assert "could not be read" in rs.check_issue(issue(resolved="yesterday"), "Resolved")[0]
    assert "without a key" in rs.check_issue({"fields": {}}, "Created")[0]


def test_report_shows_warnings():
    sections = [("Created", []), ("Resolved", []), ("Open now", [])]
    title, text, page = rs.make_report("KAN", date(2026, 10, 12), date(2026, 10, 19), sections,
                                       "Filters: types=all; priorities=all", ["A-1 is odd"])
    assert "Data warnings (1)" in text and "A-1 is odd" in text
    assert "Data warnings (1)" in page
    _, text, _ = rs.make_report("KAN", date(2026, 10, 12), date(2026, 10, 19), sections, "f", [])
    assert "Data warnings" not in text


if __name__ == "__main__":
    tests = [name for name in dir() if name.startswith("test_")]
    for name in tests:
        globals()[name]()
        print("ok  ", name)
    print(f"\nAll {len(tests)} tests passed.")
