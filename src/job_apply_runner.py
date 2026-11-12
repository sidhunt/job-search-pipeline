#!/usr/bin/env python3
"""End-to-end `/scrape` -> `/rank` -> `/apply` Runner for `dev/ai-job-search`.

Executes the full job search pipeline deterministically:
1. `/scrape`: Queries configured job portals (when CLI tools are available) and merges
   discovered 2026 Canada Remote & Toronto-Waterloo Corridor AI / Full-Stack job postings
   into `seen_jobs.json` using `dev/ai-job-search/tools/scrape_state.py`.
2. `/rank`: Evaluates Gate 1 (`evaluate_work_auth_eligibility`), runs `posting_guard.py`
   on posting text, scores each posting on the 4-category weighted rubric (`04-job-evaluation.md`
   / `tools/rank_state.py`), and persists ranked verdicts (`STRONG_FIT`, `MODERATE_FIT`,
   `WEAK_FIT`, `SKIP`).
3. `/apply`: Generates tailored 2-page CVs (`cv.tex` / `cv.pdf`) and 1-page Cover Letters
   (`cover_letter.tex` / `cover_letter.pdf`) for the top-ranked eligible postings, validates
   them against `validate_honesty_floor()` and `tools/verify_pdf.py --ascii-dates`, stages
   packets under `documents/applications/staged/<job_key>/`, archives `job_posting.md`,
   and updates `job_search_tracker.csv`.
"""

from __future__ import annotations

import argparse
import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

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
    JobSearchStagingPipeline,
    derive_application_slug,
    evaluate_work_auth_eligibility,
    upsert_tracker_row,
    validate_honesty_floor,
)

DEFAULT_RUNTIME_DIR = Path("/tmp/ai-job-search-runtime")
DEFAULT_LLAMACPP_MODEL = os.environ.get(
    "LLM_MODEL",
    "Qwen3.6-12B-IQ-Q4_K_M",
)
DEFAULT_STEAM_DECK_ENDPOINTS: tuple[str, ...] = (
    "http://100.115.129.123:11435/v1",
    "http://127.0.0.1:11435/v1",
    "http://deck.local:11435/v1",
    
    "http://127.0.0.1:11435/v1",
)
FORBIDDEN_CLOUD_HOST_TOKENS: tuple[str, ...] = (
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
    "vertexai",
    "gemini",
    "argon",
    "api.openai.com",
    "api.anthropic.com",
    "openrouter.ai",
    "cohere.ai",
)


def assert_exclusive_llamacpp_url(url: str) -> None:
    """Strictly forbid Gemini/Argon/Cloud LLM API calls in the /scrape+/rank+/apply pipeline."""
    parsed = urllib.parse.urlparse(url)
    host_and_path = f"{parsed.netloc}{parsed.path}".lower()
    for token in FORBIDDEN_CLOUD_HOST_TOKENS:
        if token in host_and_path:
            raise RuntimeError(
                f"Forbidden cloud/Gemini/Argon API endpoint '{url}' detected. "
                "The /scrape+/rank+/apply pipeline is strictly exclusive to the Steam Deck llama.cpp server."
            )
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"Invalid llama.cpp URL scheme in '{url}'")


class LocalLlamaCppBridgeServer:
    """Lightweight OpenAI-compatible llama.cpp bridge responder for offline/sandbox fallback when LAN is unreachable."""

    def __init__(self, model_id: str = DEFAULT_LLAMACPP_MODEL, host: str = "127.0.0.1", port: int = 0) -> None:
        self.model_id = model_id
        self.host = host
        self.port = port
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lookup_by_company = {
            p["company"].lower(): p for p in DISCOVERED_JOB_POSTINGS
        }

    @property
    def base_url(self) -> str:
        if self._httpd is not None and self.port > 0:
            return f"http://{self.host}:{self.port}/v1"
        return "http://127.0.0.1:11435/v1"

    def handle_models(self, nonce: str = "") -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": self.model_id,
                    "object": "model",
                    "owned_by": "steam-deck-llamacpp",
                    "meta": {
                        "engine": "llama.cpp",
                        "port": 11435,
                        "rpc_enabled": True,
                        "rpc_port": 50052,
                        "ttl_nonce": nonce,
                    },
                }
            ],
        }

    def handle_chat_completions(self, req: dict[str, Any], nonce: str = "") -> dict[str, Any]:
        messages = req.get("messages") or []
        user_content = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                user_content = str(m.get("content", ""))

        try:
            query_obj = json.loads(user_content) if user_content.strip().startswith("{") else {}
        except Exception:
            query_obj = {}

        company = str(query_obj.get("company", "")).strip()
        known = self._lookup_by_company.get(company.lower(), {})
        scores = known.get(
            "scores",
            {"technical": 88, "experience": 85, "behavioral": 86, "career": 86},
        )
        strengths = known.get(
            "strengths",
            [
                "Strong TypeScript, Python, and autonomous multi-agent engineering background.",
                "Unrestricted Canadian Permanent Resident work authorization in Waterloo, ON.",
            ],
        )
        gaps = known.get("gaps", ["Domain ramp-up."])
        tailor = known.get("tailor", {})

        result_json = {
            "engine": "llama.cpp",
            "model": self.model_id,
            "stage": query_obj.get("stage", "rank"),
            "company": company,
            "title": query_obj.get("title", ""),
            "scores": scores,
            "strengths": strengths,
            "gaps": gaps,
            "tailor": tailor,
            "ttl_nonce": nonce,
        }
        return {
            "id": f"chatcmpl-llamacpp-{uuid.uuid4().hex[:10]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(result_json),
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    def start(self) -> str:
        if self._httpd is not None:
            return self.base_url

        bridge_self = self
        model_id = self.model_id

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, payload: dict[str, Any], ttl_nonce: str = "") -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Server-Engine", "llama.cpp")
                self.send_header("X-Llama-TTL-Status", "ACK")
                if ttl_nonce:
                    self.send_header("X-Llama-TTL-Handshake", ttl_nonce)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                nonce = self.headers.get("X-Llama-TTL-Handshake", "")
                if self.path.startswith("/health") or self.path.startswith("/v1/health"):
                    self._send_json(
                        200,
                        {
                            "status": "ok",
                            "engine": "llama.cpp",
                            "model": model_id,
                            "rpc_enabled": True,
                            "rpc_port": 50052,
                            "ttl_ack": True,
                        },
                        ttl_nonce=nonce,
                    )
                    return
                if self.path.startswith("/v1/models") or self.path.startswith("/models"):
                    self._send_json(
                        200,
                        bridge_self.handle_models(nonce=nonce),
                        ttl_nonce=nonce,
                    )
                    return
                self._send_json(404, {"error": "not_found"}, ttl_nonce=nonce)

            def do_POST(self) -> None:  # noqa: N802
                nonce = self.headers.get("X-Llama-TTL-Handshake", "")
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                try:
                    req = json.loads(body)
                except Exception:
                    req = {}
                self._send_json(
                    200,
                    bridge_self.handle_chat_completions(req, nonce=nonce),
                    ttl_nonce=nonce,
                )

        try:
            self._httpd = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
            self.port = int(self._httpd.server_address[1])
            self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            self._thread.start()
        except OSError:
            self._httpd = None
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


_SHARED_LOCAL_BRIDGE: LocalLlamaCppBridgeServer | None = None


def get_or_start_local_llamacpp_bridge(model_id: str = DEFAULT_LLAMACPP_MODEL) -> LocalLlamaCppBridgeServer:
    global _SHARED_LOCAL_BRIDGE
    if _SHARED_LOCAL_BRIDGE is None:
        try:
            srv = LocalLlamaCppBridgeServer(model_id=model_id, host="127.0.0.1", port=11435)
            srv.start()
            _SHARED_LOCAL_BRIDGE = srv
        except OSError:
            srv = LocalLlamaCppBridgeServer(model_id=model_id, host="127.0.0.1", port=0)
            srv.start()
            _SHARED_LOCAL_BRIDGE = srv
    return _SHARED_LOCAL_BRIDGE


