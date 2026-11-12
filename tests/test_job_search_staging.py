#!/usr/bin/env python3
"""Seam tests for `JobSearchStagingPipeline` (`scripts/job_search_staging.py`).

Tests:
1. Honesty-Floor invariant validation:
   - Rejects `TransUnion` (case-insensitive), `"autism diagnosed"`, `"US Citizen"`,
     and non-ASCII date range dashes (`U+2013`, `U+2014`).
   - Accepts `SLAM Technologies Ltd. / Roth IAMS Ltd.`, `DISSID LABS INC.`,
     and ASCII hyphen date ranges (`2022-07 - 2026-01`).
2. Work-Authorization Eligibility Gate (`evaluate_work_auth_eligibility`):
   - `PASS` for Canadian remote/hybrid roles (`CANADA_DIRECT`) and US/Global B2B C2C / EOR roles (`B2B_C2C_OR_EOR`).
   - `FAIL` for US W-2 only, US Citizenship required, Green Card only, or US Security Clearance roles.
3. End-to-end Staged-Packet compilation & `verify_pdf.py` validation:
   - Compiles `cv.pdf` (2 pages) and `cover_letter.pdf` (1 page) from the configured
     master templates (`dev/ai-job-search/cv/main_example.tex` and `cover_letters/cover_example.tex`)
     and verifies them through `dev/ai-job-search/tools/verify_pdf.py --ascii-dates`.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from scripts.job_search_staging import (
    AI_JOB_SEARCH_ROOT,
    JobSearchStagingPipeline,
    evaluate_work_auth_eligibility,
    validate_honesty_floor,
)


def test_honesty_floor_accepts_verified_ssot_templates() -> None:
    cv_tex = (AI_JOB_SEARCH_ROOT / "cv" / "main_example.tex").read_text(encoding="utf-8")
    cl_tex = (AI_JOB_SEARCH_ROOT / "cover_letters" / "cover_example.tex").read_text(
        encoding="utf-8"
    )

    cv_ok, cv_violations = validate_honesty_floor(cv_tex)
    assert cv_ok is True, f"Master CV template failed honesty floor: {cv_violations}"
    assert "SLAM Technologies Ltd. / Roth IAMS Ltd." in cv_tex
    assert "DISSID LABS INC." in cv_tex

    cl_ok, cl_violations = validate_honesty_floor(cl_tex)
    assert cl_ok is True, f"Master Cover Letter template failed honesty floor: {cl_violations}"


@pytest.mark.parametrize(
    ("bad_snippet", "expected_keyword"),
    [
        (
            "Senior Software Engineer at TransUnion (2022-07 - 2026-01)",
            "TransUnion",
        ),
        (
            "Candidate has autism diagnosed and ADHD combined type.",
            "autism diagnosed",
        ),
        (
            "Work Authorization: US Citizen available immediately.",
            "US Citizen",
        ),
        (
            "Full-Stack Software Developer at SLAM Technologies Ltd. (2022\u20132026)",
            "Non-ASCII dash",
        ),
    ],
)
def test_honesty_floor_rejects_forbidden_claims_and_unicode_date_dashes(
    bad_snippet: str, expected_keyword: str
) -> None:
    ok, violations = validate_honesty_floor(bad_snippet)
    assert ok is False
    assert any(expected_keyword.lower() in v.lower() for v in violations), violations


def test_work_auth_eligibility_gate_pass_and_fail_cases() -> None:
    # 1. Canadian remote role -> PASS (CANADA_DIRECT)
    ca_res = evaluate_work_auth_eligibility(
        posting_text="Seeking Senior AI Engineer in Waterloo, ON or Remote within Canada.",
        location="Waterloo, ON, Canada",
    )
    assert ca_res["verdict"] == "PASS"
    assert ca_res["engagement_mode"] == "CANADA_DIRECT"

    # 2. US Remote role open to B2B C2C / Deel / Canada -> PASS (B2B_C2C_OR_EOR)
    c2c_res = evaluate_work_auth_eligibility(
        posting_text="Remote Senior Full-Stack Engineer. Open to B2B Corp-to-Corp (C2C) or Deel contractors in North America.",
        location="Remote",
    )
    assert c2c_res["verdict"] == "PASS"
    assert c2c_res["engagement_mode"] == "B2B_C2C_OR_EOR"

    # 3. US W-2 only / no C2C -> FAIL
    w2_res = evaluate_work_auth_eligibility(
        posting_text="Remote role. Must reside in the United States. W-2 only, no C2C or Corp-to-Corp.",
        location="United States (Remote)",
    )
    assert w2_res["verdict"] == "FAIL"
    assert w2_res["engagement_mode"] == "INELIGIBLE"

    # 4. US Citizenship / Clearance required -> FAIL
    clearance_res = evaluate_work_auth_eligibility(
        posting_text="AI Systems Engineer. Must be a U.S. Citizen with an active TS/SCI clearance.",
        location="Arlington, VA",
    )
    assert clearance_res["verdict"] == "FAIL"


def test_stage_and_verify_application_packet_end_to_end(tmp_path: Path) -> None:
    pipeline = JobSearchStagingPipeline(repo_root=tmp_path)
    cv_tex = (AI_JOB_SEARCH_ROOT / "cv" / "main_example.tex").read_text(encoding="utf-8")
    cl_tex = (AI_JOB_SEARCH_ROOT / "cover_letters" / "cover_example.tex").read_text(
        encoding="utf-8"
    )

    job = {
        "job_key": "test-canada-ai-engineer-01",
        "title": "Senior Agentic AI Engineer",
        "company": "NorthStar AI Labs",
        "location": "Toronto, ON, Canada (Remote)",
        "portal": "linkedin",
        "url": "https://www.linkedin.com/jobs/view/999999",
        "description": "Senior Agentic AI Engineer (TypeScript, Python, MCP, React). Remote in Canada.",
    }
    evaluation = {
        "score": 94,
        "summary": "High-signal match for multi-agent orchestration and full-stack TypeScript/Python.",
    }

    packet_dir = pipeline.stage_application_packet(
        job=job,
        evaluation=evaluation,
        cv_tex=cv_tex,
        cover_letter_tex=cl_tex,
        staging_root=tmp_path / "applications" / "staged",
        compile_pdfs=True,
    )

    assert (packet_dir / "packet.json").is_file()
    assert (packet_dir / "evaluation.md").is_file()
    assert (packet_dir / "cv.pdf").is_file()
    assert (packet_dir / "cover_letter.pdf").is_file()
    assert (tmp_path / "job_search_tracker.csv").is_file()
    assert (
        tmp_path
        / "documents"
        / "applications"
        / "northstar_ai_labs_senior_agentic_ai_engineer"
        / "job_posting.md"
    ).is_file()

    ok, errors = pipeline.verify_staged_packet(packet_dir)
    assert ok is True, f"Staged packet verification failed: {errors}"


def test_stage_application_packet_rejects_transunion_or_ineligible_job(
    tmp_path: Path,
) -> None:
    pipeline = JobSearchStagingPipeline()
    cv_tex = (AI_JOB_SEARCH_ROOT / "cv" / "main_example.tex").read_text(encoding="utf-8")
    cl_tex = (AI_JOB_SEARCH_ROOT / "cover_letters" / "cover_example.tex").read_text(
        encoding="utf-8"
    )

    # Rejects TransUnion in CV
    with pytest.raises(ValueError, match="TransUnion"):
        pipeline.stage_application_packet(
            job={
                "job_key": "bad-cv-job",
                "title": "AI Engineer",
                "company": "Acme",
                "location": "Canada",
                "description": "Remote in Canada",
            },
            evaluation={"score": 80},
            cv_tex=cv_tex + "\n% TransUnion\nWorked at TransUnion.",
            cover_letter_tex=cl_tex,
            staging_root=tmp_path,
            compile_pdfs=False,
        )

    # Rejects US W-2-only job posting
    with pytest.raises(ValueError, match="Work-Authorization Gate"):
        pipeline.stage_application_packet(
            job={
                "job_key": "us-w2-only-job",
                "title": "AI Engineer",
                "company": "US Corp",
                "location": "United States",
                "description": "W-2 only, no C2C. Must reside in the United States.",
            },
            evaluation={"score": 80},
            cv_tex=cv_tex,
            cover_letter_tex=cl_tex,
            staging_root=tmp_path,
            compile_pdfs=False,
        )
