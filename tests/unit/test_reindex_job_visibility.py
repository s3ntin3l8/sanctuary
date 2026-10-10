from datetime import timedelta

import pytest

from app.api.v1.settings_ai import _reindex_job
from app.core.timezone import now_utc
from app.models.database import AppSettings
from app.services import user_settings_service as svc


def _age_job(db, hours: float) -> None:
    row = db.query(AppSettings).first()
    job = dict(row.settings_json["reindex_job"])
    job["ended_at"] = (now_utc() - timedelta(hours=hours)).isoformat()
    row.settings_json = {**row.settings_json, "reindex_job": job}
    db.commit()


@pytest.mark.unit
def test_a_recent_failure_is_shown_and_an_old_one_is_not(db_session):
    svc.set_reindex_running(db_session, total=10, embed_dim=1024)
    svc.set_reindex_failed(db_session, "stale: no progress for >60min")
    db_session.commit()

    assert _reindex_job(db_session).status == "failed"

    _age_job(db_session, hours=30)
    assert _reindex_job(db_session) is None


@pytest.mark.unit
def test_a_running_job_is_always_shown(db_session):
    svc.set_reindex_running(db_session, total=10, embed_dim=1024)
    db_session.commit()
    assert _reindex_job(db_session).status == "running"


@pytest.mark.unit
@pytest.mark.parametrize("ended_at", ["not-a-date", 12345])
def test_an_unreadable_ended_at_hides_the_result_instead_of_failing(
    db_session, ended_at
):
    svc.set_reindex_running(db_session, total=10, embed_dim=1024)
    svc.set_reindex_failed(db_session, "boom")
    row = db_session.query(AppSettings).first()
    job = {**row.settings_json["reindex_job"], "ended_at": ended_at}
    row.settings_json = {**row.settings_json, "reindex_job": job}
    db_session.commit()

    assert _reindex_job(db_session) is None


@pytest.mark.unit
def test_a_naive_ended_at_is_read_as_utc(db_session):
    svc.set_reindex_running(db_session, total=10, embed_dim=1024)
    svc.set_reindex_failed(db_session, "boom")
    row = db_session.query(AppSettings).first()
    job = {
        **row.settings_json["reindex_job"],
        "ended_at": (now_utc() - timedelta(hours=1)).replace(tzinfo=None).isoformat(),
    }
    row.settings_json = {**row.settings_json, "reindex_job": job}
    db_session.commit()

    assert _reindex_job(db_session).status == "failed"