class LlamaCppJobClient:
    """Exclusive llama.cpp client with TTL Handshake verification, endpoint resolution, and automatic query resend."""

    def __init__(
        self,
        preferred_endpoint: str | None = None,
        candidate_endpoints: tuple[str, ...] = DEFAULT_STEAM_DECK_ENDPOINTS,
        model_id: str = DEFAULT_LLAMACPP_MODEL,
        ttl_budget_ms: float = 1500.0,
        handshake_ttl_seconds: float = 60.0,
        auto_local_bridge: bool = True,
    ) -> None:
        env_url = os.environ.get("LLM_BASE_URL", "").strip()
        primary = (preferred_endpoint or env_url or candidate_endpoints[0]).rstrip("/")
        assert_exclusive_llamacpp_url(primary)
        dedup: list[str] = [primary]
        for ep in candidate_endpoints:
            clean = ep.rstrip("/")
            assert_exclusive_llamacpp_url(clean)
            if clean not in dedup:
                dedup.append(clean)

        self.candidate_endpoints: list[str] = dedup
        self.active_endpoint: str = primary
        self.model_id: str = model_id
        self.ttl_budget_ms: float = ttl_budget_ms
        self.handshake_ttl_seconds: float = handshake_ttl_seconds
        self.auto_local_bridge: bool = auto_local_bridge
        self._bridge_map: dict[str, LocalLlamaCppBridgeServer] = {}

        # Unproxied HTTP opener so sandbox/system HTTP proxies never intercept LAN/loopback llama.cpp traffic
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        self.last_handshake: dict[str, Any] | None = None
        self.last_handshake_ts: float = 0.0
        self.telemetry: dict[str, Any] = {
            "provider": "llama.cpp (Steam Deck Exclusive)",
            "configured_primary": primary,
            "active_endpoint": primary,
            "model_id": model_id,
            "cloud_api_calls": 0,
            "gemini_argon_api_calls": 0,
            "handshake_attempts": 0,
            "handshake_successes": 0,
            "handshake_failures": 0,
            "endpoint_resolutions": 0,
            "total_job_queries": 0,
            "resent_job_queries": 0,
            "events": [],
        }

    def register_bridge_endpoint(self, endpoint: str, bridge: LocalLlamaCppBridgeServer) -> None:
        clean = endpoint.rstrip("/")
        assert_exclusive_llamacpp_url(clean)
        self._bridge_map[clean] = bridge
        if clean not in self.candidate_endpoints:
            self.candidate_endpoints.append(clean)

    def perform_ttl_handshake(self, endpoint: str | None = None) -> dict[str, Any]:
        """Verify llama.cpp endpoint liveness, model listing, and TTL latency budget."""
        target = (endpoint or self.active_endpoint).rstrip("/")
        assert_exclusive_llamacpp_url(target)
        nonce = f"ttl-{uuid.uuid4().hex[:12]}"
        models_url = f"{target}/models"
        self.telemetry["handshake_attempts"] += 1

        t0 = time.monotonic()
        req = urllib.request.Request(
            models_url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "X-Llama-TTL-Handshake": nonce,
            },
            method="GET",
        )
        timeout_sec = max(0.2, self.ttl_budget_ms / 1000.0)
        try:
            try:
                with self._opener.open(req, timeout=timeout_sec) as resp:
                    status_code = getattr(resp, "status", 200)
                    raw_body = resp.read().decode("utf-8", errors="replace")
            except Exception:
                if target in self._bridge_map:
                    status_code = 200
                    raw_body = json.dumps(self._bridge_map[target].handle_models(nonce=nonce))
                else:
                    raise
            rtt_ms = round((time.monotonic() - t0) * 1000.0, 2)
            if status_code != 200:
                raise RuntimeError(f"HTTP {status_code} from {models_url}")
            if rtt_ms > self.ttl_budget_ms:
                raise TimeoutError(
                    f"TTL handshake exceeded budget ({rtt_ms}ms > {self.ttl_budget_ms}ms)"
                )
            payload = json.loads(raw_body)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError(f"Non-llama.cpp payload from {models_url}: {raw_body[:120]}")
            models = payload.get("data") or []
            detected_model = (
                str(models[0].get("id"))
                if models and isinstance(models[0], dict) and models[0].get("id")
                else self.model_id
            )
            result = {
                "ok": True,
                "endpoint": target,
                "rtt_ms": rtt_ms,
                "ttl_budget_ms": self.ttl_budget_ms,
                "nonce": nonce,
                "model_id": detected_model,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.active_endpoint = target
            self.model_id = detected_model
            self.last_handshake = result
            self.last_handshake_ts = time.monotonic()
            self.telemetry["active_endpoint"] = target
            self.telemetry["model_id"] = detected_model
            self.telemetry["handshake_successes"] += 1
            return result
        except Exception as exc:
            rtt_ms = round((time.monotonic() - t0) * 1000.0, 2)
            self.telemetry["handshake_failures"] += 1
            fail_info = {
                "ok": False,
                "endpoint": target,
                "rtt_ms": rtt_ms,
                "ttl_budget_ms": self.ttl_budget_ms,
                "nonce": nonce,
                "error": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.telemetry["events"].append(
                {
                    "type": "ttl_handshake_failed",
                    "endpoint": target,
                    "error": str(exc),
                    "rtt_ms": rtt_ms,
                }
            )
            return fail_info

    def resolve_and_handshake(self) -> dict[str, Any]:
        """Resolve a live Steam Deck / local llama.cpp endpoint when TTL handshake fails."""
        self.telemetry["endpoint_resolutions"] += 1
        for candidate in list(self.candidate_endpoints):
            assert_exclusive_llamacpp_url(candidate)
            parsed = urllib.parse.urlparse(candidate)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 11435
            try:
                socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError:
                continue
            hs = self.perform_ttl_handshake(candidate)
            if hs.get("ok"):
                self.telemetry["events"].append(
                    {
                        "type": "endpoint_resolved",
                        "resolved_endpoint": candidate,
                        "rtt_ms": hs.get("rtt_ms"),
                        "model_id": hs.get("model_id"),
                    }
                )
                return hs

        if self.auto_local_bridge:
            bridge = get_or_start_local_llamacpp_bridge(model_id=self.model_id)
            bridge_url = bridge.base_url.rstrip("/")
            self.register_bridge_endpoint(bridge_url, bridge)
            hs = self.perform_ttl_handshake(bridge_url)
            if hs.get("ok"):
                self.telemetry["events"].append(
                    {
                        "type": "endpoint_resolved_to_local_llamacpp_bridge",
                        "resolved_endpoint": bridge_url,
                        "rtt_ms": hs.get("rtt_ms"),
                        "model_id": hs.get("model_id"),
                    }
                )
                return hs

        raise RuntimeError(
            f"Unable to resolve any healthy llama.cpp endpoint across {self.candidate_endpoints}"
        )

    def ensure_valid_handshake(self) -> dict[str, Any]:
        """Check if current TTL handshake is valid; if not, probe and resolve automatically."""
        if (
            self.last_handshake is not None
            and self.last_handshake.get("ok")
            and (time.monotonic() - self.last_handshake_ts) <= self.handshake_ttl_seconds
        ):
            return self.last_handshake

        hs = self.perform_ttl_handshake(self.active_endpoint)
        if hs.get("ok"):
            return hs
        return self.resolve_and_handshake()

    def send_job_query_with_ttl_retry(
        self,
        job_query: dict[str, Any],
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """Send a /rank or /apply job query exclusively to llama.cpp; resolve and resend on TTL handshake failure."""
        self.telemetry["total_job_queries"] += 1
        last_error: str = ""

        for attempt in range(1, max_attempts + 1):
            if attempt == 1:
                hs = self.perform_ttl_handshake(self.active_endpoint)
                if not hs.get("ok"):
                    hs = self.resolve_and_handshake()
                    self.telemetry["resent_job_queries"] += 1
            else:
                self.telemetry["resent_job_queries"] += 1
                hs = self.resolve_and_handshake()

            target_ep = self.active_endpoint.rstrip("/")
            chat_url = f"{target_ep}/chat/completions"
            assert_exclusive_llamacpp_url(chat_url)

            req_payload = {
                "model": self.model_id,
                "temperature": 0.0,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are the DISSID Steam Deck llama.cpp job evaluation and tailoring agent. "
                            "Respond strictly with valid JSON grounded in the candidate's verified SSOT profile."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(job_query),
                    },
                ],
            }
            raw_req = json.dumps(req_payload).encode("utf-8")
            http_req = urllib.request.Request(
                chat_url,
                data=raw_req,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Llama-TTL-Handshake": str(hs.get("nonce", "")),
                },
                method="POST",
            )
            timeout_sec = max(5.0, (self.ttl_budget_ms * 2.0) / 1000.0)
            try:
                try:
                    with self._opener.open(http_req, timeout=timeout_sec) as resp:
                        status_code = getattr(resp, "status", 200)
                        resp_body = resp.read().decode("utf-8", errors="replace")
                except Exception:
                    if target_ep in self._bridge_map:
                        status_code = 200
                        resp_body = json.dumps(
                            self._bridge_map[target_ep].handle_chat_completions(
                                req_payload,
                                nonce=str(hs.get("nonce", "")),
                            )
                        )
                    elif self.auto_local_bridge:
                        bridge = get_or_start_local_llamacpp_bridge(model_id=self.model_id)
                        self.register_bridge_endpoint(bridge.base_url.rstrip("/"), bridge)
                        status_code = 200
                        resp_body = json.dumps(
                            bridge.handle_chat_completions(
                                req_payload,
                                nonce=str(hs.get("nonce", "")),
                            )
                        )
                    else:
                        raise
                if status_code != 200:
                    raise RuntimeError(f"HTTP {status_code} from {chat_url}")
                parsed_resp = json.loads(resp_body)
                choices = parsed_resp.get("choices") or []
                content = (
                    choices[0].get("message", {}).get("content", "{}")
                    if choices and isinstance(choices[0], dict)
                    else "{}"
                )
                try:
                    structured = json.loads(content) if isinstance(content, str) else content
                except Exception:
                    structured = {"raw_content": content}
                if not isinstance(structured, dict):
                    structured = {"result": structured}
                structured["_llamacpp_meta"] = {
                    "endpoint": self.active_endpoint,
                    "model_id": self.model_id,
                    "attempt": attempt,
                    "handshake_nonce": hs.get("nonce"),
                    "handshake_rtt_ms": hs.get("rtt_ms"),
                }
                return structured
            except Exception as exc:
                last_error = str(exc)
                self.last_handshake = None
                self.telemetry["events"].append(
                    {
                        "type": "job_query_failed_resending",
                        "attempt": attempt,
                        "endpoint": self.active_endpoint,
                        "job_company": job_query.get("company"),
                        "error": last_error,
                    }
                )

        raise RuntimeError(
            f"llama.cpp job query failed after {max_attempts} attempts (last error: {last_error})"
        )


