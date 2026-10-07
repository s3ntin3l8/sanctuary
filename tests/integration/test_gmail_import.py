"""Gmail history import: index grouping, listing, import selection and run control."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import redis
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, GmailMessageIndex, IngestBatch, Proceeding, User
from app.models.enums import (
    CaseStatus,
    IngestBatchSourceType,
    Jurisdiction,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services import gmail_index_service, gmail_runs, user_settings_service

pytestmark = pytest.mark.integration

client = TestClient(app)

CREDS = json.dumps({"token": "t", "refresh_token": "r"})  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _runs(fake_run_state):
    return fake_run_state


def _admin(db) -> int:
    return db.query(User).filter_by(email="admin@localhost").one().id


def _connect(db, uid, allowlist=("lawyer@example.com",)):
    user_settings_service.set_gmail_credentials(
        db, uid, credentials_json=CREDS, connected_at="2026-01-01T00:00:00+00:00"
    )
    user_settings_service.set_gmail_inbox_filters(
        db, uid, allowlist=list(allowlist), label_filter=""
    )
    db.commit()


def _index(db, uid, gmail_id, subject, day, *, thread=None, message_id=None, **extra):
    gmail_index_service.upsert_metadata(
        db,
        uid,
        [
            {
                "gmail_id": gmail_id,
                "thread_id": thread or f"thread-{gmail_id}",
                "message_id": message_id or f"<{gmail_id}@x>",
                "sender": "lawyer@example.com",
                "subject": subject,
                "sent_at": datetime(2024, 1, day, tzinfo=UTC),
                "has_attachments": False,
                "size_estimate": 100,
                **extra,
            }
        ],
    )
    gmail_index_service.assign_group_keys(db, uid)
    db.commit()


def _group(db, gmail_id):
    row = db.query(GmailMessageIndex).filter_by(gmail_id=gmail_id).one()
    return row.group_key, row.group_kind


# --- Grouping ----------------------------------------------------------------


def test_reference_comes_from_the_subject(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 Schriftsatz", 1)
    _index(db_session, uid, "b", "Ladung 003 F 426/25", 2)
    _index(db_session, uid, "c", "Allgemeine Frage", 3)
    assert _group(db_session, "a") == ("8372-25", "internal_id")
    assert _group(db_session, "b") == ("3 F 426/25", "az_court")
    assert _group(db_session, "c") == (None, None)


def test_file_number_beats_court_reference_when_both_are_present(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 zu 003 F 426/25", 1)
    assert _group(db_session, "a") == ("8372-25", "internal_id")


def test_reply_without_a_reference_inherits_its_threads(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "letter", "8372/25 Schriftsatz", 1, thread="T1")
    _index(db_session, uid, "reply", "AW: Ihr Schreiben", 5, thread="T1")
    _index(db_session, uid, "other", "AW: Ihr Schreiben", 6, thread="T2")
    assert _group(db_session, "reply") == ("8372-25", "internal_id")
    assert _group(db_session, "other") == (None, None)


def test_a_message_keeps_its_own_reference_inside_a_thread(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "first", "8372/25 Anfang", 1, thread="T1")
    _index(db_session, uid, "second", "9001/24 Neues Verfahren", 2, thread="T1")
    assert _group(db_session, "second") == ("9001-24", "internal_id")


def test_thread_inheritance_follows_late_indexed_messages(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "reply", "AW: Ihr Schreiben", 5, thread="T1")
    assert _group(db_session, "reply") == (None, None)
    _index(db_session, uid, "letter", "8372/25 Schriftsatz", 1, thread="T1")
    assert _group(db_session, "reply") == ("8372-25", "internal_id")


def test_reindexing_the_same_message_is_a_no_op(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 x", 1)
    _index(db_session, uid, "a", "8372/25 x", 1)
    assert db_session.query(GmailMessageIndex).count() == 1


# --- Groups endpoint ---------------------------------------------------------


def test_groups_are_oldest_first_with_unreferenced_last(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "n1", "no reference", 1)  # oldest overall, but unreferenced
    _index(db_session, uid, "b1", "9001/24 later case", 10)
    _index(db_session, uid, "a1", "8372/25 earlier case", 2)
    _index(db_session, uid, "a2", "8372/25 earlier case again", 4)

    groups = client.get("/api/v1/gmail/groups").json()["groups"]

    assert [g["key"] for g in groups] == ["8372-25", "9001-24", "unreferenced"]
    first = groups[0]
    assert first["count"] == 2 and first["ingested_count"] == 0
    assert first["kind"] == "internal_id"
    assert first["first_at"].startswith("2024-01-02") and first["last_at"].startswith(
        "2024-01-04"
    )
    assert groups[2]["kind"] is None and groups[2]["matched_case_id"] is None


def test_groups_show_the_case_they_would_file_into(db_session):
    uid = _admin(db_session)
    db_session.add(
        Case(
            id="8372-25",
            title="Vogt",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    other = Case(
        id="ADV-1",
        title="Other",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(
        Proceeding(
            case_id="ADV-1",
            court_name="AG Testhausen",
            court_level=ProceedingCourtLevel.AG,
            status=ProceedingStatus.ACTIVE,
            az_court="3 F 426/25",  # the canonical form normalize_az_court stores,
        )
    )
    db_session.commit()
    _index(db_session, uid, "a", "8372/25 x", 1)
    _index(db_session, uid, "b", "Ladung 003 F 426/25", 2)
    _index(db_session, uid, "c", "9001/24 no case yet", 3)

    matched = {
        g["key"]: g["matched_case_id"]
        for g in client.get("/api/v1/gmail/groups").json()["groups"]
    }

    assert matched == {"8372-25": "8372-25", "3 F 426/25": "ADV-1", "9001-24": None}


def test_ingested_is_derived_from_batches_and_flips_back_on_delete(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 x", 1, message_id="<a@x>")
    _index(db_session, uid, "b", "8372/25 y", 2, message_id="<b@x>")
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="x",
        message_id="<a@x>",
        owner_id=uid,
    )
    db_session.add(batch)
    db_session.commit()

    group = client.get("/api/v1/gmail/groups").json()["groups"][0]
    assert group["ingested_count"] == 1
    items = client.get("/api/v1/gmail/messages", params={"group": "8372-25"}).json()[
        "items"
    ]
    assert [(m["gmail_id"], m["ingested"]) for m in items] == [
        ("a", True),
        ("b", False),
    ]

    db_session.delete(batch)
    db_session.commit()
    assert client.get("/api/v1/gmail/groups").json()["groups"][0]["ingested_count"] == 0


def test_messages_page_oldest_first_with_cursor(db_session):
    uid = _admin(db_session)
    for day, gid in enumerate(["m3", "m1", "m2"], start=1):  # inserted out of order
        _index(db_session, uid, gid, "8372/25 x", {"m1": 1, "m2": 2, "m3": 3}[gid])

    page1 = client.get(
        "/api/v1/gmail/messages", params={"group": "8372-25", "limit": 2}
    ).json()
    assert [m["gmail_id"] for m in page1["items"]] == ["m1", "m2"]
    page2 = client.get(
        "/api/v1/gmail/messages",
        params={"group": "8372-25", "limit": 2, "cursor": page1["next_cursor"]},
    ).json()
    assert [m["gmail_id"] for m in page2["items"]] == ["m3"]
    assert page2["next_cursor"] is None


def test_messages_unreferenced_group(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 x", 1)
    _index(db_session, uid, "n", "plain", 2)
    items = client.get(
        "/api/v1/gmail/messages", params={"group": "unreferenced"}
    ).json()["items"]
    assert [m["gmail_id"] for m in items] == ["n"]


def test_the_index_is_private_to_its_owner(db_session):
    from app.services import auth_service

    other = auth_service.create_user(
        db_session,
        email="other@example.com",
        password="password123",  # pragma: allowlist secret
    )
    _index(db_session, other.id, "theirs", "8372/25 x", 1)
    assert client.get("/api/v1/gmail/groups").json()["groups"] == []


def test_the_index_survives_clear_all_data(db_session):
    from app.services.maintenance_service import clear_all_data

    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 x", 1)
    clear_all_data(db_session)
    assert db_session.query(GmailMessageIndex).count() == 1


# --- Index endpoints ---------------------------------------------------------


def test_index_refresh_needs_a_ready_mailbox(db_session):
    assert client.post("/api/v1/gmail/index").json()["code"] == "gmail_not_connected"
    uid = _admin(db_session)
    _connect(db_session, uid, allowlist=())
    assert client.post("/api/v1/gmail/index").json()["code"] == "gmail_allowlist_empty"


def test_index_refresh_dispatches_once_while_running(db_session):
    _connect(db_session, _admin(db_session))
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        assert client.post("/api/v1/gmail/index").status_code == 202
        assert client.post("/api/v1/gmail/index").status_code == 202  # idempotent
    assert dispatch.call_count == 1
    task, user_id, run_id = dispatch.call_args.args
    assert run_id == gmail_runs.get_run("index", user_id)["run_id"]


def test_index_status_reports_progress_and_counts(db_session):
    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 x", 1)
    assert gmail_runs.begin_run(
        "index", uid, {"run_id": "r1", "total": 10, "done": 4, "skipped": 2}
    )
    status = client.get("/api/v1/gmail/index/status").json()
    assert status["running"] is True
    assert (status["done"], status["total"], status["indexed_count"]) == (4, 10, 1)
    assert status["skipped"] == 2
    assert status["last_indexed_at"]

    gmail_runs.finish_run("index", uid, "r1", error="boom")
    status = client.get("/api/v1/gmail/index/status").json()
    assert status["running"] is False and status["error"] == "boom"


# --- Import selection --------------------------------------------------------


def _seed_history(db, uid):
    _index(db, uid, "a1", "8372/25 one", 1)
    _index(db, uid, "b1", "9001/24 one", 2)
    _index(db, uid, "a2", "8372/25 two", 3)
    _index(db, uid, "n1", "plain", 4)
    _index(db, uid, "a3", "8372/25 three", 5)


def _import(**body):
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        response = client.post("/api/v1/gmail/import", json=body)
    return response, dispatch


def test_import_next_n_oldest_across_everything(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)

    response, dispatch = _import(oldest_n=3)

    assert response.status_code == 202 and response.json() == {"queued": 3}
    state = gmail_runs.get_run("import", uid)
    assert state["remaining"] == ["a1", "b1", "a2"]  # oldest first, across groups
    assert state["total"] == 3 and state["sequential"] is True
    task, user_id, run_id = dispatch.call_args.args
    assert (user_id, run_id) == (uid, state["run_id"])


def test_import_a_group_skips_what_is_already_ingested(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    db_session.add(
        IngestBatch(
            source_type=IngestBatchSourceType.EMAIL,
            subject="x",
            message_id="<a1@x>",
            owner_id=uid,
        )
    )
    db_session.commit()

    response, _ = _import(group="8372-25")

    assert response.json() == {"queued": 2}
    assert gmail_runs.get_run("import", uid)["remaining"] == ["a2", "a3"]


def test_import_the_unreferenced_group(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    _import(group="unreferenced")
    assert gmail_runs.get_run("import", uid)["remaining"] == ["n1"]


def test_import_an_explicit_pick_is_still_ordered_oldest_first(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    _import(gmail_ids=["a3", "b1", "does-not-exist"])
    assert gmail_runs.get_run("import", uid)["remaining"] == ["b1", "a3"]


def test_import_before_a_date_and_non_sequential(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    _import(before="2024-01-03", sequential=False)
    state = gmail_runs.get_run("import", uid)
    assert state["remaining"] == ["a1", "b1"] and state["sequential"] is False


def test_import_with_nothing_left_queues_nothing(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    response, dispatch = _import(group="8372-25")
    assert response.status_code == 202 and response.json() == {"queued": 0}
    dispatch.assert_not_called()
    assert gmail_runs.get_run("import", uid) is None


def test_import_is_capped_at_100_per_request(db_session):
    _connect(db_session, _admin(db_session))
    assert (
        client.post("/api/v1/gmail/import", json={"oldest_n": 101}).status_code == 422
    )


def test_only_one_import_runs_at_a_time(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    first, _ = _import(oldest_n=2)
    second, dispatch = _import(oldest_n=2)
    assert first.status_code == 202
    assert second.status_code == 409 and second.json()["code"] == "import_running"
    dispatch.assert_not_called()


def test_a_finished_import_does_not_block_the_next(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    _import(oldest_n=1)
    gmail_runs.finish_run("import", uid, gmail_runs.get_run("import", uid)["run_id"])
    response, _ = _import(oldest_n=1)
    assert response.status_code == 202


def test_import_needs_a_ready_mailbox(db_session):
    assert (
        client.post("/api/v1/gmail/import", json={}).json()["code"]
        == "gmail_not_connected"
    )


# --- Run status + cancel -----------------------------------------------------


def test_import_status_when_nothing_has_run(db_session):
    status = client.get("/api/v1/gmail/import/status").json()
    assert status["active"] is False and status["total"] == 0


def test_import_status_while_running_and_after_cancel(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    _import(oldest_n=3)

    status = client.get("/api/v1/gmail/import/status").json()
    assert status["active"] is True and status["total"] == 3 and status["done"] == 0
    assert status["waiting"] is False

    assert client.delete("/api/v1/gmail/import").status_code == 204
    status = client.get("/api/v1/gmail/import/status").json()
    assert status["active"] is False and status["cancelled"] is True
    assert status["finished_at"]
    assert gmail_runs.get_run("import", uid)["remaining"] == []


def test_cancel_without_a_running_import_is_a_409(db_session):
    response = client.delete("/api/v1/gmail/import")
    assert (
        response.status_code == 409 and response.json()["code"] == "no_import_running"
    )


# --- Robustness --------------------------------------------------------------


def test_a_malformed_page_cursor_is_a_422_not_a_500(db_session):
    response = client.get("/api/v1/gmail/messages", params={"cursor": "not-a-cursor"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


def _age(fake, kind, uid, seconds):
    """Make a run look like nothing has touched it for `seconds`."""
    key = f"sanctuary:gmail_{kind}:{uid}"
    state = json.loads(fake.store[key])
    state["updated_at"] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    fake.store[key] = json.dumps(state)


def test_a_run_whose_task_vanished_does_not_lock_the_user_out(
    db_session, fake_run_state
):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    _import(oldest_n=2)
    _age(fake_run_state, "import", uid, 3600)  # no hop has touched it for an hour

    assert client.get("/api/v1/gmail/import/status").json()["active"] is False
    response, dispatch = _import(oldest_n=2)  # a fresh run may take over
    assert response.status_code == 202 and response.json() == {"queued": 2}
    dispatch.assert_called_once()


def test_a_stuck_index_refresh_can_be_started_again(db_session, fake_run_state):
    uid = _admin(db_session)
    _connect(db_session, uid)
    gmail_runs.begin_run(
        "index", uid, {"run_id": "old", "total": 0, "done": 0, "skipped": 0}
    )
    _age(fake_run_state, "index", uid, 3600)
    assert client.get("/api/v1/gmail/index/status").json()["running"] is False
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        assert client.post("/api/v1/gmail/index").status_code == 202
    dispatch.assert_called_once()


def test_starting_without_redis_is_a_clear_503_not_a_silent_no_op(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    down = patch.object(
        gmail_runs, "_get_client", side_effect=redis.ConnectionError("down")
    )
    with down, patch("app.tasks.dispatch.dispatch_task") as dispatch:
        imported = client.post("/api/v1/gmail/import", json={"oldest_n": 2})
        indexed = client.post("/api/v1/gmail/index")
    for response in (imported, indexed):
        assert response.status_code == 503
        assert response.json()["code"] == "run_state_unavailable"
    dispatch.assert_not_called()


def test_disconnect_forgets_the_mailbox_mirror_and_stops_runs(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    _import(oldest_n=2)
    gmail_runs.begin_run(
        "index", uid, {"run_id": "r1", "total": 0, "done": 0, "skipped": 0}
    )

    with patch("app.api.v1.settings_gmail.revoke_token"):
        assert client.delete("/api/v1/settings/gmail").status_code == 200

    # A different account may be connected next: nothing of the old one lingers.
    assert db_session.query(GmailMessageIndex).count() == 0
    assert gmail_runs.is_active(gmail_runs.get_run("import", uid)) is False
    assert gmail_runs.is_active(gmail_runs.get_run("index", uid)) is False


def test_disconnect_works_even_when_redis_is_down(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _seed_history(db_session, uid)
    down = patch.object(
        gmail_runs, "_get_client", side_effect=redis.ConnectionError("down")
    )
    with down, patch("app.api.v1.settings_gmail.revoke_token"):
        response = client.delete("/api/v1/settings/gmail")
    assert response.status_code == 200 and response.json()["connected"] is False
    assert db_session.query(GmailMessageIndex).count() == 0


# --- Local cache -------------------------------------------------------------


def test_messages_and_status_report_what_is_cached(db_session):
    from app.services import gmail_cache

    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 x", 1)
    _index(db_session, uid, "b", "8372/25 y", 2)
    gmail_cache.write(uid, "b", b"12345")

    items = client.get("/api/v1/gmail/messages", params={"group": "8372-25"}).json()[
        "items"
    ]
    assert [(m["gmail_id"], m["cached"]) for m in items] == [("a", False), ("b", True)]
    status = client.get("/api/v1/gmail/index/status").json()
    assert (status["cached_count"], status["cached_bytes"]) == (1, 5)


def test_clearing_the_cache_deletes_local_copies_only(db_session):
    from app.services import gmail_cache

    uid = _admin(db_session)
    _index(db_session, uid, "a", "8372/25 x", 1, message_id="<a@x>")
    gmail_cache.write(uid, "a", b"raw")
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="x",
        message_id="<a@x>",
        owner_id=uid,
    )
    db_session.add(batch)
    db_session.commit()

    assert client.delete("/api/v1/gmail/cache").status_code == 204

    assert gmail_cache.stats(uid) == (0, 0)
    assert (
        db_session.get(IngestBatch, batch.id) is not None
    )  # imported bundles untouched
    assert db_session.query(GmailMessageIndex).count() == 1  # and so is the index
    assert client.delete("/api/v1/gmail/cache").status_code == 204  # idempotent


def test_clearing_the_cache_leaves_other_users_alone(db_session):
    from app.services import auth_service, gmail_cache

    other = auth_service.create_user(
        db_session,
        email="other-cache@example.com",
        password="password123",  # pragma: allowlist secret
    )
    gmail_cache.write(other.id, "theirs", b"x")
    client.delete("/api/v1/gmail/cache")
    assert gmail_cache.read(other.id, "theirs") == b"x"


def test_clear_all_data_keeps_the_cache_directory_and_its_files(db_session):
    from app.services import gmail_cache
    from app.services.maintenance_service import clear_all_data

    uid = _admin(db_session)
    gmail_cache.write(uid, "a", b"raw")
    clear_all_data(db_session)
    assert gmail_cache.read(uid, "a") == b"raw"
