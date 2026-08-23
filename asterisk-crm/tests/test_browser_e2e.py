"""Real-browser acceptance tests for the MO54 Calls deal workspace.

The P0 API tests use an in-process HTTP client, which is intentionally good at
checking the API contract but cannot tell us whether a cached/mobile browser
can actually follow a deal card.  This module opens the locally installed
Google Chrome through the Chrome DevTools Protocol and drives mouse events
against the real static application.  No browser binary, driver, npm package,
or Python package is downloaded.

The backend fixture is deliberately a tiny loopback-only HTTP server.  It
serves the checked-in frontend assets and returns deterministic CRM responses;
there is no production database, .env, Docker Compose project, or MO54
resource involved.  The tests therefore stay focused on the browser routing
and error-handling contract.

Run with::

    python tests/test_browser_e2e.py

The test is intentionally a required acceptance gate when Chrome is available
on the developer/CI host.  It skips only when Chrome itself is absent.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
STATIC = API / "static"
DEAL_ID = "f8f939d4-152d-4d20-b1d5-52aeb6318db8"
CONTACT_ID = "cc440ed8-e157-4b1d-95ab-f9bd6ccf1d2f"
PRIMARY_PHONE_ID = "914e9234-0cb5-4ca9-b9b8-eb0e3012e5d5"
MANUAL_CONTACT_ID = "a4a6d342-7564-45fc-99ce-3163d9079d5a"
MANUAL_PRIMARY_PHONE_ID = "9ac38e5f-065f-4994-bc2f-fccb474a0911"
MANUAL_ASSISTANT_PHONE_ID = "bfab68a1-8fb9-4f7e-b9fa-2f9c67e98791"


def chrome_binary() -> str | None:
    """Return a locally installed Chrome/Chromium binary, never install one."""

    candidates = [
        os.environ.get("CHROME_BINARY", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class DevToolsError(RuntimeError):
    pass


class CDPClient:
    """Minimal stdlib WebSocket client for the Chrome DevTools Protocol.

    Selenium/Playwright would add a large dependency and usually download a
    driver/browser.  CDP is stable for the small set of commands this test
    needs: navigation, JavaScript evaluation, viewport emulation, and native
    mouse input.
    """

    def __init__(self, websocket_url: str):
        parsed = urllib.parse.urlparse(websocket_url)
        self._socket = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        self._socket.settimeout(10)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        response = self._read_headers()
        if not response.startswith("HTTP/1.1 101"):
            self.close()
            raise DevToolsError(f"Chrome refused the DevTools WebSocket: {response.splitlines()[0] if response else 'no response'}")
        self._next_id = 0

    def _read_headers(self) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self._socket.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return data.decode("iso-8859-1", errors="replace")

    def _recv_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self._socket.recv(size - len(data))
            if not chunk:
                raise DevToolsError("Chrome closed the DevTools WebSocket")
            data.extend(chunk)
        return bytes(data)

    def _send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        mask = secrets.token_bytes(4)
        size = len(encoded)
        if size < 126:
            header = bytes((0x81, 0x80 | size))
        elif size <= 0xFFFF:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", size)
        else:
            header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(encoded))
        self._socket.sendall(header + mask + masked)

    def _recv_json(self) -> dict[str, Any]:
        fragments: list[bytes] = []
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            final = bool(first & 0x80)
            masked = bool(second & 0x80)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._recv_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(size) if size else b""
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise DevToolsError("Chrome closed the DevTools WebSocket")
            if opcode == 0x9:  # Ping.
                self._socket.sendall(bytes((0x8A, len(payload))) + payload)
                continue
            if opcode not in {0x0, 0x1}:
                continue
            fragments.append(payload)
            if final:
                return json.loads(b"".join(fragments).decode("utf-8"))

    def command(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 10) -> dict[str, Any]:
        self._next_id += 1
        command_id = self._next_id
        self._send_json({"id": command_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._socket.settimeout(max(0.1, deadline - time.monotonic()))
            message = self._recv_json()
            if message.get("id") != command_id:
                continue  # CDP events are expected while the page is loading.
            if "error" in message:
                raise DevToolsError(f"{method}: {message['error']}")
            return message.get("result", {})
        raise DevToolsError(f"Timed out waiting for Chrome command {method}")

    def close(self) -> None:
        if getattr(self, "_socket", None) is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None


class ChromeBrowser:
    """One headless Chrome tab, using actual pointer input for clicks."""

    def __init__(self):
        binary = chrome_binary()
        if not binary:
            raise unittest.SkipTest("Local Google Chrome/Chromium is required for browser acceptance tests")
        self._binary = binary
        self._profile = tempfile.TemporaryDirectory(prefix="mo54-calls-browser-")
        self._debug_port = free_port()
        self._process: subprocess.Popen[str] | None = None
        self.cdp: CDPClient | None = None

    def start(self) -> None:
        self._process = subprocess.Popen(
            [
                self._binary,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={self._debug_port}",
                f"--user-data-dir={self._profile.name}",
                "--window-size=1440,1000",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        version_url = f"http://127.0.0.1:{self._debug_port}/json/version"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(version_url, timeout=1) as response:
                    version = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(0.1)
        else:
            self.close()
            raise DevToolsError("Chrome did not expose a DevTools endpoint")

        new_target = urllib.request.Request(
            f"http://127.0.0.1:{self._debug_port}/json/new?{urllib.parse.quote('about:blank', safe='')}",
            method="PUT",
        )
        with urllib.request.urlopen(new_target, timeout=5) as response:
            target = json.load(response)
        self.cdp = CDPClient(target["webSocketDebuggerUrl"])
        self.cdp.command("Page.enable")
        self.cdp.command("Runtime.enable")

    def close(self) -> None:
        if self.cdp:
            self.cdp.close()
            self.cdp = None
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._process = None
        self._profile.cleanup()

    def _cdp(self) -> CDPClient:
        if not self.cdp:
            raise DevToolsError("Chrome was not started")
        return self.cdp

    def evaluate(self, expression: str) -> Any:
        result = self._cdp().command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True},
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise DevToolsError(f"Page evaluation failed: {details.get('text', details)}")
        remote = result.get("result", {})
        return remote.get("value")

    def wait_for(self, expression: str, *, timeout: float = 10, message: str | None = None) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = self.evaluate(expression)
            except DevToolsError:
                last = None
            if last:
                return last
            time.sleep(0.05)
        raise AssertionError(message or f"Timed out waiting for browser condition: {expression}; last={last!r}")

    def goto(self, url: str) -> None:
        self._cdp().command("Page.navigate", {"url": url})
        self.wait_for("document.readyState === 'complete'", message=f"Page did not load {url}")

    def click(self, selector: str) -> None:
        selector_json = json.dumps(selector)
        rect = self.wait_for(
            "(() => {"
            f"const element = document.querySelector({selector_json});"
            "if (!element) return null;"
            "element.scrollIntoView({block:'center', inline:'center'});"
            "const box = element.getBoundingClientRect();"
            "if (!box.width || !box.height) return null;"
            "return {x: box.left + box.width / 2, y: box.top + box.height / 2};"
            "})()",
            message=f"Clickable control not found: {selector}",
        )
        self._cdp().command("Input.dispatchMouseEvent", {"type": "mousePressed", **rect, "button": "left", "clickCount": 1})
        self._cdp().command("Input.dispatchMouseEvent", {"type": "mouseReleased", **rect, "button": "left", "clickCount": 1})

    def fill(self, selector: str, value: str) -> None:
        """Enter text through Chrome's input channel, then submit with a native click."""

        self.click(selector)
        self.evaluate(
            "(() => {"
            f"const element = document.querySelector({json.dumps(selector)});"
            "element.value = ''; element.dispatchEvent(new Event('input', {bubbles:true}));"
            "})()"
        )
        self._cdp().command("Input.insertText", {"text": value})

    def viewport(self, width: int, height: int, *, mobile: bool) -> None:
        self._cdp().command(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile},
        )

    def text(self, selector: str) -> str:
        return str(self.evaluate(f"document.querySelector({json.dumps(selector)})?.textContent || ''"))

    def exists(self, selector: str) -> bool:
        return bool(self.evaluate(f"Boolean(document.querySelector({json.dumps(selector)}))"))