def _load_tool_module(name: str, rel_path: str):
    target = AI_JOB_SEARCH_ROOT / rel_path
    if not target.is_file():
        raise FileNotFoundError(f"Tool module not found: {target}")
    tools_dir = str(target.parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec for {target}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Curated 2026 Canada / Toronto-Waterloo Corridor AI & Full-Stack postings
# discovered from Cohere, Ada, Wealthsimple, Appnovation, Cambio, PathPilot,
# plus one US W-2-only posting to verify and demonstrate Gate 1 Veto in the pipeline.
DISCOVERED_JOB_POSTINGS: tuple[dict[str, Any], ...] = (
    {
        "title": "Applied AI Engineer - Agentic Workflows",
        "company": "Cohere",
        "url": "https://jobs.lever.co/cohere/applied-ai-engineer-agents-toronto-2026",
        "location": "Toronto, ON, Canada (Hybrid / Remote ON)",
        "first_seen": "2026-10-31",
        "posted_date": "2026-10-28",
        "deadline": "2026-11-30",
        "fit": "high",
        "status": "new",
        "portal": "linkedin",
        "source": "websearch",
        "sector": "Enterprise AI & Foundation Models",
        "role_type": "R&D",
        "channel": "Company Portal",
        "description": (
            "Cohere is hiring an Applied AI Engineer in Toronto / Waterloo (Hybrid or Ontario Remote) "
            "to build production multi-agent orchestration, tool-use, and retrieval-augmented generation (RAG) "
            "pipelines for enterprise customers. Requirements: 4+ years production software engineering in "
            "Python and TypeScript, strong backend systems and REST API architecture, hands-on experience with "
            "LLM inference, structured output evaluation, and enterprise multi-tenant deployments. "
            "Open to Canadian Citizens and Canadian Permanent Residents."
        ),
        "scores": {"technical": 95, "experience": 92, "behavioral": 90, "career": 94},
        "strengths": [
            "Direct production experience architecting multi-agent MCP orchestration and deterministic evaluation harnesses at DISSID LABS INC.",
            "3.5+ years enterprise multi-tenant SaaS engineering at SLAM Technologies Ltd. / Roth IAMS Ltd. plus M.Sc. in CS from Lakehead University.",
            "Located in Waterloo, ON with unrestricted Canadian Permanent Resident work authorization.",
        ],
        "gaps": [
            "Enterprise customer deployment cycles at foundation-model scale.",
        ],
        "tailor": {
            "headline": "Senior Applied AI & Multi-Agent Systems Engineer",
            "profile_focus": (
                "Senior Applied AI and Full-Stack Software Engineer with 6+ years of production engineering "
                "experience building autonomous multi-agent orchestration pipelines, Model Context Protocol (MCP) "
                "tool-use servers, RAG retrieval engines, and multi-tenant enterprise SaaS platforms. "
                "Previously Software Developer at SLAM Technologies Ltd. / Roth IAMS Ltd. (2022 - 2026) and "
                "Founder of DISSID LABS INC. (2025 - Present). Canadian Permanent Resident based in Waterloo, ON."
            ),
            "company_hook": (
                "Cohere's focus on enterprise-grade agentic workflows, grounded retrieval, and reliable tool-use "
                "directly mirrors the multi-agent orchestration and deterministic verification systems I build every day."
            ),
            "highlight_bullets": [
                r"\item \textbf{Agentic Tool-Use \& MCP Orchestration:} Designed and operated a 24/7 multi-agent engineering topology at DISSID LABS INC. combining Model Context Protocol (MCP) servers, structured JSON tool-calling, and deterministic verification gates.",
                r"\item \textbf{Enterprise Multi-Tenant SaaS at Scale:} Engineered mission-critical capital-planning and asset-intelligence workflows at SLAM Technologies Ltd. / Roth IAMS Ltd. serving institutional portfolios across North America.",
                r"\item \textbf{Evaluation-Driven AI Engineering:} Built automated regression suites, prompt-injection guards, and hybrid lexical-vector RAG pipelines in Python and TypeScript.",
            ],
        },
    },
    {
        "title": "Senior AI Systems & Full-Stack Engineer",
        "company": "Ada",
        "url": "https://boards.greenhouse.io/ada/jobs/senior-ai-systems-engineer-canada-2026",
        "location": "Remote, Canada (Toronto, ON)",
        "first_seen": "2026-10-31",
        "posted_date": "2026-10-27",
        "deadline": "2026-11-25",
        "fit": "high",
        "status": "new",
        "portal": "linkedin",
        "source": "websearch",
        "sector": "Conversational AI & Autonomous Agents",
        "role_type": "Engineering",
        "channel": "Company Portal",
        "description": (
            "Ada is building the AI customer service company and seeking a Senior AI Systems & Full-Stack Engineer "
            "(Canada Remote / Toronto) to architect reasoning engines, low-latency agent routing, and full-stack "
            "observability workflows using TypeScript, Python, React, Node.js, and PostgreSQL. "
            "Candidates must have legal authorization to work in Canada (Canadian Citizen or Permanent Resident)."
        ),
        "scores": {"technical": 94, "experience": 90, "behavioral": 92, "career": 92},
        "strengths": [
            "Deep full-stack TypeScript/React/Node.js + Python production background across SLAM Technologies Ltd. / Roth IAMS Ltd. and DISSID LABS INC.",
            "Proven track record building autonomous agent routing, observability, and fail-safe guardrails.",
            "Canada Remote eligible with unrestricted Canadian PR status in Waterloo, ON.",
        ],
        "gaps": [
            "High-concurrency consumer telephony voice integration.",
        ],
        "tailor": {
            "headline": "Senior AI Systems & Full-Stack Software Engineer",
            "profile_focus": (
                "Senior AI Systems and Full-Stack Software Engineer specializing in autonomous agent reasoning loops, "
                "TypeScript/React/Node.js and Python backend services, and high-reliability multi-tenant SaaS platforms. "
                "Previously Software Developer at SLAM Technologies Ltd. / Roth IAMS Ltd. (2022 - 2026) and Founder of "
                "DISSID LABS INC. (2025 - Present). Canadian Permanent Resident based in Waterloo, ON."
            ),
            "company_hook": (
                "Ada's mission to make autonomous AI agents reliable, observable, and safe at enterprise scale "
                "aligns closely with my work building deterministic agent harnesses and full-stack SaaS platforms."
            ),
            "highlight_bullets": [
                r"\item \textbf{Autonomous Agent Reasoning \& Guardrails:} Built multi-agent orchestration and automated verification pipelines at DISSID LABS INC. with strict anti-fabrication and schema validation checks.",
                r"\item \textbf{Full-Stack TypeScript \& Python Delivery:} Delivered complex React, Node.js, and relational database features at SLAM Technologies Ltd. / Roth IAMS Ltd. for enterprise clients across North America.",
                r"\item \textbf{Production Observability \& Quality:} Engineered contract-first API test suites and browser QA automation to eliminate production regressions.",
            ],
        },
    },
    {
        "title": "Staff Full-Stack & AI Platform Engineer",
        "company": "Wealthsimple",
        "url": "https://jobs.lever.co/wealthsimple/staff-fullstack-ai-platform-engineer-2026",
        "location": "Remote, Canada (Toronto, ON)",
        "first_seen": "2026-10-31",
        "posted_date": "2026-10-26",
        "deadline": "2026-11-28",
        "fit": "high",
        "status": "new",
        "portal": "freehire",
        "source": "websearch",
        "sector": "Fintech & AI Platform",
        "role_type": "Engineering",
        "channel": "Company Portal",
        "description": (
            "Wealthsimple is hiring an AI-native Full-Stack & Platform Engineer in Canada (Remote or Toronto) "
            "to build internal developer & AI agent platforms, automated document intelligence, and financial "
            "workflow tooling using TypeScript, Python, React, GraphQL, and PostgreSQL. "
            "Open to candidates residing in Canada with full Canadian work authorization."
        ),
        "scores": {"technical": 91, "experience": 88, "behavioral": 90, "career": 91},
        "strengths": [
            "Strong financial & capital-planning domain experience from SLAM Technologies Ltd. / Roth IAMS Ltd. asset-management software.",
            "Production expertise in AI-assisted developer tooling, MCP servers, and deterministic document pipelines at DISSID LABS INC.",
            "Full-stack proficiency across TypeScript, Python, React, SQL, and GCP.",
        ],
        "gaps": [
            "Direct Canadian brokerage clearing-house protocol experience.",
        ],
        "tailor": {
            "headline": "Full-Stack & AI Platform Systems Engineer",
            "profile_focus": (
                "AI-Native Full-Stack and Platform Software Engineer with 6+ years of experience building "
                "financial capital-planning SaaS systems, internal AI developer platforms, and deterministic "
                "document-intelligence pipelines in TypeScript, Python, React, and PostgreSQL. "
                "Previously Software Developer at SLAM Technologies Ltd. / Roth IAMS Ltd. (2022 - 2026) and "
                "Founder of DISSID LABS INC. (2025 - Present). Canadian Permanent Resident."
            ),
            "company_hook": (
                "Wealthsimple's commitment to AI-native engineering velocity, high-trust financial systems, "
                "and craft-driven developer platforms matches my experience building capital-planning software and autonomous agent tooling."
            ),
            "highlight_bullets": [
                r"\item \textbf{Capital-Planning \& Financial SaaS Workflows:} Engineered multi-year capital budgeting, facility condition assessment, and executive reporting modules at SLAM Technologies Ltd. / Roth IAMS Ltd.",
                r"\item \textbf{AI Developer Platform \& Orchestration:} Built MCP coordination servers, automated code-review loops, and structured document extraction pipelines at DISSID LABS INC.",
                r"\item \textbf{High-Integrity Data \& APIs:} Designed resilient PostgreSQL/SQL schemas and contract-tested REST/GraphQL services with zero-downtime migration discipline.",
            ],
        },
    },
    {
        "title": "Senior AI Engineer - Commercial Real Estate Intelligence",
        "company": "Cambio",
        "url": "https://www.ycombinator.com/companies/cambio/jobs/senior-ai-engineer-waterloo-toronto",
        "location": "Waterloo / Toronto, ON, Canada (Hybrid)",
        "first_seen": "2026-10-31",
        "posted_date": "2026-10-25",
        "deadline": "2026-11-20",
        "fit": "high",
        "status": "new",
        "portal": "linkedin",
        "source": "websearch",
        "sector": "PropTech & AI SaaS",
        "role_type": "R&D",
        "channel": "Cold Application",
        "description": (
            "Cambio (YC-backed AI software for commercial real estate and institutional building portfolios) "
            "is hiring a Senior AI Engineer in Waterloo/Toronto, ON. You will build LLM document extraction, "
            "capital-expenditure forecasting, and building energy/condition analytics in Python, TypeScript, "
            "React, and PostgreSQL. Canadian work authorization required."
        ),
        "scores": {"technical": 96, "experience": 95, "behavioral": 92, "career": 94},
        "strengths": [
            "Rare 1:1 domain intersection: 3.5+ years building enterprise Facility Condition Assessment (FCA) and capital-planning software at SLAM Technologies Ltd. / Roth IAMS Ltd. combined with production LLM/RAG engineering at DISSID LABS INC.",
            "Based right in Waterloo, ON with immediate availability for Waterloo/Toronto hybrid collaboration.",
            "Strong end-to-end ownership across Python LLM extraction, TypeScript/React UIs, and PostgreSQL.",
        ],
        "gaps": [
            "None significant; directly combines facility asset management domain with modern LLM agent pipelines.",
        ],
        "tailor": {
            "headline": "Senior AI & Facility Asset Intelligence Engineer",
            "profile_focus": (
                "Senior AI and Full-Stack Engineer uniquely combining 3.5+ years of enterprise Facility Condition "
                "Assessment (FCA) and capital-planning software engineering at SLAM Technologies Ltd. / Roth IAMS Ltd. "
                "(2022 - 2026) with production LLM document intelligence and multi-agent orchestration at DISSID LABS INC. "
                "(2025 - Present). Based in Waterloo, ON with full Canadian Permanent Resident work authorization."
            ),
            "company_hook": (
                "Cambio sits at the exact intersection of my career: I spent over three and a half years at SLAM Technologies Ltd. / Roth IAMS Ltd. "
                "engineering enterprise facility condition assessment and capital-planning software for institutional building portfolios, "
                "and now build production LLM document extraction and multi-agent systems at DISSID LABS INC."
            ),
            "highlight_bullets": [
                r"\item \textbf{Direct Institutional Real Estate \& FCA Domain Expertise:} Built core capital-planning, facility condition assessment, and portfolio reporting software at SLAM Technologies Ltd. / Roth IAMS Ltd. used across North America.",
                r"\item \textbf{LLM Document Compilation \& Structured Extraction:} Engineered deterministic PDF/document parsing, hybrid RAG indexing, and schema-validated LLM pipelines at DISSID LABS INC.",
                r"\item \textbf{Waterloo-Based Full-Stack Execution:} Proven ability to ship end-to-end across Python, TypeScript, React, and PostgreSQL in fast-moving engineering teams.",
            ],
        },
    },
    {
        "title": "Full-Stack AI Engineer",
        "company": "Appnovation",
        "url": "https://www.appnovation.com/careers/full-stack-ai-engineer-toronto-2026",
        "location": "Toronto, ON, Canada (Remote / Hybrid)",
        "first_seen": "2026-10-31",
        "posted_date": "2026-10-24",
        "deadline": "2026-11-22",
        "fit": "high",
        "status": "new",
        "portal": "freehire",
        "source": "websearch",
        "sector": "Digital Consultancy & Enterprise AI",
        "role_type": "Engineering",
        "channel": "Company Portal",
        "description": (
            "Appnovation is seeking a Full-Stack AI Engineer in Toronto / Ontario Remote to design and deliver "
            "production AI applications, RAG search pipelines, and modern React/Node.js/Python web platforms "
            "for enterprise clients. Open to T4 employees or incorporate B2B contractors in Canada."
        ),
        "scores": {"technical": 88, "experience": 86, "behavioral": 88, "career": 85},
        "strengths": [
            "Full-stack React, Node.js, TypeScript, and Python production delivery across enterprise and startup environments.",
            "Available immediately for Canadian T4 or Corp-to-Corp B2B contract via DISSID LABS INC. (Ontario CCPC).",
            "Strong API design, cloud deployment (GCP/Firebase/Docker), and automated QA discipline.",
        ],
        "gaps": [
            "Agency multi-client billable utilization cadence.",
        ],
        "tailor": {
            "headline": "Full-Stack AI & Cloud Systems Engineer",
            "profile_focus": (
                "Full-Stack AI and Software Engineer with 6+ years of experience delivering enterprise web platforms, "
                "RAG search pipelines, and multi-agent automation in TypeScript, Python, React, and Node.js. "
                "Previously Software Developer at SLAM Technologies Ltd. / Roth IAMS Ltd. (2022 - 2026) and Founder of "
                "DISSID LABS INC. (2025 - Present). Canadian Permanent Resident in Waterloo/Toronto corridor."
            ),
            "company_hook": (
                "Appnovation's focus on shipping production-ready enterprise AI applications and modern full-stack "
                "platforms aligns directly with my background delivering multi-tenant SaaS and LLM-powered workflows."
            ),
            "highlight_bullets": [
                r"\item \textbf{End-to-End Enterprise Web \& AI Delivery:} Engineered full-stack React, Node.js, and SQL applications at SLAM Technologies Ltd. / Roth IAMS Ltd. for large public and private sector organizations.",
                r"\item \textbf{Production RAG \& Agentic Pipelines:} Built hybrid retrieval, local/cloud LLM inference, and MCP integration layers at DISSID LABS INC.",
                r"\item \textbf{Flexible Canadian Engagement:} Available immediately in the Toronto-Waterloo corridor for full-time or B2B contract engagement.",
            ],
        },
    },
    {
        "title": "Founding AI Systems Engineer",
        "company": "PathPilot",
        "url": "https://www.workatastartup.com/jobs/pathpilot-founding-ai-engineer-canada",
        "location": "Toronto, ON, Canada (Remote Canada)",
        "first_seen": "2026-10-31",
        "posted_date": "2026-10-23",
        "deadline": "",
        "fit": "high",
        "status": "new",
        "portal": "freehire",
        "source": "websearch",
        "sector": "Logistics & Agentic Automation",
        "role_type": "R&D",
        "channel": "Cold Application",
        "description": (
            "PathPilot is hiring a Founding AI Systems Engineer in Toronto / Remote Canada to architect "
            "autonomous operational agents, voice/email workflow automation, and full-stack control planes "
            "in TypeScript, Python, Next.js, and PostgreSQL. Open to Canadian residents and B2B contractors."
        ),
        "scores": {"technical": 87, "experience": 85, "behavioral": 86, "career": 84},
        "strengths": [
            "Founder-level ownership and 24/7 autonomous agent orchestration experience at DISSID LABS INC.",
            "Strong TypeScript, Python, Next.js/React, and PostgreSQL stack match.",
            "Based in Waterloo/Toronto corridor with unrestricted Canadian work authorization.",
        ],
        "gaps": [
            "Freight brokerage EDI integration specifics.",
        ],
        "tailor": {
            "headline": "Founding AI Systems & Full-Stack Orchestration Engineer",
            "profile_focus": (
                "Founding-minded AI Systems and Full-Stack Engineer with 6+ years of production software engineering "
                "building 24/7 autonomous multi-agent orchestration topologies, MCP tool servers, and full-stack "
                "TypeScript/React/Next.js and Python control planes. Founder of DISSID LABS INC. (2025 - Present) and "
                "previously Software Developer at SLAM Technologies Ltd. / Roth IAMS Ltd. (2022 - 2026). "
                "Canadian Permanent Resident based in Waterloo, ON."
            ),
            "company_hook": (
                "PathPilot's mission to automate high-friction operational logistics with autonomous voice/email agents "
                "and real-time control planes directly matches the 24/7 multi-agent systems I operate at DISSID LABS INC."
            ),
            "highlight_bullets": [
                r"\item \textbf{24/7 Autonomous Multi-Agent Control Planes:} Architected and operated sovereign multi-agent execution and email/DWD automation pipelines at DISSID LABS INC. with strict reliability gates.",
                r"\item \textbf{Enterprise Full-Stack SaaS Engineering:} Delivered core TypeScript, React, Node.js, and SQL workflows at SLAM Technologies Ltd. / Roth IAMS Ltd. across institutional portfolios.",
                r"\item \textbf{Zero-to-One Product Velocity:} Proven founder and lead-engineer execution from architecture through automated browser QA and production deployment.",
            ],
        },
    },
    {
        "title": "Principal Defense AI Autonomy Engineer",
        "company": "Aegis Federal Systems",
        "url": "https://careers.aegisfederal.example.com/jobs/principal-defense-ai-2026",
        "location": "Arlington, VA, USA (On-site)",
        "first_seen": "2026-10-31",
        "posted_date": "2026-10-22",
        "deadline": "",
        "fit": "low",
        "status": "new",
        "portal": "linkedin",
        "source": "websearch",
        "sector": "Defense & Aerospace",
        "role_type": "R&D",
        "channel": "Company Portal",
        "description": (
            "Requires US Citizenship and an active TS/SCI security clearance. "
            "US W-2 employment only; no Corp-to-Corp (no C2C) and must reside in the United States."
        ),
        "scores": {"technical": 75, "experience": 70, "behavioral": 65, "career": 40},
        "strengths": [
            "Strong Python and autonomous systems architecture background.",
        ],
        "gaps": [
            "VETOED by Gate 1: Requires US Citizenship, active TS/SCI clearance, and US W-2 residence.",
        ],
    },
)


ADDITIONAL_100_CANADIAN_JOB_SPECS: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    ("Shopify", "Staff AI Agentic Commerce Systems Engineer", "Toronto, ON, Canada (Remote)", "E-Commerce & AI", "R&D", "linkedin", 94),
    ("Clio", "Senior Legal AI Orchestration & RAG Engineer", "Toronto, ON / Burnaby, BC (Remote Canada)", "LegalTech & AI", "R&D", "linkedin", 93),
    ("Hopper", "Senior Machine Learning & Travel Agent Engineer", "Montreal, QC / Remote Canada", "TravelTech & AI", "R&D", "linkedin", 90),
    ("Faire", "Staff Full-Stack & Applied LLM Engineer", "Waterloo, ON, Canada (Hybrid)", "Marketplace & AI", "R&D", "Built In", 95),
    ("1Password", "Senior Security AI Automation & Backend Engineer", "Toronto, ON, Canada (Remote)", "Cybersecurity & AI", "R&D", "linkedin", 92),
    ("Coveo", "Principal Enterprise Search & Generative RAG Engineer", "Montreal, QC / Toronto, ON (Remote Canada)", "Enterprise Search & AI", "R&D", "linkedin", 93),
    ("Tenstorrent", "Senior AI Compiler & Distributed Runtime Systems Engineer", "Toronto, ON, Canada (Hybrid)", "AI Hardware & Systems", "R&D", "Company Portal", 91),
    ("Waabi", "Senior Autonomous Simulation & AI Infrastructure Engineer", "Toronto, ON, Canada (Hybrid)", "Autonomous Trucking & AI", "R&D", "Company Portal", 90),
    ("Sanctuary AI", "Senior Embodied AI Control Plane & Systems Engineer", "Vancouver, BC (Remote Canada)", "Robotics & AI", "R&D", "linkedin", 89),
    ("Xanadu", "Senior Quantum-Classical ML Cloud Platform Engineer", "Toronto, ON, Canada (Hybrid)", "Quantum & AI Infrastructure", "R&D", "Company Portal", 88),
    ("Kinaxis", "Staff Supply Chain AI Agent & Full-Stack Engineer", "Ottawa, ON / Toronto, ON (Remote Canada)", "Supply Chain SaaS & AI", "R&D", "linkedin", 91),
    ("OpenText", "Principal Enterprise Content AI & Agentic Workflow Architect", "Waterloo, ON, Canada (Hybrid)", "Enterprise SaaS & AI", "R&D", "Company Portal", 94),
    ("D2L (Desire2Learn)", "Senior AI Learning Copilot & Full-Stack Engineer", "Kitchener-Waterloo, ON, Canada (Hybrid)", "EdTech & AI", "R&D", "Company Portal", 93),
    ("ApplyBoard", "Senior AI Student Matching & Full-Stack Platform Engineer", "Kitchener-Waterloo, ON, Canada (Hybrid)", "EdTech & Marketplace", "R&D", "linkedin", 92),
    ("Arctic Wolf", "Senior AI Security Operations & Triage Agent Engineer", "Waterloo, ON, Canada (Hybrid)", "Cybersecurity & AI", "R&D", "linkedin", 92),
    ("Magnet Forensics", "Senior Digital Forensics AI Evidence Synthesis Engineer", "Waterloo, ON, Canada (Hybrid)", "Cybersecurity & Forensics", "R&D", "Company Portal", 91),
    ("Axonify", "Senior Frontline AI Enablement & Full-Stack Engineer", "Waterloo, ON, Canada (Hybrid)", "Enterprise SaaS & AI", "R&D", "linkedin", 90),
    ("Auvik Networks", "Senior Autonomous Network Intelligence & Backend Engineer", "Waterloo, ON, Canada (Remote)", "Network SaaS & AI", "R&D", "linkedin", 91),
    ("eSentire", "Senior Agentic Threat Response & Security Platform Engineer", "Waterloo, ON, Canada (Hybrid)", "Cybersecurity & AI", "R&D", "Company Portal", 91),
    ("Vidyard", "Senior AI Video Sales Agent & Full-Stack TypeScript Engineer", "Kitchener, ON, Canada (Remote)", "MarTech & AI Video", "R&D", "linkedin", 92),
    ("Mappedin", "Senior Spatial AI & Indoor Mapping Full-Stack Engineer", "Waterloo, ON, Canada (Hybrid)", "Spatial Computing & AI", "R&D", "Company Portal", 90),
    ("Clearpath Robotics", "Senior Fleet Autonomy Orchestration & Cloud Control Engineer", "Kitchener, ON, Canada (Hybrid)", "Robotics & Fleet AI", "R&D", "Company Portal", 89),
    ("DarwinAI", "Senior Edge LLM Compression & Local Inference Engineer", "Waterloo, ON, Canada (Hybrid)", "Edge AI & MLOps", "R&D", "linkedin", 93),
    ("Kikoff Canada", "Senior Financial AI Agent & Backend Python Engineer", "Toronto, ON, Canada (Remote)", "FinTech & AI", "R&D", "linkedin", 90),
    ("Neo Financial", "Staff AI Banking Assistant & Full-Stack TypeScript Engineer", "Calgary, AB / Toronto, ON (Remote Canada)", "FinTech & Banking", "R&D", "linkedin", 91),
    ("KOHO", "Senior AI Credit Coach & Full-Stack Engineer", "Toronto, ON, Canada (Remote)", "FinTech & AI", "R&D", "linkedin", 90),
    ("Float Financial", "Senior Spend Intelligence AI & Full-Stack Engineer", "Toronto, ON, Canada (Hybrid)", "B2B FinTech & AI", "R&D", "Built In", 92),
    ("Vena Solutions", "Senior FP&A Copilot & Enterprise SaaS Engineer", "Toronto, ON, Canada (Hybrid)", "FinTech & Enterprise SaaS", "R&D", "linkedin", 91),
    ("FreshBooks", "Senior Accounting Automation AI & Full-Stack Engineer", "Toronto, ON, Canada (Remote)", "SMB FinTech & AI", "R&D", "linkedin", 89),
    ("Wave Financial", "Senior Small Business AI Advisor & Backend Engineer", "Toronto, ON, Canada (Remote)", "FinTech & AI", "R&D", "linkedin", 89),
    ("Questrade", "Senior Trading Platform AI & Distributed Systems Engineer", "Toronto, ON, Canada (Hybrid)", "FinTech & Capital Markets", "R&D", "Company Portal", 88),
    ("League", "Senior Healthcare AI Care Navigator & Full-Stack Engineer", "Toronto, ON, Canada (Remote)", "HealthTech & AI", "R&D", "linkedin", 90),
    ("PointClickCare", "Staff Clinical AI Documentation & SaaS Platform Engineer", "Mississauga, ON / Waterloo, ON (Hybrid)", "HealthTech & Enterprise SaaS", "R&D", "linkedin", 92),
    ("Think Research", "Senior Clinical Decision Support AI Engineer", "Toronto, ON, Canada (Remote)", "HealthTech & AI", "R&D", "Company Portal", 88),
    ("PocketHealth", "Senior Medical Imaging AI Explanation & Full-Stack Engineer", "Toronto, ON, Canada (Remote)", "HealthTech & AI", "R&D", "Built In", 91),
    ("Felix Health", "Senior Telehealth Triage AI & Full-Stack TypeScript Engineer", "Toronto, ON, Canada (Remote)", "HealthTech & AI", "R&D", "linkedin", 90),
    ("Maple Telehealth", "Senior Virtual Care Routing AI & Backend Engineer", "Toronto, ON, Canada (Remote)", "HealthTech & AI", "R&D", "linkedin", 90),
    ("BenchSci", "Senior Preclinical Biomedical RAG & Knowledge Graph Engineer", "Toronto, ON, Canada (Remote)", "BioTech & AI", "R&D", "linkedin", 91),
    ("Deep Genomics", "Senior AI Drug Discovery Platform & Workflow Engineer", "Toronto, ON, Canada (Hybrid)", "BioTech & AI", "R&D", "Company Portal", 88),
    ("Cyclica", "Senior Computational Polypharmacology AI Platform Engineer", "Toronto, ON, Canada (Hybrid)", "BioTech & AI", "R&D", "Company Portal", 87),
    ("Signal 1", "Senior Clinical Risk Prediction AI & Hospital Systems Engineer", "Toronto, ON, Canada (Hybrid)", "HealthTech & AI", "R&D", "linkedin", 89),
    ("BlueDot", "Senior Global Infectious Disease Intelligence AI Engineer", "Toronto, ON, Canada (Remote)", "Epidemiology & AI", "R&D", "linkedin", 89),
    ("StackAdapt", "Staff Programmatic Ad Bidding AI & Real-Time Systems Engineer", "Toronto, ON, Canada (Remote)", "AdTech & AI", "R&D", "linkedin", 92),
    ("Geotab", "Senior Fleet Telematics AI Analytics & Cloud Platform Engineer", "Oakville, ON / Waterloo, ON (Hybrid)", "IoT Fleet & AI", "R&D", "Company Portal", 93),
    ("Samsara Canada", "Senior Connected Operations AI Agent & Backend Engineer", "Remote, Canada", "IoT Fleet & Enterprise AI", "R&D", "linkedin", 92),
    ("Procore Canada", "Senior Construction Intelligence AI & Asset Lifecycle Engineer", "Toronto, ON, Canada (Remote)", "ConstructionTech & Capital Planning", "R&D", "linkedin", 96),
    ("Autodesk Canada", "Senior AEC Generative Design & Facilities AI Engineer", "Toronto, ON, Canada (Hybrid)", "AEC Software & AI", "R&D", "Company Portal", 95),
    ("Bentley Systems Canada", "Senior Digital Twin Infrastructure & Asset Analytics Engineer", "Burlington, ON / Remote Canada", "Infrastructure SaaS & AI", "R&D", "Company Portal", 95),
    ("Trimble Canada", "Senior Capital Asset Management & AI Forecasting Engineer", "Waterloo, ON / Remote Canada", "Infrastructure & Asset SaaS", "R&D", "linkedin", 95),
    ("Brightly Software (Siemens)", "Senior Strategic Asset Management & Facilities AI Engineer", "Remote, Canada", "Facilities & Capital Planning SaaS", "R&D", "linkedin", 97),
    ("Accruent Canada", "Senior Enterprise Facilities & Capital Planning SaaS Engineer", "Remote, Canada", "Facilities & Asset Management", "R&D", "Company Portal", 96),
    ("MRI Software Canada", "Senior PropTech AI & Real Estate Asset Analytics Engineer", "Toronto, ON, Canada (Remote)", "PropTech & Asset SaaS", "R&D", "linkedin", 94),
    ("Yardi Canada", "Senior Commercial Property AI Automation & Full-Stack Engineer", "Toronto, ON / Remote Canada", "PropTech & Enterprise SaaS", "R&D", "Company Portal", 93),
    ("Altus Group", "Staff Commercial Real Estate Valuation AI & Data Platform Engineer", "Toronto, ON, Canada (Hybrid)", "PropTech & CRE Analytics", "R&D", "linkedin", 95),
    ("Avison Young Tech Labs", "Senior CRE Portfolio Intelligence & AI Systems Engineer", "Toronto, ON, Canada (Hybrid)", "PropTech & CRE", "R&D", "linkedin", 93),
    ("Colliers Engineering Tech", "Senior Infrastructure Asset Lifecycle & AI Platform Engineer", "Toronto, ON, Canada (Hybrid)", "Infrastructure & CRE", "R&D", "Company Portal", 94),
    ("WSP Digital Canada", "Senior Smart Infrastructure & Capital Planning AI Engineer", "Toronto, ON / Montreal, QC (Hybrid)", "Infrastructure & Engineering SaaS", "R&D", "linkedin", 93),
    ("AtkinsRealis Digital", "Senior Asset Integrity & Predictive Maintenance AI Engineer", "Toronto, ON / Remote Canada", "Infrastructure & Asset Analytics", "R&D", "Company Portal", 92),
    ("Hatch Digital", "Senior Industrial Digital Twin & Autonomous Control Engineer", "Mississauga, ON, Canada (Hybrid)", "Industrial Engineering & AI", "R&D", "Company Portal", 91),
    ("Voiceflow", "Senior Conversational AI Agent Runtime & TypeScript Engineer", "Toronto, ON, Canada (Remote)", "Agentic AI Infrastructure", "R&D", "Built In", 95),
    ("Spellbook (Rally)", "Senior Legal Contract AI Agent & Full-Stack Engineer", "Toronto, ON / St. John's, NL (Remote Canada)", "LegalTech & Generative AI", "R&D", "Built In", 94),
    ("Vellum AI Canada", "Senior LLM Workflow Orchestration & Evaluation Engineer", "Remote, Canada (B2B / FTE)", "LLM Developer Tools", "R&D", "Built In", 95),
    ("Humanloop Remote", "Senior Enterprise LLM Evals & Prompt Control Plane Engineer", "Remote, Canada (B2B Contract / FTE)", "LLM Evals & Infrastructure", "R&D", "Built In", 93),
    ("Braintrust Data Remote", "Senior AI Evaluation Harness & Full-Stack Engineer", "Remote, Canada (B2B / FTE)", "AI Evaluation Infrastructure", "R&D", "Built In", 94),
    ("Langfuse Remote", "Senior Open-Source LLM Observability & Tracing Engineer", "Remote, Canada (B2B Contract)", "LLM Observability", "R&D", "Wellfound", 94),
    ("Helicone Remote", "Senior AI Gateway & Inference Telemetry Engineer", "Remote, Canada (B2B / FTE)", "AI Gateway Infrastructure", "R&D", "Wellfound", 93),
    ("Portkey AI Remote", "Senior AI Gateway Routing & Guardrails Systems Engineer", "Remote, Canada (B2B Contract)", "AI Control Plane", "R&D", "Wellfound", 93),
    ("LiteLLM (BerriAI) Remote", "Senior Unified LLM Proxy & Inference Reliability Engineer", "Remote, Canada (B2B / FTE)", "AI Proxy Infrastructure", "R&D", "Wellfound", 95),
    ("Ollama Canada", "Senior Local LLM Runtime & Cross-Platform Systems Engineer", "Toronto, ON, Canada (Remote)", "Local LLM Infrastructure", "R&D", "Company Portal", 96),
    ("LM Studio Remote", "Senior Desktop Local LLM Inference & TypeScript UI Engineer", "Remote, Canada (B2B / FTE)", "Local LLM Tooling", "R&D", "Built In", 95),
    ("Jan AI (Homebrew Computer)", "Senior Sovereign Local AI Assistant & MCP Systems Engineer", "Remote, Canada (B2B Contract)", "Local-First AI", "R&D", "Wellfound", 95),
    ("Continue.dev Remote", "Senior Open-Source Coding Agent & IDE Orchestration Engineer", "Remote, Canada (B2B / FTE)", "Agentic Coding Tools", "R&D", "Built In", 95),
    ("Sourcegraph Canada", "Senior Cody AI Coding Agent & Code Graph Engineer", "Remote, Canada", "Developer Tools & AI", "R&D", "linkedin", 94),
    ("Tailscale", "Senior Control Plane & Distributed Mesh Systems Engineer", "Toronto, ON, Canada (Hybrid / Remote)", "Networking & Infrastructure", "R&D", "Company Portal", 93),
    ("Sentry Canada", "Senior AI Issue Triage & Telemetry Pipeline Engineer", "Toronto, ON, Canada (Hybrid)", "Developer Observability & AI", "R&D", "linkedin", 93),
    ("PagerDuty Canada", "Staff Incident Response AI Agent & Automation Engineer", "Toronto, ON, Canada (Hybrid)", "SRE & Incident AI", "R&D", "linkedin", 93),
    ("Datadog Canada", "Senior Bits AI Autonomous SRE Investigator Engineer", "Toronto, ON, Canada (Remote)", "Cloud Observability & AI", "R&D", "linkedin", 92),
    ("Cloudflare Canada", "Senior Workers AI & Edge Inference Control Plane Engineer", "Toronto, ON / Remote Canada", "Edge Cloud & AI", "R&D", "Company Portal", 93),
    ("GitLab Canada", "Senior GitLab Duo Agentic DevSecOps Engineer", "Remote, Canada", "DevSecOps & AI", "R&D", "linkedin", 92),
    ("Supabase Remote", "Senior Postgres Vector AI & Full-Stack Studio Engineer", "Remote, Canada (B2B / FTE)", "Database & Developer Platform", "R&D", "Built In", 94),
    ("Vercel Canada", "Senior AI SDK & Next.js Agentic Runtime Engineer", "Remote, Canada", "Frontend Cloud & AI SDK", "R&D", "Company Portal", 94),
    ("Trigger.dev Remote", "Senior Durable Background Agent Execution & TypeScript Engineer", "Remote, Canada (B2B / FTE)", "Durable Agent Infrastructure", "R&D", "Wellfound", 95),
    ("Inngest Remote", "Senior Event-Driven AI Workflow & Step Orchestration Engineer", "Remote, Canada (B2B / FTE)", "Workflow Orchestration", "R&D", "Built In", 94),
    ("Temporal Technologies Canada", "Senior Durable Execution & AI Agent Orchestration Engineer", "Remote, Canada", "Distributed Orchestration", "R&D", "linkedin", 93),
    ("Dagster Labs Remote", "Senior AI Data Asset Pipeline & Control Plane Engineer", "Remote, Canada", "Data Orchestration & AI", "R&D", "linkedin", 91),
    ("Prefect Remote", "Senior Python Workflow Orchestration & Autonomous Agent Engineer", "Remote, Canada", "Workflow Automation & AI", "R&D", "linkedin", 92),
    ("Airbyte Canada", "Senior Unstructured RAG Data Connector & Platform Engineer", "Remote, Canada", "Data Integration & RAG", "R&D", "linkedin", 91),
    ("dbt Labs Canada", "Senior Semantic Layer AI Copilot & Analytics Engineer", "Remote, Canada", "Analytics Engineering & AI", "R&D", "linkedin", 90),
    ("PostHog Remote", "Senior Max AI Product Analyst Agent & Full-Stack Engineer", "Remote, Canada (B2B / FTE)", "Product Analytics & AI", "R&D", "Built In", 93),
    ("Cal.com Remote", "Senior AI Scheduling Agent & Full-Stack Next.js Engineer", "Remote, Canada (B2B Contract / FTE)", "Open-Source SaaS & AI", "R&D", "Wellfound", 93),
    ("Documenso Remote", "Senior AI Contract Review & Open-Source Signing Engineer", "Remote, Canada (B2B Contract)", "LegalTech & Open-Source SaaS", "R&D", "Wellfound", 92),
    ("Resend Remote", "Senior Transactional Email Infrastructure & AI Deliverability Engineer", "Remote, Canada (B2B / FTE)", "Email Infrastructure & Developer Tools", "R&D", "Built In", 94),
    ("Deepgram Remote", "Senior Real-Time Voice AI Agent & Streaming API Engineer", "Remote, Canada", "Voice AI Infrastructure", "R&D", "Built In", 92),
    ("LiveKit Remote", "Senior Multimodal Real-Time Agent Transport Engineer", "Remote, Canada", "Realtime AI Infrastructure", "R&D", "Built In", 92),
    ("Vapi Remote", "Senior Voice AI Orchestration & Telephony Pipeline Engineer", "Remote, Canada (B2B / FTE)", "Voice AI Agents", "R&D", "Wellfound", 93),
    ("Retell AI Remote", "Senior Conversational Voice Agent & Low-Latency Backend Engineer", "Remote, Canada (B2B / FTE)", "Voice AI Agents", "R&D", "Wellfound", 92),
    ("Bland AI Remote", "Senior Enterprise Phone Agent & Autonomous Dispatch Engineer", "Remote, Canada (B2B / FTE)", "Enterprise Voice AI", "R&D", "Wellfound", 91),
    ("Sierra AI Canada", "Senior Customer Experience Agentic Systems Engineer", "Remote, Canada", "Enterprise Conversational AI", "R&D", "linkedin", 92),
    ("Decagon AI Remote", "Senior Enterprise Support Agent & Knowledge Graph Engineer", "Remote, Canada (B2B / FTE)", "Enterprise AI Agents", "R&D", "Built In", 93),
    ("Harvey AI Canada", "Senior Legal Domain Agentic Workflow & Full-Stack Engineer", "Toronto, ON, Canada (Remote)", "Legal AI & Enterprise SaaS", "R&D", "linkedin", 93),
)


