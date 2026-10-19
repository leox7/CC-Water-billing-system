"""The audit log as the developer reads it (TASK-071, AUDIT_LOG.md sections 7 and 8).

Every query in AUDIT_LOG.md section 7 is run here, word for word apart from the example ids, on
events made through the real flow. If a column is renamed, these fail before the queries in the
document stop working on the server. The pruning script in scripts/ is checked the same way.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

from app.features.payments import service as payments
from app.features.readings import service as readings
from tests.conftest import TEST_CSRF_TOKEN, set_session
from tests.test_permissions import app_routes

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# AUDIT_LOG.md section 7, with the example ids (7f3a9c1e, 12, 41) as parameters
SECTION_7_QUERIES = {
    "latest 50 events": """
        SELECT created_at, level, event_type, actor_label, summary
        FROM audit_log ORDER BY audit_id DESC LIMIT 50""",
    "problems in the last 24 hours": """
        SELECT created_at, level, event_type, actor_label, summary, details
        FROM audit_log
        WHERE level IN ('warning','error')
          AND created_at > NOW() - INTERVAL 1 DAY
        ORDER BY audit_id DESC""",
    "everything in one request": """
        SELECT created_at, event_type, summary, details
        FROM audit_log WHERE request_id = :request_id ORDER BY audit_id""",
    "the full story of one customer": """
        SELECT created_at, event_type, actor_label, summary
        FROM audit_log WHERE customer_id = :customer_id ORDER BY audit_id""",
    "the history of one reading": """
        SELECT created_at, event_type, actor_label, summary, details
        FROM audit_log WHERE entity_type = 'reading' AND entity_id = :reading_id ORDER BY audit_id""",
    "SMS problems this week": """
        SELECT created_at, event_type, summary, details
        FROM audit_log
        WHERE event_type IN ('sms.failed','sms.send_blocked','sms.resend_refused')
          AND created_at > NOW() - INTERVAL 7 DAY
        ORDER BY audit_id DESC""",
    "who deleted or changed things": """
        SELECT created_at, event_type, actor_label, summary, details
        FROM audit_log
        WHERE event_type IN ('reading.deleted','reading.updated','bill.deleted',
                             'bill.recalculated','customer.removed','customer.updated')
        ORDER BY audit_id DESC""",
    "sign in trouble": """
        SELECT created_at, event_type, ip_address, summary
        FROM audit_log
        WHERE event_type LIKE 'auth.%' AND created_at > NOW() - INTERVAL 1 DAY
        ORDER BY audit_id DESC""",
    "how busy was each event type today": """
        SELECT event_type, COUNT(*) AS times
        FROM audit_log WHERE created_at >= CURDATE()
        GROUP BY event_type ORDER BY times DESC""",
}


@pytest.fixture
def a_day_of_events(web, db_session, make_user, make_customer, sign_in_as, sms):
    """A sign in that fails, a reading whose SMS fails, a payment sent through the web page, and
    a correction: one of each kind of row the queries look for."""
    owner = make_user(role="owner", phone="254722000111")
    customer = make_customer()
    # the login page would have put a CSRF token in the session
    set_session(web, {"csrf_token": TEST_CSRF_TOKEN})
    web.post("/login", data={"identifier": "0722000111", "password": "wrong-password-1",
                             "csrf_token": TEST_CSRF_TOKEN})
    sms.failure = "Insufficient balance"
    result = readings.create_reading(db_session, owner, customer.customer_id,
                                     {"current_reading": "10", "reading_date": "2026-09-30"})
    sign_in_as(web, owner)
    paid = web.post(f"/bills/{result.bill.bill_id}/payments", headers={"HX-Request": "true"},
                    data={"csrf_token": TEST_CSRF_TOKEN, "amount": "500", "payment_date": "2026-10-01"})
    # a bill with a payment is corrected in place (PRD 4.4), so the reading keeps its id
    readings.correct_reading(db_session, owner, result.reading.reading_id,
                             {"current_reading": "12", "reading_date": "2026-09-30"})
    return {"customer_id": customer.customer_id, "reading_id": result.reading.reading_id,
            "request_id": paid.headers["X-Request-ID"]}


def run(db_session, query, ids):
    return db_session.execute(text(query), ids).mappings().all()


def test_every_section_7_query_runs_and_finds_its_rows(db_session, a_day_of_events):
    found = {name: run(db_session, query, a_day_of_events) for name, query in SECTION_7_QUERIES.items()}

    events = [row["event_type"] for row in found["latest 50 events"]]
    assert {"reading.created", "bill.generated", "sms.failed", "reading.updated", "payment.recorded"} <= set(events)
    assert "auth.login_failed" in [row["event_type"] for row in found["problems in the last 24 hours"]]
    assert "auth.login_failed" in [row["event_type"] for row in found["sign in trouble"]]
    sms_problems = {row["event_type"] for row in found["SMS problems this week"]}
    assert sms_problems == {"sms.failed"}
    assert "reading.updated" in [row["event_type"] for row in found["who deleted or changed things"]]
    customer_events = [row["event_type"] for row in found["the full story of one customer"]]
    assert customer_events[:2] == ["reading.created", "bill.generated"] and "payment.recorded" in customer_events
    assert [row["event_type"] for row in found["the history of one reading"]] == ["reading.created", "reading.updated"]
    assert sum(row["times"] for row in found["how busy was each event type today"]) >= len(events)


def test_the_request_query_finds_what_one_request_did(db_session, a_day_of_events):
    rows = run(db_session, SECTION_7_QUERIES["everything in one request"], a_day_of_events)

    # the Reference on an error page, or the X-Request-ID header, leads to what that request did
    assert [row["event_type"] for row in rows] == ["payment.recorded", "bill.status_changed"]


def test_the_pruning_script_removes_only_rows_older_than_12_months(db_session, a_day_of_events):
    db_session.execute(text("INSERT INTO audit_log (created_at, level, event_type, summary) "
                            "VALUES (NOW() - INTERVAL 13 MONTH, 'info', 'system.started', 'an old row'), "
                            "(NOW() - INTERVAL 11 MONTH, 'info', 'system.started', 'a recent row')"))
    before = db_session.scalar(text("SELECT COUNT(*) FROM audit_log"))
    script = (PROJECT_ROOT / "scripts" / "prune_audit_log.sql").read_text(encoding="utf-8")
    without_comments = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))
    statements = [part.strip() for part in without_comments.split(";") if part.strip()]

    counted = db_session.execute(text(statements[0])).mappings().one()
    for statement in statements[1:]:
        db_session.execute(text(statement))

    assert counted["rows_to_remove"] == 1
    assert db_session.scalar(text("SELECT COUNT(*) FROM audit_log")) == before - 1
    summaries = db_session.scalars(text("SELECT summary FROM audit_log")).all()
    assert "an old row" not in summaries and "a recent row" in summaries


def test_no_route_shows_the_audit_log(web):
    # AUDIT_LOG.md rule 11 and section 8: there is no route, screen or export for it
    paths = [path for _, path in app_routes(web.app.routes)]
    controllers = list((PROJECT_ROOT / "app" / "features").glob("*/controller.py"))

    assert len(paths) > 40
    assert not [path for path in paths if "audit" in path or "activity" in path]
    assert controllers and not [path for path in controllers if "AuditLog" in path.read_text(encoding="utf-8")]
