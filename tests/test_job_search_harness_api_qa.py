#!/usr/bin/env python3
"""Automated Deterministic Epic QA Test Suite for DISSID AI Job Search Code Harness.

Validates:
1. OpenAPI 3.0.3 Contract Compliance (`openapi_job_search_harness.yaml` & `openapi_job_search_harness.json`)
2. Live HTTP Endpoints on `http://127.0.0.1:8765/`:
   - `GET /` (Dashboard HTML & Dual Inbox Pill Presence)
   - `GET /api/state` (Real-time Metric Counters: 112 Submitted, 92 Drafted)
   - `GET /api/telemetry` (Live Event Stream Buffer)
   - `POST /api/action` [action='gmail_sync'] (Dual-Inbox Sync across work@example.com & jane.doe@example.com)
   - `POST /api/action` [action='run_pipeline'] (Live Llama.cpp Scrape/Rank/Apply Pipeline)
   - `POST /api/action` [action='batch_apply_all'] (Deduplicated Batch Application Submission)
   - Negative Test: `POST /api/action` [action='unsupported_action'] -> HTTP 400 Bad Request
"""

from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.request
import pytest

BASE_URL = "http://127.0.0.1:8765"
SPEC_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_YAML_PATH = SPEC_ROOT / "openapi_job_search_harness.yaml"
OPENAPI_JSON_PATH = SPEC_ROOT / "openapi_job_search_harness.json"


def _http_request(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: float = 25.0,
) -> tuple[int, dict | str]:
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8")
            if "application/json" in content_type:
                return resp.status, json.loads(raw)
            return resp.status, raw
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        try:
            return err.code, json.loads(raw)
        except Exception:
            return err.code, raw


def test_openapi_contract_spec_validity() -> None:
    """Asserts that openapi_job_search_harness.yaml and .json exist and define required paths."""
    assert OPENAPI_YAML_PATH.is_file(), f"Missing OpenAPI YAML at {OPENAPI_YAML_PATH}"
    assert OPENAPI_JSON_PATH.is_file(), f"Missing OpenAPI JSON at {OPENAPI_JSON_PATH}"

    spec_raw = OPENAPI_YAML_PATH.read_text(encoding="utf-8")
    assert "openapi: 3.0.3" in spec_raw
    assert "DISSID AI Job Search Code Harness" in spec_raw

    spec = json.loads(OPENAPI_JSON_PATH.read_text(encoding="utf-8"))
    assert spec.get("openapi") == "3.0.3"
    assert "paths" in spec
    assert "/" in spec["paths"]
    assert "/api/state" in spec["paths"]
    assert "/api/telemetry" in spec["paths"]
    assert "/api/action" in spec["paths"]


def test_get_dashboard_html_and_dual_inbox_pill() -> None:
    """Verifies GET / returns HTTP 200 with both work@example.com and jane.doe@example.com declared."""
    status, html_content = _http_request("/")
    assert status == 200
    assert isinstance(html_content, str)
    assert "Job Search Dashboard" in html_content
    # Assert dual-inbox presence in the UI
    assert "work@example.com" in html_content
    assert "jane.doe@example.com" in html_content
    assert "DWD &amp; Personal Inboxes Connected" in html_content


def test_get_api_state_counts() -> None:
    """Verifies GET /api/state returns HTTP 200 with non-zero submitted applications."""
    status, state = _http_request("/api/state")
    assert status == 200
    assert isinstance(state, dict)
    assert state.get("ok") is True
    assert state.get("sent_count") >= 200
    assert state.get("total_rows") >= 204

    bc = state.get("bucket_counts", {})
    assert bc.get("Submitted", 0) >= 200


def test_get_api_telemetry() -> None:
    """Verifies GET /api/telemetry returns HTTP 200 and an events list."""
    status, telem = _http_request("/api/telemetry")
    assert status == 200
    assert isinstance(telem, dict)
    assert telem.get("ok") is True
    assert "recent_events" in telem or "events" in telem


def test_post_action_gmail_sync_dual_inbox() -> None:
    """Verifies POST /api/action with action=gmail_sync scans both inboxes."""
    status, resp = _http_request("/api/action", method="POST", payload={"action": "gmail_sync"})
    assert status == 200
    assert isinstance(resp, dict)
    assert resp.get("ok") is True
    assert resp.get("action") == "gmail_sync"
    raw_output = resp.get("output", "")
    assert "Gmail Sync" in raw_output
    # Assert dual rail status reported in raw output
    assert "work_dwd" in raw_output
    assert "work@example.com" in raw_output
    assert "jane.doe@example.com" in raw_output
    assert "personal_mail" in raw_output or "personal_himalaya" in raw_output


def test_post_action_batch_apply_all() -> None:
    """Verifies POST /api/action with action=batch_apply_all executes idempotently."""
    status, resp = _http_request("/api/action", method="POST", payload={"action": "batch_apply_all"})
    assert status == 200
    assert isinstance(resp, dict)
    assert resp.get("ok") is True
    assert resp.get("action") == "batch_apply_all"
    assert "skipped_duplicates_count" in resp
    assert resp.get("failed_count") == 0


def test_post_action_run_pipeline() -> None:
    """Verifies POST /api/action with action=run_pipeline runs against Steam Deck llama.cpp."""
    status, resp = _http_request("/api/action", method="POST", payload={"action": "run_pipeline", "limit": 1})
    assert status == 200
    assert isinstance(resp, dict)
    assert resp.get("ok") is True
    assert resp.get("action") == "run_pipeline"

    summary = resp.get("summary", {})
    assert summary.get("ok") is True
    llamacpp = summary.get("llamacpp", {})
    assert llamacpp.get("active_endpoint") == "http://127.0.0.1:11435/v1"
    assert llamacpp.get("cloud_api_calls") == 0


def test_post_action_negative_unsupported() -> None:
    """Verifies POST /api/action with an unsupported action returns HTTP 400 Bad Request."""
    status, resp = _http_request("/api/action", method="POST", payload={"action": "unsupported_unknown_xyz"})
    assert status == 400
    assert isinstance(resp, dict)
    assert resp.get("ok") is False
    assert "Unsupported action" in resp.get("error", "")