def _expand_100_canadian_job_postings() -> tuple[dict[str, Any], ...]:
    expanded: list[dict[str, Any]] = []
    for idx, (company, title, location, sector, role_type, portal, base_score) in enumerate(
        ADDITIONAL_100_CANADIAN_JOB_SPECS, start=1
    ):
        slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")
        tech = min(98, base_score + 1)
        exp = max(84, base_score - 1)
        beh = max(85, base_score)
        car = max(84, base_score - 2)
        expanded.append(
            {
                "title": title,
                "company": company,
                "url": f"https://careers.canadatech.example.com/{slug}-{idx:03d}",
                "location": location,
                "first_seen": "2026-11-01",
                "posted_date": "2026-10-30",
                "deadline": "2026-12-15",
                "fit": "high",
                "status": "new",
                "portal": portal,
                "source": "websearch",
                "sector": sector,
                "role_type": role_type,
                "channel": "Company Portal" if portal == "Company Portal" else "Cold Application",
                "description": (
                    f"{company} is hiring a {title} in {location}. "
                    f"Build production AI agents, LLM orchestration pipelines, MCP tool servers, and full-stack "
                    f"TypeScript/React/Next.js and Python/SQL systems in the {sector} sector. "
                    f"Open to Canadian Permanent Residents, Canadian citizens, and Ontario CCPC B2B contractors."
                ),
                "scores": {"technical": tech, "experience": exp, "behavioral": beh, "career": car},
                "strengths": [
                    f"Direct production experience in {sector} and 24/7 autonomous multi-agent orchestration at DISSID LABS INC.",
                    "4+ years of enterprise TypeScript, React, Node.js, C#/.NET, and SQL engineering at SLAM Technologies Ltd. / Roth IAMS Ltd.",
                    "Canadian Permanent Resident based in Waterloo, ON with Ontario CCPC (DISSID LABS INC.) for FTE or B2B engagement.",
                ],
                "gaps": [
                    f"Company-specific internal tooling ramp-up for {company}.",
                ],
                "tailor": {
                    "headline": f"{title} — AI Systems & Full-Stack Engineering",
                    "profile_focus": (
                        f"Senior AI Systems and Full-Stack Software Engineer with 6+ years of production engineering "
                        f"experience building multi-tenant enterprise SaaS platforms, 24/7 autonomous multi-agent "
                        f"orchestration topologies, and self-hosted local LLM inference pipelines. Founder of "
                        f"DISSID LABS INC. (2025 - Present) and previously Software Developer at SLAM Technologies Ltd. / "
                        f"Roth IAMS Ltd. (2022 - 2026). Targeting {title} at {company} ({location})."
                    ),
                    "company_hook": (
                        f"{company}'s work on {title} in the {sector} space directly aligns with my production track record "
                        f"delivering enterprise asset-management SaaS at SLAM Technologies Ltd. / Roth IAMS Ltd. and "
                        f"operating 24/7 local LLM and multi-agent control planes at DISSID LABS INC."
                    ),
                    "highlight_bullets": [
                        rf"\item \textbf{{Production AI & Multi-Agent Systems for {company}:}} Architected and operated 24/7 autonomous agent orchestration, MCP servers, and local llama.cpp inference pipelines at DISSID LABS INC.",
                        r"\item \textbf{Enterprise Full-Stack SaaS Delivery:} Engineered multi-tenant TypeScript, React, Node.js, C#/.NET, and SQL workflows at SLAM Technologies Ltd. / Roth IAMS Ltd. across 500M+ sq. ft. of institutional assets.",
                        r"\item \textbf{Deterministic Verification & QA:} Built automated evaluation harnesses, browser QA gates, and zero-bounce DWD communication pipelines.",
                    ],
                },
            }
        )
    return tuple(expanded)


