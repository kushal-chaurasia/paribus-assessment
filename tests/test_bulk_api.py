import json
import time

from conftest import csv_file

CSV = "name,address,phone\nGeneral Hospital,1 Main St,555-1234\nCity Clinic,2 Oak Ave,\nMercy,3 Elm St,555-9999\n"


def test_bulk_create_success(client, upstream):
    resp = client.post("/hospitals/bulk", files=csv_file(CSV))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["total_hospitals"] == 3
    assert body["processed_hospitals"] == 3
    assert body["failed_hospitals"] == 0
    assert body["batch_activated"] is True
    assert body["processing_time_seconds"] >= 0
    assert [h["row"] for h in body["hospitals"]] == [1, 2, 3]
    assert {h["status"] for h in body["hospitals"]} == {"created_and_activated"}

    created = upstream.in_batch(body["batch_id"])
    assert len(created) == 3 and all(h["active"] for h in created)
    assert {h["phone"] for h in created} == {"555-1234", None, "555-9999"}
    assert upstream.count("PATCH", "/activate") == 1


def test_partial_failure_leaves_batch_inactive(client, upstream):
    upstream.fail_names.add("City Clinic")

    body = client.post("/hospitals/bulk", files=csv_file(CSV)).json()

    assert body["status"] == "failed"
    assert body["processed_hospitals"] == 2
    assert body["failed_hospitals"] == 1
    assert body["batch_activated"] is False
    failed = next(h for h in body["hospitals"] if h["status"] == "failed")
    assert failed["row"] == 2 and "422" in failed["error"]
    assert upstream.count("PATCH") == 0
    assert not any(h["active"] for h in upstream.hospitals)


def test_resume_retries_only_failed_rows(client, upstream):
    upstream.fail_names.add("City Clinic")
    job = client.post("/hospitals/bulk", files=csv_file(CSV)).json()
    batch_id = job["batch_id"]

    upstream.fail_names.clear()
    body = client.post(f"/hospitals/bulk/jobs/{job['job_id']}/resume").json()

    assert body["status"] == "completed"
    assert body["processed_hospitals"] == 3
    assert upstream.count("POST", "/hospitals/") == 4
    assert len(upstream.in_batch(batch_id)) == 3


def test_resume_adopts_hospitals_already_created_upstream(client, upstream):
    """A create whose response was lost must be adopted, not duplicated."""
    upstream.ghost_names.add("Mercy")
    body = client.post("/hospitals/bulk", files=csv_file(CSV)).json()
    assert body["failed_hospitals"] == 1
    ghost_id = next(h["id"] for h in upstream.hospitals if h["name"] == "Mercy")

    upstream.ghost_names.clear()
    posts_before = upstream.count("POST")
    resumed = client.post(f"/hospitals/bulk/jobs/{body['job_id']}/resume").json()

    assert resumed["status"] == "completed"
    assert upstream.count("POST") == posts_before
    assert len(upstream.in_batch(body["batch_id"])) == 3
    assert next(h for h in resumed["hospitals"] if h["name"] == "Mercy")["hospital_id"] == ghost_id


def test_activation_failure_can_be_resumed(client, upstream):
    upstream.activate_failures.extend([500, 500, 500])

    body = client.post("/hospitals/bulk", files=csv_file(CSV)).json()
    assert body["status"] == "activation_failed"
    assert body["processed_hospitals"] == 3
    assert body["batch_activated"] is False

    resumed = client.post(f"/hospitals/bulk/jobs/{body['job_id']}/resume").json()
    assert resumed["status"] == "completed"
    assert resumed["batch_activated"] is True
    assert upstream.count("POST") == 3


def test_transient_upstream_errors_are_retried(client, upstream):
    upstream.create_failures.extend([503, 502])

    body = client.post("/hospitals/bulk", files=csv_file("name,address\nGeneral,1 Main St\n")).json()

    assert body["status"] == "completed"
    assert upstream.count("POST") == 3


