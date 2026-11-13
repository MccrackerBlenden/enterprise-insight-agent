"""No network access, model quota, or project runtime dependencies required."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.check_release_safety import blocked_path, scan, secret_hits
from scripts.smoke_local_docker import verify


class LocalResponseHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        routes = {
            "/": ("text/html", b"<!doctype html><html><body>demo</body></html>"),
            "/api/health": ("application/json", json.dumps({"status": "ok", "model_ready": False, "synthetic_data": True}).encode()),
            "/api/overview": ("application/json", json.dumps({"synthetic": True, "companies": 12, "records": 432, "ranking": [{}] * 12, "years": [{"year": y} for y in (2023, 2024, 2025)]}).encode()),
        }
        if self.path not in routes:
            self.send_error(404)
            return
        content_type, body = routes[self.path]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class ReleaseToolsTests(unittest.TestCase):
    def test_restricted_paths(self):
        for name in ("backend/.env", "backend/.env.production", "data/raw/contest.xlsx", "backend/data/demo.sqlite", "backend/evals/results/run.json", "frontend/node_modules/pkg/index.js", "downloads/source.zip"):
            self.assertTrue(blocked_path(name), name)
        for name in ("backend/.env.example", "backend/evals/results/.gitignore", "frontend/src/App.tsx", "backend/uv.lock"):
            self.assertFalse(blocked_path(name), name)

    def test_secret_detection_redacted(self):
        key = b"sk-" + b"X" * 30
        found = secret_hits(b"some content\nOPENAI_API_KEY=" + key + b"\n")
        self.assertTrue(found)
        self.assertNotIn(key.decode(), "\n".join(found))
        self.assertFalse(secret_hits(b"OPENAI_API_KEY=\nOPENAI_MODEL=\n"))

    def test_scan_catches_secret_without_echoing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "bad.py").write_text('DASHSCOPE_' + 'API_KEY=secret_test_value', encoding="utf-8")
            failures, _ = scan(root, ["bad.py"])
            self.assertTrue(failures)
            self.assertNotIn("secret_test_value", "\n".join(failures))

    def test_lockfiles_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "README.md").write_text("safe", encoding="utf-8")
            failures, warnings = scan(root, ["README.md"], require_locks=True)
            self.assertEqual(len(failures), 2)
            self.assertFalse(warnings)

    def test_smoke_uses_read_only_get(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalResponseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            checks, warnings = verify(f"http://127.0.0.1:{server.server_address[1]}")
            self.assertEqual(len(checks), 3)
            self.assertEqual(len(warnings), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