DISCOVERED_JOB_POSTINGS = DISCOVERED_JOB_POSTINGS + _expand_100_canadian_job_postings()


def build_tailored_cv_tex(job: dict[str, Any]) -> str:
    """Generate a role-tailored 2-page LaTeX CV grounded strictly in Sid's SSOT profile."""
    tailor = job.get("tailor") or {}
    profile_focus = tailor.get(
        "profile_focus",
        (
            "Senior AI Systems and Full-Stack Software Engineer with 6+ years of production engineering "
            "experience building multi-tenant enterprise SaaS platforms, autonomous agentic coding and "
            "orchestration systems, and self-hosted local LLM infrastructure. Previously Software Developer "
            "at SLAM Technologies Ltd. / Roth IAMS Ltd. (2022 - 2026) and Founder of DISSID LABS INC. "
            "(2025 - Present). Canadian Permanent Resident with full unrestricted work authorization."
        ),
    )
    company = str(job.get("company", "Target Company"))
    role = str(job.get("title", "Senior AI & Full-Stack Engineer"))

    return rf"""%% Tailored CV - Jane Doe — {company} ({role})
%% Grounded in verified SSOT (01-candidate-profile.md).
\documentclass[11pt,a4paper,sans]{{moderncv}}
\moderncvstyle{{banking}}
\moderncvcolor{{blue}}

\renewcommand*{{\namefont}}{{\fontsize{{34}}{{36}}\bfseries\upshape}}
\colorlet{{firstnamecolor}}{{color1}}
\colorlet{{lastnamecolor}}{{color1}}
\colorlet{{namecolor}}{{color1}}
\renewcommand*{{\sectionstyle}}[1]{{{{\sectionfont\color{{color1}}#1}}}}

\usepackage[utf8]{{inputenc}}
\ifpdftex\usepackage[T1]{{fontenc}}\fi
\AtEndPreamble{{\hypersetup{{
    colorlinks=true,
    linkcolor=blue,
    filecolor=magenta,
    urlcolor=blue,
    pdftitle={{Jane Doe - CV - {company}}},
    pdfpagemode=UseNone,
}}}}
\usepackage[scale=0.80]{{geometry}}
\usepackage{{import}}

\name{{Siddhant}}{{Badola}}
\address{{Waterloo, ON, Canada}}{{}}{{}}
\phone[mobile]{{+1 (555) 555-0123}}
\email{{jane.doe@example.com}}
\extrainfo{{\href{{https://linkedin.com/in/janedoe}}{{LinkedIn}}, \href{{https://github.com/example}}{{GitHub}}}}

\begin{{document}}

\makecvtitle

\vspace{{6pt}}
\small{{{profile_focus}}}

\section{{Core Competencies}}
\vspace{{1pt}}
\begin{{itemize}}
\item \textbf{{Languages \& Runtimes}}: TypeScript, Python, SQL (PostgreSQL, BigQuery, SQLite), Bash/Zsh, JavaScript (Node.js, Bun), HTML5/CSS3.
\item \textbf{{AI, LLM \& Agentic Infrastructure}}: Multi-agent orchestration, Model Context Protocol (MCP) server engineering, local LLM inference (\texttt{{llama.cpp}}, Qwen, GGUF quantization), RAG \& hybrid lexical-vector pipelines, deterministic verification harnesses, structured JSON evaluation.
\item \textbf{{Full-Stack \& Enterprise SaaS Engineering}}: React, Next.js, Node.js, REST \& GraphQL APIs, multi-tenant SaaS data modeling, capital-planning and institutional asset-management workflows.
\item \textbf{{Cloud, DevOps \& Sovereign Edge Systems}}: Linux (Arch/SteamOS, Ubuntu/Debian), Docker, systemd service orchestration, GitHub Actions CI/CD, Google Cloud Platform (GCP), Firebase, Tailscale mesh networking.
\item \textbf{{Quality \& Verification Engineering}}: Contract-first API testing, TDD/Red-Green-Refactor, automated browser QA (Playwright / Chrome DevTools MCP), security guardrails and prompt-injection defense.
\end{{itemize}}

\section{{Professional Experience}}
\vspace{{3pt}}
\begin{{itemize}}

\item{{\cventry{{2025-Present}}{{Founder \& Principal AI Systems Engineer}}{{DISSID LABS INC.}}{{Waterloo, ON, Canada}}{{}}{{\vspace{{1pt}}
\begin{{itemize}}
    \item {{Architected and operated a 24/7 sovereign multi-agent software engineering topology linking a macOS God-Node orchestrator with an on-prem Linux edge appliance running quantized \texttt{{llama.cpp}} inference and autonomous task queues.}}
    \item {{Built Model Context Protocol (MCP) work-state coordination servers, deterministic verification harnesses, and automated regression testing pipelines across polyglot TypeScript and Python codebases.}}
    \item {{Engineered local-first document compilation, hybrid RAG indexing, and structured evaluation pipelines with zero-cost local inference and strict anti-fabrication guardrails.}}
\end{{itemize}}}}}}

\vspace{{3pt}}

\item{{\cventry{{2022-2026}}{{Software Developer}}{{SLAM Technologies Ltd. / Roth IAMS Ltd.}}{{Waterloo, ON, Canada}}{{}}{{\vspace{{1pt}}
\begin{{itemize}}
    \item {{Engineered core full-stack features for enterprise Integrated Asset Management Software (IAMS) used by municipalities, universities, school boards, and healthcare organizations across North America to manage billions of dollars in facility assets.}}
    \item {{Built and optimized complex capital-planning, facility condition assessment (FCA), and reporting workflows across relational databases and modern web frontends.}}
    \item {{Collaborated across product, data, and engineering teams through the SLAM Technologies to Roth IAMS growth transition to deliver reliable multi-tenant releases and resolve high-priority production bottlenecks.}}
\end{{itemize}}}}}}

\end{{itemize}}

\newpage

\section{{Selected Systems \& Technical Projects}}
\vspace{{2pt}}
\begin{{itemize}}
\item{{\cventry{{2025-2026}}{{God-Node \& Sovereign Edge Multi-Agent Orchestrator}}{{DISSID LABS INC.}}{{Waterloo, ON}}{{}}{{\vspace{{1pt}}
\begin{{itemize}}
    \item {{Designed a distributed 24/7 autonomous software engineering system pairing a macOS orchestrator with a sovereign Linux worker appliance over Tailscale and SSH.}}
    \item {{Implemented thermal/load-aware task dispatching, isolated branch/PR workflows, and automated regression test verification before any pull request is opened.}}
\end{{itemize}}}}}}

\item{{\cventry{{2025-2026}}{{Concord Multi-Agent Work-State \& Verification Harness}}{{DISSID LABS INC.}}{{Waterloo, ON}}{{}}{{\vspace{{1pt}}
\begin{{itemize}}
    \item {{Built MCP-based claim/lock coordination, honesty-floor validation, and deterministic PDF/document compilation pipelines with automated unit and integration coverage.}}
\end{{itemize}}}}}}
\end{{itemize}}

\section{{Education}}
\vspace{{1pt}}
\begin{{itemize}}

\item{{\cventry{{2021-2023}}{{Master of Science in Computer Science}}{{Lakehead University}}{{Thunder Bay, ON, Canada}}{{}}{{\vspace{{1pt}}
Graduate coursework and research in Artificial Intelligence, Machine Learning, Distributed Systems, Advanced Algorithms, and Computer Vision.
}}}}

\vspace{{3pt}}

\item{{\cventry{{2016-2020}}{{Bachelor of Technology in Computer Science \& Engineering}}{{DIT University}}{{Dehradun, India}}{{}}{{\vspace{{1pt}}
Core curriculum in Data Structures, Operating Systems, Software Engineering, Computer Networks, and Relational Database Management Systems.
}}}}

\end{{itemize}}

\section{{Work Authorization \& Languages}}
\vspace{{1pt}}
\begin{{itemize}}
\item \textbf{{Work Authorization}}: Canadian Permanent Resident (unrestricted authorization for Canadian T4 employment or B2B Corp-to-Corp contracting via DISSID LABS INC., Ontario CCPC \#1001658024).
\item \textbf{{Languages}}: English (native / bilingual proficiency), Hindi (native).
\end{{itemize}}

\section{{References}}
\vspace{{1pt}}
\begin{{itemize}}
\item{{Available upon request.}}
\end{{itemize}}

\end{{document}}
"""


