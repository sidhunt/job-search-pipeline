# Autonomous Job Search Pipeline & Deterministic Harness

A production-grade, local-first autonomous job application, ranking, verification, and webform submission pipeline with an interactive live UI dashboard.

---

## 🌟 Key Architecture & Capabilities

```
[Job Discovery & Scrape] 
         │
         ▼
[Gate 1: Work-Auth & Honesty Floor] ──── Rejected / Non-compliant (Pruned)
         │
         ▼ (Eligible)
[4-Tier Weighted Reranking Engine] ───── Weak / Poor Fit (Archived)
         │
         ▼ (Strong / Moderate Fit)
[LaTeX Staging & ATS PDF Verification] ── cv.pdf (2 pages) + cover_letter.pdf (1 page)
         │
         ▼
[Live Dashboard & Human Approval Gate] ── http://127.0.0.1:8765/
         │
         ▼ (Approved)
[Playwright Interactive Webform Runner] ─ Modal solving, field injection, submit verification
```

### 1. Live UI Dashboard Server (`http://127.0.0.1:8765/`)
* **Self-Contained Report Engine**: Parses the 14 canonical columns of `job_search_tracker.csv` and merges historical stages from `outcome.md`.
* **Dynamic Inline SVG Charts**: Real-time visualization for Application Status Breakdown, Distribution by Sector, Sourcing Channel, and Historical Application Funnel.
* **REST & Telemetry API**: Full OpenAPI 3.0.3 specification (`openapi_job_search_harness.yaml`) exposing `/api/state`, `/api/action`, and `/files/<job_key>/<file>`.

### 2. Deterministic 4-Tier Reranking Engine
Scores scraped job postings using a multi-category weighted rubric:
* **Technical Stack Alignment**: 30%
* **Experience & Seniority Fit**: 25%
* **Behavioral & Culture Match**: 15%
* **Career Growth & Strategic Velocity**: 30%
* **Classification Bands**: `Strong Fit` (≥75), `Good Fit` (≥60), `Moderate Fit` (≥45), `Weak Fit` (≥30), `Poor Fit` (<30).

### 3. Work-Auth Eligibility & Honesty-Floor Gate
* **Gate 1**: Automatic deterministic discard of US W-2-only roles requiring US physical presence, US Citizenship/Green Card, or Security Clearance.
* **Honesty Floor**: Hard invariant verification rejecting forbidden hallucinations, false citizenship claims, and enforcing strict ATS ASCII date ranges.

### 4. Interactive Webform & Easy Apply Automation
* Built on Playwright with persistent context support.
* Decoupled candidate profile loader with zero hardcoded credentials.
* Simulated keystroke delays, radio and select input resolution, and automated file input injection for compiled PDFs.

---

## 🚀 Quickstart

### Prerequisites
* Python 3.10+
* Bun or Node.js 20+
* Playwright

### Installation
```bash
# Clone the repository
git clone https://github.com/sidhunt/job-search-pipeline.git
cd job-search-pipeline

# Configure candidate profile
cp config/candidate_profile.example.json config/candidate_profile.json
# Edit config/candidate_profile.json with your actual profile info

# Configure environment variables
cp .env.example .env

# Install Playwright dependencies
bun install
bunx playwright install chromium
```

### Running the Services

1. **Launch the Live UI Dashboard**:
   ```bash
   python3 src/job_dashboard_server.py --port 8765
   ```
   Open `http://127.0.0.1:8765/` in your browser.

2. **Execute Candidate Reranking & Staging**:
   ```bash
   python3 src/job_apply_runner.py
   ```

3. **Run Interactive Webform Automation**:
   ```bash
   bun automation/webform_interactive_runner.ts
   ```

---

## 🔒 Privacy & Safety Invariant

* **No Blind Submissions**: All automation runners enforce human review gates prior to final form dispatch.
* **Zero Credential Leaks**: Candidate identity is decoupled via `config/candidate_profile.json` (gitignored).
* **Local First**: All evaluation metrics, databases, and LLM endpoints default to local inference without sending telemetry to external services.

---

## 📄 License
MIT License.
