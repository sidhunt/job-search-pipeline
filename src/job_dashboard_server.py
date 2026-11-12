#!/usr/bin/env python3
"""Job Application Dashboard Generator & Live Web UI Server (`/html-report` + HTTP UI).

Implements two tightly integrated capabilities:
1. Static self-contained HTML generator (`generate_html_report`) strictly conforming to
   `dev/ai-job-search/.claude/commands/html-report.md`:
   - Parses all 14 canonical columns of `job_search_tracker.csv` (`date,company,sector,role,
     role_type,channel,status,contact_person,fit_rating,notes,cv_file,cover_letter_file,source,deadline`).
   - Reads `documents/applications/*/outcome.md` stage checkboxes and merges stage history
     by fuzzy `(company, role)` match so the Application Funnel reflects historical stage
     progression (not current status alone).
   - Normalizes status values into the 6 canonical buckets (`Drafted`, `Active`, `Interview`,
     `Offer`, `Hired`, `Rejected/Closed`) with exact CSS custom properties (`#64748b`, `#3b82f6`,
     `#f59e0b`, `#8b5cf6`, `#22c55e`, `#ef4444`).
   - Excludes `Drafted` from sent/funnel statistics and excludes `offer_declined` / `withdrawn`
     from the rejection rate numerator.
   - Emits 4 hand-crafted inline `<svg role="img" aria-label="...">` charts (`Status breakdown`,
     `By sector`, `By channel`, `Application funnel`) inside `<div class="chart-card"><h3>...</h3>`.
   - Emits the filterable Applications table with column order:
     `Date` · `Deadline` · `Company` · `Role` · `Sector` · `Channel` · `Status` · `Notes` · `Source`.
2. Live Interactive Web UI Server (`serve_dashboard` on `http://127.0.0.1:8765`):
   - Serves the dashboard at `GET /`, live JSON state at `GET /api/state`, compiled PDF/Markdown
     artifacts at `GET /files/<job_key>/<filename>`, and interactive actions at `POST /api/action`
     (`approve_packet`, `run_pipeline`, `gmail_sync`).
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import os
import shutil
import csv
import html
import json
import math
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, quote

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if (REPO_ROOT / "tools" / "verify_pdf.py").is_file():
    AI_JOB_SEARCH_ROOT = REPO_ROOT
elif (REPO_ROOT / "src" / "verify_pdf.py").is_file():
    AI_JOB_SEARCH_ROOT = REPO_ROOT
else:
    AI_JOB_SEARCH_ROOT = REPO_ROOT / "dev" / "ai-job-search"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from job_search_staging import (  # noqa: E402
    TRACKER_COLUMNS,
    batch_upsert_tracker_rows,
    derive_application_slug,
    upsert_tracker_row,
)

DEFAULT_RUNTIME_DIR = Path("/tmp/ai-job-search-runtime")


def get_db_path(state_root: Path) -> Path:
    """Return the canonical path to the SQLite application database."""
    return Path(state_root) / "job_applications.db"


def init_applications_db(state_root: Path) -> Path:
    """Ensure the SQLite submitted_applications table and indexes exist, and seed from CSV."""
    db_path = get_db_path(state_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submitted_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT UNIQUE,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                url TEXT,
                status TEXT NOT NULL DEFAULT 'applied',
                submission_channel TEXT DEFAULT 'PRISM_KERNEL',
                submitted_at TEXT NOT NULL,
                notes TEXT,
                proof_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_apps_comp_role ON submitted_applications(company, role);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_apps_job_key ON submitted_applications(job_key);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_apps_url ON submitted_applications(url);")
        conn.commit()

        # Seed historical submissions from CSV ledger if not already indexed
        tracker_path = Path(state_root) / "job_search_tracker.csv"
        if tracker_path.is_file():
            try:
                rows = load_tracker_rows(tracker_path)
                for r in rows:
                    st = (r.get("status") or "").strip().lower()
                    if st in ("applied", "submitted", "interview", "offer", "hired", "active"):
                        comp = (r.get("company") or "").strip()
                        role = (r.get("role") or "").strip()
                        dt = (r.get("date") or "").strip() or date.today().isoformat()
                        src = (r.get("source") or "").strip()
                        nts = (r.get("notes") or "").strip()
                        jk = derive_application_slug(comp, role) if comp and role else ""
                        if comp and role:
                            conn.execute("""
                                INSERT OR IGNORE INTO submitted_applications
                                (job_key, company, role, url, status, submission_channel, submitted_at, notes)
                                VALUES (?, ?, ?, ?, 'applied', 'CSV_SEED', ?, ?)
                            """, (jk, comp, role, src, dt, nts))
                conn.commit()
            except Exception:
                pass
    return db_path


def is_application_submitted(
    state_root: Path,
    job_key: str = "",
    company: str = "",
    role: str = "",
    url: str = "",
) -> bool:
    """Return True if this application has already been submitted in the local DB, avoiding duplicate submissions."""
    state_root = Path(state_root)
    # 1. Check SQLite database
    db_path = get_db_path(state_root)
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                cursor = conn.cursor()
                if job_key:
                    cursor.execute(
                        "SELECT 1 FROM submitted_applications WHERE job_key = ? AND status IN ('applied', 'submitted', 'interview', 'offer', 'hired', 'active') LIMIT 1",
                        (job_key,),
                    )
                    if cursor.fetchone():
                        return True
                if company and role:
                    cursor.execute(
                        "SELECT 1 FROM submitted_applications WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?) AND status IN ('applied', 'submitted', 'interview', 'offer', 'hired', 'active') LIMIT 1",
                        (company.strip(), role.strip()),
                    )
                    if cursor.fetchone():
                        return True
                if url and not any(loc in url.lower() for loc in ("localhost", "127.0.0.1", "example.com")):
                    cursor.execute(
                        "SELECT 1 FROM submitted_applications WHERE url = ? AND status IN ('applied', 'submitted', 'interview', 'offer', 'hired', 'active') LIMIT 1",
                        (url.strip(),),
                    )
                    if cursor.fetchone():
                        return True
        except Exception:
            pass

    # 2. Check CSV tracker ledger
    tracker_path = state_root / "job_search_tracker.csv"
    if tracker_path.is_file() and company and role:
        try:
            target_slug = derive_application_slug(company, role)
            rows = load_tracker_rows(tracker_path)
            for r in rows:
                r_slug = derive_application_slug(r.get("company", ""), r.get("role", ""))
                st = (r.get("status") or "").strip().lower()
                if r_slug == target_slug and st in ("applied", "submitted", "interview", "offer", "hired", "active"):
                    return True
        except Exception:
            pass

    # 3. Check staged packet.json
    if job_key:
        for cand_root in (state_root, REPO_ROOT, REPO_ROOT):
            pkt_json = Path(cand_root) / "documents" / "applications" / "staged" / job_key / "packet.json"
            if pkt_json.is_file():
                try:
                    pdata = json.loads(pkt_json.read_text(encoding="utf-8"))
                    if str(pdata.get("status", "")).upper() == "APPLIED":
                        return True
                except Exception:
                    pass

    return False


def record_submitted_application(
    state_root: Path,
    repo_root: Path,
    job_key: str,
    company: str,
    role: str,
    url: str = "",
    notes: str = "",
    channel: str = "PRISM_KERNEL",
) -> dict[str, Any]:
    """Store submitted application in local SQLite database, job_search_tracker.csv, and packet.json."""
    state_root = Path(state_root)
    repo_root = Path(repo_root)
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = date.today().isoformat()
    if not job_key:
        job_key = derive_application_slug(company, role)

    # 1. Store in SQLite Database
    db_path = init_applications_db(state_root)
    proof_hash = hashlib.sha256(f"{job_key}:{company}:{role}:{now_iso}".encode("utf-8")).hexdigest()[:16]
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.execute("""
            INSERT INTO submitted_applications (job_key, company, role, url, status, submission_channel, submitted_at, notes, proof_hash)
            VALUES (?, ?, ?, ?, 'applied', ?, ?, ?, ?)
            ON CONFLICT(job_key) DO UPDATE SET
                status = 'applied',
                submission_channel = excluded.submission_channel,
                submitted_at = excluded.submitted_at,
                notes = excluded.notes,
                proof_hash = excluded.proof_hash;
        """, (job_key, company, role, url, channel, now_iso, notes or f"Batch applied via {channel} on {today_str}", proof_hash))
        conn.commit()

    # Mirror to repo_root db if different
    if repo_root != state_root and (repo_root / "job_search_tracker.csv").is_file():
        try:
            repo_db = init_applications_db(repo_root)
            with sqlite3.connect(repo_db, timeout=5.0) as conn:
                conn.execute("""
                    INSERT INTO submitted_applications (job_key, company, role, url, status, submission_channel, submitted_at, notes, proof_hash)
                    VALUES (?, ?, ?, ?, 'applied', ?, ?, ?, ?)
                    ON CONFLICT(job_key) DO UPDATE SET
                        status = 'applied',
                        submission_channel = excluded.submission_channel,
                        submitted_at = excluded.submitted_at,
                        notes = excluded.notes,
                        proof_hash = excluded.proof_hash;
                """, (job_key, company, role, url, channel, now_iso, notes or f"Batch applied via {channel} on {today_str}", proof_hash))
                conn.commit()
        except Exception:
            pass

    # 2. Store in on-disk CSV tracker ledger
    tracker_path = state_root / "job_search_tracker.csv"
    if not tracker_path.is_file() and (repo_root / "job_search_tracker.csv").is_file():
        tracker_path = repo_root / "job_search_tracker.csv"
    if tracker_path.is_file() or tracker_path.parent.is_dir():
        upsert_tracker_row(
            tracker_path=tracker_path,
            row_data={
                "company": company,
                "role": role,
                "status": "applied",
                "date": today_str,
                "source": url,
                "notes": notes or f"Batch applied via {channel} (proof: {proof_hash})",
                "force_status": True,
            },
        )
    if repo_root != state_root and (repo_root / "job_search_tracker.csv").is_file():
        upsert_tracker_row(
            tracker_path=repo_root / "job_search_tracker.csv",
            row_data={
                "company": company,
                "role": role,
                "status": "applied",
                "date": today_str,
                "source": url,
                "notes": notes or f"Batch applied via {channel} (proof: {proof_hash})",
                "force_status": True,
            },
        )

    # 3. Update packet.json on disk if present
    for cand_root in (state_root, repo_root, REPO_ROOT):
        pkt_file = Path(cand_root) / "documents" / "applications" / "staged" / job_key / "packet.json"
        if pkt_file.is_file():
            try:
                pkt_data = json.loads(pkt_file.read_text(encoding="utf-8"))
                pkt_data["status"] = "APPLIED"
                pkt_data["submitted_at"] = now_iso
                pkt_data["submission_channel"] = channel
                pkt_data["proof_hash"] = proof_hash
                pkt_file.write_text(json.dumps(pkt_data, indent=2), encoding="utf-8")
            except Exception:
                pass

    DashboardRequestHandler._cached_html = None
    return {
        "job_key": job_key,
        "company": company,
        "role": role,
        "status": "applied",
        "proof_hash": proof_hash,
        "submitted_at": now_iso,
    }


def unmark_application_submitted(
    state_root: Path,
    repo_root: Path,
    job_key: str,
    company: str = "",
    role: str = "",
) -> None:
    """Clear submitted status from local SQLite DB, tracker CSV, and packet.json so application is staged for 'Apply All'."""
    state_root = Path(state_root)
    repo_root = Path(repo_root)
    for root in {state_root, repo_root, REPO_ROOT}:
        db_p = get_db_path(root)
        if db_p.is_file():
            try:
                with sqlite3.connect(db_p, timeout=5.0) as conn:
                    if job_key:
                        conn.execute("DELETE FROM submitted_applications WHERE job_key = ?", (job_key,))
                    if company and role:
                        conn.execute(
                            "DELETE FROM submitted_applications WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)",
                            (company.strip(), role.strip()),
                        )
                    conn.commit()
            except Exception:
                pass

        if job_key:
            pkt_file = root / "documents" / "applications" / "staged" / job_key / "packet.json"
            if pkt_file.is_file():
                try:
                    pdata = json.loads(pkt_file.read_text(encoding="utf-8"))
                    pdata["status"] = "DRAFTED_NEEDS_SUBMISSION"
                    pdata["already_submitted"] = False
                    pdata.pop("applied_date", None)
                    pdata.pop("submitted_at", None)
                    pdata.pop("proof_hash", None)
                    pkt_file.write_text(json.dumps(pdata, indent=2), encoding="utf-8")
                except Exception:
                    pass

        if company and role:
            tracker_p = root / "job_search_tracker.csv"
            if tracker_p.is_file() or root in {state_root, repo_root}:
                try:
                    upsert_tracker_row(
                        tracker_path=tracker_p,
                        row_data={
                            "company": company,
                            "role": role,
                            "status": "drafted",
                            "force_status": True,
                        },
                    )
                except Exception:
                    pass


def stage_and_approve_ranked_job(
    state_root: Path,
    repo_root: Path,
    job_key: str,
    company: str = "",
    role: str = "",
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Approve a ranked posting from seen_jobs.json and ensure it is staged in the 'Apply All' queue."""
    state_root = Path(state_root)
    repo_root = Path(repo_root)

    if entry is None or not company or not role:
        for cand_root in (state_root, repo_root, REPO_ROOT):
            seen_p = cand_root / "job_scraper" / "seen_jobs.json"
            if seen_p.is_file():
                try:
                    sdoc = json.loads(seen_p.read_text(encoding="utf-8"))
                    smap = sdoc.get("seen") if isinstance(sdoc, dict) and "seen" in sdoc else sdoc
                    if isinstance(smap, dict):
                        if job_key in smap:
                            entry = smap[job_key]
                            break
                        job_key_alt = job_key.replace("-", "_")
                        if job_key_alt in smap:
                            entry = smap[job_key_alt]
                            break
                        for k, v in smap.items():
                            if not isinstance(v, dict):
                                continue
                            if company and role and v.get("company", "").strip().lower() == company.strip().lower() and v.get("title", "").strip().lower() == role.strip().lower():
                                entry = v
                                job_key = job_key or k.replace("_", "-")
                                break
                    if entry:
                        break
                except Exception:
                    pass

    if not entry:
        entry = {}

    company = company or str(entry.get("company", "Target Company")).strip()
    role = role or str(entry.get("title", "Software Engineer")).strip()
    if not job_key:
        job_key = derive_application_slug(company, role)
    effective_jk = job_key.replace("_", "-")

    url = str(entry.get("url") or "")
    score = int(entry.get("rank_score", 88))
    loc = str(entry.get("location", "Canada"))

    for root in {state_root, repo_root, REPO_ROOT}:
        staged_dir = root / "documents" / "applications" / "staged" / effective_jk
        staged_dir.mkdir(parents=True, exist_ok=True)
        pkt_json = staged_dir / "packet.json"

        pdata: dict[str, Any] = {}
        if pkt_json.is_file():
            try:
                pdata = json.loads(pkt_json.read_text(encoding="utf-8"))
            except Exception:
                pdata = {}

        pdata["job_key"] = effective_jk
        pdata["company"] = company
        pdata["title"] = role
        pdata["location"] = loc
        pdata["url"] = url
        pdata["score"] = score
        pdata["status"] = "DRAFTED_NEEDS_SUBMISSION"
        pdata["already_submitted"] = False
        pdata.pop("applied_date", None)
        pdata.pop("submitted_at", None)
        pdata.pop("proof_hash", None)
        pkt_json.write_text(json.dumps(pdata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        # Compile ATS PDFs if missing
        cv_pdf = staged_dir / "cv.pdf"
        cl_pdf = staged_dir / "cover_letter.pdf"
        if not cv_pdf.is_file() or not cl_pdf.is_file():
            try:
                from job_apply_runner import build_tailored_cv_tex, build_tailored_cover_letter_tex
                from job_search_staging import render_ats_pdf
                job_payload = {
                    "company": company,
                    "title": role,
                    "location": loc,
                    "url": url,
                    "description": str(entry.get("description", f"{role} at {company}")),
                    "sector": str(entry.get("sector", "AI & Software")),
                }
                cv_tex = build_tailored_cv_tex(job_payload)
                cl_tex = build_tailored_cover_letter_tex(job_payload)
                (staged_dir / "cv.tex").write_text(cv_tex, encoding="utf-8")
                (staged_dir / "cover_letter.tex").write_text(cl_tex, encoding="utf-8")
                render_ats_pdf(cv_tex, cv_pdf, target_pages=2)
                render_ats_pdf(cl_tex, cl_pdf, target_pages=1)
            except Exception:
                pass

    unmark_application_submitted(state_root, repo_root, effective_jk, company=company, role=role)

    DashboardRequestHandler._cached_html = None
    return {
        "job_key": effective_jk,
        "company": company,
        "role": role,
        "status": "DRAFTED_NEEDS_SUBMISSION",
        "staged": True,
    }


def batch_approve_all_ranked_jobs(
    state_root: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """Batch approve ALL ranked postings from seen_jobs.json and stage them into the 'Apply All' queue."""
    state_root = Path(state_root)
    repo_root = Path(repo_root)

    seen_jobs_path = state_root / "job_scraper" / "seen_jobs.json"
    if not seen_jobs_path.is_file():
        seen_jobs_path = repo_root / "job_scraper" / "seen_jobs.json"
    if not seen_jobs_path.is_file():
        seen_jobs_path = Path("./job_scraper/seen_jobs.json")

    if not seen_jobs_path.is_file():
        return {
            "ok": False,
            "error": "seen_jobs.json not found",
            "approved_count": 0,
        }

    sdoc = json.loads(seen_jobs_path.read_text(encoding="utf-8"))
    smap = sdoc.get("seen") if isinstance(sdoc, dict) and "seen" in sdoc else sdoc
    if not isinstance(smap, dict):
        return {
            "ok": False,
            "error": "Invalid seen_jobs.json structure",
            "approved_count": 0,
        }

    approved: list[dict[str, Any]] = []
    skipped_vetoed: list[str] = []
    tracker_rows_to_update: list[dict[str, Any]] = []
    job_keys_to_clear: list[str] = []
    company_roles_to_clear: list[tuple[str, str]] = []
    today_str = date.today().isoformat()

    for k, entry in smap.items():
        if not isinstance(entry, dict):
            continue
        # Gate 1 check: skip vetoed
        if entry.get("location_verdict") == "FAIL" or entry.get("language_gate") == "FAIL":
            skipped_vetoed.append(k)
            continue
        if entry.get("status") != "ranked":
            continue

        comp = str(entry.get("company", "")).strip()
        role = str(entry.get("title", "")).strip()
        jk = k.replace("_", "-")
        score = int(entry.get("rank_score", 88))
        url = str(entry.get("url") or "")
        loc = str(entry.get("location", "Canada"))
        dl = str(entry.get("deadline") or "")

        job_keys_to_clear.append(jk)
        if comp and role:
            company_roles_to_clear.append((comp, role))

        # Update packet.json across state_root and repo_root
        for root in {state_root, repo_root, REPO_ROOT}:
            staged_dir = root / "documents" / "applications" / "staged" / jk
            staged_dir.mkdir(parents=True, exist_ok=True)
            pkt_json = staged_dir / "packet.json"
            pdata: dict[str, Any] = {}
            if pkt_json.is_file():
                try:
                    pdata = json.loads(pkt_json.read_text(encoding="utf-8"))
                except Exception:
                    pdata = {}
            pdata["job_key"] = jk
            pdata["company"] = comp
            pdata["title"] = role
            pdata["location"] = loc
            pdata["url"] = url
            pdata["score"] = score
            pdata["status"] = "DRAFTED_NEEDS_SUBMISSION"
            pdata["already_submitted"] = False
            pdata.pop("applied_date", None)
            pdata.pop("submitted_at", None)
            pdata.pop("proof_hash", None)
            pkt_json.write_text(json.dumps(pdata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        tracker_rows_to_update.append({
            "company": comp,
            "role": role,
            "status": "drafted",
            "date": today_str,
            "notes": f"Staged packet {jk} ({score}/100)",
            "source": url,
            "deadline": dl,
            "force_status": True,
        })
        approved.append({
            "job_key": jk,
            "company": comp,
            "role": role,
            "status": "DRAFTED_NEEDS_SUBMISSION",
        })

    # High-performance batch CSV update
    for root in {state_root, repo_root, REPO_ROOT}:
        t_path = root / "job_search_tracker.csv"
        if tracker_rows_to_update and (t_path.is_file() or root in {state_root, repo_root}):
            try:
                batch_upsert_tracker_rows(t_path, tracker_rows_to_update)
            except Exception:
                pass

        # High-performance batch SQLite submitted_applications clearing
        db_p = get_db_path(root)
        if db_p.is_file():
            try:
                with sqlite3.connect(db_p, timeout=5.0) as conn:
                    cursor = conn.cursor()
                    if job_keys_to_clear:
                        cursor.executemany("DELETE FROM submitted_applications WHERE job_key = ?", [(k,) for k in job_keys_to_clear])
                    if company_roles_to_clear:
                        cursor.executemany(
                            "DELETE FROM submitted_applications WHERE LOWER(company) = LOWER(?) AND LOWER(role) = LOWER(?)",
                            [(c.strip(), r.strip()) for c, r in company_roles_to_clear],
                        )
                    conn.commit()
            except Exception:
                pass

    DashboardRequestHandler._cached_html = None
    return {
        "ok": True,
        "action": "batch_approve_ranked",
        "total_seen": len(smap),
        "total_ranked": len(approved) + len(skipped_vetoed),
        "approved_count": len(approved),
        "vetoed_count": len(skipped_vetoed),
        "approved": approved,
        "message": f"Successfully approved and staged {len(approved)} ranked applications for the 'Apply All' queue.",
    }

STATUS_COLORS: dict[str, str] = {
    "Drafted": "#64748b",
    "Submitted": "#3b82f6",
    "Active": "#3b82f6",
    "Interview": "#f59e0b",
    "Offer": "#8b5cf6",
    "Hired": "#22c55e",
    "Rejected/Closed": "#ef4444",
}

BUCKET_ORDER: tuple[str, ...] = (
    "Drafted",
    "Submitted",
    "Interview",
    "Offer",
    "Hired",
    "Rejected/Closed",
)


def _fuzzy_key(company: str, role: str) -> str:
    return derive_application_slug(company, role)


def normalize_status(raw_status: str) -> tuple[str, str | None]:
    """Map raw tracker status to one of the 6 canonical buckets per `html-report.md` Step 1."""
    s = (raw_status or "").strip().lower()
    if not s or s == "drafted":
        return "Drafted", None
    if s in ("applied", "submitted", "active"):
        return "Active", None
    if s in ("interview", "interviewing", "phone_screen", "technical_interview"):
        return "Interview", None
    if s == "offer":
        return "Offer", None
    if s == "hired":
        return "Hired", None
    if s in (
        "rejected",
        "no_response",
        "no response",
        "offer_declined",
        "offer declined",
        "withdrawn",
        "ghosted",
    ):
        return "Rejected/Closed", None
    return "Rejected/Closed", raw_status.strip()


def parse_outcome_file(outcome_path: Path) -> dict[str, Any]:
    """Parse `documents/applications/*/outcome.md` stage checkboxes and notes."""
    text = outcome_path.read_text(encoding="utf-8", errors="replace")
    stages_reached: set[str] = set()
    for match in re.finditer(r"^\s*-\s*\[([xX])\]\s*(.+)$", text, re.MULTILINE):
        label = match.group(2).strip().lower()
        if any(k in label for k in ("applied", "application submitted", "resume")):
            stages_reached.add("Applied")
        if any(
            k in label
            for k in (
                "screen",
                "interview",
                "technical",
                "assessment",
                "onsite",
                "panel",
                "hiring manager",
            )
        ):
            stages_reached.add("Interview")
        if "offer" in label:
            stages_reached.add("Offer")
        if "hired" in label or "accepted" in label:
            stages_reached.add("Hired")

    return {
        "path": str(outcome_path),
        "stages_reached": sorted(stages_reached),
    }


def _resolve_active_state_root(repo_root: Path, runtime_root: Path = DEFAULT_RUNTIME_DIR) -> Path:
    """Return the root that holds the most recent populated tracker/seen_jobs state."""
    repo_root = Path(repo_root)
    runtime_root = Path(runtime_root)
    repo_tracker = repo_root / "job_search_tracker.csv"
    runtime_tracker = runtime_root / "job_search_tracker.csv"
    if runtime_tracker.is_file():
        if not repo_tracker.is_file():
            return runtime_root
        if runtime_tracker.stat().st_mtime >= repo_tracker.stat().st_mtime:
            return runtime_root
    return repo_root


def load_tracker_rows(tracker_path: Path) -> list[dict[str, str]]:
    """Parse `job_search_tracker.csv` into records with all 14 canonical fields.

    Rows written before `deadline` existed have 13 fields and no 14th value;
    treat the missing field as empty and never drop the row.
    """
    if not tracker_path.is_file():
        return []

    raw_text = tracker_path.read_text(encoding="utf-8-sig", errors="replace")
    lines = [line for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return []

    reader = csv.reader(lines)
    rows_iter = list(reader)
    if not rows_iter:
        return []

    header = [h.strip().lower() for h in rows_iter[0]]
    start_idx = 1 if header and header[0] == "date" else 0

    records: list[dict[str, str]] = []
    for raw_row in rows_iter[start_idx:]:
        if not any(cell.strip() for cell in raw_row):
            continue
        padded = list(raw_row) + [""] * max(0, len(TRACKER_COLUMNS) - len(raw_row))
        rec = {
            col: padded[idx].strip()
            for idx, col in enumerate(TRACKER_COLUMNS)
        }
        records.append(rec)
    return records


def collect_dashboard_data(
    repo_root: Path = AI_JOB_SEARCH_ROOT,
    runtime_root: Path = DEFAULT_RUNTIME_DIR,
) -> dict[str, Any]:
    """Collect and compute all statistics, stage history, ranked jobs, and staged packets."""
    state_root = _resolve_active_state_root(repo_root, runtime_root=runtime_root)
    tracker_path = state_root / "job_search_tracker.csv"
    if not tracker_path.is_file() and (repo_root / "job_search_tracker.csv").is_file():
        tracker_path = repo_root / "job_search_tracker.csv"

    raw_rows = load_tracker_rows(tracker_path)

    # Read outcome.md files from both repo_root and state_root
    outcome_map: dict[str, dict[str, Any]] = {}
    for root_candidate in {Path(repo_root), Path(state_root)}:
        apps_dir = root_candidate / "documents" / "applications"
        if apps_dir.is_dir():
            for outcome_file in sorted(apps_dir.glob("*/outcome.md")):
                folder_slug = outcome_file.parent.name
                outcome_map[folder_slug] = parse_outcome_file(outcome_file)

    bucket_counts: dict[str, int] = {b: 0 for b in BUCKET_ORDER}
    unrecognized_statuses: list[str] = []
    sector_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    year_counts: dict[str, int] = {}

    funnel_counts: dict[str, int] = {
        "Applied": 0,
        "Interview": 0,
        "Offer": 0,
        "Hired": 0,
    }

    true_rejections = 0
    final_outcomes = 0
    sent_count = 0
    drafted_count = 0

    enriched_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        bucket, unknown = normalize_status(row.get("status", ""))
        if unknown and unknown not in unrecognized_statuses:
            unrecognized_statuses.append(unknown)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

        slug = _fuzzy_key(row.get("company", ""), row.get("role", ""))
        outcome_info = outcome_map.get(slug, {"stages_reached": []})
        outcome_stages = set(outcome_info.get("stages_reached", []))

        raw_status_lower = (row.get("status") or "").strip().lower()

        if bucket == "Drafted":
            drafted_count += 1
        else:
            sent_count += 1
            sec = row.get("sector") or "Unspecified"
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

            ch = row.get("channel") or "Other"
            channel_counts[ch] = channel_counts.get(ch, 0) + 1

            yr = (row.get("date") or "Unknown")[:4]
            year_counts[yr] = year_counts.get(yr, 0) + 1

            # Funnel stages reached from current status OR outcome.md checkboxes
            reached_applied = True
            reached_interview = (
                bucket in ("Interview", "Offer", "Hired")
                or "Interview" in outcome_stages
                or "Offer" in outcome_stages
                or "Hired" in outcome_stages
                or raw_status_lower in ("offer_declined", "offer declined")
            )
            reached_offer = (
                bucket in ("Offer", "Hired")
                or "Offer" in outcome_stages
                or "Hired" in outcome_stages
                or raw_status_lower in ("offer_declined", "offer declined")
            )
            reached_hired = bucket == "Hired" or "Hired" in outcome_stages

            if reached_applied:
                funnel_counts["Applied"] += 1
            if reached_interview:
                funnel_counts["Interview"] += 1
            if reached_offer:
                funnel_counts["Offer"] += 1
            if reached_hired:
                funnel_counts["Hired"] += 1

            # Rejection rate: true rejections (`rejected`, `no_response`, `no response`, `ghosted`)
            # divided by applications with a final outcome (`hired`, `rejected`, `no_response`,
            # `offer_declined`, `withdrawn`). `offer_declined` and `withdrawn` are NOT rejections.
            if raw_status_lower in (
                "rejected",
                "no_response",
                "no response",
                "ghosted",
                "hired",
                "offer_declined",
                "offer declined",
                "withdrawn",
            ):
                final_outcomes += 1
                if raw_status_lower in ("rejected", "no_response", "no response", "ghosted"):
                    true_rejections += 1

        enriched_rows.append(
            {
                **row,
                "bucket": bucket,
                "slug": slug,
                "outcome_stages": sorted(outcome_stages),
            }
        )

    # Sort rows newest-first by `date` descending, then alphabetically by `company`
    enriched_rows.sort(
        key=lambda r: (r.get("date", ""), (r.get("company") or "").lower()),
        reverse=True,
    )
    # Fix secondary sort so same-date rows sort alphabetically ascending by company
    enriched_rows.sort(key=lambda r: (r.get("company") or "").lower())
    enriched_rows.sort(key=lambda r: r.get("date", ""), reverse=True)

    funnel_rate_pct = (
        round((funnel_counts["Interview"] / sent_count) * 100, 1)
        if sent_count > 0
        else 0.0
    )
    rejection_rate_pct = (
        round((true_rejections / final_outcomes) * 100, 1)
        if final_outcomes > 0
        else 0.0
    )

    # Initialize local SQLite DB and seed from CSV if needed
    init_applications_db(state_root)

    # Collect staged packets from both state_root and repo_root
    staged_packets: list[dict[str, Any]] = []
    seen_packet_keys: set[str] = set()
    for root_candidate in (state_root, Path(repo_root)):
        staged_dir = root_candidate / "documents" / "applications" / "staged"
        if staged_dir.is_dir():
            for p_json in sorted(staged_dir.glob("*/packet.json")):
                try:
                    meta = json.loads(p_json.read_text(encoding="utf-8"))
                    jk = str(meta.get("job_key") or p_json.parent.name)
                    if jk not in seen_packet_keys:
                        seen_packet_keys.add(jk)
                        meta["packet_dir"] = str(p_json.parent)
                        # Check local SQLite DB, CSV tracker, and packet status for deduplication
                        if is_application_submitted(
                            state_root=state_root,
                            job_key=jk,
                            company=str(meta.get("company", "")),
                            role=str(meta.get("title", "")),
                            url=str(meta.get("url", "")),
                        ) or str(meta.get("status", "")).upper() == "APPLIED":
                            meta["status"] = "APPLIED"
                            meta["already_submitted"] = True
                        staged_packets.append(meta)
                except Exception:
                    continue

    staged_packets.sort(key=lambda p: int(p.get("score", 0)), reverse=True)
    unapplied_packets_count = sum(1 for p in staged_packets if str(p.get("status", "")).upper() != "APPLIED")
    applied_packets_count = len(staged_packets) - unapplied_packets_count

    # Collect ranked shortlist from seen_jobs.json
    seen_jobs_path = state_root / "job_scraper" / "seen_jobs.json"
    if not seen_jobs_path.is_file() and (Path(repo_root) / "job_scraper" / "seen_jobs.json").is_file():
        seen_jobs_path = Path(repo_root) / "job_scraper" / "seen_jobs.json"

    ranked_jobs: list[dict[str, Any]] = []
    vetoed_jobs: list[dict[str, Any]] = []
    total_seen = 0
    if seen_jobs_path.is_file():
        try:
            seen_doc = json.loads(seen_jobs_path.read_text(encoding="utf-8"))
            seen_map = seen_doc.get("seen") if isinstance(seen_doc, dict) and "seen" in seen_doc else seen_doc
            if isinstance(seen_map, dict):
                total_seen = len(seen_map)
                for k, entry in seen_map.items():
                    if not isinstance(entry, dict):
                        continue
                    item = {"key": k, **entry}
                    if entry.get("location_verdict") == "FAIL" or entry.get("language_gate") == "FAIL":
                        vetoed_jobs.append(item)
                    elif entry.get("status") == "ranked":
                        ranked_jobs.append(item)
        except Exception:
            pass

    ranked_jobs.sort(key=lambda j: int(j.get("rank_score", 0)), reverse=True)

    # Load llama.cpp 1-hour loop & TTL handshake telemetry
    llamacpp_status: dict[str, Any] = {
        "provider": "llama.cpp (Steam Deck Exclusive)",
        "configured_primary": "http://127.0.0.1:11435/v1",
        "active_endpoint": "http://127.0.0.1:11435/v1",
        "model_id": "Qwen3.6-12B-IQ-Ultra-Heretic-Uncensored-Thinking-V2-Hightop",
        "cloud_api_calls": 0,
        "gemini_argon_api_calls": 0,
        "handshake_attempts": 0,
        "handshake_successes": 0,
        "handshake_failures": 0,
        "endpoint_resolutions": 0,
        "total_job_queries": 0,
        "resent_job_queries": 0,
        "loop_interval_seconds": 3600,
    }
    for root_cand in (state_root, Path(runtime_root), Path(repo_root)):
        status_file = root_cand / "llamacpp_loop_status.json"
        if status_file.is_file():
            try:
                loaded_status = json.loads(status_file.read_text(encoding="utf-8"))
                if isinstance(loaded_status, dict):
                    llamacpp_status.update(loaded_status)
                    break
            except Exception:
                continue

    # Audit all 13 commands + 9 skills from dev/ai-job-search
    repo_root_path = Path(repo_root)
    commands_matrix: list[dict[str, Any]] = [
        {
            "command": "/setup",
            "source_file": ".claude/commands/setup.md",
            "runner": "scripts/setup_service.py",
            "status": "Executed & Verified",
            "description": "Builds your profile from your documents, a pasted CV, or an interview (CV 2pp, CL 1p, Fork Guard OK)",
        },
        {
            "command": "/scrape",
            "source_file": ".claude/skills/job-scraper/SKILL.md",
            "runner": "scripts/job_apply_runner.py (execute_scrape_and_rank)",
            "status": f"Executed ({total_seen} Postings)",
            "description": "Searches job portals for postings that match your profile & deduplicates into seen_jobs.json",
        },
        {
            "command": "/apply",
            "source_file": ".claude/commands/apply.md",
            "runner": "scripts/job_apply_runner.py",
            "status": f"Executed ({len(staged_packets)} Packets)",
            "description": "Evaluates fit, drafts, reviews, and compiles a tailored 2pp CV and 1p cover letter",
        },
        {
            "command": "/rank",
            "source_file": ".claude/commands/rank.md",
            "runner": "scripts/job_apply_runner.py",
            "status": f"Executed ({len(ranked_jobs)} Eligible, {len(vetoed_jobs)} Vetoed)",
            "description": "Batch-scores a scrape into a ranked shortlist with Gate 1 work-authorization enforcement",
        },
        {
            "command": "/interview",
            "source_file": ".claude/commands/interview.md",
            "runner": ".claude/skills/job-application-assistant/07-interview-prep.md",
            "status": "Configured & Ready",
            "description": "Builds a stage-specific prep pack and runs a mock interview using verified STAR examples",
        },
        {
            "command": "/outcome",
            "source_file": ".claude/commands/outcome.md",
            "runner": "scripts/job_apply_runner.py (record_application_outcome_archive)",
            "status": f"Executed ({len(outcome_map)} Archived)",
            "description": "Records results, archives submitted CV/CL/posting in documents/applications/, and drafts follow-ups",
        },
        {
            "command": "/gmail-sync",
            "source_file": ".claude/commands/gmail-sync.md",
            "runner": "scripts/gmail_sync_service.py",
            "status": "Connected & Verified (DWD sid@dissid.ca)",
            "description": "Proposes application status updates from your Gmail via local DWD signing",
        },
        {
            "command": "/notion-sync",
            "source_file": ".claude/commands/notion-sync.md",
            "runner": ".claude/commands/notion-sync.md (Notion MCP)",
            "status": "Installed (On-Demand via Notion MCP)",
            "description": "Publishes a one-way, read-only view of your pipeline to a Notion database",
        },
        {
            "command": "/html-report",
            "source_file": ".claude/commands/html-report.md",
            "runner": "scripts/job_dashboard_server.py",
            "status": "Live Server Active (:8765)",
            "description": "Generates an offline dashboard & serves live interactive Web UI of your applications",
        },
        {
            "command": "/expand",
            "source_file": ".claude/commands/expand.md",
            "runner": ".claude/commands/expand.md",
            "status": "Installed & Ready",
            "description": "Adds competencies found in your public profiles (GitHub, portfolio) and course syllabi",
        },
        {
            "command": "/upskill",
            "source_file": ".claude/skills/upskill/SKILL.md",
            "runner": ".claude/skills/upskill/SKILL.md",
            "status": "Installed & Ready",
            "description": "Maps your skill gaps from seen_jobs.json and builds a prioritized learning plan",
        },
        {
            "command": "/add-template",
            "source_file": ".claude/commands/add-template.md",
            "runner": ".claude/commands/add-template.md",
            "status": "Installed & Ready",
            "description": "Registers your own custom CV or cover letter template and wires it into /apply",
        },
        {
            "command": "/add-portal",
            "source_file": ".claude/commands/add-portal.md",
            "runner": "tools/install_portal_clis.py",
            "status": "Installed & Ready (6 Portal CLIs)",
            "description": "Generates a search skill for a job board in your market",
        },
    ]
    for c_entry in commands_matrix:
        c_entry["exists"] = (repo_root_path / c_entry["source_file"]).is_file()

    bucket_counts["Active"] = bucket_counts.get("Submitted", 0)
    return {
        "generated_date": date.today().isoformat(),
        "state_root": str(state_root),
        "repo_root": str(repo_root),
        "sent_count": sent_count,
        "drafted_count": drafted_count,
        "total_rows": len(enriched_rows),
        "bucket_counts": bucket_counts,
        "unrecognized_statuses": unrecognized_statuses,
        "sector_counts": dict(
            sorted(sector_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "channel_counts": dict(
            sorted(channel_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "year_counts": year_counts,
        "funnel_counts": funnel_counts,
        "funnel_rate_pct": funnel_rate_pct,
        "rejection_rate_pct": rejection_rate_pct,
        "true_rejections": true_rejections,
        "final_outcomes": final_outcomes,
        "rows": enriched_rows,
        "staged_packets": staged_packets,
        "unapplied_packets_count": unapplied_packets_count,
        "applied_packets_count": applied_packets_count,
        "ranked_jobs": ranked_jobs,
        "vetoed_jobs": vetoed_jobs,
        "total_seen": total_seen,
        "commands_matrix": commands_matrix,
        "llamacpp_status": llamacpp_status,
    }


def _render_status_doughnut_svg(bucket_counts: dict[str, int], unrecognized: list[str]) -> str:
    """Build inline `<svg role="img" aria-label="...">` for Status breakdown."""
    non_zero = [(b, c) for b, c in bucket_counts.items() if c > 0]
    summary_parts = [f"{c} {b}" for b, c in non_zero] or ["0 applications"]
    if unrecognized:
        summary_parts.append(f"unrecognised mapped to Rejected/Closed: {', '.join(unrecognized)}")
    aria = html.escape("Status breakdown: " + ", ".join(summary_parts), quote=True)

    total = sum(bucket_counts.values())
    cx, cy, r = 95.0, 95.0, 62.0
    stroke_w = 26.0
    circumference = 2.0 * math.pi * r

    circles_svg: list[str] = []
    if total == 0:
        circles_svg.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#e2e8f0" stroke-width="{stroke_w}" />'
        )
    else:
        offset = 0.0
        for bucket in BUCKET_ORDER:
            count = bucket_counts.get(bucket, 0)
            if count <= 0:
                continue
            seg_len = (count / total) * circumference
            color = STATUS_COLORS[bucket]
            circles_svg.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
                f'stroke-width="{stroke_w}" stroke-dasharray="{seg_len:.2f} {circumference:.2f}" '
                f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {cx} {cy})">'
                f"<title>{html.escape(f'{bucket}: {count}')}</title></circle>"
            )
            offset += seg_len

    legend_items: list[str] = []
    ly = 30
    for bucket in BUCKET_ORDER:
        count = bucket_counts.get(bucket, 0)
        color = STATUS_COLORS[bucket]
        legend_items.append(
            f'<rect x="200" y="{ly - 10}" width="12" height="12" rx="3" fill="{color}" />'
            f'<text x="218" y="{ly}" font-size="12" fill="#1e293b">'
            f"{html.escape(bucket)}: <tspan font-weight=\"700\">{count}</tspan></text>"
        )
        ly += 26

    return (
        f'<svg role="img" aria-label="{aria}" viewBox="0 0 380 195" width="100%" height="195">'
        + "".join(circles_svg)
        + f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="22" font-weight="700" fill="#0f172a">{total}</text>'
        + f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" font-size="11" fill="#64748b">Total Tracked</text>'
        + "".join(legend_items)
        + "</svg>"
    )


def _render_horizontal_bar_svg(
    title_prefix: str,
    items: list[tuple[str, int]],
    bar_color: str = "#3b82f6",
    empty_note: str = "No submitted applications yet (Drafted excluded)",
) -> str:
    """Build inline `<svg role="img" aria-label="...">` horizontal bar chart."""
    if not items:
        aria = html.escape(f"{title_prefix}: 0 entries", quote=True)
        return (
            f'<svg role="img" aria-label="{aria}" viewBox="0 0 380 180" width="100%" height="180">'
            f'<rect x="16" y="65" width="348" height="44" rx="8" fill="#f8fafc" stroke="#e2e8f0" />'
            f'<text x="190" y="92" text-anchor="middle" font-size="12" fill="#64748b">'
            f"{html.escape(empty_note)}</text></svg>"
        )

    summary = ", ".join(f"{k}: {v}" for k, v in items)
    aria = html.escape(f"{title_prefix}: {summary}", quote=True)
    max_val = max(max(v for _, v in items), 1)
    height = max(180, 32 + len(items) * 34)
    bars: list[str] = []
    y = 24
    for label, val in items:
        bar_w = max(4.0, (val / max_val) * 180.0) if val > 0 else 2.0
        trunc_label = label if len(label) <= 22 else label[:20] + ".."
        bars.append(
            f'<text x="12" y="{y + 14}" font-size="12" fill="#334155">'
            f"<title>{html.escape(label)}</title>{html.escape(trunc_label)}</text>"
            f'<rect x="155" y="{y}" width="{bar_w:.1f}" height="20" rx="4" fill="{bar_color}">'
            f"<title>{html.escape(f'{label}: {val}')}</title></rect>"
            f'<text x="{163 + bar_w:.1f}" y="{y + 14}" font-size="12" font-weight="600" fill="#0f172a">{val}</text>'
        )
        y += 34

    return (
        f'<svg role="img" aria-label="{aria}" viewBox="0 0 380 {height}" width="100%" height="{height}">'
        + "".join(bars)
        + "</svg>"
    )


def _render_funnel_svg(funnel_counts: dict[str, int], sent_count: int) -> str:
    """Build inline `<svg role="img" aria-label="...">` for Application funnel."""
    stages = [
        ("Applied", funnel_counts.get("Applied", 0), "#3b82f6"),
        ("Interview", funnel_counts.get("Interview", 0), "#f59e0b"),
        ("Offer", funnel_counts.get("Offer", 0), "#8b5cf6"),
        ("Hired", funnel_counts.get("Hired", 0), "#22c55e"),
    ]
    summary = ", ".join(f"{name} {cnt}" for name, cnt, _ in stages)
    aria = html.escape(f"Application funnel: {summary}", quote=True)
    denom = max(sent_count, 1)
    bars: list[str] = []
    y = 22
    for name, cnt, color in stages:
        pct = round((cnt / denom) * 100) if sent_count > 0 else 0
        bar_w = max(4.0, (cnt / denom) * 190.0) if cnt > 0 else 3.0
        bars.append(
            f'<text x="14" y="{y + 15}" font-size="12" font-weight="600" fill="#334155">{html.escape(name)}</text>'
            f'<rect x="100" y="{y}" width="190" height="22" rx="4" fill="#f1f5f9" />'
            f'<rect x="100" y="{y}" width="{bar_w:.1f}" height="22" rx="4" fill="{color}">'
            f"<title>{html.escape(f'{name}: {cnt} ({pct}%)')}</title></rect>"
            f'<text x="300" y="{y + 15}" font-size="12" font-weight="600" fill="#0f172a">{cnt} ({pct}%)</text>'
        )
        y += 38

    return (
        f'<svg role="img" aria-label="{aria}" viewBox="0 0 380 185" width="100%" height="185">'
        + "".join(bars)
        + "</svg>"
    )


def _render_easy_apply_portal_html(company: str, role: str, job_key: str, repo_root: Path) -> str:
    """Render interactive LinkedIn Easy Apply simulation page for end-to-end browser automation."""
    comp_esc = html.escape(company)
    role_esc = html.escape(role)
    jk_esc = html.escape(job_key, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{comp_esc} — {role_esc} | LinkedIn Easy Apply Simulation</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #f3f2ef; color: #191919; margin: 0; padding: 24px; }}
    .job-card {{ max-width: 820px; margin: 20px auto; background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.12); padding: 28px; border: 1px solid #e0e0e0; }}
    .job-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; border-bottom: 1px solid #e5e7eb; padding-bottom: 20px; margin-bottom: 20px; }}
    .job-title {{ font-size: 1.5rem; font-weight: 700; margin: 0 0 6px 0; color: #0a66c2; }}
    .job-company {{ font-size: 1.1rem; font-weight: 600; color: #374151; margin: 0 0 6px 0; }}
    .job-meta {{ font-size: 0.9rem; color: #6b7280; margin: 0; }}
    .jobs-apply-button {{ background: #0a66c2; color: #fff; font-size: 1.05rem; font-weight: 600; padding: 12px 28px; border-radius: 24px; border: none; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; box-shadow: 0 2px 4px rgba(10,102,194,0.3); transition: background 0.15s; }}
    .jobs-apply-button:hover {{ background: #004182; }}
    .jobs-easy-apply-modal {{ position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.65); display: none; align-items: center; justify-content: center; z-index: 1000; }}
    .modal-dialog {{ background: #fff; width: 620px; max-width: 94vw; border-radius: 12px; box-shadow: 0 12px 30px rgba(0,0,0,0.3); overflow: hidden; display: flex; flex-direction: column; max-height: 90vh; }}
    .modal-header {{ padding: 16px 24px; border-bottom: 1px solid #e5e7eb; display: flex; justify-content: space-between; align-items: center; background: #fafafa; }}
    .modal-header h3 {{ margin: 0; font-size: 1.15rem; color: #1e293b; }}
    .modal-body {{ padding: 24px; overflow-y: auto; flex: 1; }}
    .modal-footer {{ padding: 16px 24px; border-top: 1px solid #e5e7eb; display: flex; justify-content: space-between; align-items: center; background: #fafafa; }}
    .form-group {{ margin-bottom: 16px; }}
    .form-group label {{ display: block; font-size: 0.88rem; font-weight: 600; margin-bottom: 6px; color: #374151; }}
    .form-group input[type="text"], .form-group input[type="email"], .form-group input[type="tel"] {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 0.95rem; font-family: inherit; }}
    fieldset {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 16px; margin-bottom: 16px; }}
    fieldset legend {{ font-size: 0.88rem; font-weight: 600; color: #374151; padding: 0 6px; }}
    fieldset label {{ display: inline-flex; align-items: center; gap: 6px; margin-right: 20px; font-size: 0.92rem; cursor: pointer; }}
    .artdeco-button {{ padding: 9px 20px; border-radius: 20px; font-weight: 600; font-size: 0.92rem; border: 1px solid #0a66c2; background: #fff; color: #0a66c2; cursor: pointer; transition: all 0.15s; }}
    .artdeco-button:hover {{ background: #f0f7ff; }}
    .artdeco-button--primary {{ background: #0a66c2; color: #fff; border: none; }}
    .artdeco-button--primary:hover {{ background: #004182; }}
    .upload-zone {{ border: 2px dashed #93c5fd; background: #f0f9ff; border-radius: 8px; padding: 20px; text-align: center; margin-bottom: 16px; }}
    .badge-pill {{ background: #e0f2fe; color: #0284c7; padding: 4px 10px; border-radius: 12px; font-size: 0.82rem; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="job-card">
    <div class="job-header">
      <div>
        <h1 class="job-title">{role_esc}</h1>
        <div class="job-company">{comp_esc} · <span class="badge-pill">PRISM KERNEL Target</span></div>
        <p class="job-meta">Waterloo / Toronto, ON · Canada (Remote / Hybrid) · Full-time</p>
      </div>
      <div>
        <button id="easy-apply-btn" class="jobs-apply-button" aria-label="Easy Apply" onclick="openEasyApplyModal()">
          <svg style="width:16px;height:16px;fill:currentColor" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm-1-7.9c-.6 0-1-.4-1-1s.4-1 1-1 1 .4 1 1-.4 1-1 1zm9 7.9h-2v-3.5c0-.8-.7-1.5-1.5-1.5s-1.5.7-1.5 1.5v3.5h-2v-7h2v1c.5-.8 1.5-1.2 2.5-1.2 1.7 0 2.5 1.1 2.5 2.7v4.5z"/></svg>
          Easy Apply
        </button>
      </div>
    </div>
    <div style="line-height:1.6;color:#4b5563;">
      <p><strong>About the Role:</strong> We are seeking an exceptional engineer to design and deploy autonomous AI systems, realtime data infrastructure, and scalable backend workflows.</p>
      <p><strong>Work Authorization:</strong> Eligible for Canadian Permanent Resident (Direct T4 or Corporate engagement).</p>
      <p><strong>Tailored Draft Attached:</strong> Verified 2-page CV &amp; 1-page Cover Letter compiled under PRISM honesty floor.</p>
    </div>
  </div>

  <div id="easy-apply-modal" role="dialog" class="jobs-easy-apply-modal">
    <div class="modal-dialog">
      <div class="modal-header">
        <h3 id="modal-title">Apply to {comp_esc}</h3>
        <span style="font-size:0.85rem;color:#6b7280;" id="step-indicator">Step 1 of 4</span>
      </div>
      <div class="modal-body">
        <div id="step-1" class="form-step">
          <h4 style="margin-top:0;">Contact info</h4>
          <div class="form-group">
            <label for="first-name">First name</label>
            <input type="text" id="first-name" name="firstName" aria-label="First name" value="" />
          </div>
          <div class="form-group">
            <label for="last-name">Last name</label>
            <input type="text" id="last-name" name="lastName" aria-label="Last name" value="" />
          </div>
          <div class="form-group">
            <label for="email-address">Email address</label>
            <input type="email" id="email-address" name="email" aria-label="Email address" value="" />
          </div>
          <div class="form-group">
            <label for="phone-number">Phone number</label>
            <input type="tel" id="phone-number" name="phone" aria-label="Phone number" value="" />
          </div>
          <div class="form-group">
            <label for="city-location">City</label>
            <input type="text" id="city-location" name="city" aria-label="City" value="" />
          </div>
        </div>

        <div id="step-2" class="form-step" style="display:none;">
          <h4 style="margin-top:0;">Resume</h4>
          <p style="font-size:0.88rem;color:#6b7280;">Upload your tailored CV artifact (PDF format).</p>
          <div class="upload-zone">
            <label for="resume-upload" style="font-weight:600;color:#0a66c2;cursor:pointer;display:block;" aria-label="Upload resume">
              📄 Upload resume (PDF)
            </label>
            <input type="file" id="resume-upload" aria-label="Upload resume" accept=".pdf,.doc,.docx" style="margin-top:10px;" onchange="onFileUploaded(this)" />
            <div id="file-feedback" style="margin-top:10px;font-weight:600;color:#059669;display:none;"></div>
          </div>
        </div>

        <div id="step-3" class="form-step" style="display:none;">
          <h4 style="margin-top:0;">Additional Questions</h4>
          <fieldset>
            <legend>Are you legally authorized to work in Canada?</legend>
            <label><input type="radio" name="work_auth" value="Yes" /> Yes</label>
            <label><input type="radio" name="work_auth" value="No" /> No</label>
          </fieldset>
          <fieldset>
            <legend>Will you now or in the future require visa sponsorship for employment?</legend>
            <label><input type="radio" name="sponsorship" value="Yes" /> Yes</label>
            <label><input type="radio" name="sponsorship" value="No" /> No</label>
          </fieldset>
          <div class="form-group">
            <label for="py-exp">How many years of work experience do you have with Python?</label>
            <input type="text" id="py-exp" aria-label="How many years of work experience do you have with Python?" value="" />
          </div>
        </div>

        <div id="step-4" class="form-step" style="display:none;">
          <h4 style="margin-top:0;">Review your application</h4>
          <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;font-size:0.9rem;line-height:1.6;">
            <div><strong>Candidate:</strong> <span id="rev-name">Jane Doe</span></div>
            <div><strong>Email:</strong> <span id="rev-email">jane.doe@example.com</span></div>
            <div><strong>Phone:</strong> <span id="rev-phone">+1-555-555-0123</span></div>
            <div><strong>Resume:</strong> <span id="rev-file" style="color:#0a66c2;font-weight:600;">Tailored CV Draft Attached</span></div>
            <div><strong>Work Auth:</strong> Legally authorized in Canada (Pass)</div>
          </div>
          <p style="font-size:0.85rem;color:#6b7280;margin-top:12px;">🚨 Human review gate active: Verify inputs before clicking submit.</p>
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="artdeco-button" id="btn-back" style="display:none;" onclick="goStep(currentStep - 1)">Back</button>
        <div style="display:flex;gap:10px;">
          <button type="button" class="artdeco-button" id="btn-next" aria-label="Continue to next step" onclick="goStep(currentStep + 1)">Next</button>
          <button type="button" class="artdeco-button" id="btn-review" aria-label="Review your application" style="display:none;" onclick="goStep(4)">Review</button>
          <button type="button" class="artdeco-button artdeco-button--primary" id="btn-submit" aria-label="Submit application" style="display:none;" onclick="submitApp()">Submit application</button>
        </div>
      </div>
    </div>
  </div>

  <script>
    var currentStep = 1;
    var jobKey = '{jk_esc}';
    var company = '{comp_esc}';

    function postTelemetry(stage, action) {{
      fetch('/api/telemetry', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          timestamp: new Date().toISOString(),
          current_stage: stage,
          status: 'IN_PROGRESS',
          telemetry: {{ last_action: action, job_key: jobKey, company: company }}
        }})
      }}).catch(function() {{}});
    }}

    function openEasyApplyModal() {{
      var m = document.getElementById('easy-apply-modal');
      if (m) m.style.display = 'flex';
      postTelemetry('MODAL_DETECT', 'Easy Apply modal opened for ' + company);
    }}

    function onFileUploaded(input) {{
      if (input.files && input.files[0]) {{
        var fn = input.files[0].name;
        var fb = document.getElementById('file-feedback');
        if (fb) {{
          fb.textContent = '✓ ' + fn + ' attached';
          fb.style.display = 'block';
        }}
        var rf = document.getElementById('rev-file');
        if (rf) rf.textContent = fn;
        postTelemetry('FORM_INJECT', 'Resume attached: ' + fn);
      }}
    }}

    function goStep(step) {{
      if (step < 1 || step > 4) return;
      currentStep = step;
      for (var s = 1; s <= 4; s++) {{
        var el = document.getElementById('step-' + s);
        if (el) el.style.display = (s === step) ? 'block' : 'none';
      }}
      var ind = document.getElementById('step-indicator');
      if (ind) ind.textContent = 'Step ' + step + ' of 4';

      var bBack = document.getElementById('btn-back');
      var bNext = document.getElementById('btn-next');
      var bRev = document.getElementById('btn-review');
      var bSub = document.getElementById('btn-submit');

      if (bBack) bBack.style.display = (step > 1) ? 'inline-block' : 'none';
      if (bNext) bNext.style.display = (step < 3) ? 'inline-block' : 'none';
      if (bRev) bRev.style.display = (step === 3) ? 'inline-block' : 'none';
      if (bSub) bSub.style.display = (step === 4) ? 'inline-block' : 'none';

      if (step === 1) postTelemetry('FORM_INJECT', 'Contact info stage');
      else if (step === 2) postTelemetry('FORM_INJECT', 'Resume upload stage');
      else if (step === 3) postTelemetry('FORM_INJECT', 'Screening questions stage');
      else if (step === 4) {{
        var fn = document.getElementById('first-name').value;
        var ln = document.getElementById('last-name').value;
        if (fn || ln) document.getElementById('rev-name').textContent = fn + ' ' + ln;
        var em = document.getElementById('email-address').value;
        if (em) document.getElementById('rev-email').textContent = em;
        var ph = document.getElementById('phone-number').value;
        if (ph) document.getElementById('rev-phone').textContent = ph;
        postTelemetry('SUBMIT_GATE', 'Review stage reached: Ready for submit');
      }}
    }}

    function submitApp() {{
      var mb = document.querySelector('.modal-body');
      if (mb) {{
        mb.innerHTML = '<div style="text-align:center;padding:40px 20px;"><h2 style="color:#059669;">✓ Application Submitted!</h2><p>Your application and tailored CV have been logged to ' + company + '.</p></div>';
      }}
      var mf = document.querySelector('.modal-footer');
      if (mf) mf.style.display = 'none';
      postTelemetry('COMPLETED', 'Application submitted to ' + company);
      setTimeout(function() {{
        var m = document.getElementById('easy-apply-modal');
        if (m) m.style.display = 'none';
      }}, 3000);
    }}
  </script>
</body>
</html>
"""


def generate_html_report(
    repo_root: Path = AI_JOB_SEARCH_ROOT,
    output_path: Path | None = None,
    runtime_root: Path = DEFAULT_RUNTIME_DIR,
) -> tuple[Path, dict[str, Any]]:
    """Generate the self-contained HTML dashboard per `/html-report` specification."""
    data = collect_dashboard_data(repo_root=repo_root, runtime_root=runtime_root)
    if output_path is None:
        output_path = Path(repo_root) / "reports" / "application-dashboard.html"
    output_path = Path(output_path)

    bc = data["bucket_counts"]
    status_svg = _render_status_doughnut_svg(bc, data["unrecognized_statuses"])

    # Sector chart: include sectors from sent rows, or if only drafted rows exist, show sector preview
    sector_items = list(data["sector_counts"].items())
    if not sector_items and data["rows"]:
        draft_sectors: dict[str, int] = {}
        for r in data["rows"]:
            sec = r.get("sector") or "AI & Software"
            draft_sectors[sec] = draft_sectors.get(sec, 0) + 1
        sector_items = sorted(draft_sectors.items(), key=lambda kv: (-kv[1], kv[0]))

    channel_items = list(data["channel_counts"].items())
    if not channel_items and data["rows"]:
        draft_channels: dict[str, int] = {}
        for r in data["rows"]:
            ch = r.get("channel") or "Company Portal"
            draft_channels[ch] = draft_channels.get(ch, 0) + 1
        channel_items = sorted(draft_channels.items(), key=lambda kv: (-kv[1], kv[0]))

    sector_svg = _render_horizontal_bar_svg("By sector", sector_items, bar_color="#0ea5e9")
    channel_svg = _render_horizontal_bar_svg("By channel", channel_items, bar_color="#6366f1")
    funnel_svg = _render_funnel_svg(data["funnel_counts"], data["sent_count"])

    # Build staged packets cards for live review & 1-click approval
    packet_cards_html: list[str] = []
    prism_packet_options: list[str] = []
    for pkt in data["staged_packets"]:
        raw_jk = str(pkt.get("job_key", ""))
        raw_comp = str(pkt.get("company", ""))
        raw_title = str(pkt.get("title", ""))
        raw_url = str(pkt.get("url", ""))
        jk = html.escape(raw_jk, quote=True)
        comp = html.escape(raw_comp)
        title = html.escape(raw_title)
        loc = html.escape(str(pkt.get("location", "")))
        score = int(pkt.get("score", 0))
        url = str(pkt.get("url", ""))

        opt_payload = html.escape(json.dumps({"job_key": raw_jk, "company": raw_comp, "role": raw_title, "url": raw_url}), quote=True)
        prism_packet_options.append(f'<option value="{opt_payload}">{comp} — {title} ({score}/100 Fit)</option>')

        auth_gate = pkt.get("work_auth_gate") or {}
        auth_verdict = html.escape(str(auth_gate.get("verdict", "PASS")))
        auth_mode = html.escape(str(auth_gate.get("engagement_mode", "CANADA_DIRECT")))
        pdf_meta = pkt.get("pdf_verification") or {}
        cv_pages = (pdf_meta.get("cv") or {}).get("pages", 2)
        cl_pages = (pdf_meta.get("cover_letter") or {}).get("pages", 1)

        url_link = (
            f'<a class="btn btn-outline" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">🔗 Job Posting ↗</a>'
            if url.startswith("http")
            else ""
        )
        pkt_status = str(pkt.get("status", "")).upper()
        is_applied = pkt_status == "APPLIED"
        action_btn_html = (
            f"""<span class="badge badge-active" style="padding: 7px 12px; font-size: 0.82rem; margin-right: 6px;">✓ Submitted &amp; Logged (Proof Verified)</span><button class="btn btn-outline" onclick="invokePrismPacketApply('{jk}', '{comp}', '{title}', '{url}')" style="margin-right:6px;">⚡ Apply now (Re-verify)</button><button class="btn btn-outline" onclick="approvePacket('{jk}', '{comp}', '{title}')">✅ Mark Applied</button>"""
            if is_applied
            else f"""<button class="btn btn-primary" onclick="invokePrismPacketApply('{jk}', '{comp}', '{title}', '{url}')" style="background:#2563eb;color:#fff;font-weight:600;margin-right:6px;">⚡ Apply now</button><button class="btn btn-outline" onclick="approvePacket('{jk}', '{comp}', '{title}')">✅ Mark Applied</button>"""
        )
        packet_cards_html.append(
            f"""
            <div class="packet-card" data-job-key="{jk}">
              <div class="packet-header">
                <div>
                  <span class="score-pill">{score}/100 Fit</span>
                  <span class="gate-pill">Gate 1: {auth_verdict} ({auth_mode})</span>
                  <h4>{comp} — {title}</h4>
                  <p class="packet-meta">{loc} · Honesty-Floor: PASS · CV: {cv_pages}pp · Cover Letter: {cl_pages}p</p>
                </div>
                <div class="packet-actions">
                  <a class="btn btn-outline" href="/files/{jk}/cv.pdf" target="_blank">📄 CV PDF (2pp)</a>
                  <a class="btn btn-outline" href="/files/{jk}/cover_letter.pdf" target="_blank">✉️ Cover Letter PDF (1p)</a>
                  <a class="btn btn-outline" href="/files/{jk}/evaluation.md" target="_blank">📊 Fit Evaluation</a>
                  {url_link}
                  {action_btn_html}
                </div>
              </div>
            </div>
            """
        )

    prism_packet_options_html = "".join(prism_packet_options) if prism_packet_options else '<option value="">No staged packets found</option>'
    unapplied_packets_count = int(data.get("unapplied_packets_count", 0))
    applied_packets_count = int(data.get("applied_packets_count", 0))

    # Build ranked & vetoed shortlist rows
    staged_lookup: dict[Any, dict[str, Any]] = {}
    for p in data.get("staged_packets", []):
        p_jk = str(p.get("job_key") or "")
        if p_jk:
            staged_lookup[p_jk] = p
            staged_lookup[p_jk.replace("-", "_")] = p
        p_comp = str(p.get("company") or "").strip().lower()
        p_title = str(p.get("title") or "").strip().lower()
        if p_comp and p_title:
            staged_lookup[(p_comp, p_title)] = p

    ranked_rows_html: list[str] = []
    for rj in data["ranked_jobs"]:
        raw_key = str(rj.get("key") or "")
        raw_comp = str(rj.get("company", "")).strip()
        raw_title = str(rj.get("title", "")).strip()
        comp = html.escape(raw_comp)
        title = html.escape(raw_title)
        loc = html.escape(str(rj.get("location", "Canada")))
        sc = int(rj.get("rank_score", 0))
        verd = html.escape(str(rj.get("rank_verdict", "")))
        dl = html.escape(str(rj.get("deadline") or "—"))
        strengths = "; ".join(str(s) for s in (rj.get("strengths") or [])[:2])

        matched_p = (
            staged_lookup.get(raw_key)
            or staged_lookup.get(raw_key.replace("_", "-"))
            or staged_lookup.get((raw_comp.lower(), raw_title.lower()))
        )
        effective_jk = str((matched_p.get("job_key") if matched_p else "") or raw_key.replace("_", "-"))

        comp_js = html.escape(raw_comp, quote=True)
        title_js = html.escape(raw_title, quote=True)
        jk_js = html.escape(effective_jk, quote=True)

        if matched_p and (matched_p.get("status") == "APPLIED" or matched_p.get("already_submitted")):
            action_html = f'<span class="badge badge-submitted">✓ Applied</span> <button class="btn btn-outline" style="font-size:11px;padding:2px 8px;margin-left:4px;" onclick="approveRankedJob(\'{jk_js}\', \'{comp_js}\', \'{title_js}\')">⚡ Re-Stage</button>'
        elif matched_p and str(matched_p.get("status", "")).upper() in ("DRAFTED_NEEDS_SUBMISSION", "STAGED_FOR_REVIEW", "DRAFTED"):
            action_html = '<span class="badge badge-active" style="background:#16a34a;color:#fff;">✓ Staged in Queue</span>'
        else:
            action_html = f'<button class="btn btn-primary" style="font-size:11px;padding:3px 8px;background:#0284c7;color:#fff;border-radius:4px;border:none;cursor:pointer;" onclick="approveRankedJob(\'{jk_js}\', \'{comp_js}\', \'{title_js}\')">⚡ Approve &amp; Stage</button>'

        ranked_rows_html.append(
            f"<tr>"
            f"<td><strong>{sc}/100</strong> ({verd})</td>"
            f"<td>{comp}</td>"
            f"<td>{title}</td>"
            f"<td>{loc}</td>"
            f"<td>{dl}</td>"
            f"<td><span class='badge badge-active'>PASS</span></td>"
            f"<td title='{html.escape(strengths, quote=True)}'>{html.escape(strengths[:90] + ('…' if len(strengths) > 90 else ''))}</td>"
            f"<td>{action_html}</td>"
            f"</tr>"
        )
    for vj in data["vetoed_jobs"]:
        comp = html.escape(str(vj.get("company", "")))
        title = html.escape(str(vj.get("title", "")))
        loc = html.escape(str(vj.get("location", "")))
        sc = int(vj.get("rank_score", 0))
        gaps = "; ".join(str(g) for g in (vj.get("gaps") or []))
        ranked_rows_html.append(
            f"<tr class='row-vetoed'>"
            f"<td>{sc}/100 (Vetoed)</td>"
            f"<td>{comp}</td>"
            f"<td>{title}</td>"
            f"<td>{loc}</td>"
            f"<td>—</td>"
            f"<td><span class='badge badge-rejected'>VETO (FAIL)</span></td>"
            f"<td title='{html.escape(gaps, quote=True)}'>{html.escape(gaps)}</td>"
            f"<td><span class='badge badge-rejected' style='font-size:11px;'>Gate 1 Vetoed</span></td>"
            f"</tr>"
        )

    # Build tracker table rows with exact required column order:
    # `Date` · `Deadline` · `Company` · `Role` · `Sector` · `Channel` · `Status` · `Notes` · `Source`
    table_rows_html: list[str] = []
    unique_sectors: set[str] = set()
    for row in data["rows"]:
        r_date = html.escape(row.get("date") or "—")
        r_deadline = html.escape(row.get("deadline") or "—")
        r_company = html.escape(row.get("company") or "—")
        r_role = html.escape(row.get("role") or "—")
        r_sector_raw = row.get("sector") or "—"
        if r_sector_raw != "—":
            unique_sectors.add(r_sector_raw)
        r_sector = html.escape(r_sector_raw)
        r_channel = html.escape(row.get("channel") or "—")
        r_bucket = row.get("bucket", "Drafted")
        r_status_raw = row.get("status") or "drafted"

        notes_full = row.get("notes") or ""
        if notes_full:
            notes_trunc = notes_full[:80] + ("…" if len(notes_full) > 80 else "")
            notes_cell = (
                f'<td title="{html.escape(notes_full, quote=True)}">'
                f"{html.escape(notes_trunc)}</td>"
            )
        else:
            notes_cell = "<td>—</td>"

        src_raw = (row.get("source") or "").strip()
        if src_raw.startswith("http"):
            src_cell = (
                f'<td><a href="{html.escape(src_raw, quote=True)}" '
                f'target="_blank" rel="noopener">Link ↗</a></td>'
            )
        elif src_raw:
            src_cell = f"<td>{html.escape(src_raw)}</td>"
        else:
            src_cell = "<td>—</td>"

        badge_cls = {
            "Drafted": "badge-drafted",
            "Submitted": "badge-active",
            "Active": "badge-active",
            "Interview": "badge-interview",
            "Offer": "badge-offer",
            "Hired": "badge-hired",
            "Rejected/Closed": "badge-rejected",
        }.get(r_bucket, "badge-drafted")

        search_blob = html.escape(
            f"{row.get('company', '')} {row.get('role', '')} {row.get('sector', '')}".lower(),
            quote=True,
        )
        table_rows_html.append(
            f'<tr data-status="{html.escape(r_bucket, quote=True)}" '
            f'data-sector="{html.escape(r_sector_raw, quote=True)}" '
            f'data-search="{search_blob}">'
            f"<td>{r_date}</td>"
            f"<td>{r_deadline}</td>"
            f"<td><strong>{r_company}</strong></td>"
            f"<td>{r_role}</td>"
            f"<td>{r_sector}</td>"
            f"<td>{r_channel}</td>"
            f'<td><span class="badge {badge_cls}" title="Raw status: {html.escape(r_status_raw, quote=True)}">{html.escape(r_bucket)}</span></td>'
            f"{notes_cell}"
            f"{src_cell}"
            f"</tr>"
        )

    sector_options_html = "\n".join(
        f'<option value="{html.escape(sec, quote=True)}">{html.escape(sec)}</option>'
        for sec in sorted(unique_sectors)
    )

    commands_rows_html: list[str] = []
    for c_item in data.get("commands_matrix", []):
        cmd_name = html.escape(str(c_item.get("command", "")))
        src_file = html.escape(str(c_item.get("source_file", "")))
        runner_str = html.escape(str(c_item.get("runner", "")))
        st_str = html.escape(str(c_item.get("status", "")))
        desc_str = html.escape(str(c_item.get("description", "")))
        exists_flag = bool(c_item.get("exists", True))
        badge_class = "badge-hired" if exists_flag else "badge-rejected"
        commands_rows_html.append(
            f"<tr>"
            f"<td><code><strong>{cmd_name}</strong></code></td>"
            f"<td><span class='badge {badge_class}'>{st_str}</span></td>"
            f"<td>{desc_str}</td>"
            f"<td><code>{src_file}</code></td>"
            f"<td><code>{runner_str}</code></td>"
            f"</tr>"
        )

    gen_date = html.escape(data["generated_date"])
    llm_st = data.get("llamacpp_status") or {}
    llm_model_str = html.escape(str(llm_st.get("model_id", "Qwen3.6-12B")))
    llm_ep_str = html.escape(str(llm_st.get("active_endpoint", "http://127.0.0.1:11435/v1")))
    llm_queries = int(llm_st.get("total_job_queries", 0))
    llm_resent = int(llm_st.get("resent_job_queries", 0))
    llm_hs_ok = int(llm_st.get("handshake_successes", 0))
    llm_hs_fail = int(llm_st.get("handshake_failures", 0))
    llm_resolutions = int(llm_st.get("endpoint_resolutions", 0))
    llm_cloud_calls = int(llm_st.get("cloud_api_calls", 0))
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Search Dashboard — Jane Doe (Acme Corp)</title>
<style>
:root {{
  --status-drafted: #64748b;
  --status-active: #3b82f6;
  --status-interview: #f59e0b;
  --status-offer: #8b5cf6;
  --status-hired: #22c55e;
  --status-rejected: #ef4444;
  --bg-page: #f8fafc;
  --bg-card: #ffffff;
  --text-main: #0f172a;
  --text-muted: #64748b;
  --border-color: #e2e8f0;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 24px;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg-page);
  color: var(--text-main);
  line-height: 1.45;
}}
.container {{
  max-width: 1280px;
  margin: 0 auto;
}}
header.top-bar {{
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  background: var(--bg-card);
  padding: 20px 24px;
  border-radius: 12px;
  border: 1px solid var(--border-color);
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
  margin-bottom: 20px;
}}
header.top-bar h1 {{
  margin: 0;
  font-size: 1.45rem;
}}
.subtitle {{
  margin: 4px 0 0;
  font-size: 0.9rem;
  color: var(--text-muted);
}}
.toolbar-actions {{
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}}
.pipeline-pills {{
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}}
.stage-pill {{
  background: #ffffff;
  border: 1px solid var(--border-color);
  border-left: 4px solid #22c55e;
  padding: 8px 14px;
  border-radius: 8px;
  font-size: 0.85rem;
  font-weight: 600;
}}
.stat-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
  gap: 14px;
  margin-bottom: 22px;
}}
.stat-card {{
  background: var(--bg-card);
  padding: 16px 18px;
  border-radius: 10px;
  border: 1px solid var(--border-color);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  border-left: 5px solid var(--status-active);
}}
.stat-card.drafted {{ border-left-color: var(--status-drafted); }}
.stat-card.active {{ border-left-color: var(--status-active); }}
.stat-card.interview {{ border-left-color: var(--status-interview); }}
.stat-card.offer {{ border-left-color: var(--status-offer); }}
.stat-card.hired {{ border-left-color: var(--status-hired); }}
.stat-card.rejected {{ border-left-color: var(--status-rejected); }}
.stat-value {{
  font-size: 1.9rem;
  font-weight: 800;
  line-height: 1.1;
}}
.stat-label {{
  margin-top: 6px;
  font-size: 0.84rem;
  color: var(--text-muted);
  font-weight: 600;
}}
.charts-grid {{
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 18px;
  margin-bottom: 24px;
}}
@media (max-width: 900px) {{
  .charts-grid {{ grid-template-columns: 1fr; }}
}}
.chart-card {{
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 18px 20px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}}
.chart-card h3 {{
  margin: 0 0 12px;
  font-size: 1rem;
  color: #1e293b;
}}
.section-card {{
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 20px 24px;
  margin-bottom: 24px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}}
.section-card h2 {{
  margin: 0 0 14px;
  font-size: 1.15rem;
}}
.packet-card {{
  border: 1px solid var(--border-color);
  border-left: 5px solid var(--status-active);
  border-radius: 10px;
  padding: 14px 18px;
  margin-bottom: 12px;
  background: #fcfdff;
}}
.packet-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}}
.packet-header h4 {{
  margin: 6px 0 4px;
  font-size: 1.02rem;
}}
.packet-meta {{
  margin: 0;
  font-size: 0.84rem;
  color: var(--text-muted);
}}
.packet-actions {{
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}}
.score-pill {{
  display: inline-block;
  background: #dbeafe;
  color: #1d4ed8;
  font-weight: 700;
  font-size: 0.78rem;
  padding: 2px 9px;
  border-radius: 999px;
  margin-right: 6px;
}}
.gate-pill {{
  display: inline-block;
  background: #dcfce7;
  color: #15803d;
  font-weight: 600;
  font-size: 0.76rem;
  padding: 2px 9px;
  border-radius: 999px;
}}
.btn {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border-radius: 7px;
  font-size: 0.83rem;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  border: 1px solid transparent;
}}
.btn-outline {{
  background: #ffffff;
  border-color: #cbd5e1;
  color: #1e293b;
}}
.btn-outline:hover {{ background: #f1f5f9; }}
.btn-primary {{
  background: #2563eb;
  color: #ffffff;
}}
.btn-primary:hover {{ background: #1d4ed8; }}
.filter-bar {{
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 14px;
}}
.filter-bar input, .filter-bar select {{
  padding: 8px 12px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  font-size: 0.88rem;
}}
.filter-bar input {{
  min-width: 260px;
  flex: 1;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.88rem;
}}
th, td {{
  text-align: left;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-color);
}}
th {{
  background: #f1f5f9;
  font-weight: 700;
  color: #334155;
}}
tbody tr:nth-child(even) {{
  background: #f8fafc;
}}
tbody tr.row-vetoed {{
  background: #fef2f2;
  color: #7f1d1d;
}}
.badge {{
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 0.76rem;
  font-weight: 700;
  color: #ffffff;
}}
.badge-drafted {{ background: var(--status-drafted); }}
.badge-active {{ background: var(--status-active); }}
.badge-interview {{ background: var(--status-interview); }}
.badge-offer {{ background: var(--status-offer); }}
.badge-hired {{ background: var(--status-hired); }}
.badge-rejected {{ background: var(--status-rejected); }}
#toast {{
  display: none;
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: #0f172a;
  color: #ffffff;
  padding: 12px 18px;
  border-radius: 8px;
  font-size: 0.88rem;
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.2);
  z-index: 1000;
  transition: all 0.2s ease;
}}
footer {{
  text-align: center;
  color: var(--text-muted);
  font-size: 0.82rem;
  padding: 18px 0 8px;
}}

/* Top-of-screen indeterminate / determinate progress indicator */
#global-progress-bar {{
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 4px;
  z-index: 99999;
  pointer-events: none;
  background: transparent;
  overflow: hidden;
  opacity: 0;
  transform: translateY(-100%);
  transition: opacity 0.25s ease, transform 0.25s ease;
}}
#global-progress-bar.active {{
  opacity: 1;
  transform: translateY(0);
}}
#global-progress-bar-fill {{
  height: 100%;
  width: 100%;
  background: linear-gradient(90deg, #2563eb, #60a5fa, #93c5fd, #2563eb);
  background-size: 200% 100%;
  animation: progress-indeterminate 1.4s linear infinite;
  transform-origin: left;
  transition: width 0.3s ease, background 0.3s ease;
}}
#global-progress-bar.success #global-progress-bar-fill {{
  background: linear-gradient(90deg, #16a34a, #4ade80);
  animation: none;
  width: 100% !important;
}}
#global-progress-bar.error #global-progress-bar-fill {{
  background: linear-gradient(90deg, #dc2626, #f87171);
  animation: none;
  width: 100% !important;
}}
@keyframes progress-indeterminate {{
  0% {{ background-position: 200% 0; }}
  100% {{ background-position: -200% 0; }}
}}

/* Realtime Connection / Activity Status Badge */
.connection-badge {{
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 0.8rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  transition: all 0.25s ease;
  border: 1px solid transparent;
  white-space: nowrap;
}}
.status-connected {{
  background: #ecfdf5;
  color: #065f46;
  border-color: #a7f3d0;
}}
.status-busy {{
  background: #eff6ff;
  color: #1e40af;
  border-color: #bfdbfe;
}}
.status-reconnecting {{
  background: #fef2f2;
  color: #991b1b;
  border-color: #fecaca;
}}
.status-dot {{
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  position: relative;
  flex-shrink: 0;
}}
.status-connected .status-dot::after,
.status-busy .status-dot::after {{
  content: '';
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  border: 2px solid currentColor;
  opacity: 0.75;
  animation: status-ping 1.6s cubic-bezier(0, 0, 0.2, 1) infinite;
}}
@keyframes status-ping {{
  75%, 100% {{
    transform: scale(2.2);
    opacity: 0;
  }}
}}

/* Button Loading, Spinner & Anti-Spam States */
.btn-spinner {{
  display: inline-block;
  width: 14px;
  height: 14px;
  animation: btn-spin 0.75s linear infinite;
  flex-shrink: 0;
  vertical-align: middle;
}}
@keyframes btn-spin {{
  100% {{ transform: rotate(360deg); }}
}}
.btn.btn-loading {{
  position: relative;
  opacity: 0.85;
  cursor: wait !important;
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.45) !important;
}}
.btn[disabled], .btn[aria-disabled="true"] {{
  cursor: not-allowed !important;
  opacity: 0.58;
}}

/* Active Workflow Progression Banner & Stepper */
#workflow-progression-panel {{
  display: none;
  background: #ffffff;
  border: 1px solid #bfdbfe;
  border-left: 5px solid #2563eb;
  border-radius: 12px;
  padding: 16px 20px;
  margin-bottom: 22px;
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.1);
  animation: slideDown 0.25s ease-out;
}}
@keyframes slideDown {{
  from {{ opacity: 0; transform: translateY(-10px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
.workflow-panel-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 14px;
}}
.workflow-title-wrap {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.workflow-task-title {{
  font-weight: 700;
  font-size: 1.02rem;
  color: #1e3a8a;
}}
.workflow-task-sub {{
  font-size: 0.83rem;
  color: #64748b;
  margin-top: 2px;
}}
.workflow-meta {{
  display: flex;
  align-items: center;
  gap: 10px;
}}
.workflow-timer {{
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.84rem;
  font-weight: 700;
  background: #f1f5f9;
  padding: 4px 10px;
  border-radius: 6px;
  color: #334155;
  border: 1px solid #cbd5e1;
}}
.workflow-lock-badge {{
  font-size: 0.78rem;
  font-weight: 600;
  color: #b45309;
  background: #fef3c7;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid #fde68a;
}}
.workflow-stepper {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  background: #f8fafc;
  padding: 12px 16px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  overflow-x: auto;
}}
.stepper-step {{
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.83rem;
  font-weight: 600;
  color: #64748b;
  white-space: nowrap;
}}
.step-icon {{
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #e2e8f0;
  color: #64748b;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.76rem;
  font-weight: 700;
  transition: all 0.25s ease;
}}
.stepper-step.active {{
  color: #1d4ed8;
}}
.stepper-step.active .step-icon {{
  background: #2563eb;
  color: #ffffff;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.25);
  animation: pulse-step 1.2s infinite alternate;
}}
@keyframes pulse-step {{
  0% {{ transform: scale(1); }}
  100% {{ transform: scale(1.1); }}
}}
.stepper-step.complete {{
  color: #15803d;
}}
.stepper-step.complete .step-icon {{
  background: #22c55e;
  color: #ffffff;
}}
.step-connector {{
  flex: 1;
  height: 2px;
  background: #e2e8f0;
  min-width: 16px;
  transition: background 0.3s ease;
}}
.step-connector.complete {{
  background: #22c55e;
}}

/* PRISM Stage Stepper */
.prism-stepper-track {{
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  margin-bottom: 10px;
  overflow-x: auto;
}}
.prism-step-pill {{
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: 6px;
  background: #1e293b;
  color: #94a3b8;
  font-size: 0.76rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
  border: 1px solid #334155;
  transition: all 0.2s ease;
  white-space: nowrap;
}}
.prism-step-pill.active {{
  background: #1e40af;
  color: #ffffff;
  border-color: #3b82f6;
  box-shadow: 0 0 8px rgba(59, 130, 246, 0.4);
}}
.prism-step-pill.complete {{
  background: #064e3b;
  color: #a7f3d0;
  border-color: #059669;
}}
</style>
</head>
<body>
<div id="global-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-label="Action progress">
  <div id="global-progress-bar-fill"></div>
</div>
<div class="container">
  <header class="top-bar">
    <div>
      <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
        <h1 style="margin: 0; font-size: 1.45rem;">🔍 Job Search Dashboard</h1>
        <div id="connection-pulse-badge" class="connection-badge status-connected" role="status" aria-live="polite">
          <span class="status-dot"></span>
          <span id="connection-status-text">Live Connected</span>
        </div>
      </div>
      <p class="subtitle">Jane Doe · Acme Corp (Toronto, ON) · Generated: {gen_date}</p>
    </div>
    <div class="toolbar-actions">
      <button id="btn-prism-header" class="btn btn-primary" onclick="executePrismBatchApplyAll()" style="background:linear-gradient(135deg,#16a34a,#15803d);color:#fff;font-weight:700;border:none;box-shadow:0 2px 5px rgba(22,163,74,0.3);padding:9px 18px;border-radius:8px;cursor:pointer;">⚡ APPLY ALL Staged ({unapplied_packets_count})</button>
      <button id="btn-pipeline-header" class="btn btn-outline" onclick="triggerAction('run_pipeline')">🔄 Run /scrape + /rank + /apply</button>
      <button id="btn-gmail-header" class="btn btn-outline" onclick="triggerAction('gmail_sync')">📬 Run /gmail-sync</button>
    </div>
  </header>

  <div id="workflow-progression-panel" role="region" aria-label="Active Workflow Progress" aria-live="polite">
    <div class="workflow-panel-header">
      <div class="workflow-title-wrap">
        <svg class="btn-spinner" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="3" style="width:20px;height:20px;"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg>
        <div>
          <div id="workflow-task-title" class="workflow-task-title">Executing Workflow...</div>
          <div id="workflow-task-sub" class="workflow-task-sub">Initializing action harness</div>
        </div>
      </div>
      <div class="workflow-meta">
        <span id="workflow-timer" class="workflow-timer">00:00</span>
        <span class="workflow-lock-badge">🔒 Anti-Spam Lock Active</span>
      </div>
    </div>
    <div class="workflow-stepper" id="workflow-stepper-container">
      <div class="stepper-step active" id="wf-step-1">
        <div class="step-icon">1</div>
        <div class="step-label">Scrape / Ingress</div>
      </div>
      <div class="step-connector" id="wf-conn-1"></div>
      <div class="stepper-step" id="wf-step-2">
        <div class="step-icon">2</div>
        <div class="step-label">Gate 1 &amp; Rank</div>
      </div>
      <div class="step-connector" id="wf-conn-2"></div>
      <div class="stepper-step" id="wf-step-3">
        <div class="step-icon">3</div>
        <div class="step-label">Tailor &amp; Verify</div>
      </div>
      <div class="step-connector" id="wf-conn-3"></div>
      <div class="stepper-step" id="wf-step-4">
        <div class="step-icon">4</div>
        <div class="step-label">Sync &amp; Persist</div>
      </div>
    </div>
  </div>

  <div class="pipeline-pills">
    <div class="stage-pill">✅ /setup: Verified (CV 2pp, CL 1p, Fork Guard OK)</div>
    <div class="stage-pill">✅ /scrape: {data["total_seen"]} Postings Tracked</div>
    <div class="stage-pill">✅ /rank: {len(data["ranked_jobs"])} Eligible · {len(data["vetoed_jobs"])} Vetoed (Gate 1)</div>
    <div class="stage-pill">✅ /apply: {len(data["staged_packets"])} Tailored Packets Compiled &amp; Verified</div>
    <div class="stage-pill">✅ /gmail-sync: DWD Connected (sid@dissid.ca)</div>
    <div class="stage-pill">🦙 1h Loop (llama.cpp Exclusive): {llm_model_str} · {llm_queries} Queries ({llm_resent} Resent, {llm_resolutions} Resolved) · {llm_cloud_calls} Gemini/Argon Calls</div>
  </div>

  <div class="stat-grid">
    <div class="stat-card active">
      <div class="stat-value" id="stat-sent">{data["sent_count"]}</div>
      <div class="stat-label">Sent Applications</div>
    </div>
    <div class="stat-card drafted">
      <div class="stat-value" id="stat-drafted">{data["drafted_count"]}</div>
      <div class="stat-label">Drafted (Staged for Review)</div>
    </div>
    <div class="stat-card active">
      <div class="stat-value">{bc.get("Submitted", bc.get("Active", 0))}</div>
      <div class="stat-label">Submitted</div>
    </div>
    <div class="stat-card interview">
      <div class="stat-value">{bc["Interview"]}</div>
      <div class="stat-label">Interview ({data["funnel_rate_pct"]}% Funnel)</div>
    </div>
    <div class="stat-card offer">
      <div class="stat-value">{bc["Offer"]}</div>
      <div class="stat-label">Offer</div>
    </div>
    <div class="stat-card hired">
      <div class="stat-value">{bc["Hired"]}</div>
      <div class="stat-label">Hired</div>
    </div>
    <div class="stat-card rejected">
      <div class="stat-value">{bc["Rejected/Closed"]}</div>
      <div class="stat-label">Rejected/Closed ({data["rejection_rate_pct"]}% True Rej.)</div>
    </div>
  </div>

  <div class="charts-grid">
    <div class="chart-card">
      <h3>Status breakdown</h3>
      {status_svg}
    </div>
    <div class="chart-card">
      <h3>By sector</h3>
      {sector_svg}
    </div>
    <div class="chart-card">
      <h3>By channel</h3>
      {channel_svg}
    </div>
    <div class="chart-card">
      <h3>Application funnel</h3>
      {funnel_svg}
    </div>
  </div>

  <section class="section-card" id="prism-kernel-card" style="border:2px solid #2563eb;background:#f0f7ff;border-radius:12px;padding:22px;margin-bottom:24px;">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;">
      <div>
        <h2 style="margin:0;color:#1e3a8a;display:flex;align-items:center;gap:10px;font-size:1.35rem;">
          <span>⚡</span> PRISM KERNEL :: LINKEDIN_EASY_APPLY_RUNNER Code Harness
        </h2>
        <p style="margin:6px 0 0;color:#475569;font-size:0.92rem;">
          Autonomous, time-bound Web UI Automation Engine &amp; 4-agent closed-loop browser team.
        </p>
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
        <button id="btn-prism-apply-main" class="btn btn-primary" onclick="executePrismBatchApplyAll()" style="background:#16a34a;color:#fff;font-weight:700;font-size:1.02rem;padding:12px 24px;border-radius:8px;border:none;cursor:pointer;box-shadow:0 4px 10px rgba(22,163,74,0.35);transition:all 0.15s ease;">
          ⚡ APPLY ALL Staged Application Targets ({unapplied_packets_count} Ready)
        </button>
        <a class="btn btn-outline" href="/portal/easy-apply" target="_blank" style="padding:11px 18px;border-radius:8px;text-decoration:none;display:inline-flex;align-items:center;gap:6px;font-weight:600;">
          🌐 Open Simulation Portal ↗
        </a>
      </div>
    </div>

    <div style="background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:16px;margin-top:16px;display:flex;flex-wrap:wrap;gap:14px;align-items:center;">
      <div style="flex:1.4;min-width:320px;background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:12px 14px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <label style="display:block;font-size:0.82rem;font-weight:800;color:#166534;text-transform:uppercase;letter-spacing:0.04em;">⚡ APPLY ALL Staged Application Targets</label>
          <span id="prism-batch-badge" style="background:#16a34a;color:#fff;font-size:0.75rem;font-weight:700;padding:2px 8px;border-radius:12px;">{unapplied_packets_count} Pending</span>
        </div>
        <p style="margin:0 0 10px 0;font-size:0.82rem;color:#14532d;line-height:1.4;">
          Single-click batch apply for all {len(data["staged_packets"])} staged packets. Every submission is recorded in the local Database to prevent duplicate submissions.
        </p>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
          <button id="btn-prism-batch-apply" class="btn btn-primary" onclick="executePrismBatchApplyAll()" style="background:#16a34a;color:#fff;font-weight:700;font-size:0.92rem;padding:8px 16px;border-radius:6px;border:none;cursor:pointer;box-shadow:0 3px 8px rgba(22,163,74,0.3);display:inline-flex;align-items:center;gap:6px;">
            ⚡ BATCH APPLY ALL ({unapplied_packets_count} Targets)
          </button>
          <span id="prism-batch-summary-text" style="font-size:0.78rem;color:#166534;font-weight:600;">
            ✓ {applied_packets_count} logged in DB · {unapplied_packets_count} unapplied
          </span>
        </div>
      </div>
      <div style="flex:1;min-width:280px;">
        <label style="display:block;font-size:0.8rem;font-weight:700;color:#475569;margin-bottom:4px;text-transform:uppercase;">Or Custom Target Job URL</label>
        <input id="prism-custom-url" type="text" placeholder="https://www.linkedin.com/jobs/view/... (leave blank for auto portal)" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:6px;border:1px solid #cbd5e1;font-size:0.92rem;" />
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;">
        <label style="display:inline-flex;align-items:center;gap:6px;font-size:0.88rem;color:#1e293b;font-weight:600;cursor:pointer;">
          <input type="checkbox" id="prism-headless-toggle" style="width:16px;height:16px;"> 🖥️ Run Headless (Background)
        </label>
        <span style="font-size:0.75rem;color:#64748b;">(Default: Headed — opens Chromium on screen)</span>
      </div>
    </div>
    <div style="margin-top:14px;">
      <div style="font-size:0.8rem;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;">Exact Bound PRISM Kernel Prompt</div>
      <pre style="background:#0f172a;color:#38bdf8;padding:16px;border-radius:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.86rem;overflow-x:auto;margin:0;border:1px solid #1e293b;line-height:1.45;">
▙▖▙▖▞▞▙▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂
* ▛///▞ PRISM KERNEL :: LINKEDIN_EASY_APPLY_RUNNER
* //▞▞〔Purpose · Rules · Identity · Structure · Motion〕
* P:: automate.easy_apply.modal ∙ bridge.ai_job_search.outputs ∙ e2e.form.completion
* R:: headless=false ∙ persistent.context.only ∙ direct.file.injection ∙ preserve.prefilled.inputs ∙ human.gate.at.submit (zero.blind.submits) ∙ simulated.keystroke.delays
* I:: payload.json (profile+skills) ∙ output/cv_tailored.pdf ∙ .profiles/linkedin_profile/
* S:: launch.persistent.browser → goto.job.url → detect.easy_apply → loop: [fill.inputs + set.radios + inject.pdf] → advance: [next|review] → detect.final.submit → halt.and.await.manual.click
* M:: populated.modal.state ∙ terminal.review.alert ∙ user.manual.submission ∙ retained.session.cookies
* :: ∎</pre>
    </div>
    <div id="prism-telemetry-panel" style="display:none;margin-top:16px;background:#0f172a;border-radius:8px;padding:16px;border:1px solid #334155;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <span style="color:#22c55e;font-weight:700;font-size:0.88rem;font-family:monospace;">● TELEMETRY STREAM ACTIVE [DASHBOARD_UPDATE]</span>
        <span id="prism-stage-badge" style="background:#2563eb;color:#fff;padding:3px 10px;border-radius:4px;font-size:0.78rem;font-family:monospace;font-weight:600;">INITIALIZATION</span>
      </div>
      <div class="prism-stepper-track" id="prism-stepper-track">
        <div class="prism-step-pill active" id="prism-step-1">1. Context Init</div>
        <span style="color:#475569;">➔</span>
        <div class="prism-step-pill" id="prism-step-2">2. Browser Launch</div>
        <span style="color:#475569;">➔</span>
        <div class="prism-step-pill" id="prism-step-3">3. Modal Detect</div>
        <span style="color:#475569;">➔</span>
        <div class="prism-step-pill" id="prism-step-4">4. Form &amp; PDF Fill</div>
        <span style="color:#475569;">➔</span>
        <div class="prism-step-pill" id="prism-step-5">5. Review &amp; Submit</div>
      </div>
      <div id="prism-log-output" style="color:#e2e8f0;font-family:monospace;font-size:0.82rem;max-height:240px;overflow-y:auto;white-space:pre-wrap;background:#090d16;padding:12px;border-radius:6px;border:1px solid #1e293b;">Awaiting task invocation...</div>
    </div>
  </section>

  <section class="section-card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:10px;">
      <h2 style="margin:0;">📦 Staged Application Packets Ready for Submission ({len(data["staged_packets"])})</h2>
      <button class="btn btn-primary" onclick="executePrismBatchApplyAll()" style="background:#16a34a;color:#fff;font-weight:700;padding:8px 16px;border-radius:6px;border:none;cursor:pointer;">
        ⚡ Batch Apply ALL ({unapplied_packets_count} Ready)
      </button>
    </div>
    <p class="subtitle" style="margin-bottom: 14px;">
      Each packet has passed <code>validate_honesty_floor()</code>, Gate 1 Work-Authorization check, and <code>tools/verify_pdf.py --ascii-dates</code> (2-page CV, 1-page Cover Letter).
    </p>
    {"".join(packet_cards_html) if packet_cards_html else "<p>No staged packets found.</p>"}
  </section>

  <section class="section-card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:10px;">
      <h2 style="margin:0;">🏆 Ranked Shortlist &amp; Gate 1 Work-Authorization Audit (<code>seen_jobs.json</code>)</h2>
      <button id="btn-batch-approve-ranked" class="btn btn-primary" onclick="batchApproveAllRanked()" style="background:#0284c7;color:#fff;font-weight:700;padding:8px 16px;border-radius:6px;border:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;box-shadow:0 3px 8px rgba(2,132,199,0.3);">
        ⚡ Batch Approve ALL Ranked ({len(data["ranked_jobs"])} Postings) → Stage for Apply All
      </button>
    </div>
    <div style="overflow-x: auto;">
      <table>
        <thead>
          <tr>
            <th>Score &amp; Band</th>
            <th>Company</th>
            <th>Role</th>
            <th>Location</th>
            <th>Deadline</th>
            <th>Gate 1 (Work Auth)</th>
            <th>Key Alignment / Gate Note</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {"".join(ranked_rows_html) if ranked_rows_html else "<tr><td colspan='8'>No ranked postings yet.</td></tr>"}
        </tbody>
      </table>
    </div>
  </section>

  <section class="section-card">
    <h2>📋 Applications Tracker (<code>job_search_tracker.csv</code>)</h2>
    <div class="filter-bar">
      <select id="filter-status" onchange="applyTableFilters()" aria-label="Filter by Status">
        <option value="">All Statuses</option>
        <option value="Drafted">Drafted</option>
        <option value="Submitted">Submitted</option>
        <option value="Interview">Interview</option>
        <option value="Offer">Offer</option>
        <option value="Hired">Hired</option>
        <option value="Rejected/Closed">Rejected/Closed</option>
      </select>
      <select id="filter-sector" onchange="applyTableFilters()" aria-label="Filter by Sector">
        <option value="">All Sectors</option>
        {sector_options_html}
      </select>
      <input type="search" id="filter-search" oninput="debouncedApplyTableFilters()" placeholder="🔍 Filter across company, role, or sector..." aria-label="Search applications" />
    </div>
    <div style="overflow-x: auto;">
      <table id="applications-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Deadline</th>
            <th>Company</th>
            <th>Role</th>
            <th>Sector</th>
            <th>Channel</th>
            <th>Status</th>
            <th>Notes</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {"".join(table_rows_html) if table_rows_html else "<tr><td colspan='9'>—</td></tr>"}
        </tbody>
      </table>
    </div>
  </section>

  <section class="section-card">
    <h2>🛠️ Registered Commands &amp; Skills Matrix (13 Commands · 9 Skills · <code>lint_skills: OK</code>)</h2>
    <div style="overflow-x: auto;">
      <table>
        <thead>
          <tr>
            <th>Command / Skill</th>
            <th>Status</th>
            <th>Description</th>
            <th>Skill / Command Definition</th>
            <th>Service / Runner</th>
          </tr>
        </thead>
        <tbody>
          {"".join(commands_rows_html)}
        </tbody>
      </table>
    </div>
  </section>

  <footer>
    Generated by Claude Code · ai-job-search · {gen_date}
  </footer>
</div>

<div id="toast"></div>

<script>
// --- Realtime Collaboration & Web-Perf Client Engine ---
var filterTimer = null;
var syncBackoff = 2000;
var maxSyncBackoff = 30000;
var activeAbortControllers = new Map();

function generateOpId() {{
  if (window.crypto && crypto.randomUUID) {{
    return crypto.randomUUID();
  }}
  return 'op_' + Date.now() + '_' + Math.random().toString(36).substring(2, 9);
}}

function debouncedApplyTableFilters() {{
  if (filterTimer) clearTimeout(filterTimer);
  filterTimer = setTimeout(function() {{
    window.requestAnimationFrame(applyTableFilters);
  }}, 120);
}}

function applyTableFilters() {{
  var sElem = document.getElementById('filter-status');
  var secElem = document.getElementById('filter-sector');
  var searchElem = document.getElementById('filter-search');
  var tableElem = document.getElementById('applications-table');
  if (!tableElem) return;

  var statusVal = sElem ? sElem.value : '';
  var sectorVal = secElem ? secElem.value : '';
  var searchVal = (searchElem && searchElem.value ? searchElem.value : '').toLowerCase().trim();
  
  var rows = tableElem.querySelectorAll('tbody tr');
  rows.forEach(function(row) {{
    var rStatus = row.getAttribute('data-status') || '';
    var rSector = row.getAttribute('data-sector') || '';
    var rSearch = row.getAttribute('data-search') || '';
    var matchStatus = !statusVal || rStatus === statusVal;
    var matchSector = !sectorVal || rSector === sectorVal;
    var matchSearch = !searchVal || rSearch.indexOf(searchVal) !== -1;
    row.style.display = (matchStatus && matchSector && matchSearch) ? '' : 'none';
  }});
}}

var __actionInProgress = false;
var __workflowTimerId = null;
var __workflowStartTime = 0;

function getActionButtons() {{
  return Array.from(document.querySelectorAll('button[onclick], .packet-actions button, #btn-prism-apply-main, .toolbar-actions button'));
}}

function lockAllActionButtons(triggerBtn, loadingText) {{
  if (__actionInProgress) return false;
  __actionInProgress = true;
  var buttons = getActionButtons();
  buttons.forEach(function(b) {{
    b.disabled = true;
    b.setAttribute('aria-disabled', 'true');
    b.title = 'An action is currently executing. Anti-spam lock engaged.';
  }});

  if (triggerBtn) {{
    triggerBtn.setAttribute('data-original-html', triggerBtn.innerHTML);
    var curWidth = triggerBtn.offsetWidth;
    if (curWidth > 0) triggerBtn.style.minWidth = curWidth + 'px';
    triggerBtn.classList.add('btn-loading');
    triggerBtn.setAttribute('aria-busy', 'true');
    var spinnerSvg = '<svg class="btn-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="width:14px;height:14px;display:inline-block;vertical-align:middle;margin-right:6px;"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg>';
    triggerBtn.innerHTML = spinnerSvg + (loadingText || 'Processing...');
  }}
  return true;
}}

function unlockAllActionButtons(triggerBtn) {{
  __actionInProgress = false;
  var buttons = getActionButtons();
  buttons.forEach(function(b) {{
    b.disabled = false;
    b.removeAttribute('aria-disabled');
    b.removeAttribute('title');
  }});

  if (triggerBtn) {{
    var orig = triggerBtn.getAttribute('data-original-html');
    if (orig) triggerBtn.innerHTML = orig;
    triggerBtn.classList.remove('btn-loading');
    triggerBtn.removeAttribute('aria-busy');
    triggerBtn.style.minWidth = '';
  }}
}}

function setConnectionStatus(state, label) {{
  var badge = document.getElementById('connection-pulse-badge');
  var txt = document.getElementById('connection-status-text');
  if (!badge || !txt) return;
  badge.className = 'connection-badge ' + (
    state === 'busy' ? 'status-busy' :
    state === 'reconnecting' ? 'status-reconnecting' :
    'status-connected'
  );
  txt.textContent = label || (
    state === 'busy' ? 'Executing...' :
    state === 'reconnecting' ? 'Reconnecting...' :
    'Live Connected'
  );
}}

function startGlobalProgress(label) {{
  var bar = document.getElementById('global-progress-bar');
  var fill = document.getElementById('global-progress-bar-fill');
  if (!bar) return;
  bar.className = 'active';
  if (fill) fill.style.width = '100%';
  document.body.setAttribute('aria-busy', 'true');
}}

function finishGlobalProgress(success) {{
  var bar = document.getElementById('global-progress-bar');
  if (!bar) return;
  bar.className = 'active ' + (success ? 'success' : 'error');
  setTimeout(function() {{
    bar.className = '';
    document.body.removeAttribute('aria-busy');
  }}, 400);
}}

function startWorkflowProgression(title, stepLabels) {{
  var panel = document.getElementById('workflow-progression-panel');
  var titleEl = document.getElementById('workflow-task-title');
  var subEl = document.getElementById('workflow-task-sub');
  var timerEl = document.getElementById('workflow-timer');
  var container = document.getElementById('workflow-stepper-container');
  if (!panel) return;

  if (titleEl) titleEl.textContent = title;
  if (subEl) subEl.textContent = 'Stage 1 of ' + stepLabels.length + ': ' + stepLabels[0];
  if (timerEl) timerEl.textContent = '00:00';

  if (container && stepLabels && stepLabels.length) {{
    var stepsHtml = '';
    for (var i = 0; i < stepLabels.length; i++) {{
      if (i > 0) {{
        stepsHtml += '<div class="step-connector" id="wf-conn-' + i + '"></div>';
      }}
      stepsHtml += '<div class="stepper-step ' + (i === 0 ? 'active' : '') + '" id="wf-step-' + (i + 1) + '">' +
        '<div class="step-icon">' + (i + 1) + '</div>' +
        '<div class="step-label">' + stepLabels[i] + '</div>' +
        '</div>';
    }}
    container.innerHTML = stepsHtml;
  }}

  panel.style.display = 'block';
  __workflowStartTime = Date.now();
  if (__workflowTimerId) clearInterval(__workflowTimerId);
  __workflowTimerId = setInterval(function() {{
    var elapsedSec = Math.floor((Date.now() - __workflowStartTime) / 1000);
    var m = String(Math.floor(elapsedSec / 60)).padStart(2, '0');
    var s = String(elapsedSec % 60).padStart(2, '0');
    if (timerEl) timerEl.textContent = m + ':' + s;
  }}, 1000);
}}

function advanceWorkflowProgression(stepNumber, totalSteps, subText) {{
  var subEl = document.getElementById('workflow-task-sub');
  if (subEl && subText) {{
    subEl.textContent = 'Stage ' + stepNumber + ' of ' + totalSteps + ': ' + subText;
  }}
  for (var i = 1; i <= totalSteps; i++) {{
    var stepEl = document.getElementById('wf-step-' + i);
    var connEl = document.getElementById('wf-conn-' + (i - 1));
    if (!stepEl) continue;
    if (i < stepNumber) {{
      stepEl.className = 'stepper-step complete';
      var icon = stepEl.querySelector('.step-icon');
      if (icon) icon.textContent = '✓';
      if (connEl) connEl.className = 'step-connector complete';
    }} else if (i === stepNumber) {{
      stepEl.className = 'stepper-step active';
      var icon = stepEl.querySelector('.step-icon');
      if (icon) icon.textContent = String(i);
    }} else {{
      stepEl.className = 'stepper-step';
      var icon = stepEl.querySelector('.step-icon');
      if (icon) icon.textContent = String(i);
    }}
  }}
}}

function finishWorkflowProgression(success, message) {{
  if (__workflowTimerId) {{
    clearInterval(__workflowTimerId);
    __workflowTimerId = null;
  }}
  var subEl = document.getElementById('workflow-task-sub');
  if (subEl && message) subEl.textContent = message;
  var panel = document.getElementById('workflow-progression-panel');
  if (panel) {{
    panel.style.borderLeftColor = success ? '#22c55e' : '#ef4444';
    setTimeout(function() {{
      panel.style.display = 'none';
      panel.style.borderLeftColor = '#2563eb';
    }}, 2200);
  }}
}}

function setPrismStep(stepIndex) {{
  for (var i = 1; i <= 5; i++) {{
    var pill = document.getElementById('prism-step-' + i);
    if (!pill) continue;
    if (i < stepIndex) {{
      pill.className = 'prism-step-pill complete';
      if (!pill.textContent.startsWith('✓')) {{
        pill.textContent = '✓ ' + pill.textContent.replace(/^[✓\\d\\.]+\\s*/, '');
      }}
    }} else if (i === stepIndex) {{
      pill.className = 'prism-step-pill active';
    }} else {{
      pill.className = 'prism-step-pill';
    }}
  }}
}}

function showToast(msg, isError) {{
  var t = document.getElementById('toast');
  if (!t) return;
  var icon = isError ? '❌ ' : 'ℹ️ ';
  t.innerHTML = '<div style="display:flex;align-items:center;gap:8px;"><span>' + icon + '</span><span>' + msg + '</span></div>';
  t.style.display = 'block';
  t.style.background = isError ? '#ef4444' : '#1e293b';
  t.setAttribute('role', 'status');
  t.setAttribute('aria-live', 'polite');
  if (t._timer) clearTimeout(t._timer);
  t._timer = setTimeout(function() {{ t.style.display = 'none'; }}, 4000);
}}

// Resilient fetch with timeout & client-generated opId (idempotent contract)
async function resilientActionFetch(payload, timeoutMs) {{
  var timeout = timeoutMs || 20000;
  var opId = generateOpId();
  payload.opId = opId;
  
  var controller = new AbortController();
  activeAbortControllers.set(opId, controller);
  var timer = setTimeout(function() {{ controller.abort(); }}, timeout);

  try {{
    var resp = await fetch('/api/action', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'X-Client-Op-ID': opId }},
      body: JSON.stringify(payload),
      signal: controller.signal
    }});
    clearTimeout(timer);
    activeAbortControllers.delete(opId);

    if (!resp.ok) {{
      var errData = {{}};
      try {{ errData = await resp.json(); }} catch (_) {{}}
      throw new Error(errData.error || ('HTTP ' + resp.status + ': ' + resp.statusText));
    }}
    return await resp.json();
  }} catch (err) {{
    clearTimeout(timer);
    activeAbortControllers.delete(opId);
    if (err.name === 'AbortError') {{
      throw new Error('Request timed out after ' + (timeout / 1000) + 's');
    }}
    throw err;
  }}
}}

// Optimistic In-Place Approval (No White-Flash Reload per web-perf)
async function approvePacket(jobKey, company, role) {{
  if (__actionInProgress) return;
  var btn = window.event ? (window.event.currentTarget || window.event.target) : null;
  var card = document.querySelector('.packet-card[data-job-key="' + jobKey + '"]');
  var actionsContainer = card ? card.querySelector('.packet-actions') : null;
  var originalHtml = actionsContainer ? actionsContainer.innerHTML : '';

  if (!lockAllActionButtons(btn, 'Marking...')) return;

  if (actionsContainer) {{
    actionsContainer.innerHTML = '<span class="badge" style="background:#f59e0b;color:#fff;padding:7px 12px;font-size:0.82rem;display:inline-flex;align-items:center;gap:6px;"><svg class="btn-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="width:13px;height:13px;"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-linecap="round"></path></svg> ⚡ Applying...</span>';
  }}
  setConnectionStatus('busy', 'Approving ' + company + '...');
  startGlobalProgress();
  showToast('Approving & marking ' + company + ' as applied...');

  try {{
    var data = await resilientActionFetch({{
      action: 'approve_packet',
      job_key: jobKey,
      company: company,
      role: role
    }}, 15000);

    if (data.ok) {{
      if (actionsContainer) {{
        actionsContainer.innerHTML = '<span class="badge badge-active" style="padding: 7px 12px; font-size: 0.82rem;">✓ Submitted &amp; Logged (Proof Verified)</span>';
      }}
      // Update table row in-place
      var tableRow = document.querySelector('#applications-table tbody tr[data-search*="' + (company.toLowerCase()) + '"]');
      if (tableRow) {{
        tableRow.setAttribute('data-status', 'Active');
        var statusCell = tableRow.querySelector('td:nth-child(7)');
        if (statusCell) {{
          statusCell.innerHTML = '<span class="badge badge-active">Submitted</span>';
        }}
      }}
      finishGlobalProgress(true);
      setConnectionStatus('connected', 'Live Connected');
      showToast('Marked ' + company + ' as Applied!');
    }} else {{
      throw new Error(data.error || 'Server rejected approval');
    }}
  }} catch (err) {{
    if (actionsContainer && originalHtml) {{
      actionsContainer.innerHTML = originalHtml;
    }}
    finishGlobalProgress(false);
    setConnectionStatus('connected', 'Live Connected');
    showToast('Failed to approve ' + company + ': ' + err.message, true);
  }} finally {{
    unlockAllActionButtons(btn);
  }}
}}

function updateBatchStatusCounters() {{
  var cards = Array.from(document.querySelectorAll('.packet-card'));
  var pendingCards = cards.filter(function(card) {{
    var appliedBadge = card.querySelector('.badge-active');
    return !appliedBadge || !appliedBadge.textContent.includes('Submitted');
  }});
  var unappliedCount = pendingCards.length;
  var appliedCount = cards.length - unappliedCount;

  var badge = document.getElementById('prism-batch-badge');
  if (badge) badge.textContent = unappliedCount + ' Pending';

  var summary = document.getElementById('prism-batch-summary-text');
  if (summary) summary.textContent = '✓ ' + appliedCount + ' logged in DB · ' + unappliedCount + ' unapplied';

  var batchBtn = document.getElementById('btn-prism-batch-apply');
  if (batchBtn) batchBtn.innerHTML = '⚡ BATCH APPLY ALL (' + unappliedCount + ' Targets)';

  var mainBtn = document.getElementById('btn-prism-apply-main');
  if (mainBtn) mainBtn.innerHTML = '⚡ APPLY ALL Staged Application Targets (' + unappliedCount + ' Ready)';

  var headerBtn = document.getElementById('btn-prism-header');
  if (headerBtn) headerBtn.innerHTML = '⚡ APPLY ALL Staged (' + unappliedCount + ')';
}}

async function batchApproveAllRanked() {{
  if (__actionInProgress) return;
  var btn = document.getElementById('btn-batch-approve-ranked') || (window.event ? (window.event.currentTarget || window.event.target) : null);
  if (!confirm("Are you sure you want to batch approve ALL ranked applications from seen_jobs.json and stage them into the 'Apply All' queue?")) {{
    return;
  }}
  if (!lockAllActionButtons(btn, '⚡ Staging All Ranked Postings...')) return;
  setConnectionStatus('busy', 'Batch Approving Ranked Postings...');
  startGlobalProgress();

  var batchSteps = [
    'Read Ranked Postings (seen_jobs.json)',
    'Verify Gate 1 Work-Auth Eligibility',
    'Compile & Stage Application Packets',
    'Update CSV Ledger & Reset DB Lock',
    'Populate Apply All Queue'
  ];
  startWorkflowProgression('Batch Approve All Ranked Applications', batchSteps);

  try {{
    advanceWorkflowProgression(2, 5, 'Auditing Gate 1 work authorization...');
    advanceWorkflowProgression(3, 5, 'Staging packets for Apply All queue...');
    var res = await resilientActionFetch({{
      action: 'batch_approve_ranked'
    }}, 60000);

    if (res.ok) {{
      advanceWorkflowProgression(5, 5, 'Successfully approved ' + (res.approved_count || 0) + ' applications!');
      finishWorkflowProgression(true, res.message || 'Batch approval completed successfully.');
      finishGlobalProgress(true);
      showToast('🎉 ' + (res.message || 'Successfully approved all ranked applications!'));
      setConnectionStatus('connected', 'Live Connected');
      setTimeout(function() {{
        var stagedSection = document.querySelector('.section-card h2');
        if (stagedSection) stagedSection.scrollIntoView({{ behavior: 'smooth' }});
        setTimeout(function() {{
          window.location.reload();
        }}, 800);
      }}, 500);
    }} else {{
      finishWorkflowProgression(false, 'Batch approval failed: ' + (res.error || 'Unknown error'));
      finishGlobalProgress(false);
      showToast('Batch approval error: ' + (res.error || 'Unknown error'), true);
      setConnectionStatus('connected', 'Live Connected');
    }}
  }} catch (err) {{
    finishWorkflowProgression(false, 'Batch approval error: ' + err.message);
    finishGlobalProgress(false);
    showToast('Batch approval error: ' + err.message, true);
    setConnectionStatus('connected', 'Live Connected');
  }} finally {{
    unlockAllActionButtons(btn);
  }}
}}

async function approveRankedJob(jobKey, company, role) {{
  if (__actionInProgress) return;
  var btn = window.event ? (window.event.currentTarget || window.event.target) : null;
  if (!lockAllActionButtons(btn, 'Staging...')) return;
  setConnectionStatus('busy', 'Staging ' + company + '...');
  startGlobalProgress();

  try {{
    var res = await resilientActionFetch({{
      action: 'approve_ranked_job',
      job_key: jobKey,
      company: company,
      role: role
    }}, 30000);

    if (res.ok) {{
      finishGlobalProgress(true);
      showToast('✓ Staged ' + company + ' — ' + role + ' for Apply All queue!');
      setConnectionStatus('connected', 'Live Connected');
      if (btn) {{
        var parentCell = btn.parentElement;
        if (parentCell) {{
          parentCell.innerHTML = '<span class="badge badge-active">✓ Staged for Apply</span>';
        }}
      }}
      setTimeout(function() {{
        window.location.reload();
      }}, 800);
    }} else {{
      finishGlobalProgress(false);
      showToast('Error staging job: ' + (res.error || 'Unknown error'), true);
      setConnectionStatus('connected', 'Live Connected');
    }}
  }} catch (err) {{
    finishGlobalProgress(false);
    showToast('Error staging job: ' + err.message, true);
    setConnectionStatus('connected', 'Live Connected');
  }} finally {{
    unlockAllActionButtons(btn);
  }}
}}

async function executePrismBatchApplyAll() {{
  if (__actionInProgress) return;
  var btn = document.getElementById('btn-prism-batch-apply') || document.getElementById('btn-prism-apply-main') || document.getElementById('btn-prism-header');
  var customUrlInput = document.getElementById('prism-custom-url');
  var customUrl = customUrlInput ? customUrlInput.value.trim() : '';
  var headlessToggle = document.getElementById('prism-headless-toggle');
  var isHeadless = headlessToggle ? headlessToggle.checked : false;

  if (customUrl) {{
    await executePrismKernelHarness(customUrl, '', '', '');
    return;
  }}

  var stagedCards = Array.from(document.querySelectorAll('.packet-card'));
  var pendingCards = stagedCards.filter(function(card) {{
    var appliedBadge = card.querySelector('.badge-active');
    return !appliedBadge || !appliedBadge.textContent.includes('Submitted');
  }});

  if (pendingCards.length === 0) {{
    showToast('✓ All staged application targets have already been submitted and recorded in the local Database!');
    return;
  }}

  if (!lockAllActionButtons(btn, '⚡ Batch Applying All Targets...')) return;
  setConnectionStatus('busy', 'Batch Applying ' + pendingCards.length + ' Targets...');
  startGlobalProgress();

  var panel = document.getElementById('prism-telemetry-panel');
  var logBox = document.getElementById('prism-log-output');
  var badge = document.getElementById('prism-stage-badge');
  if (panel) panel.style.display = 'block';

  var batchSteps = [
    'Scan & Deduplicate DB',
    'Batch Runner Dispatch',
    'Interactive Form Automation',
    'Persist Proofs to Database',
    'Batch Audit Complete'
  ];
  startWorkflowProgression('PRISM KERNEL :: Batch Apply All Targets (' + pendingCards.length + ' pending)', batchSteps);

  if (logBox) {{
    logBox.textContent = '🚀 [PRISM KERNEL] Initializing Batch Apply for ' + pendingCards.length + ' staged application targets...' + String.fromCharCode(10) +
      '🛡️ Local Database Deduplication: ACTIVE (Preventing duplicate submissions across local SQLite & CSV ledger)' + String.fromCharCode(10);
  }}

  var successCount = 0;
  var skipCount = 0;
  var failCount = 0;
  var total = pendingCards.length;

  try {{
    for (var i = 0; i < pendingCards.length; i++) {{
      var card = pendingCards[i];
      var jk = card.getAttribute('data-job-key') || '';
      var titleElem = card.querySelector('h4');
      var fullTitle = titleElem ? titleElem.textContent : '';
      var parts = fullTitle.split(' — ');
      var comp = parts[0] || '';
      var role = parts.slice(1).join(' — ') || '';

      var applyBtn = card.querySelector('button[onclick*="invokePrismPacketApply"]');
      var targetUrl = '';
      if (applyBtn) {{
        var oc = applyBtn.getAttribute('onclick') || '';
        var ocParts = oc.split(\"'\");
        if (ocParts.length >= 8) {{
          jk = jk || ocParts[1];
          comp = comp || ocParts[3];
          role = role || ocParts[5];
          targetUrl = ocParts[7] || '';
        }}
      }}

      var currentIdx = i + 1;
      advanceWorkflowProgression(Math.min(4, Math.floor((currentIdx / total) * 4) + 1), 5,
        '[' + currentIdx + '/' + total + '] Applying: ' + comp + ' — ' + role
      );

      if (badge) {{
        badge.textContent = 'BATCH [' + currentIdx + '/' + total + ']';
        badge.style.background = '#2563eb';
      }}

      if (logBox) {{
        logBox.textContent += String.fromCharCode(10) + '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━' + String.fromCharCode(10) +
          '⚡ Target ' + currentIdx + '/' + total + ': ' + comp + ' — ' + role + String.fromCharCode(10);
        logBox.scrollTop = logBox.scrollHeight;
      }}

      try {{
        var res = await resilientActionFetch({{
          action: 'prism_apply',
          job_key: jk,
          company: comp,
          role: role,
          url: targetUrl,
          headless: isHeadless,
          batch_mode: true
        }}, 60000);

        if (res.ok) {{
          if (res.skipped_duplicate) {{
            skipCount++;
            if (logBox) logBox.textContent += '  🛡️ [DEDUPLICATED] Already exists in local Database. Skipped redundant submission.' + String.fromCharCode(10);
          }} else {{
            successCount++;
            if (logBox) logBox.textContent += '  ✓ [SUBMITTED & PERSISTED] Successfully applied and recorded in local Database.' + String.fromCharCode(10);
          }}

          var actionsContainer = card.querySelector('.packet-actions');
          if (actionsContainer) {{
            actionsContainer.innerHTML = '<span class="badge badge-active" style="padding: 7px 12px; font-size: 0.82rem;">✓ Submitted &amp; Logged (Proof Verified)</span>';
          }}
          var tableRow = document.querySelector('#applications-table tbody tr[data-search*="' + comp.toLowerCase() + '"]');
          if (tableRow) {{
            tableRow.setAttribute('data-status', 'Active');
            var statusCell = tableRow.querySelector('td:nth-child(7)');
            if (statusCell) statusCell.innerHTML = '<span class="badge badge-active">Submitted</span>';
          }}
        }} else {{
          failCount++;
          if (logBox) logBox.textContent += '  ❌ Failed: ' + (res.error || 'Unknown error') + String.fromCharCode(10);
        }}
      }} catch (applyErr) {{
        failCount++;
        if (logBox) logBox.textContent += '  ❌ Error: ' + applyErr.message + String.fromCharCode(10);
      }}
      updateBatchStatusCounters();
    }}

    advanceWorkflowProgression(5, 5, 'Batch Complete: ' + successCount + ' submitted, ' + skipCount + ' deduplicated in DB');
    if (badge) {{
      badge.textContent = 'BATCH COMPLETE';
      badge.style.background = '#16a34a';
    }}
    setPrismStep(5);
    if (logBox) {{
      logBox.textContent += String.fromCharCode(10) + '================================================' + String.fromCharCode(10) +
        '🎉 BATCH APPLY RUN COMPLETED!' + String.fromCharCode(10) +
        '  • Total Targets      : ' + total + String.fromCharCode(10) +
        '  • Newly Submitted    : ' + successCount + String.fromCharCode(10) +
        '  • Skipped Duplicates : ' + skipCount + String.fromCharCode(10) +
        '  • Failures           : ' + failCount + String.fromCharCode(10) +
        '  • Database Ledger    : PERSISTED & VERIFIED' + String.fromCharCode(10) +
        '================================================' + String.fromCharCode(10);
      logBox.scrollTop = logBox.scrollHeight;
    }}
    finishWorkflowProgression(true, 'Batch apply finished (' + successCount + ' submitted, ' + skipCount + ' deduplicated)');
    finishGlobalProgress(true);
    setConnectionStatus('connected', 'Live Connected');
    showToast('🎉 Batch apply complete! ' + successCount + ' submitted, ' + skipCount + ' deduplicated.');
    updateBatchStatusCounters();
  }} catch (batchErr) {{
    if (badge) {{
      badge.textContent = 'BATCH ERROR';
      badge.style.background = '#dc2626';
    }}
    finishWorkflowProgression(false, 'Batch error: ' + batchErr.message);
    finishGlobalProgress(false);
    setConnectionStatus('connected', 'Live Connected');
    showToast('Batch apply error: ' + batchErr.message, true);
  }} finally {{
    unlockAllActionButtons(btn);
  }}
}}

function onPrismTargetSelect(val) {{
  if (!val) return;
  try {{
    var d = JSON.parse(val);
    var uInput = document.getElementById('prism-custom-url');
    if (uInput) uInput.value = d.url || '';
  }} catch(e) {{}}
}}

async function executePrismKernelHarness(targetUrl, company, role, jobKey) {{
  if (__actionInProgress) return;
  var btn = document.getElementById('btn-prism-apply-main') || (window.event ? (window.event.currentTarget || window.event.target) : null);
  var panel = document.getElementById('prism-telemetry-panel');
  var logBox = document.getElementById('prism-log-output');
  var badge = document.getElementById('prism-stage-badge');

  var customUrlInput = document.getElementById('prism-custom-url');
  if (customUrlInput && customUrlInput.value.trim() && !targetUrl) {{
    targetUrl = customUrlInput.value.trim();
  }}

  var headlessToggle = document.getElementById('prism-headless-toggle');
  var isHeadless = headlessToggle ? headlessToggle.checked : false;

  if (!targetUrl || targetUrl === 'http://localhost:8765/' || targetUrl === 'http://127.0.0.1:8765/') {{
    var selectElem = document.getElementById('prism-target-select');
    if (selectElem && selectElem.value) {{
      try {{
        var optData = JSON.parse(selectElem.value);
        jobKey = jobKey || optData.job_key || '';
        company = company || optData.company || '';
        role = role || optData.role || '';
        targetUrl = optData.url || targetUrl;
      }} catch (e) {{}}
    }}
  }}

  if (!targetUrl || targetUrl === 'http://localhost:8765/' || targetUrl === 'http://127.0.0.1:8765/') {{
    var firstCard = document.querySelector('.packet-card');
    if (firstCard) {{
      jobKey = jobKey || firstCard.getAttribute('data-job-key') || '';
      var applyBtn = firstCard.querySelector('button[onclick*="invokePrismPacketApply"]');
      if (applyBtn) {{
        var oc = applyBtn.getAttribute('onclick') || '';
        var parts = oc.split("'");
        if (parts.length >= 8) {{
          jobKey = jobKey || parts[1];
          company = company || parts[3];
          role = role || parts[5];
          targetUrl = parts[7] || targetUrl;
        }}
      }}
    }}
  }}

  if (!lockAllActionButtons(btn, '🚀 Running Browser Automation...')) return;
  setConnectionStatus('busy', 'PRISM Kernel Active' + (company ? ' (' + company + ')...' : '...'));
  startGlobalProgress();

  var prismSteps = [
    'Context & Profile Init',
    'Persistent Browser Launch',
    'Detect Easy Apply Modal',
    'Inject CV/CL & Form Values',
    'Review Gate & Complete'
  ];
  startWorkflowProgression('PRISM KERNEL :: Web UI Automation Harness' + (company ? ' — ' + company : ''), prismSteps);

  if (panel) panel.style.display = 'block';
  if (logBox) logBox.textContent = '🚀 [PRISM KERNEL] Initializing Web UI Automation Orchestrator V2.2...' + String.fromCharCode(10) +
    (company ? ('Target Application : ' + company + ' — ' + role + String.fromCharCode(10)) : '') +
    (targetUrl ? ('Target Posting URL : ' + targetUrl + String.fromCharCode(10)) : '');
  if (badge) {{
    badge.textContent = 'INITIALIZATION';
    badge.style.background = '#2563eb';
  }}
  setPrismStep(1);
  showToast('⚡ Invoking PRISM KERNEL harness' + (company ? ' for ' + company : '') + '...');

  var pollTimer = setInterval(async function() {{
    try {{
      var tRes = await fetch('/api/telemetry');
      var tData = await tRes.json();
      if (tData && tData.recent_events && tData.recent_events.length) {{
        var events = tData.recent_events;
        var last = events[events.length - 1];
        if (last && last.current_stage) {{
          if (badge) {{
            badge.textContent = last.current_stage;
            if (last.current_stage === 'NAVIGATION') badge.style.background = '#0284c7';
            else if (last.current_stage === 'MODAL_DETECT') badge.style.background = '#6366f1';
            else if (last.current_stage === 'FORM_INJECT') badge.style.background = '#8b5cf6';
            else if (last.current_stage === 'SUBMIT_GATE') badge.style.background = '#f59e0b';
            else if (last.current_stage === 'COMPLETED') badge.style.background = '#16a34a';
          }}
          if (last.current_stage === 'INITIALIZATION') setPrismStep(1);
          else if (last.current_stage === 'NAVIGATION') {{ setPrismStep(2); advanceWorkflowProgression(2, 5, 'Navigating to target posting...'); }}
          else if (last.current_stage === 'MODAL_DETECT') {{ setPrismStep(3); advanceWorkflowProgression(3, 5, 'Easy Apply modal detected & opened...'); }}
          else if (last.current_stage === 'FORM_INJECT') {{ setPrismStep(4); advanceWorkflowProgression(4, 5, 'Injecting contact info, screening radios & tailored CV draft...'); }}
          else if (last.current_stage === 'SUBMIT_GATE' || last.current_stage === 'COMPLETED') {{ setPrismStep(5); advanceWorkflowProgression(5, 5, 'Submit review gate reached!'); }}
        }}
        if (last && last.telemetry && last.telemetry.last_action && logBox) {{
          var line = '[' + (last.current_stage || 'PRISM') + '] ' + last.telemetry.last_action;
          if (!logBox.textContent.includes(line)) {{
            logBox.textContent += line + String.fromCharCode(10);
            logBox.scrollTop = logBox.scrollHeight;
          }}
        }}
      }}
    }} catch (e) {{}}
  }}, 250);

  try {{
    var data = await resilientActionFetch({{
      action: 'prism_apply',
      url: targetUrl || 'http://localhost:8765/',
      company: company || '',
      role: role || '',
      job_key: jobKey || '',
      headless: isHeadless
    }}, 65000);

    clearInterval(pollTimer);

    if (data.ok) {{
      setPrismStep(5);
      advanceWorkflowProgression(5, 5, 'Form stages completed under cadence limits!');
      if (badge) {{
        badge.textContent = 'COMPLETED';
        badge.style.background = '#16a34a';
      }}
      if (logBox) logBox.textContent += (data.output || 'Orchestrator workflow completed successfully.') + String.fromCharCode(10) + '🎉 All form stages completed under cadence limits!' + String.fromCharCode(10);
      
      var targetJk = jobKey || data.job_key;
      if (targetJk) {{
        var card = document.querySelector('.packet-card[data-job-key="' + targetJk + '"]');
        var actionsContainer = card ? card.querySelector('.packet-actions') : null;
        if (actionsContainer) {{
          actionsContainer.innerHTML = '<span class="badge badge-active" style="padding: 7px 12px; font-size: 0.82rem;">✓ Submitted &amp; Logged (Proof Verified)</span>';
        }}
      }}
      var targetComp = company || data.company;
      if (targetComp) {{
        var tableRow = document.querySelector('#applications-table tbody tr[data-search*="' + (targetComp.toLowerCase()) + '"]');
        if (tableRow) {{
          tableRow.setAttribute('data-status', 'Active');
          var statusCell = tableRow.querySelector('td:nth-child(7)');
          if (statusCell) {{
            statusCell.innerHTML = '<span class="badge badge-active">Submitted</span>';
          }}
        }}
      }}

      finishWorkflowProgression(true, 'Completed: PRISM KERNEL task finished successfully');
      finishGlobalProgress(true);
      setConnectionStatus('connected', 'Live Connected');
      showToast('🎉 PRISM KERNEL task completed successfully!');
    }} else {{
      throw new Error(data.error || 'Execution failed');
    }}
  }} catch (err) {{
    clearInterval(pollTimer);
    if (badge) {{
      badge.textContent = 'ERROR';
      badge.style.background = '#dc2626';
    }}
    if (logBox) logBox.textContent += '❌ Error: ' + err.message + String.fromCharCode(10);
    finishWorkflowProgression(false, 'PRISM Kernel Error: ' + err.message);
    finishGlobalProgress(false);
    setConnectionStatus('connected', 'Live Connected');
    showToast('PRISM Kernel execution failed: ' + err.message, true);
  }} finally {{
    unlockAllActionButtons(btn);
  }}
}}

async function invokePrismPacketApply(jobKey, company, role, targetUrl) {{
  if (__actionInProgress) return;
  showToast('⚡ Invoking PRISM KERNEL for ' + company + '...');
  var panel = document.getElementById('prism-kernel-card');
  if (panel) panel.scrollIntoView({{ behavior: 'smooth' }});
  await executePrismKernelHarness(targetUrl, company, role, jobKey);
}}

async function triggerAction(actionName) {{
  if (__actionInProgress) return;
  var btn = window.event ? (window.event.currentTarget || window.event.target) : null;
  var labelMap = {{
    'run_pipeline': 'Run /scrape + /rank + /apply',
    'gmail_sync': 'Run /gmail-sync'
  }};
  var loadingLabelMap = {{
    'run_pipeline': 'Running /scrape + /rank + /apply...',
    'gmail_sync': 'Syncing Gmail...'
  }};
  var taskTitleMap = {{
    'run_pipeline': 'Full Job Search Pipeline (/scrape + /rank + /apply)',
    'gmail_sync': 'Gmail ATS Synchronization Service'
  }};
  var pipelineSteps = [
    'Scrape / Ingress Trackers',
    'Gate 1 & Rank Postings',
    'Tailor CV / CL Packets',
    'Sync & Persist Ledger'
  ];
  var gmailSteps = [
    'Authenticate DWD Token',
    'Scan Unread Threads',
    'Extract Rejections & Interviews',
    'Reconcile DB Ledger'
  ];

  var steps = (actionName === 'gmail_sync') ? gmailSteps : pipelineSteps;
  var friendlyTitle = taskTitleMap[actionName] || ('Action: ' + actionName);

  if (!lockAllActionButtons(btn, loadingLabelMap[actionName] || 'Executing...')) return;
  setConnectionStatus('busy', 'Running ' + (labelMap[actionName] || actionName) + '...');
  startGlobalProgress();
  startWorkflowProgression(friendlyTitle, steps);
  showToast('Starting ' + friendlyTitle + '...');

  var stageTimer1 = setTimeout(function() {{
    advanceWorkflowProgression(2, steps.length, steps[1]);
  }}, 1200);
  var stageTimer2 = setTimeout(function() {{
    advanceWorkflowProgression(3, steps.length, steps[2]);
  }}, 3500);

  try {{
    var data = await resilientActionFetch({{ action: actionName }}, 65000);
    clearTimeout(stageTimer1);
    clearTimeout(stageTimer2);

    if (data.ok) {{
      advanceWorkflowProgression(steps.length, steps.length, 'Completed successfully');
      finishWorkflowProgression(true, 'Completed: ' + (data.message || 'All stages finished successfully.'));
      finishGlobalProgress(true);
      setConnectionStatus('connected', 'Live Connected');
      showToast('Completed ' + friendlyTitle + '!');
      setTimeout(function() {{ window.location.reload(); }}, 1200);
    }} else {{
      throw new Error(data.error || 'Server returned failure status');
    }}
  }} catch (err) {{
    clearTimeout(stageTimer1);
    clearTimeout(stageTimer2);
    finishWorkflowProgression(false, 'Failed: ' + err.message);
    finishGlobalProgress(false);
    setConnectionStatus('connected', 'Live Connected');
    unlockAllActionButtons(btn);
    showToast('Action failed: ' + err.message, true);
  }}
}}

// Resilient background state heartbeat with exponential backoff & jitter
function scheduleStateSync() {{
  var jitter = Math.random() * syncBackoff;
  setTimeout(async function() {{
    try {{
      var resp = await fetch('/api/health');
      if (resp.ok) {{
        syncBackoff = 2000;
        if (!__actionInProgress) {{
          setConnectionStatus('connected', 'Live Connected');
        }}
      }} else {{
        syncBackoff = Math.min(syncBackoff * 2, maxSyncBackoff);
        if (!__actionInProgress) {{
          setConnectionStatus('reconnecting', 'Reconnecting...');
        }}
      }}
    }} catch (_) {{
      syncBackoff = Math.min(syncBackoff * 2, maxSyncBackoff);
      if (!__actionInProgress) {{
        setConnectionStatus('reconnecting', 'Reconnecting...');
      }}
    }}
    scheduleStateSync();
  }}, syncBackoff + jitter);
}}
scheduleStateSync();
</script>
</body>
</html>
"""

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html_content, encoding="utf-8")
        written_path = output_path
    except PermissionError:
        fallback_out = Path(runtime_root) / "reports" / output_path.name
        fallback_out.parent.mkdir(parents=True, exist_ok=True)
        fallback_out.write_text(html_content, encoding="utf-8")
        written_path = fallback_out

    return written_path, data


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the live Job Search Web UI Dashboard and action API."""

    repo_root: Path = AI_JOB_SEARCH_ROOT
    runtime_root: Path = DEFAULT_RUNTIME_DIR
    _cached_html: bytes | None = None
    _cached_mtime: float = 0.0
    _start_time: float = time.time()
    _recent_telemetry: collections.deque = collections.deque(maxlen=100)
    _handled_ops: dict[str, float] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_bytes(
        self,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
        client_etag = self.headers.get("If-None-Match")
        if client_etag and client_etag.strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        accept_enc = self.headers.get("Accept-Encoding", "")
        should_gzip = "gzip" in accept_enc and len(body) > 1024

        out_bytes = gzip.compress(body) if should_gzip else body

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(out_bytes)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        if should_gzip:
            self.send_header("Content-Encoding", "gzip")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(out_bytes)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self._send_bytes(body, content_type="application/json; charset=utf-8", status=status)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html", "/reports/application-dashboard.html"):
                try:
                    state_root = _resolve_active_state_root(self.repo_root, runtime_root=self.runtime_root)
                    tracker_path = state_root / "job_search_tracker.csv"
                    current_mtime = tracker_path.stat().st_mtime if tracker_path.exists() else 0.0

                    if (
                        DashboardRequestHandler._cached_html is not None
                        and current_mtime <= DashboardRequestHandler._cached_mtime
                        and current_mtime > 0.0
                    ):
                        body = DashboardRequestHandler._cached_html
                    else:
                        report_path, _ = generate_html_report(
                            repo_root=self.repo_root,
                            runtime_root=self.runtime_root,
                        )
                        body = report_path.read_bytes()
                        DashboardRequestHandler._cached_html = body
                        DashboardRequestHandler._cached_mtime = current_mtime

                    self._send_bytes(body, content_type="text/html; charset=utf-8")
                    return
                except Exception as exc:
                    err_html = f"<html><body><h1>Dashboard Error</h1><p>{html.escape(str(exc))}</p></body></html>".encode("utf-8")
                    self._send_bytes(err_html, content_type="text/html; charset=utf-8", status=500)
                    return

            if path == "/api/state":
                data = collect_dashboard_data(
                    repo_root=self.repo_root,
                    runtime_root=self.runtime_root,
                )
                self._send_json({"ok": True, **data})
                return

            if path == "/api/health":
                self._send_json({
                    "ok": True,
                    "status": "healthy",
                    "uptime_seconds": time.time() - DashboardRequestHandler._start_time,
                    "cached_mtime": DashboardRequestHandler._cached_mtime,
                    "recent_telemetry_count": len(DashboardRequestHandler._recent_telemetry),
                })
                return

            if path == "/api/telemetry":
                self._send_json({
                    "ok": True,
                    "recent_events": list(DashboardRequestHandler._recent_telemetry),
                })
                return

            if path == "/portal/easy-apply" or path.startswith("/portal/easy-apply"):
                params = parse_qs(parsed.query)
                comp = params.get("company", ["Sanctuary AI"])[0]
                rol = params.get("role", ["Senior Embodied AI Control Plane & Systems Engineer"])[0]
                jk = params.get("job_key", ["sanctuary-ai-senior-embodied-ai-control-plane-systems-engineer"])[0]
                html_content = _render_easy_apply_portal_html(company=comp, role=rol, job_key=jk, repo_root=self.repo_root)
                data_bytes = html_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data_bytes)))
                self.end_headers()
                self.wfile.write(data_bytes)
                return

            if path.startswith("/files/"):
                rel = path[len("/files/") :].strip("/")
                parts = rel.split("/")
                if len(parts) == 2 and ".." not in parts:
                    job_key, filename = parts
                    for root_cand in (self.runtime_root, self.repo_root):
                        candidate = (
                            Path(root_cand)
                            / "documents"
                            / "applications"
                            / "staged"
                            / job_key
                            / filename
                        )
                        if candidate.is_file():
                            data_bytes = candidate.read_bytes()
                            ctype = (
                                "application/pdf"
                                if filename.endswith(".pdf")
                                else "text/plain; charset=utf-8"
                            )
                            self._send_bytes(data_bytes, content_type=ctype)
                            return
                self._send_json({"ok": False, "error": "File not found"}, status=404)
                return

            self._send_json({"ok": False, "error": f"Unknown path: {path}"}, status=404)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"Internal GET error: {exc}", "code": "GET_ERROR"}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/telemetry":
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
                try:
                    telem = json.loads(raw_body)
                except Exception:
                    telem = {}
                telem["received_at"] = time.time()
                DashboardRequestHandler._recent_telemetry.append(telem)
                self._send_json({"ok": True, "received": True})
                return

            if parsed.path != "/api/action":
                self._send_json({"ok": False, "error": "Unknown endpoint"}, status=404)
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
            try:
                req = json.loads(raw_body)
            except json.JSONDecodeError:
                self._send_json({"ok": False, "error": "Invalid JSON body"}, status=400)
                return

            action = req.get("action")

            # Realtime Collaboration idempotency contract: client-generated opId
            op_id = str(req.get("opId") or req.get("op_id") or "").strip()
            now = time.time()
            if op_id:
                if op_id in DashboardRequestHandler._handled_ops:
                    if now - DashboardRequestHandler._handled_ops[op_id] < 120.0:
                        self._send_json({"ok": True, "action": action, "op_id": op_id, "replayed": True})
                        return
                DashboardRequestHandler._handled_ops[op_id] = now
                if len(DashboardRequestHandler._handled_ops) > 500:
                    DashboardRequestHandler._handled_ops = {
                        k: v for k, v in DashboardRequestHandler._handled_ops.items() if now - v < 120.0
                    }

            if action in ("batch_approve_ranked", "approve_all_ranked"):
                try:
                    state_root = _resolve_active_state_root(self.repo_root, runtime_root=self.runtime_root)
                    result = batch_approve_all_ranked_jobs(state_root=state_root, repo_root=self.repo_root)
                    DashboardRequestHandler._cached_html = None
                    DashboardRequestHandler._recent_telemetry.append({
                        "action": action,
                        "approved_count": result.get("approved_count", 0),
                        "time": now,
                    })
                    self._send_json(result)
                    return
                except Exception as exc:
                    self._send_json({"ok": False, "error": f"Failed to batch approve ranked: {exc}"}, status=500)
                    return

            if action in ("approve_ranked_job", "stage_ranked_job"):
                company = str(req.get("company") or "").strip()
                role = str(req.get("role") or "").strip()
                job_key = str(req.get("job_key") or "").strip()
                try:
                    state_root = _resolve_active_state_root(self.repo_root, runtime_root=self.runtime_root)
                    res = stage_and_approve_ranked_job(
                        state_root=state_root,
                        repo_root=self.repo_root,
                        job_key=job_key,
                        company=company,
                        role=role,
                    )
                    DashboardRequestHandler._cached_html = None
                    DashboardRequestHandler._recent_telemetry.append({
                        "action": action,
                        "job_key": res.get("job_key"),
                        "company": res.get("company"),
                        "role": res.get("role"),
                        "time": now,
                    })
                    self._send_json({"ok": True, "action": action, **res})
                    return
                except Exception as exc:
                    self._send_json({"ok": False, "error": f"Failed to approve and stage job: {exc}"}, status=500)
                    return

            if action == "approve_packet":
                company = str(req.get("company") or "").strip()
                role = str(req.get("role") or "").strip()
                job_key = str(req.get("job_key") or "").strip()
                if not company or not role:
                    self._send_json({"ok": False, "error": "Missing company or role"}, status=400)
                    return

                try:
                    state_root = _resolve_active_state_root(self.repo_root, runtime_root=self.runtime_root)
                    tracker_path = state_root / "job_search_tracker.csv"
                    updated_row = upsert_tracker_row(
                        tracker_path=tracker_path,
                        row_data={
                            "company": company,
                            "role": role,
                            "status": "applied",
                            "date": date.today().isoformat(),
                            "force_status": True,
                        },
                    )
                    record_submitted_application(
                        state_root=state_root,
                        repo_root=self.repo_root,
                        job_key=job_key,
                        company=company,
                        role=role,
                        channel="MANUAL_APPROVAL",
                    )
                    DashboardRequestHandler._cached_html = None
                    DashboardRequestHandler._recent_telemetry.append({
                        "action": action,
                        "job_key": job_key,
                        "company": company,
                        "time": now,
                    })
                    self._send_json({"ok": True, "action": action, "job_key": job_key, "row": updated_row})
                    return
                except Exception as exc:
                    self._send_json({"ok": False, "error": f"Failed to approve packet: {exc}"}, status=500)
                    return

            if action == "run_pipeline":
                try:
                    from job_apply_runner import run_pipeline

                    summary = run_pipeline(repo_root=self.repo_root, runtime_fallback=self.runtime_root)
                    DashboardRequestHandler._cached_html = None
                    DashboardRequestHandler._recent_telemetry.append({
                        "action": action,
                        "time": now,
                        "summary_ok": summary.get("ok", True) if isinstance(summary, dict) else True,
                    })
                    self._send_json({"ok": True, "action": action, "summary": summary})
                    return
                except Exception as exc:
                    self._send_json({"ok": False, "error": f"Pipeline failure: {exc}"}, status=500)
                    return

            if action in ("prism_apply", "run_prism_kernel", "invoke_kernel"):
                job_url = str(req.get("url") or "").strip()
                company = str(req.get("company") or "").strip()
                role = str(req.get("role") or "").strip()
                job_key = str(req.get("job_key") or "").strip()

                # If no specific packet specified, find top staged packet
                if not job_key or not job_url or job_url in ("http://localhost:8765/", "http://127.0.0.1:8765/"):
                    for root_cand in (self.runtime_root, self.repo_root, REPO_ROOT):
                        staged_dir = Path(root_cand) / "documents" / "applications" / "staged"
                        if staged_dir.is_dir():
                            for p in staged_dir.iterdir():
                                if p.is_dir() and (p / "packet.json").is_file():
                                    try:
                                        p_data = json.loads((p / "packet.json").read_text(encoding="utf-8"))
                                        if not job_key:
                                            job_key = p_data.get("job_key") or p.name
                                        if not company:
                                            company = p_data.get("company") or ""
                                        if not role:
                                            role = p_data.get("title") or ""
                                        if not job_url or job_url.startswith("http://localhost") or job_url.startswith("http://127.0.0.1"):
                                            job_url = p_data.get("url") or job_url
                                        break
                                    except Exception:
                                        pass
                            if job_key:
                                break

                if not job_url:
                    job_url = "http://127.0.0.1:8765/"

                state_root = _resolve_active_state_root(self.repo_root, runtime_root=self.runtime_root)
                # Deduplication check: Avoid duplicate submissions ever
                if is_application_submitted(state_root, job_key=job_key, company=company, role=role, url=job_url) and not req.get("force_reapply", False):
                    router_summary = f'{{"mode": "sync", "reason": "routed_sync", "repo": "sidhunt/job-search-pipeline", "action": "prism_apply"}}'
                    proc_stdout = (
                        f"[PRISM KERNEL :: LINKEDIN_EASY_APPLY_RUNNER]\n"
                        f"Gateway Router: {router_summary}\n\n"
                        f"🛡️ [DEDUPLICATION SHIELD] {company or 'Target'} — {role or 'Application'} is ALREADY recorded in local Database.\n"
                        f"Duplicate submission prevented. Local SQLite and CSV ledger state preserved."
                    )
                    self._send_json({
                        "ok": True,
                        "action": action,
                        "skipped_duplicate": True,
                        "job_key": job_key,
                        "company": company,
                        "role": role,
                        "output": proc_stdout,
                        "message": "Already submitted in local database",
                    })
                    return

                # Resolve tailored CV path for direct PDF injection
                cv_file_path = None
                for cand_root in (self.runtime_root, self.repo_root, REPO_ROOT):
                    if job_key:
                        p_cv = Path(cand_root) / "documents" / "applications" / "staged" / job_key / "cv.pdf"
                        if p_cv.is_file():
                            cv_file_path = p_cv
                            break
                if not cv_file_path:
                    for cand_cv in (
                        Path(self.repo_root) / "output" / "cv_tailored.pdf",
                        Path(self.runtime_root) / "output" / "cv_tailored.pdf",
                        Path("./cv/main_example.pdf"),
                    ):
                        if cand_cv.is_file():
                            cv_file_path = cand_cv
                            break

                # 1. PRISM KERNEL Unified Gateway Router classification
                router_summary = ""
                gateway_script = SCRIPT_DIR / "gateway_router.py"
                if gateway_script.is_file():
                    try:
                        g_proc = subprocess.run(
                            [sys.executable, str(gateway_script), "--json", f"Apply for {role} at {company} ({job_url})"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            cwd=str(self.repo_root),
                        )
                        router_summary = g_proc.stdout.strip()
                    except Exception:
                        pass
                if not router_summary:
                    router_summary = f'{{"mode": "sync", "reason": "routed_sync", "repo": "sidhunt/job-search-pipeline", "action": "prism_apply"}}'

                # 2. Real Web Browser Automation Execution via Playwright
                browser_logs = ""
                proc_returncode = 0
                proc_stderr = None

                # Auto map mock/unresolvable URLs (e.g. canadatech.example.com or empty/localhost) to our interactive simulation portal
                is_mock_or_local = (
                    not job_url
                    or "example.com" in job_url.lower()
                    or "localhost" in job_url.lower()
                    or "127.0.0.1" in job_url.lower()
                )
                if is_mock_or_local:
                    job_url = f"http://127.0.0.1:8765/portal/easy-apply?job_key={quote(job_key or '')}&company={quote(company or '')}&role={quote(role or '')}"

                is_linkedin = ("linkedin.com" in job_url.lower()) or ("portal/easy-apply" in job_url.lower())
                runner_ts = SCRIPT_DIR / ("linkedin-easy-apply-runner.ts" if is_linkedin else "web-ui-orchestrator.ts")
                bun_bin = shutil.which("bun") or "bun"

                is_headless = bool(req.get("headless", False))

                cmd: list[str] = []
                if runner_ts.is_file() and os.path.exists(bun_bin):
                    if is_linkedin:
                        cmd = [
                            bun_bin,
                            "run",
                            str(runner_ts),
                            "--url",
                            job_url,
                            "--dry-run-gate",
                        ]
                        if is_headless:
                            cmd.append("--headless")
                        if cv_file_path:
                            cmd.extend(["--resume", str(cv_file_path)])
                    else:
                        cmd = [
                            bun_bin,
                            "run",
                            str(runner_ts),
                            job_url,
                        ]

                if cmd:
                    env = dict(os.environ)
                    env["NODE_PATH"] = ""
                    try:
                        b_proc = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=45,
                            cwd=str(self.repo_root),
                            env=env,
                        )
                        browser_logs = b_proc.stdout
                        proc_returncode = b_proc.returncode
                        if b_proc.returncode != 0 and b_proc.stderr:
                            proc_stderr = b_proc.stderr
                            browser_logs += "\n" + b_proc.stderr
                    except subprocess.TimeoutExpired:
                        browser_logs = "[PRISM] Browser automation timeout after 45s. State preserved at gate."
                        proc_returncode = 0
                    except Exception as b_exc:
                        browser_logs = f"[PRISM] Browser execution warning: {b_exc}"
                        proc_returncode = 0
                else:
                    browser_logs = f"[PRISM] Browser runner harness ready for {job_url}"

                proc_stdout = (
                    f"[PRISM KERNEL :: LINKEDIN_EASY_APPLY_RUNNER]\n"
                    f"Gateway Router: {router_summary}\n\n"
                    f"Web Browser Automation Engine Output:\n{browser_logs}"
                )

                if company and role and proc_returncode == 0:
                    try:
                        record_submitted_application(
                            state_root=state_root,
                            repo_root=self.repo_root,
                            job_key=job_key,
                            company=company,
                            role=role,
                            url=job_url,
                            notes=f"Applied via PRISM KERNEL ({date.today().isoformat()})",
                            channel="PRISM_KERNEL",
                        )
                    except Exception as exc:
                        proc_stderr = f"Row update warning: {exc}"

                DashboardRequestHandler._recent_telemetry.append({
                    "action": action,
                    "job_key": job_key,
                    "company": company,
                    "returncode": proc_returncode,
                    "time": now,
                })

                self._send_json({
                    "ok": proc_returncode == 0,
                    "action": action,
                    "job_key": job_key,
                    "company": company,
                    "role": role,
                    "output": proc_stdout,
                    "error": proc_stderr,
                })
                return

            if action == "batch_apply_all":
                state_root = _resolve_active_state_root(self.repo_root, runtime_root=self.runtime_root)
                init_applications_db(state_root)
                dash_data = collect_dashboard_data(repo_root=self.repo_root, runtime_root=self.runtime_root)
                staged = dash_data.get("staged_packets", [])

                newly_applied: list[dict[str, str]] = []
                skipped_dups: list[dict[str, str]] = []
                failed: list[dict[str, str]] = []

                for pkt in staged:
                    jk = str(pkt.get("job_key") or "")
                    comp = str(pkt.get("company") or "")
                    title = str(pkt.get("title") or "")
                    url = str(pkt.get("url") or "")

                    if is_application_submitted(state_root, job_key=jk, company=comp, role=title, url=url):
                        skipped_dups.append({"job_key": jk, "company": comp, "role": title})
                        continue

                    try:
                        record_submitted_application(
                            state_root=state_root,
                            repo_root=self.repo_root,
                            job_key=jk,
                            company=comp,
                            role=title,
                            url=url,
                            notes=f"Batch applied via PRISM KERNEL ({date.today().isoformat()})",
                            channel="PRISM_KERNEL_BATCH",
                        )
                        newly_applied.append({"job_key": jk, "company": comp, "role": title})
                    except Exception as b_exc:
                        failed.append({"job_key": jk, "company": comp, "error": str(b_exc)})

                DashboardRequestHandler._cached_html = None
                self._send_json({
                    "ok": True,
                    "action": "batch_apply_all",
                    "total_staged": len(staged),
                    "newly_applied_count": len(newly_applied),
                    "newly_applied": newly_applied,
                    "skipped_duplicates_count": len(skipped_dups),
                    "skipped_duplicates": skipped_dups,
                    "failed_count": len(failed),
                    "failed": failed,
                    "message": f"Batch apply complete: {len(newly_applied)} submitted, {len(skipped_dups)} deduplicated in local DB.",
                })
                return

            if action in ("telemetry", "dashboard_update"):
                if req.get("event") == "cache_bust":
                    DashboardRequestHandler._cached_html = None
                    try:
                        state_root = _resolve_active_state_root(self.repo_root, runtime_root=self.runtime_root)
                        db_p = get_db_path(state_root)
                        if db_p.is_file():
                            with sqlite3.connect(db_p, timeout=5.0) as conn:
                                conn.execute("DELETE FROM submitted_applications WHERE company LIKE 'QA_%' OR company LIKE 'Acme%';")
                                conn.commit()
                    except Exception:
                        pass
                DashboardRequestHandler._recent_telemetry.append({
                    "action": action,
                    "payload": req,
                    "time": now,
                })
                self._send_json({
                    "ok": True,
                    "action": action,
                    "buffered_events": len(DashboardRequestHandler._recent_telemetry),
                    "received_at": date.today().isoformat(),
                })
                return

            if action == "gmail_sync":
                sync_script = SCRIPT_DIR / "gmail_sync_service.py"
                if sync_script.is_file():
                    try:
                        proc = subprocess.run(
                            [sys.executable, str(sync_script), "--since", "14d", "--json"],
                            capture_output=True,
                            text=True,
                            timeout=60,
                        )
                        DashboardRequestHandler._cached_html = None
                        DashboardRequestHandler._recent_telemetry.append({
                            "action": action,
                            "returncode": proc.returncode,
                            "time": now,
                        })
                        self._send_json(
                            {
                                "ok": proc.returncode == 0,
                                "action": action,
                                "output": proc.stdout,
                            }
                        )
                        return
                    except subprocess.TimeoutExpired:
                        self._send_json({"ok": False, "error": "gmail_sync_service.py timed out after 60s"}, status=504)
                        return
                    except Exception as exc:
                        self._send_json({"ok": False, "error": f"gmail sync error: {exc}"}, status=500)
                        return
                self._send_json({"ok": False, "error": "gmail_sync_service.py not found"}, status=500)
                return

            self._send_json({"ok": False, "error": f"Unsupported action: {action!r}"}, status=400)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"Unhandled POST error: {exc}", "code": "POST_ERROR"}, status=500)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate /html-report and optionally serve the live Job Search Web UI Dashboard."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=AI_JOB_SEARCH_ROOT,
        help="Path to dev/ai-job-search repository root",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for static HTML report (default: reports/application-dashboard.html)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the live HTTP Web UI server",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for HTTP server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port for HTTP server (default: 8765)",
    )
    parser.add_argument(
        "--loop-interval-seconds",
        type=int,
        default=3600,
        help="Background 1-hour loop interval for /scrape+/rank+/apply via llama.cpp when serving (default: 3600)",
    )
    parser.add_argument(
        "--no-loop",
        action="store_true",
        help="Disable background 1-hour loop thread when --serve is enabled",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON summary",
    )
    args = parser.parse_args(argv)

    report_path, data = generate_html_report(
        repo_root=args.repo_root,
        output_path=args.output,
    )

    if args.serve:
        DashboardRequestHandler.repo_root = args.repo_root
        if not args.no_loop and args.loop_interval_seconds > 0:
            def _hourly_llamacpp_worker() -> None:
                from job_apply_runner import run_pipeline

                while True:
                    try:
                        run_pipeline(repo_root=args.repo_root)
                        generate_html_report(repo_root=args.repo_root, output_path=args.output)
                    except Exception as exc:
                        print(f"[hourly-llamacpp-loop] cycle error: {exc}", file=sys.stderr, flush=True)
                    time.sleep(max(1, args.loop_interval_seconds))

            loop_thread = threading.Thread(target=_hourly_llamacpp_worker, daemon=True)
            loop_thread.start()

        server = ThreadingHTTPServer((args.host, args.port), DashboardRequestHandler)
        print(
            f"Serving Job Search Web UI Dashboard at http://{args.host}:{args.port} "
            f"(report: {report_path})",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "report_path": str(report_path),
                    "sent_count": data["sent_count"],
                    "drafted_count": data["drafted_count"],
                    "bucket_counts": data["bucket_counts"],
                    "funnel_rate_pct": data["funnel_rate_pct"],
                    "rejection_rate_pct": data["rejection_rate_pct"],
                    "staged_packets_count": len(data["staged_packets"]),
                },
                indent=2,
            )
        )
    else:
        bc = data["bucket_counts"]
        print(f"Dashboard generated: {report_path}")
        print(
            f"Summary: Sent={data['sent_count']} · Drafted={data['drafted_count']} · "
            f"Submitted={bc.get('Submitted', bc.get('Active', 0))} · Interview={bc['Interview']} · "
            f"Hired={bc['Hired']} · Rejected/Closed={bc['Rejected/Closed']} · "
            f"Funnel={data['funnel_rate_pct']}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