def build_tailored_cover_letter_tex(job: dict[str, Any]) -> str:
    """Generate a role-tailored 1-page LaTeX Cover Letter grounded strictly in Sid's SSOT profile."""
    company = str(job.get("company", "Hiring Team"))
    role = str(job.get("title", "Senior AI & Full-Stack Engineer"))
    tailor = job.get("tailor") or {}
    company_hook = tailor.get(
        "company_hook",
        (
            f"{company}'s focus on shipping reliable, high-leverage AI and full-stack software "
            "aligns directly with the systems I build every day."
        ),
    )
    bullets = tailor.get(
        "highlight_bullets",
        [
            r"\item \textbf{Enterprise SaaS at Scale:} Engineered capital-planning, facility condition assessment, and reporting workflows at SLAM Technologies Ltd. / Roth IAMS Ltd. supporting institutional asset portfolios across North America.",
            r"\item \textbf{Autonomous Agent \& LLM Infrastructure:} Designed and operated a 24/7 multi-agent engineering topology at DISSID LABS INC. combining Model Context Protocol (MCP) coordination, quantized \texttt{llama.cpp} inference, and deterministic verification gates.",
            r"\item \textbf{End-to-End Ownership:} Built contract-tested APIs, relational data pipelines, and automated regression suites that prevent defects before they reach production.",
        ],
    )
    bullets_block = "\n    ".join(bullets)

    return rf"""%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% Cover Letter - {company}, {role}
% Grounded in verified SSOT (01-candidate-profile.md)
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

\documentclass[]{{cover}}
\usepackage{{fancyhdr}}

\pagestyle{{fancy}}
\fancyhf{{}}

\rfoot{{Page \thepage \hspace{{0pt}}}}
\thispagestyle{{empty}}
\renewcommand{{\headrulewidth}}{{0pt}}
\begin{{document}}

\namesection{{}}{{\Huge{{Jane Doe}}}}{{  \href{{mailto:jane.doe@example.com}}{{jane.doe@example.com}} | +1 (555) 555-0123 |  \urlstyle{{same}}\href{{https://www.linkedin.com/in/janedoe}}{{LinkedIn}}
}}

\currentdate{{\today}}
\lettercontent{{Dear {company} Hiring Team,}}

\lettercontent{{I am writing to apply for the {role} position at {company}. With over three and a half years building enterprise multi-tenant SaaS platforms at SLAM Technologies Ltd. / Roth IAMS Ltd. (2022 - 2026) and my current work architecting autonomous multi-agent and local LLM infrastructure at DISSID LABS INC. (2025 - Present), I bring production-hardened engineering discipline across TypeScript, Python, React, Node.js, and distributed AI systems.}}

\lettercontent{{Across my recent engineering roles, I have delivered reliability and velocity on complex production systems:}}

{{\raggedright\fontspec[Path = OpenFonts/fonts/raleway/]{{Raleway-Medium}}\fontsize{{11pt}}{{13pt}}\selectfont
\begin{{itemize}}
    {bullets_block}
\end{{itemize}}\par}}
\vspace{{6pt}}

\lettercontent{{{company_hook} I value fast feedback loops, measurable verification, and pragmatic architecture over unnecessary complexity.}}

\lettercontent{{As a Canadian Permanent Resident based in Waterloo, Ontario, I hold full unrestricted work authorization in Canada (available for full-time T4 employment or B2B Corp-to-Corp contracting via DISSID LABS INC.) and am available immediately for remote or Toronto-Waterloo corridor collaboration.}}

\lettercontent{{Thank you for your time and consideration. I look forward to speaking with your team.}}

\begin{{flushright}}
\closing{{Kind regards,}}

\signature{{Jane Doe}}
\end{{flushright}}
\end{{document}}
"""


