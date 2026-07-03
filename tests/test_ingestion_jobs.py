from __future__ import annotations


def test_text_ingestion_records_succeeded_job(db_session):
    from citara.core.ingestion.text import add_text_source
    from citara.core.jobs import get_ingestion_job, list_ingestion_jobs

    source = add_text_source(db_session, title="Job Note", text="job status text")

    jobs = list_ingestion_jobs(db_session)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source_id == source.id
    assert job.job_type == "text_ingestion"
    assert job.status == "succeeded"
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.input_json == {"title": "Job Note", "collection_id": None}
    assert job.result_json["source_id"] == source.id
    assert job.result_json["chunk_count"] >= 1
    assert get_ingestion_job(db_session, job.id).id == job.id


def test_get_missing_ingestion_job_returns_none(db_session):
    from citara.core.jobs import get_ingestion_job

    assert get_ingestion_job(db_session, "job_missing") is None


def test_api_exposes_ingestion_jobs():
    from fastapi.testclient import TestClient

    from citara.adapters.api.main import create_app

    with TestClient(create_app()) as client:
        response = client.post("/sources/text", json={"title": "API Job Note", "text": "api job status text"})
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"].startswith("job_")

        job_response = client.get(f"/jobs/{body['job_id']}")
        assert job_response.status_code == 200
        job = job_response.json()
        assert job["job_id"] == body["job_id"]
        assert job["source_id"] == body["source_id"]
        assert job["status"] == "succeeded"
        assert job["job_type"] == "text_ingestion"

        list_response = client.get("/jobs")
        assert list_response.status_code == 200
        assert any(item["job_id"] == body["job_id"] for item in list_response.json()["jobs"])


def test_api_missing_ingestion_job_returns_404():
    from fastapi.testclient import TestClient

    from citara.adapters.api.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/jobs/job_missing")
        assert response.status_code == 404