def test_completed_batch_cannot_be_resumed(client):
    job_id = client.post("/hospitals/bulk", files=csv_file(CSV)).json()["job_id"]

    assert client.post(f"/hospitals/bulk/jobs/{job_id}/resume").status_code == 409


def test_invalid_csv_is_rejected_before_any_upstream_call(client, upstream):
    resp = client.post("/hospitals/bulk", files=csv_file("name,address\n,1 Main St\n"))

    assert resp.status_code == 422
    assert resp.json()["errors"][0] == {"row": 1, "field": "name", "message": "'name' is required"}
    assert not upstream.calls


def test_too_many_rows_rejected(client):
    body = "name,address\n" + "".join(f"H{i},Addr {i}\n" for i in range(21))
    resp = client.post("/hospitals/bulk", files=csv_file(body))
    assert resp.status_code == 422
    assert resp.json()["total_hospitals"] == 21


def test_non_csv_upload_rejected(client):
    assert client.post("/hospitals/bulk", files=csv_file("x", filename="data.txt")).status_code == 415


def test_validate_endpoint(client, upstream):
    resp = client.post("/hospitals/bulk/validate", files=csv_file(CSV))

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["total_hospitals"] == 3
    assert body["hospitals"][1] == {"row": 2, "name": "City Clinic", "address": "2 Oak Ave", "phone": None}
    assert not upstream.calls


def test_unknown_batch_returns_404(client):
    assert client.get("/hospitals/bulk/jobs/does-not-exist").status_code == 404


def _wait_for_terminal(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/hospitals/bulk/jobs/{job_id}").json()
        if body["status"] not in ("pending", "processing"):
            return body
        time.sleep(0.02)
    raise AssertionError("batch did not finish in time")


def test_async_mode_returns_202_and_is_pollable(client):
    resp = client.post("/hospitals/bulk?async=true", files=csv_file(CSV))

    assert resp.status_code == 202
    accepted = resp.json()
    assert accepted["status_url"] == f"/hospitals/bulk/jobs/{accepted['job_id']}"
    assert _wait_for_terminal(client, accepted["job_id"])["status"] == "completed"


def _read_events(response) -> list[dict]:
    events, current = [], {}
    for line in response.iter_lines():
        if not line:
            if current:
                events.append(current)
            current = {}
        elif not line.startswith(":"):
            key, _, value = line.partition(": ")
            current[key] = json.loads(value) if key == "data" else value
    return events


def test_sse_streams_progress_until_done(client):
    accepted = client.post("/hospitals/bulk?async=true", files=csv_file(CSV)).json()
    assert accepted["events_url"] == f"/hospitals/bulk/jobs/{accepted['job_id']}/events"

    with client.stream("GET", accepted["events_url"]) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _read_events(resp)

    assert events[0]["retry"] == "3000"
    assert [e["id"] for e in events] == [str(i) for i in range(1, len(events) + 1)]
    assert all(e["event"] == "progress" for e in events[:-1])
    final = events[-1]
    assert final["event"] == "complete"
    assert final["data"]["status"] == "completed"
    assert final["data"]["processed_hospitals"] == 3


def test_sse_for_finished_batch_sends_single_complete_event(client):
    job_id = client.post("/hospitals/bulk", files=csv_file(CSV)).json()["job_id"]

    with client.stream("GET", f"/hospitals/bulk/jobs/{job_id}/events") as resp:
        events = _read_events(resp)

    assert [e["event"] for e in events] == ["complete"]


def test_sse_unknown_batch_returns_404(client):
    assert client.get("/hospitals/bulk/jobs/does-not-exist/events").status_code == 404


def test_rollback_deletes_batch_upstream(client, upstream):
    job = client.post("/hospitals/bulk", files=csv_file(CSV)).json()
    batch_id = job["batch_id"]

    body = client.delete(f"/hospitals/bulk/jobs/{job['job_id']}").json()

    assert body["status"] == "rolled_back"
    assert {h["status"] for h in body["hospitals"]} == {"rolled_back"}
    assert upstream.in_batch(batch_id) == []