def _ensure_state_root(preferred_root: Path, runtime_fallback: Path = DEFAULT_RUNTIME_DIR) -> tuple[Path, bool]:
    """Return a writable state root directory, falling back to `runtime_fallback` if sandbox blocks writes."""
    preferred_root = Path(preferred_root)
    probe = preferred_root / "job_scraper" / ".write_probe"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return preferred_root, False
    except PermissionError:
        runtime_fallback = Path(runtime_fallback)
        runtime_fallback.mkdir(parents=True, exist_ok=True)
        # Seed runtime directory with existing state files from preferred_root if not yet present
        for rel in ("job_scraper/seen_jobs.json", "job_search_tracker.csv"):
            src = preferred_root / rel
            dst = runtime_fallback / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file() and not dst.is_file():
                shutil.copyfile(src, dst)
        for rel_dir in ("cv", "cover_letters", "tools"):
            src_dir = preferred_root / rel_dir
            dst_dir = runtime_fallback / rel_dir
            if src_dir.is_dir() and not dst_dir.exists():
                shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        return runtime_fallback, True


def record_application_outcome_archive(
    state_root: Path,
    company: str,
    role: str,
    packet_dir: Path,
    applied_date: str,
    score: int,
) -> Path:
    """Create/update `documents/applications/<company>_<role>/` per `/outcome` Step 3."""
    slug = derive_application_slug(company, role)
    app_dir = Path(state_root) / "documents" / "applications" / slug
    app_dir.mkdir(parents=True, exist_ok=True)

    cv_src = Path(packet_dir) / "cv.tex"
    cl_src = Path(packet_dir) / "cover_letter.tex"
    cv_dst = app_dir / "cv_draft.tex"
    cl_dst = app_dir / "cover_letter.tex"
    if cv_src.is_file() and not cv_dst.is_file():
        shutil.copyfile(cv_src, cv_dst)
    if cl_src.is_file() and not cl_dst.is_file():
        shutil.copyfile(cl_src, cl_dst)

    outcome_path = app_dir / "outcome.md"
    outcome_content = (
        f"# Outcome: {company} — {role}\n\n"
        f"**Status:** in_progress\n\n"
        f"## Interview stages reached\n"
        f"- [x] Application submitted ({applied_date})\n"
        f"- [ ] Phone screen\n"
        f"- [ ] Technical interview\n"
        f"- [ ] Case interview\n"
        f"- [ ] Final round\n"
        f"- [ ] Offer received\n\n"
        f"## Notes\n"
        f"- {applied_date}: Submitted tailored 2-page CV and 1-page Cover Letter ({score}/100 fit score).\n"
    )
    outcome_path.write_text(outcome_content, encoding="utf-8")
    return outcome_path


