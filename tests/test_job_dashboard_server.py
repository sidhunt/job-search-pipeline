"""Unit tests for scripts/job_dashboard_server.py and scripts/job_apply_runner.py."""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import job_apply_runner
import job_dashboard_server
import job_search_staging


class TestJobDashboardServer(unittest.TestCase):
    def test_normalize_status_buckets(self) -> None:
        self.assertEqual(job_dashboard_server.normalize_status("drafted"), ("Drafted", None))
        self.assertEqual(job_dashboard_server.normalize_status("applied"), ("Active", None))
        self.assertEqual(job_dashboard_server.normalize_status("phone_screen"), ("Interview", None))
        self.assertEqual(job_dashboard_server.normalize_status("technical_interview"), ("Interview", None))
        self.assertEqual(job_dashboard_server.normalize_status("offer"), ("Offer", None))
        self.assertEqual(job_dashboard_server.normalize_status("hired"), ("Hired", None))
        self.assertEqual(job_dashboard_server.normalize_status("rejected"), ("Rejected/Closed", None))
        self.assertEqual(job_dashboard_server.normalize_status("offer_declined"), ("Rejected/Closed", None))
        self.assertEqual(job_dashboard_server.normalize_status("withdrawn"), ("Rejected/Closed", None))
        self.assertEqual(
            job_dashboard_server.normalize_status("custom_unknown"),
            ("Rejected/Closed", "custom_unknown"),
        )

    def test_load_tracker_rows_handles_14_and_13_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = pathlib.Path(tmp) / "job_search_tracker.csv"
            csv_path.write_text(
                "date,company,sector,role,role_type,channel,status,contact_person,fit_rating,notes,cv_file,cover_letter_file,source\n"
                "2026-10-30,Acme Corp,AI,Engineer,R&D,Company Portal,applied,,5,Legacy row,cv.pdf,cl.pdf,linkedin\n",
                encoding="utf-8",
            )
            rows = job_dashboard_server.load_tracker_rows(csv_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["company"], "Acme Corp")
            self.assertEqual(rows[0]["deadline"], "")
            self.assertEqual(rows[0]["status"], "applied")

    def test_collect_dashboard_data_funnel_and_rejection_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            runtime = pathlib.Path(tmp) / "runtime"
            (repo / "documents" / "applications" / "acme_engineer").mkdir(parents=True)
            (repo / "documents" / "applications" / "acme_engineer" / "outcome.md").write_text(
                "# Outcome\n"
                "- [x] Applied\n"
                "- [x] Phone screen\n"
                "- [ ] Offer\n",
                encoding="utf-8",
            )
            runtime.mkdir(parents=True)
            tracker = runtime / "job_search_tracker.csv"
            tracker.write_text(
                "date,company,sector,role,role_type,channel,status,contact_person,fit_rating,notes,cv_file,cover_letter_file,source,deadline\n"
                "2026-10-31,Draft Co,AI,AI Eng,R&D,Cold Application,drafted,,5,Note,cv.pdf,cl.pdf,linkedin,2026-11-20\n"
                "2026-10-31,Acme,AI,Engineer,R&D,Company Portal,rejected,,5,Note,cv.pdf,cl.pdf,linkedin,2026-11-21\n"
                "2026-10-31,Beta,AI,Staff Eng,R&D,Company Portal,withdrawn,,4,Note,cv.pdf,cl.pdf,linkedin,\n"
                "2026-10-31,Gamma,AI,Lead Eng,R&D,Company Portal,offer_declined,,5,Note,cv.pdf,cl.pdf,linkedin,\n",
                encoding="utf-8",
            )
            data = job_dashboard_server.collect_dashboard_data(repo_root=repo, runtime_root=runtime)
            # Sent excludes Drafted (1 drafted, 3 sent)
            self.assertEqual(data["sent_count"], 3)
            self.assertEqual(data["bucket_counts"]["Drafted"], 1)
            self.assertEqual(data["bucket_counts"]["Rejected/Closed"], 3)
            # Funnel merges outcome.md Phone screen checkbox for Acme + offer_declined for Gamma
            self.assertEqual(data["funnel_counts"]["Applied"], 3)
            self.assertEqual(data["funnel_counts"]["Interview"], 2)
            self.assertEqual(data["funnel_counts"]["Offer"], 1)
            # True rejection numerator excludes withdrawn and offer_declined (only Acme is true rejection: 1/3 = 33.3%)
            self.assertEqual(data["true_rejections"], 1)
            self.assertEqual(data["final_outcomes"], 3)
            self.assertAlmostEqual(data["rejection_rate_pct"], 33.3, places=1)

    def test_generate_html_report_escapes_special_chars_and_renders_4_svgs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            runtime = pathlib.Path(tmp) / "runtime"
            runtime.mkdir(parents=True)
            tracker = runtime / "job_search_tracker.csv"
            tracker.write_text(
                "date,company,sector,role,role_type,channel,status,contact_person,fit_rating,notes,cv_file,cover_letter_file,source,deadline\n"
                '2026-10-31,"R&D <script>alert(1)</script>","AI & Data","Role ""Alpha""",R&D,Company Portal,applied,,5,"Notes & <tags>",cv.pdf,cl.pdf,linkedin,2026-11-15\n',
                encoding="utf-8",
            )
            out_path = runtime / "reports" / "application-dashboard.html"
            written_path, _ = job_dashboard_server.generate_html_report(
                repo_root=repo,
                output_path=out_path,
                runtime_root=runtime,
            )
            self.assertTrue(written_path.exists())
            html_doc = written_path.read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", html_doc)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_doc)
            self.assertEqual(html_doc.count('<svg role="img"'), 4)
            self.assertIn("<th>Date</th>\n            <th>Deadline</th>\n            <th>Company</th>", html_doc)

    def test_pipeline_vetoes_us_w2_and_stages_top_roles(self) -> None:
        postings = job_apply_runner.DISCOVERED_JOB_POSTINGS
        us_roles = [p for p in postings if "Aegis Federal" in p["company"]]
        self.assertEqual(len(us_roles), 1)
        gate1 = job_search_staging.evaluate_work_auth_eligibility(
            f"{us_roles[0]['location']} {us_roles[0]['description']}"
        )
        self.assertEqual(gate1["verdict"], "FAIL")
        self.assertEqual(gate1["engagement_mode"], "INELIGIBLE")

    def test_assert_exclusive_llamacpp_url_blocks_gemini_argon_and_cloud_apis(self) -> None:
        forbidden_urls = [
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
            "https://us-central1-aiplatform.googleapis.com/v1/projects/p/models/argon-4",
            "https://api.openai.com/v1/chat/completions",
            "https://api.anthropic.com/v1/messages",
            "https://openrouter.ai/api/v1/chat/completions",
        ]
        for url in forbidden_urls:
            with self.assertRaises(RuntimeError):
                job_apply_runner.assert_exclusive_llamacpp_url(url)

        # Steam Deck and local llama.cpp endpoints must pass
        job_apply_runner.assert_exclusive_llamacpp_url("http://127.0.0.1:11435/v1")
        job_apply_runner.assert_exclusive_llamacpp_url("http://deck.local:11435/v1")
        job_apply_runner.assert_exclusive_llamacpp_url("http://127.0.0.1:11435/v1")

    def test_llamacpp_ttl_handshake_failure_resolves_endpoint_and_resends_job_query(self) -> None:
        bridge = job_apply_runner.LocalLlamaCppBridgeServer(port=0)
        live_url = bridge.start()
        try:
            # Configure primary endpoint as a dead port so initial TTL handshake fails,
            # forcing resolve_and_handshake() -> live_url and resending the job query.
            client = job_apply_runner.LlamaCppJobClient(
                preferred_endpoint="http://127.0.0.1:19/v1",
                candidate_endpoints=("http://127.0.0.1:19/v1", live_url),
                ttl_budget_ms=800.0,
                auto_local_bridge=False,
            )
            client.register_bridge_endpoint(live_url, bridge)
            resp = client.send_job_query_with_ttl_retry(
                {
                    "stage": "rank",
                    "company": "Cohere",
                    "title": "Applied AI Engineer - Agentic Workflows",
                }
            )
            self.assertEqual(resp.get("engine"), "llama.cpp")
            self.assertEqual(resp.get("company"), "Cohere")
            self.assertIn("scores", resp)
            self.assertEqual(client.active_endpoint, live_url.rstrip("/"))
            self.assertGreaterEqual(client.telemetry["handshake_failures"], 1)
            self.assertGreaterEqual(client.telemetry["endpoint_resolutions"], 1)
            self.assertEqual(client.telemetry["resent_job_queries"], 1)
            self.assertEqual(client.telemetry["cloud_api_calls"], 0)
            self.assertEqual(client.telemetry["gemini_argon_api_calls"], 0)
        finally:
            bridge.stop()

    def test_steamdeck_llama_rpc_enablement_and_bridge_rpc_metadata(self) -> None:
        import subprocess
        from pathlib import Path

        noddy_root = Path(__file__).resolve().parents[1]
        rpc_script = noddy_root / "scripts" / "enable-steamdeck-llama-rpc.sh"
        self.assertTrue(rpc_script.is_file(), "enable-steamdeck-llama-rpc.sh must exist")

        # Verify shell syntax and dry-run output
        syntax_res = subprocess.run(
            ["bash", "-n", str(rpc_script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(syntax_res.returncode, 0, syntax_res.stderr)

        dry_res = subprocess.run(
            ["bash", str(rpc_script), "--dry-run", "--rpc-port", "50052"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(dry_res.returncode, 0, dry_res.stderr)
        self.assertIn("-DGGML_RPC=ON", dry_res.stdout)
        self.assertIn("llama-rpc-server.service", dry_res.stdout)
        self.assertIn("LLAMA_ARG_RPC=", dry_res.stdout)

        # Verify LocalLlamaCppBridgeServer exposes rpc_enabled and rpc_port=50052
        bridge = job_apply_runner.LocalLlamaCppBridgeServer(port=0)
        models_payload = bridge.handle_models(nonce="rpc-test-nonce")
        meta = models_payload["data"][0]["meta"]
        self.assertTrue(meta.get("rpc_enabled"))
        self.assertEqual(meta.get("rpc_port"), 50052)

        # Verify job_dashboard_server invokes gmail_sync_service.py with --since 14d (not --days)
        dashboard_src = (noddy_root / "scripts" / "job_dashboard_server.py").read_text(encoding="utf-8")
        self.assertIn('["--since", "14d", "--json"]'[1:], dashboard_src)
        self.assertNotIn('"--days", "14"', dashboard_src)

    def test_discovered_job_postings_includes_100_additional_canadian_roles(self) -> None:
        self.assertEqual(len(job_apply_runner.ADDITIONAL_100_CANADIAN_JOB_SPECS), 100)
        self.assertEqual(len(job_apply_runner.DISCOVERED_JOB_POSTINGS), 107)
        eligible = [
            p
            for p in job_apply_runner.DISCOVERED_JOB_POSTINGS
            if job_apply_runner.evaluate_work_auth_eligibility(
                p.get("description", ""), p.get("location", "")
            )["verdict"]
            == "PASS"
        ]
        self.assertEqual(len(eligible), 106)

    def test_run_pipeline_defaults_to_drafted_until_external_submission(self) -> None:
        import inspect
        import json

        sig_pipeline = inspect.signature(job_apply_runner.run_pipeline)
        sig_loop = inspect.signature(job_apply_runner.run_hourly_loop)
        self.assertFalse(
            sig_pipeline.parameters["mark_applied"].default,
            "run_pipeline must default to mark_applied=False so unsubmitted packets stay 'drafted'",
        )
        self.assertFalse(
            sig_loop.parameters["mark_applied"].default,
            "run_hourly_loop must default to mark_applied=False so hourly loop never fabricates 'applied' rows",
        )

        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = pathlib.Path(tmp) / "runtime"
            summary = job_apply_runner.run_pipeline(
                repo_root=runtime_dir,
                runtime_fallback=runtime_dir,
                max_apply=1,
            )
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["apply"]["staged_count"], 1)
            pkt = summary["apply"]["staged_packets"][0]
            self.assertEqual(pkt["status"], "drafted")
            self.assertIsNone(pkt["outcome_path"])

            pkt_json = json.loads(
                (pathlib.Path(pkt["packet_dir"]) / "packet.json").read_text(encoding="utf-8")
            )
            self.assertEqual(pkt_json["status"], "DRAFTED_NEEDS_SUBMISSION")
            self.assertNotIn("applied_date", pkt_json)

            rows = job_dashboard_server.load_tracker_rows(runtime_dir / "job_search_tracker.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "drafted")

    def test_batch_approve_all_ranked_jobs(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = pathlib.Path(tmp)
            scraper_dir = tmp_root / "job_scraper"
            scraper_dir.mkdir(parents=True)
            seen_jobs = {
                "seen": {
                    "cohere_applied-ai-engineer": {
                        "company": "Cohere",
                        "title": "Applied AI Engineer",
                        "location": "Toronto, ON",
                        "status": "ranked",
                        "rank_score": 93,
                        "rank_verdict": "HIGH FIT",
                        "location_verdict": "PASS",
                        "language_gate": "PASS",
                        "url": "https://jobs.lever.co/cohere/applied-ai",
                    },
                    "us_only_role": {
                        "company": "US Corp",
                        "title": "US Secret Agent",
                        "location": "Washington, DC",
                        "status": "ranked",
                        "rank_score": 50,
                        "rank_verdict": "LOW FIT",
                        "location_verdict": "FAIL",
                        "language_gate": "PASS",
                    },
                }
            }
            (scraper_dir / "seen_jobs.json").write_text(json.dumps(seen_jobs), encoding="utf-8")

            res = job_dashboard_server.batch_approve_all_ranked_jobs(tmp_root, tmp_root)
            self.assertTrue(res["ok"])
            self.assertEqual(res["approved_count"], 1)
            self.assertEqual(res["vetoed_count"], 1)

            # Check staged packet created with DRAFTED_NEEDS_SUBMISSION
            pkt_json_p = tmp_root / "documents" / "applications" / "staged" / "cohere-applied-ai-engineer" / "packet.json"
            self.assertTrue(pkt_json_p.is_file())
            pkt_meta = json.loads(pkt_json_p.read_text(encoding="utf-8"))
            self.assertEqual(pkt_meta["status"], "DRAFTED_NEEDS_SUBMISSION")
            self.assertFalse(pkt_meta.get("already_submitted", True))

            # Check tracker row
            rows = job_dashboard_server.load_tracker_rows(tmp_root / "job_search_tracker.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["company"], "Cohere")
            self.assertEqual(rows[0]["status"], "drafted")

            # Check collect_dashboard_data reflects unapplied packet
            dash_data = job_dashboard_server.collect_dashboard_data(repo_root=tmp_root, runtime_root=tmp_root)
            self.assertEqual(dash_data["unapplied_packets_count"], 1)
            self.assertEqual(dash_data["applied_packets_count"], 0)


if __name__ == "__main__":
    unittest.main()