class BrowserFixture:
    """Loopback-only mock CRM backend for static frontend browser tests."""

    def __init__(self):
        self.fail_detail = False
        self.fail_cashflow = False
        self.fail_contact_detail_once = False
        self.callback_returns_call_id_immediately = True
        self.requests: list[str] = []
        self.post_bodies: list[tuple[str, dict[str, Any]]] = []
        self.contacts: dict[str, dict[str, Any]] = {
            CONTACT_ID: {
                "id": CONTACT_ID,
                "full_name": "Browser Client",
                "phone_normalized": "+79990001122",
                "email": None,
                "notes": None,
                "calls": [],
                "deals": [],
                "phone_numbers": [{
                    "id": PRIMARY_PHONE_ID, "contact_id": CONTACT_ID,
                    "phone_normalized": "+79990001122", "label": "Основной",
                    "role": "customer", "is_primary": True, "active": True,
                }],
            },
        }
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _send(self, status: int, content: bytes, content_type: str, *, cache_control: str = "no-store") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", cache_control)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def _json(self, body: Any, status: int = 200) -> None:
                self._send(status, json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def _request_json(self) -> dict[str, Any]:
                raw_size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(raw_size) if raw_size else b"{}"
                try:
                    value = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    value = {}
                return value if isinstance(value, dict) else {}

            def _contact_list(self) -> list[dict[str, Any]]:
                return [{
                    "id": contact["id"], "full_name": contact.get("full_name"),
                    "phone_normalized": contact.get("phone_normalized"), "email": contact.get("email"),
                    "calls_count": len(contact.get("calls") or []), "last_call_at": None,
                } for contact in fixture.contacts.values()]

            def _contact_detail(self, contact_id: str) -> dict[str, Any] | None:
                contact = fixture.contacts.get(contact_id)
                if not contact:
                    return None
                detail = dict(contact)
                detail["phone_numbers"] = list(contact.get("phone_numbers") or [])
                detail["calls"] = list(contact.get("calls") or [])
                detail["deals"] = [self.deal_card()] if contact_id == CONTACT_ID else list(contact.get("deals") or [])
                return detail

            def _asset_path(self, url_path: str) -> Path | None:
                name = Path(url_path).name
                # Both query-hashed paths and fingerprinted filenames resolve to
                # the checked-in source asset in this test fixture.
                if name.startswith("app.") and name.endswith(".js"):
                    name = "app.js"
                elif name.startswith("styles.") and name.endswith(".css"):
                    name = "styles.css"
                candidate = (STATIC / name).resolve()
                return candidate if STATIC.resolve() in candidate.parents and candidate.is_file() else None

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler name.
                request_path = urllib.parse.urlparse(self.path).path
                fixture.requests.append(request_path)
                if request_path == "/":
                    self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                    return
                if request_path.startswith("/assets/"):
                    asset = self._asset_path(request_path)
                    if not asset:
                        self._json({"detail": "asset not found"}, 404)
                        return
                    content_type = "application/javascript; charset=utf-8" if asset.suffix == ".js" else "text/css; charset=utf-8"
                    self._send(200, asset.read_bytes(), content_type, cache_control="public, max-age=31536000, immutable")
                    return
                if request_path == "/api/me":
                    self._json({"id": "e97e2f6d-4f1a-4cd5-84b7-a9a4978d24ab", "email": "browser@test.local", "display_name": "Browser Tester", "role": "admin", "must_change_password": False})
                    return
                if request_path == "/api/dashboard":
                    self._json({})
                    return
                if request_path == "/api/dashboard/season":
                    self._json({"earned_owner_income": 0, "projected_owner_income": 12000, "safe_cash": 0, "remaining_to_goal": 800000, "goal_owner_income": 800000, "net_confirmed_customer_cash": 0, "realized_cost_outflows": 0, "open_reserved_obligations": 0})
                    return
                if request_path == "/api/calls":
                    self._json([])
                    return
                if request_path.startswith("/api/calls/initiations/"):
                    body = {"id": request_path.rsplit("/", 1)[-1], "status": "accepted"}
                    if fixture.callback_returns_call_id_immediately:
                        body["call_id"] = 42
                    self._json(body)
                    return
                if request_path == "/api/tasks":
                    self._json([])
                    return
                if request_path == "/api/pipeline":
                    self._json([{"stage": "proposal_sent", "qualification_segment": "over_80k", "count": 1, "projected_owner_income": 12000}])
                    return
                if request_path == "/api/pipeline/deals":
                    self._json([self.deal_card()])
                    return
                if request_path == "/api/deals":
                    self._json([self.deal_card()])
                    return
                if request_path == "/api/contacts":
                    self._json(self._contact_list())
                    return
                if request_path.startswith("/api/contacts/"):
                    if fixture.fail_contact_detail_once:
                        fixture.fail_contact_detail_once = False
                        self._json({"detail": "temporary contact refresh failure"}, 500)
                        return
                    detail = self._contact_detail(request_path.rsplit("/", 1)[-1])
                    if detail:
                        self._json(detail)
                    else:
                        self._json({"detail": "contact not found"}, 404)
                    return
                if request_path == f"/api/deals/{DEAL_ID}":
                    if fixture.fail_detail:
                        self._json({"detail": "Тестовая ошибка карточки сделки"}, 500)
                    else:
                        self._json(self.deal_detail())
                    return
                if request_path == f"/api/deals/{DEAL_ID}/cashflow":
                    if fixture.fail_cashflow:
                        self._json({"detail": "Тестовая ошибка cashflow"}, 500)
                    else:
                        self._json({"confirmed_customer_cash": 0, "refunds": 0, "net_confirmed_customer_cash": 0, "realized_costs": 0, "open_obligations": 0, "other_reserved_cash": 0, "safe_cash": 0, "movements": [], "obligations": []})
                    return
                self._json({"detail": f"Unhandled browser fixture route: {request_path}"}, 404)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler name.
                request_path = urllib.parse.urlparse(self.path).path
                body = self._request_json()
                fixture.requests.append(request_path)
                fixture.post_bodies.append((request_path, body))
                if request_path == "/api/contacts":
                    contact = {
                        "id": MANUAL_CONTACT_ID,
                        "full_name": body.get("full_name") or "Manual browser client",
                        "phone_normalized": "+79992223344",
                        "email": body.get("email"), "notes": body.get("notes"),
                        "calls": [], "deals": [],
                        "phone_numbers": [{
                            "id": MANUAL_PRIMARY_PHONE_ID, "contact_id": MANUAL_CONTACT_ID,
                            "phone_normalized": "+79992223344", "label": "Основной",
                            "role": "customer", "is_primary": True, "active": True,
                        }],
                    }
                    fixture.contacts[MANUAL_CONTACT_ID] = contact
                    self._json({**contact, "phone_numbers": list(contact["phone_numbers"])}, 201)
                    return
                phone_prefix = f"/api/contacts/{MANUAL_CONTACT_ID}/phone-numbers"
                if request_path == phone_prefix:
                    contact = fixture.contacts.get(MANUAL_CONTACT_ID)
                    if not contact:
                        self._json({"detail": "contact not found"}, 404)
                        return
                    phone = {
                        "id": MANUAL_ASSISTANT_PHONE_ID, "contact_id": MANUAL_CONTACT_ID,
                        "phone_normalized": "+79992223345", "label": body.get("label") or "Помощник",
                        "role": body.get("role") or "assistant", "is_primary": False, "active": True,
                    }
                    contact["phone_numbers"] = [contact["phone_numbers"][0], phone]
                    self._json(phone, 201)
                    return
                if request_path == "/api/calls/initiate":
                    body = {"id": "c3d6ef2b-8f41-4ddf-9d04-9c09526c1cc8", "status": "accepted", "call_session_id": "browser-manual", "duplicate": False}
                    if fixture.callback_returns_call_id_immediately:
                        body["call_id"] = 42
                    self._json(body)
                    return
                self._json({"detail": f"Unhandled browser fixture POST route: {request_path}"}, 404)

            @staticmethod
            def deal_card() -> dict[str, Any]:
                return {"id": DEAL_ID, "title": "Browser terrace deal", "stage": "proposal_sent", "qualification_segment": "over_80k", "contact_name": "Browser Client", "phone_normalized": "+79990001122", "commercial_value": 150000, "quoted_price": 150000, "final_contract_price": None, "amount": 150000, "projected_owner_income": 12000}

            @staticmethod
            def deal_detail() -> dict[str, Any]:
                return {"id": DEAL_ID, "title": "Browser terrace deal", "stage": "proposal_sent", "qualification_segment": "over_80k", "quoted_price": 150000, "final_contract_price": None, "estimated_budget_min": 100000, "estimated_budget_max": 150000, "pain_primary": "Тень на террасе", "decision_makers": ["Елена"], "alternative_considered": "Маркиза", "desired_install_period": "Сентябрь", "next_contact_at": None, "economics_revisions": [{"revision": 1, "ae_amount": 15000, "ae_percent": 0.1, "price_floor_ae_8": 147000, "price_floor_ae_10": 150000, "price_floor_ae_12": 153000, "owner_income_solo": 15000, "owner_income_with_partner": 7500, "economics_status": "healthy", "settings_version": 1}]}

        class QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
            # Chrome closes speculative connections aggressively.  Those are
            # normal for a browser test and must not obscure a real assertion
            # failure with stdlib server tracebacks on Windows.
            def handle_error(self, request: socket.socket, client_address: tuple[str, int]) -> None:
                error = sys.exc_info()[1]
                if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                    return
                super().handle_error(request, client_address)

        self._server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="browser-fixture", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


@contextmanager
def browser_session() -> Any:
    fixture = BrowserFixture()
    browser = ChromeBrowser()
    fixture.start()
    try:
        browser.start()
        yield fixture, browser
    finally:
        browser.close()
        fixture.close()


class DealWorkspaceBrowserAcceptanceTests(unittest.TestCase):
    """Desktop and mobile workflow acceptance, driven through native clicks."""

    @classmethod
    def setUpClass(cls) -> None:
        if not chrome_binary():
            raise unittest.SkipTest("Google Chrome/Chromium is not installed on this host")

    def assert_workspace(self, browser: ChromeBrowser) -> None:
        browser.wait_for(
            "Boolean(document.querySelector('#deal-workspace, [data-workspace=\"deal\"]'))",
            message="Deal workspace did not render",
        )
        self.assertIn("Browser terrace deal", browser.text("#title"))
        self.assertNotEqual("Обзор", browser.text("#title").strip())

    def test_desktop_pipeline_card_opens_the_deal_workspace(self) -> None:
        with browser_session() as (fixture, browser):
            browser.viewport(1440, 1000, mobile=False)
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            browser.click('button[data-view="pipeline"]')
            deal_selector = f'a.deal-link[data-deal-id="{DEAL_ID}"]'
            browser.wait_for(
                f"document.querySelector({json.dumps(deal_selector)})?.textContent.includes('Открыть сделку')",
                message="Pipeline card did not render its explicit deal-opening affordance",
            )
            self.assertIn("Открыть сделку", browser.text(deal_selector))
            browser.click(deal_selector)
            self.assert_workspace(browser)
            self.assertEqual(f"#/deals/{DEAL_ID}?from=pipeline", browser.evaluate("window.location.hash"))
            self.assertIn(f"/api/deals/{DEAL_ID}", fixture.requests)

    def test_mobile_contact_deal_and_contextual_back(self) -> None:
        with browser_session() as (fixture, browser):
            browser.viewport(390, 844, mobile=True)
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            browser.click('button[data-view="contacts"]')
            browser.click(f'[data-action="contact-detail"][data-contact-id="{CONTACT_ID}"]')
            browser.wait_for("document.querySelector('#title')?.textContent.includes('Browser Client')")
            browser.click(f'a.deal-link[data-deal-id="{DEAL_ID}"]')
            self.assert_workspace(browser)
            browser.click('[data-action="deal-back"]')
            browser.wait_for("document.querySelector('#title')?.textContent.includes('Browser Client')")
            self.assertIn("Browser terrace deal", browser.text("#content"))

    def test_mobile_manager_can_create_client_add_assistant_and_call_selected_number(self) -> None:
        with browser_session() as (fixture, browser):
            browser.viewport(390, 844, mobile=True)
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            browser.click('button[data-view="contacts"]')
            browser.wait_for("document.querySelector('#title')?.textContent.includes('Клиенты') && Boolean(document.querySelector('[data-action=\"contact-create-open\"]'))")
            browser.click('[data-action="contact-create-open"]')
            browser.fill('#contact-create-form input[name="full_name"]', 'Manual browser client')
            browser.fill('#contact-create-form input[name="phone"]', '+7 999 222-33-44')
            browser.click('#contact-create-form button[type="submit"]')
            browser.wait_for("document.querySelector('#title')?.textContent.includes('Manual browser client')")
            created_payloads = [body for path, body in fixture.post_bodies if path == '/api/contacts']
            self.assertEqual(created_payloads, [{
                'full_name': 'Manual browser client',
                'phone': '+7 999 222-33-44',
                'email': None,
                'notes': None,
            }])
            browser.wait_for("Boolean(document.querySelector('#contact-phone-form'))")
            phone_panel_layout = browser.evaluate(
                "(() => ({"
                "phones: document.querySelector('.contact-phone-panel')?.getBoundingClientRect().top,"
                "history: Array.from(document.querySelectorAll('.contact-detail-layout .panel'))"
                ".find((panel) => panel.querySelector('h2')?.textContent.includes('История звонков'))"
                "?.getBoundingClientRect().top"
                "}))()"
            )
            self.assertLess(phone_panel_layout['phones'], phone_panel_layout['history'])
            browser.fill('#contact-phone-form input[name="phone"]', '+7 999 222-33-45')
            browser.fill('#contact-phone-form input[name="label"]', 'Помощник Ольга')
            browser.click('#contact-phone-form button[type="submit"]')
            assistant_button = f'[data-action="contact-phone-call"][data-phone-number-id="{MANUAL_ASSISTANT_PHONE_ID}"]'
            browser.wait_for(f"Boolean(document.querySelector({json.dumps(assistant_button)}))")
            self.assertIn('Помощник Ольга', browser.text('#content'))
            added_payloads = [body for path, body in fixture.post_bodies if path.endswith('/phone-numbers')]
            self.assertEqual(added_payloads, [{
                'phone': '+7 999 222-33-45',
                'label': 'Помощник Ольга',
                'role': 'other',
                'make_primary': False,
            }])
            fixture.callback_returns_call_id_immediately = False
            browser.click(assistant_button)
            browser.wait_for(
                "Array.from(document.querySelectorAll('.callback-status')).some((node) => "
                "node.textContent.includes('Novofon'))",
                message="Selected assistant phone did not enter the protected callback UI flow",
            )
            initiated = [body for path, body in fixture.post_bodies if path == '/api/calls/initiate']
            self.assertEqual(len(initiated), 1)
            self.assertEqual(initiated[0]['contact_id'], MANUAL_CONTACT_ID)
            self.assertEqual(initiated[0]['contact_phone_number_id'], MANUAL_ASSISTANT_PHONE_ID)
            self.assertTrue(initiated[0]['idempotency_key'])
            self.assertTrue(any(path.startswith('/api/calls/initiations/') for path in fixture.requests))
            layout = browser.evaluate(
                "(() => {"
                f"const button = document.querySelector({json.dumps(assistant_button)});"
                "const row = button.closest('.contact-phone-row');"
                "return {buttonWidth:Math.round(button.getBoundingClientRect().width),rowWidth:Math.round(row.getBoundingClientRect().width)};"
                "})()"
            )
            self.assertGreaterEqual(layout['buttonWidth'], 300)
            self.assertGreaterEqual(layout['rowWidth'], layout['buttonWidth'])

    def test_read_only_contact_does_not_mislabel_active_number_as_disabled(self) -> None:
        with browser_session() as (fixture, browser):
            fixture.contacts[CONTACT_ID]['can_write'] = False
            browser.viewport(390, 844, mobile=True)
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            browser.click('button[data-view="contacts"]')
            browser.click(f'[data-action="contact-detail"][data-contact-id="{CONTACT_ID}"]')
            browser.wait_for("Boolean(document.querySelector('.contact-phone-row'))")
            self.assertIn('Только просмотр', browser.text('.contact-phone-row'))
            self.assertNotIn('Номер отключён', browser.text('.contact-phone-row'))
            self.assertFalse(browser.exists('[data-action="contact-phone-call"]'))

    def test_saved_manual_client_is_not_reported_as_unsaved_when_detail_refresh_fails(self) -> None:
        with browser_session() as (fixture, browser):
            browser.viewport(390, 844, mobile=True)
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            browser.click('button[data-view="contacts"]')
            browser.wait_for("Boolean(document.querySelector('[data-action=\"contact-create-open\"]'))")
            browser.click('[data-action="contact-create-open"]')
            browser.fill('#contact-create-form input[name="full_name"]', 'Refresh-safe client')
            browser.fill('#contact-create-form input[name="phone"]', '+7 999 222-33-44')
            fixture.fail_contact_detail_once = True
            browser.click('#contact-create-form button[type="submit"]')
            browser.wait_for("document.querySelector('#contact-create-error.saved')?.textContent.includes('Клиент сохранён')")
            self.assertTrue(browser.exists('[data-contact-refresh-retry="true"]'))
            self.assertEqual('Клиенты', browser.text('#title').strip())
            browser.click('[data-contact-refresh-retry="true"]')
            browser.wait_for("document.querySelector('#title')?.textContent.includes('Refresh-safe client')")

    def test_mobile_navigation_exposes_four_primary_sections_and_more_sheet(self) -> None:
        with browser_session() as (fixture, browser):
            browser.viewport(390, 844, mobile=True)
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            visible_controls = browser.evaluate(
                "Array.from(document.querySelectorAll('aside nav button')).filter((button) => {"
                "const style = getComputedStyle(button); const box = button.getBoundingClientRect();"
                "return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;"
                "}).sort((left, right) => left.getBoundingClientRect().left - right.getBoundingClientRect().left)"
                ".map((button) => button.dataset.view || button.dataset.action)"
            )
            self.assertEqual(["dashboard", "pipeline", "contacts", "calls", "mobile-more"], visible_controls)
            mobile_labels = browser.evaluate(
                "Array.from(document.querySelectorAll('aside nav button')).filter((button) => {"
                "const style = getComputedStyle(button); const box = button.getBoundingClientRect();"
                "return style.display !== 'none' && box.width > 0 && box.height > 0;"
                "}).map((button) => ({"
                "label: Array.from(button.querySelectorAll('.nav-mobile-label')).find((label) => getComputedStyle(label).display !== 'none')?.textContent.trim() || '',"
                "fontSize: getComputedStyle(button).fontSize"
                "}))"
            )
            self.assertTrue(all(item["label"] and item["fontSize"] != "0px" for item in mobile_labels))
            browser.click('button[data-action="mobile-more"]')
            browser.wait_for("Boolean(document.querySelector('#mobile-more-sheet:not(.hidden)'))")
            self.assertNotEqual("—", browser.text("#mobile-build-id").strip())
            browser.click('#mobile-more-sheet [data-action="navigate"][data-view="deals"]')
            browser.wait_for("Boolean(document.querySelector('#deal-list, .deal-list'))")
            self.assertEqual("Сделки", browser.text("#title").strip())
            browser.click('button[data-action="mobile-more"]')
            browser.wait_for("Boolean(document.querySelector('#mobile-more-sheet:not(.hidden)'))")
            browser.click('#mobile-more-sheet [data-action="mobile-logout"]')
            browser.wait_for("!document.querySelector('#login').classList.contains('hidden')")
            self.assertTrue(browser.evaluate("document.querySelector('#mobile-more-sheet').classList.contains('hidden')"))

    def test_mobile_pipeline_cards_are_readable_without_horizontal_lane_scroll(self) -> None:
        with browser_session() as (fixture, browser):
            browser.viewport(390, 844, mobile=True)
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            browser.click('button[data-view="pipeline"]')
            card_selector = f'.pipeline a.deal-card[data-deal-id="{DEAL_ID}"]'
            browser.wait_for(f"Boolean(document.querySelector({json.dumps(card_selector)}))")
            layout = browser.evaluate(
                "(() => {"
                f"const card = document.querySelector({json.dumps(card_selector)});"
                "const pipeline = document.querySelector('.pipeline');"
                "const stage = card.closest('.stage');"
                "return {"
                "cardWidth: Math.round(card.getBoundingClientRect().width),"
                "stageWidth: Math.round(stage.getBoundingClientRect().width),"
                "scrollWidth: Math.round(pipeline.scrollWidth),"
                "clientWidth: Math.round(pipeline.clientWidth),"
                "color: getComputedStyle(card).color,"
                "decoration: getComputedStyle(card).textDecorationLine"
                "};"
                "})()"
            )
            self.assertGreaterEqual(layout["cardWidth"], 280)
            self.assertGreaterEqual(layout["stageWidth"], layout["cardWidth"])
            self.assertLessEqual(layout["scrollWidth"], layout["clientWidth"] + 1)
            self.assertNotEqual("rgb(0, 0, 238)", layout["color"])
            self.assertEqual("none", layout["decoration"])

    def test_deals_tab_never_falls_back_to_dashboard(self) -> None:
        with browser_session() as (fixture, browser):
            browser.goto(fixture.url + "/")
            browser.wait_for("document.querySelector('#workspace') && !document.querySelector('#workspace').classList.contains('hidden')")
            browser.click('button[data-view="deals"]')
            browser.wait_for("Boolean(document.querySelector('#deal-list, .deal-list'))")
            self.assertEqual("Сделки", browser.text("#title").strip())
            self.assertIn("Browser terrace deal", browser.text("#content"))

    def test_legacy_query_is_normalized_to_hash_route(self) -> None:
        with browser_session() as (fixture, browser):
            browser.goto(f"{fixture.url}/?deal={DEAL_ID}")
            self.assert_workspace(browser)
            browser.wait_for(f"window.location.hash === '#/deals/{DEAL_ID}'")
            self.assertEqual("", browser.evaluate("window.location.search"))

    def test_cashflow_error_keeps_deal_information_and_offers_inline_retry(self) -> None:
        with browser_session() as (fixture, browser):
            fixture.fail_cashflow = True
            browser.goto(f"{fixture.url}/#/deals/{DEAL_ID}")
            self.assert_workspace(browser)
            browser.wait_for("Boolean(document.querySelector('#cashflow-error'))")
            self.assertIn("Тестовая ошибка cashflow", browser.text("#cashflow-error"))
            self.assertTrue(browser.exists('[data-action="retry-cashflow"]'))
            self.assertIn("Квалификация", browser.text("#content"))
            self.assertNotEqual("Обзор", browser.text("#title").strip())
            fixture.fail_cashflow = False
            browser.click('[data-action="retry-cashflow"]')
            browser.wait_for(
                "!document.querySelector('#cashflow-error') && Boolean(document.querySelector('#cash-movement-form'))",
                message="Cashflow retry did not replace its inline error with the ledger workspace",
            )

    def test_detail_error_is_visible_and_does_not_redirect_to_dashboard(self) -> None:
        with browser_session() as (fixture, browser):
            fixture.fail_detail = True
            browser.goto(f"{fixture.url}/#/deals/{DEAL_ID}")
            browser.wait_for("Boolean(document.querySelector('#deal-detail-error'))")
            self.assertIn("Тестовая ошибка карточки сделки", browser.text("#deal-detail-error"))
            self.assertTrue(browser.exists('[data-action="retry-deal-detail"]'))
            self.assertNotEqual("Обзор", browser.text("#title").strip())
            fixture.fail_detail = False
            browser.click('[data-action="retry-deal-detail"]')
            browser.wait_for(
                "!document.querySelector('#deal-detail-error') && Boolean(document.querySelector('#deal-workspace form'))",
                message="Deal-detail retry did not replace its inline error with qualification/economics",
            )


if __name__ == "__main__":
    unittest.main()