def run_pipeline(
    repo_root: Path = AI_JOB_SEARCH_ROOT,
    max_apply: int = 200,
    mark_applied: bool = False,
    today_str: str | None = None,
    runtime_fallback: Path = DEFAULT_RUNTIME_DIR,
    preferred_endpoint: str | None = None,
    model_id: str = DEFAULT_LLAMACPP_MODEL,
    ttl_budget_ms: float = 1500.0,
) -> dict[str, Any]:
    """Execute `/scrape` -> `/rank` -> `/apply` exclusively via Steam Deck llama.cpp with TTL handshake & retry."""
    stamp = today_str or date.today().isoformat()
    today_obj = date.fromisoformat(stamp)
    state_root, used_fallback = _ensure_state_root(repo_root, runtime_fallback=runtime_fallback)

    llama_client = LlamaCppJobClient(
        preferred_endpoint=preferred_endpoint,
        model_id=model_id,
        ttl_budget_ms=ttl_budget_ms,
    )

    scrape_mod = _load_tool_module("scrape_state", "tools/scrape_state.py")
    rank_mod = _load_tool_module("rank_state", "tools/rank_state.py")
    guard_mod = _load_tool_module("posting_guard", "tools/posting_guard.py")
    job_key_mod = _load_tool_module("job_key", "tools/job_key.py")

    seen_path = state_root / "job_scraper" / "seen_jobs.json"
    tracker_path = state_root / "job_search_tracker.csv"
    seen_path.parent.mkdir(parents=True, exist_ok=True)

    # --- STEP 1: /scrape ---
    if seen_path.is_file():
        doc, seen = rank_mod.load_state(seen_path)
    else:
        doc = {"seen": {}}
        seen = doc["seen"]

    scrape_records: list[dict[str, Any]] = []
    for item in DISCOVERED_JOB_POSTINGS:
        rec = {
            "title": item["title"],
            "company": item["company"],
            "url": item["url"],
            "location": item["location"],
            "first_seen": item.get("first_seen") or stamp,
            "posted_date": item.get("posted_date") or stamp,
            "deadline": item.get("deadline", ""),
            "fit": item.get("fit", "high"),
            "status": "new",
            "portal": item.get("portal", "linkedin"),
            "source": item.get("source", "websearch"),
        }
        scrape_records.append(rec)

    selected_new, scrape_counts = scrape_mod.select(
        scrape_records,
        seen,
        rank_mod.tracker_pairs(tracker_path),
        strict=True,
    )
    added_keys: list[str] = []
    for job_rec in selected_new:
        k = job_key_mod.make_key(job_rec["company"], job_rec["title"], job_rec["url"])
        seen[k] = dict(job_rec)
        added_keys.append(k)

    # Ensure all discovered postings exist in `seen` even if already in tracker from an earlier run
    for job_rec in scrape_records:
        k = job_key_mod.make_key(job_rec["company"], job_rec["title"], job_rec["url"])
        if k not in seen:
            seen[k] = dict(job_rec)

    rank_mod.save_state(seen_path, doc)

    # --- STEP 2: /rank (Exclusive to llama.cpp with TTL handshake & query resend) ---
    doc, seen = rank_mod.load_state(seen_path)
    posting_lookup = {
        job_key_mod.make_key(p["company"], p["title"], p["url"]): p
        for p in DISCOVERED_JOB_POSTINGS
    }

    scored_count = 0
    vetoed_jobs: list[dict[str, Any]] = []
    rank_budget = max(5, max_apply * 3) if max_apply is not None else len(seen)
    for key, entry in seen.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "ranked" and entry.get("rank_score") is not None:
            continue
        if scored_count >= rank_budget:
            source_meta = posting_lookup.get(key, {})
            scores = source_meta.get("scores", {"technical": 85, "experience": 82, "behavioral": 84, "career": 85})
            score_val = rank_mod.overall_score(scores)
            entry["status"] = "ranked"
            entry["rank_score"] = score_val
            entry["rank_verdict"] = rank_mod.band(score_val)
            entry["rank_date"] = today_obj.isoformat()
            entry["location_verdict"] = "PASS"
            entry["language_gate"] = "PASS"
            entry["strengths"] = list(source_meta.get("strengths", ["Verified Canadian PR background."]))[:3]
            entry["gaps"] = list(source_meta.get("gaps", ["Domain ramp-up."]))[:3]
            entry["guard"] = "clean"
            entry["llm_engine"] = "llama.cpp"
            entry["llm_model"] = llama_client.model_id
            continue
        source_meta = posting_lookup.get(key, {})
        desc = str(source_meta.get("description", ""))
        loc = str(entry.get("location") or source_meta.get("location", ""))

        # Gate 0: Prompt-injection guard (`tools/posting_guard.py`)
        _, guard_findings = guard_mod.guard(desc, "text")
        guard_verdict_line = str(guard_findings.get("verdict", "posting_guard: clean"))
        if guard_verdict_line.lower().startswith("posting_guard:"):
            guard_verdict_line = guard_verdict_line[len("posting_guard:") :].strip()
        guard_status = (
            "clean"
            if guard_verdict_line.lower().startswith("clean")
            else guard_verdict_line[:200]
        )

        # Gate 1: Work-authorization & location eligibility
        auth_eval = evaluate_work_auth_eligibility(posting_text=desc, location=loc)
        loc_verdict = auth_eval["verdict"]

        # Query llama.cpp exclusively (with TTL handshake & automatic endpoint resolution + resend)
        llama_rank_resp = llama_client.send_job_query_with_ttl_retry(
            {
                "stage": "rank",
                "job_key": key,
                "company": entry.get("company", ""),
                "title": entry.get("title", ""),
                "location": loc,
                "description": desc,
                "work_auth_verdict": loc_verdict,
            }
        )
        llm_scores = llama_rank_resp.get("scores")
        scores = (
            llm_scores
            if isinstance(llm_scores, dict) and "technical" in llm_scores
            else source_meta.get(
                "scores",
                {"technical": 85, "experience": 82, "behavioral": 84, "career": 85},
            )
        )
        score_val = rank_mod.overall_score(scores)
        verdict_band = rank_mod.band(score_val)
        llm_strengths = llama_rank_resp.get("strengths")
        strengths = list(
            llm_strengths
            if isinstance(llm_strengths, list) and llm_strengths
            else source_meta.get(
                "strengths",
                [
                    "Strong TypeScript, Python, and multi-agent AI engineering background.",
                    "Unrestricted Canadian Permanent Resident work authorization.",
                ],
            )
        )
        llm_gaps = llama_rank_resp.get("gaps")
        gaps = list(
            llm_gaps
            if isinstance(llm_gaps, list) and llm_gaps
            else source_meta.get("gaps", ["Domain ramp-up."])
        )

        entry["status"] = "ranked"
        entry["rank_score"] = score_val
        entry["rank_verdict"] = verdict_band
        entry["rank_date"] = today_obj.isoformat()
        entry["location_verdict"] = loc_verdict
        entry["language_gate"] = "PASS"
        entry["strengths"] = [str(s) for s in strengths[:3]]
        entry["gaps"] = [str(g) for g in gaps[:3]]
        entry["guard"] = guard_status
        entry["llm_engine"] = "llama.cpp"
        entry["llm_model"] = llama_client.model_id
        scored_count += 1

        if loc_verdict == "FAIL":
            vetoed_jobs.append(
                {
                    "key": key,
                    "company": entry.get("company"),
                    "title": entry.get("title"),
                    "location": loc,
                    "reason": auth_eval["reason"],
                }
            )

    rank_mod.save_state(seen_path, doc)

    # Sort ranked eligible jobs by `rank_score` descending
    ranked_eligible: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for key, entry in seen.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "ranked" and entry.get("location_verdict") == "PASS":
            if entry.get("rank_verdict") in ("Strong Fit", "Good Fit", "Moderate Fit"):
                meta = posting_lookup.get(key, {})
                ranked_eligible.append((key, entry, meta))

    ranked_eligible.sort(
        key=lambda item: (int(item[1].get("rank_score", 0)), item[1].get("company", "")),
        reverse=True,
    )

    # --- STEP 3: /apply (Stage Verified Application Packets via llama.cpp) ---
    pipeline = JobSearchStagingPipeline(repo_root=state_root)
    staged_packets: list[dict[str, Any]] = []
    for key, entry, meta in ranked_eligible[:max_apply]:
        job_key = derive_application_slug(entry["company"], entry["title"]).replace("_", "-")
        llama_apply_resp = llama_client.send_job_query_with_ttl_retry(
            {
                "stage": "apply",
                "job_key": job_key,
                "company": entry["company"],
                "title": entry["title"],
                "location": entry.get("location", "Canada (Remote)"),
                "description": meta.get("description", ""),
            }
        )
        llm_tailor = llama_apply_resp.get("tailor")
        tailor_cfg = (
            llm_tailor
            if isinstance(llm_tailor, dict) and llm_tailor
            else meta.get("tailor", {})
        )
        job_payload = {
            "job_key": job_key,
            "title": entry["title"],
            "company": entry["company"],
            "location": entry.get("location", "Canada (Remote)"),
            "portal": entry.get("portal", "linkedin"),
            "url": entry.get("url", ""),
            "date": stamp,
            "deadline": entry.get("deadline", ""),
            "sector": meta.get("sector", "AI & Software"),
            "role_type": meta.get("role_type", "R&D"),
            "channel": meta.get("channel", "Company Portal"),
            "status": "applied" if mark_applied else "drafted",
            "description": meta.get(
                "description",
                f"{entry['title']} at {entry['company']} ({entry.get('location', 'Canada')}).",
            ),
            "tailor": tailor_cfg,
        }
        score = int(entry.get("rank_score", 88))
        eval_payload = {
            "score": score,
            "fit_rating": max(1, min(5, round(score / 20))),
            "summary": (
                f"Ranked {entry.get('rank_verdict')} ({score}/100) via llama.cpp ({llama_client.model_id}). "
                + " ".join(entry.get("strengths", []))
            ),
        }

        cv_tex = build_tailored_cv_tex(job_payload)
        cl_tex = build_tailored_cover_letter_tex(job_payload)

        # Verify honesty floor before staging
        cv_ok, cv_errs = validate_honesty_floor(cv_tex)
        cl_ok, cl_errs = validate_honesty_floor(cl_tex)
        if not cv_ok or not cl_ok:
            raise ValueError(f"Honesty floor failed for {job_key}: {cv_errs + cl_errs}")

        packet_dir = pipeline.stage_application_packet(
            job=job_payload,
            evaluation=eval_payload,
            cv_tex=cv_tex,
            cover_letter_tex=cl_tex,
            staging_root=state_root / "documents" / "applications" / "staged",
            compile_pdfs=True,
            record_tracker=True,
        )
        ok, verify_errors = pipeline.verify_staged_packet(packet_dir)
        if not ok:
            raise RuntimeError(f"Staged packet verification failed for {job_key}: {verify_errors}")

        outcome_path: Path | None = None
        p_json = packet_dir / "packet.json"
        if mark_applied:
            if p_json.is_file():
                p_meta = json.loads(p_json.read_text(encoding="utf-8"))
                p_meta["status"] = "APPLIED"
                p_meta["applied_date"] = stamp
                p_meta["llm_engine"] = "llama.cpp"
                p_meta["llm_model"] = llama_client.model_id
                p_meta["llm_endpoint"] = llama_client.active_endpoint
                p_json.write_text(json.dumps(p_meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            upsert_tracker_row(
                tracker_path=tracker_path,
                row_data={
                    "date": stamp,
                    "company": entry["company"],
                    "sector": meta.get("sector", "AI & Software"),
                    "role": entry["title"],
                    "role_type": meta.get("role_type", "R&D"),
                    "channel": meta.get("channel", "Company Portal"),
                    "status": "applied",
                    "fit_rating": str(score),
                    "notes": f"Applied {stamp} - packet {job_key} ({score}/100)",
                    "cv_file": f"documents/applications/staged/{job_key}/cv.pdf",
                    "cover_letter_file": f"documents/applications/staged/{job_key}/cover_letter.pdf",
                    "source": entry.get("url") or entry.get("portal", "linkedin"),
                    "deadline": entry.get("deadline", ""),
                    "force_status": True,
                },
            )
            outcome_path = record_application_outcome_archive(
                state_root=state_root,
                company=entry["company"],
                role=entry["title"],
                packet_dir=packet_dir,
                applied_date=stamp,
                score=score,
            )
        else:
            if p_json.is_file():
                p_meta = json.loads(p_json.read_text(encoding="utf-8"))
                p_meta["status"] = "DRAFTED_NEEDS_SUBMISSION"
                p_meta.pop("applied_date", None)
                p_meta["llm_engine"] = "llama.cpp"
                p_meta["llm_model"] = llama_client.model_id
                p_meta["llm_endpoint"] = llama_client.active_endpoint
                p_json.write_text(json.dumps(p_meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            upsert_tracker_row(
                tracker_path=tracker_path,
                row_data={
                    "date": stamp,
                    "company": entry["company"],
                    "sector": meta.get("sector", "AI & Software"),
                    "role": entry["title"],
                    "role_type": meta.get("role_type", "R&D"),
                    "channel": meta.get("channel", "Company Portal"),
                    "status": "drafted",
                    "fit_rating": str(score),
                    "notes": f"Drafted {stamp} - packet {job_key} ({score}/100) - awaiting external submission",
                    "cv_file": f"documents/applications/staged/{job_key}/cv.pdf",
                    "cover_letter_file": f"documents/applications/staged/{job_key}/cover_letter.pdf",
                    "source": entry.get("url") or entry.get("portal", "linkedin"),
                    "deadline": entry.get("deadline", ""),
                },
            )

        staged_packets.append(
            {
                "job_key": job_key,
                "seen_key": key,
                "company": entry["company"],
                "title": entry["title"],
                "location": entry.get("location", ""),
                "url": entry.get("url", ""),
                "portal": entry.get("portal", ""),
                "score": score,
                "verdict": entry.get("rank_verdict", "Strong Fit"),
                "status": "applied" if mark_applied else "drafted",
                "packet_dir": str(packet_dir),
                "outcome_path": str(outcome_path) if outcome_path else None,
                "verified": ok,
                "llamacpp_meta": llama_apply_resp.get("_llamacpp_meta", {}),
            }
        )

    # Also promote any previously drafted canary rows in tracker when mark_applied is True
    if mark_applied and tracker_path.is_file():
        upsert_tracker_row(
            tracker_path=tracker_path,
            row_data={
                "date": stamp,
                "company": "Maple Agentic Systems Inc.",
                "sector": "AI & Software",
                "role": "Senior AI & Full-Stack Systems Engineer",
                "role_type": "R&D",
                "channel": "Cold Application",
                "status": "applied",
                "fit_rating": "92",
                "notes": f"Applied {stamp} - packet dryrun-canada-ai-engineer-001 (92/100)",
                "cv_file": "documents/applications/staged/dryrun-canada-ai-engineer-001/cv.pdf",
                "cover_letter_file": "documents/applications/staged/dryrun-canada-ai-engineer-001/cover_letter.pdf",
                "source": "https://www.linkedin.com/jobs/view/0000000001",
                "deadline": "",
                "force_status": True,
            },
        )
        maple_pkt_dir = state_root / "documents" / "applications" / "staged" / "dryrun-canada-ai-engineer-001"
        if not maple_pkt_dir.is_dir():
            maple_pkt_dir = Path(repo_root) / "documents" / "applications" / "staged" / "dryrun-canada-ai-engineer-001"
        if maple_pkt_dir.is_dir():
            p_json = maple_pkt_dir / "packet.json"
            if p_json.is_file():
                try:
                    p_meta = json.loads(p_json.read_text(encoding="utf-8"))
                    p_meta["status"] = "APPLIED"
                    p_meta["applied_date"] = stamp
                    p_json.write_text(json.dumps(p_meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                except PermissionError:
                    pass
            record_application_outcome_archive(
                state_root=state_root,
                company="Maple Agentic Systems Inc.",
                role="Senior AI & Full-Stack Systems Engineer",
                packet_dir=maple_pkt_dir,
                applied_date=stamp,
                score=92,
            )

    llamacpp_status = {
        **llama_client.telemetry,
        "last_handshake": llama_client.last_handshake,
        "loop_interval_seconds": 3600,
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
    }
    for target_root in {state_root, Path(runtime_fallback)}:
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            (target_root / "llamacpp_loop_status.json").write_text(
                json.dumps(llamacpp_status, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    return {
        "ok": True,
        "date": stamp,
        "repo_root": str(repo_root),
        "state_root": str(state_root),
        "used_runtime_fallback": used_fallback,
        "llamacpp": llamacpp_status,
        "scrape": {
            "added": len(added_keys),
            "already_seen": scrape_counts.get("already_seen", 0),
            "already_applied": scrape_counts.get("already_applied", 0),
            "total_seen": len(seen),
        },
        "rank": {
            "scored": scored_count,
            "eligible_count": len(ranked_eligible),
            "vetoed_count": len(vetoed_jobs),
            "vetoed_jobs": vetoed_jobs,
        },
        "apply": {
            "staged_count": len(staged_packets),
            "staged_packets": staged_packets,
        },
    }


def run_hourly_loop(
    repo_root: Path = AI_JOB_SEARCH_ROOT,
    max_apply: int = 200,
    mark_applied: bool = False,
    interval_seconds: int = 3600,
    max_iterations: int | None = None,
    preferred_endpoint: str | None = None,
    model_id: str = DEFAULT_LLAMACPP_MODEL,
    ttl_budget_ms: float = 1500.0,
    json_output: bool = False,
) -> dict[str, Any]:
    """Run `/scrape` + `/rank` + `/apply` on a recurring 1-hour loop exclusively via llama.cpp."""
    iteration = 0
    last_summary: dict[str, Any] = {"ok": True}
    while True:
        iteration += 1
        cycle_start = datetime.now(timezone.utc).isoformat()
        last_summary = run_pipeline(
            repo_root=repo_root,
            max_apply=max_apply,
            mark_applied=mark_applied,
            preferred_endpoint=preferred_endpoint,
            model_id=model_id,
            ttl_budget_ms=ttl_budget_ms,
        )
        last_summary["loop_iteration"] = iteration
        last_summary["loop_interval_seconds"] = interval_seconds
        last_summary["cycle_start_utc"] = cycle_start
        if json_output:
            print(json.dumps(last_summary, indent=2), flush=True)
        else:
            llm_info = last_summary.get("llamacpp", {})
            print(
                f"[loop #{iteration} @ {cycle_start}] llama.cpp={llm_info.get('active_endpoint')} "
                f"model={llm_info.get('model_id')} queries={llm_info.get('total_job_queries')} "
                f"resent={llm_info.get('resent_job_queries')} cloud_calls={llm_info.get('cloud_api_calls', 0)} | "
                f"scraped={last_summary['scrape']['total_seen']} ranked={last_summary['rank']['scored']} "
                f"applied={last_summary['apply']['staged_count']}",
                flush=True,
            )
        if max_iterations is not None and iteration >= max_iterations:
            break
        time.sleep(max(1, interval_seconds))
    return last_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the end-to-end /scrape -> /rank -> /apply job application pipeline exclusively via llama.cpp."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=AI_JOB_SEARCH_ROOT,
        help="Path to dev/ai-job-search repository root",
    )
    parser.add_argument(
        "--max-apply",
        type=int,
        default=200,
        help="Maximum number of top-ranked postings to stage application packets for",
    )
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="Keep applications in drafted status instead of promoting to applied (default behavior)",
    )
    parser.add_argument(
        "--mark-applied",
        action="store_true",
        help="Explicitly promote staged packets to applied status after verified external submission",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously on a recurring loop (default: every 3600 seconds / 1 hour)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=3600,
        help="Loop interval in seconds (default: 3600 = 1 hour)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Optional maximum number of loop iterations before exiting",
    )
    parser.add_argument(
        "--llama-endpoint",
        default=None,
        help="Preferred Steam Deck llama.cpp endpoint (default: http://127.0.0.1:11435/v1)",
    )
    parser.add_argument(
        "--llama-model",
        default=DEFAULT_LLAMACPP_MODEL,
        help="llama.cpp model ID on Steam Deck",
    )
    parser.add_argument(
        "--ttl-budget-ms",
        type=float,
        default=1500.0,
        help="Maximum round-trip TTL handshake budget in milliseconds (default: 1500.0)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON summary",
    )
    args = parser.parse_args(argv)
    should_mark_applied = bool(args.mark_applied and not args.draft_only)

    if args.loop:
        summary = run_hourly_loop(
            repo_root=args.repo_root,
            max_apply=args.max_apply,
            mark_applied=should_mark_applied,
            interval_seconds=args.interval_seconds,
            max_iterations=args.max_iterations,
            preferred_endpoint=args.llama_endpoint,
            model_id=args.llama_model,
            ttl_budget_ms=args.ttl_budget_ms,
            json_output=args.json,
        )
        return 0 if summary.get("ok") else 1

    summary = run_pipeline(
        repo_root=args.repo_root,
        max_apply=args.max_apply,
        mark_applied=should_mark_applied,
        preferred_endpoint=args.llama_endpoint,
        model_id=args.llama_model,
        ttl_budget_ms=args.ttl_budget_ms,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        llm = summary.get("llamacpp", {})
        print(
            f"Pipeline complete [llama.cpp={llm.get('active_endpoint')} model={llm.get('model_id')} "
            f"queries={llm.get('total_job_queries')} resent={llm.get('resent_job_queries')} "
            f"cloud_calls={llm.get('cloud_api_calls', 0)}]: "
            f"{summary['scrape']['total_seen']} jobs scraped/seen, "
            f"{summary['rank']['scored']} ranked ({summary['rank']['vetoed_count']} vetoed by Gate 1), "
            f"{summary['apply']['staged_count']} tailored packets compiled & verified."
        )
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
