"""Regression coverage for cache-safe MO54 Calls frontend delivery.

These tests never open a database or read a local .env file.  They load the
API with a deliberately unreachable process-local DSN and exercise only the
root document and its static entry assets.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"


class FrontendDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_cwd = Path.cwd()
        cls.original_env = {
            name: os.environ.get(name)
            for name in ("DATABASE_URL", "CRM_ADMIN_PASSWORD")
        }
        os.environ["DATABASE_URL"] = "postgresql://postgres:test@127.0.0.1:1/frontend_delivery"
        os.environ["CRM_ADMIN_PASSWORD"] = "frontend-delivery-test-only-password"
        os.chdir(API)
        sys.path.insert(0, str(API))

        spec = importlib.util.spec_from_file_location("frontend_delivery_app", API / "app.py")
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)
        cls.client = TestClient(cls.module.app, raise_server_exceptions=True)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.module.pool.close()
        sys.modules.pop("frontend_delivery_app", None)
        try:
            sys.path.remove(str(API))
        except ValueError:
            pass
        os.chdir(cls.original_cwd)
        for name, value in cls.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def asset_url(page: str, asset_name: str) -> tuple[str, str]:
        match = re.search(
            rf'(?:href|src)="(?P<url>/assets/{re.escape(asset_name)}\?v=(?P<hash>[0-9a-f]{{64}}))"',
            page,
        )
        assert match, f"No versioned URL for {asset_name} in delivered page"
        return match.group("url"), match.group("hash")

    def test_root_injects_current_content_hashes_and_disables_html_caching(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frontend-build"], self.module.frontend_build_id())
        self.assertIn(
            f'<meta name="build-id" content="{self.module.frontend_build_id()}">',
            response.text,
        )
        self.assertEqual(response.text.count('name="build-id"'), 1)
        for asset_name in ("app.js", "styles.css"):
            _, asset_hash = self.asset_url(response.text, asset_name)
            self.assertEqual(asset_hash, self.module.frontend_asset_hash(asset_name))

    def test_only_current_versioned_entry_assets_are_immutable(self):
        page = self.client.get("/").text
        app_url, _ = self.asset_url(page, "app.js")

        current = self.client.get(app_url)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(
            current.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

        unversioned = self.client.get("/assets/app.js")
        self.assertEqual(unversioned.status_code, 200)
        self.assertEqual(unversioned.headers["cache-control"], "no-store")

        stale = self.client.get("/assets/app.js?v=" + "0" * 64)
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.headers["cache-control"], "no-store")

    def test_changed_entry_content_generates_new_asset_url_and_build_id(self):
        with tempfile.TemporaryDirectory() as directory:
            static_dir = Path(directory)
            (static_dir / "index.html").write_text(
                """<!doctype html><head><link rel=\"stylesheet\" href=\"/assets/styles.css\"></head>
                <body><script src=\"/assets/app.js\"></script></body>""",
                encoding="utf-8",
            )
            app_asset = static_dir / "app.js"
            styles_asset = static_dir / "styles.css"
            app_asset.write_text("window.version = 'one';", encoding="utf-8")
            styles_asset.write_text("body { color: green; }", encoding="utf-8")

            with mock.patch.object(self.module, "FRONTEND_STATIC_DIR", static_dir):
                first = self.module.render_frontend_index()
                first_app_url, _ = self.asset_url(first, "app.js")
                first_styles_url, _ = self.asset_url(first, "styles.css")
                first_build = self.module.frontend_build_id()

                app_asset.write_text("window.version = 'two';", encoding="utf-8")
                second = self.module.render_frontend_index()
                second_app_url, _ = self.asset_url(second, "app.js")
                self.assertNotEqual(first_app_url, second_app_url)
                self.assertEqual(first_styles_url, self.asset_url(second, "styles.css")[0])
                self.assertNotEqual(first_build, self.module.frontend_build_id())

                styles_asset.write_text("body { color: blue; }", encoding="utf-8")
                third = self.module.render_frontend_index()
                self.assertNotEqual(first_styles_url, self.asset_url(third, "styles.css")[0])


if __name__ == "__main__":
    unittest.main()
