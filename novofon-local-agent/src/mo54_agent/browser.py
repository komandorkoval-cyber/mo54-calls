from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AgentConfig, AgentPaths
from .errors import BrowserInteractionError, StableIdentifierMissing
from .media import find_media_tool


_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_SAFE_EXTENSIONS = {".wav", ".mp3", ".ogg", ".m4a", ".aac", ".flac", ".opus"}


@dataclass(frozen=True)
class BrowserCall:
    call_session_id: str
    started_at: datetime
    duration_sec: int | None


@dataclass(frozen=True)
class DownloadedAudio:
    path: Path
    sha256: str
    duration_sec: int


def parse_novofon_datetime(value: str, timezone_name: str) -> datetime:
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                parsed = datetime.strptime(value, pattern)
                break
            except ValueError:
                continue
        else:
            raise BrowserInteractionError("call_date_missing", "Novofon row has no parseable call date", retryable=False)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(timezone_name))


def parse_duration(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if not all(part.isdigit() for part in parts) or len(parts) not in {2, 3}:
        return None
    numbers = [int(part) for part in parts]
    return numbers[0] * 60 + numbers[1] if len(numbers) == 2 else numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def media_duration(path: Path) -> int:
    try:
        ffprobe = find_media_tool("ffprobe")
        if not ffprobe:
            raise OSError("ffprobe is unavailable")
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        duration = round(float(result.stdout.strip()))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise BrowserInteractionError("media_validation_failed", "Downloaded audio could not be validated", retryable=True) from exc
    if duration <= 0:
        raise BrowserInteractionError("media_duration_invalid", "Downloaded audio duration is invalid", retryable=True)
    return duration


class NovofonBrowser:
    """Headed Edge automation. It never reads or constructs provider media URLs."""

    def __init__(self, paths: AgentPaths, config: AgentConfig):
        self.paths = paths
        self.config = config

    def _context(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserInteractionError("playwright_missing", "Playwright is not installed", retryable=False) from exc
        return sync_playwright()

    def provision(self, wait_seconds: int) -> None:
        # The public cabinet root is safe for a first manual login. Automated
        # inventory still requires a separately configured exact calls-list URL.
        target_url = self.config.novofon_calls_url or "https://my.novofon.ru/"
        with self._context() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.paths.browser_profile), channel="msedge", headless=False, accept_downloads=True
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(max(1, wait_seconds) * 1000)
            finally:
                context.close()

    def _rows_to_calls(self, rows: list[dict]) -> list[BrowserCall]:
        calls: list[BrowserCall] = []
        for row in rows:
            session = str(row.get("session") or "").strip()
            if not _SAFE_SESSION_ID.fullmatch(session):
                raise StableIdentifierMissing()
            started = parse_novofon_datetime(str(row.get("started") or ""), self.config.timezone)
            calls.append(BrowserCall(session, started, parse_duration(row.get("duration"))))
        return calls

    def inventory(self) -> list[BrowserCall]:
        if not self.config.novofon_calls_url:
            raise BrowserInteractionError("novofon_url_missing", "Set novofon_calls_url in local config", retryable=False)
        selectors = self.config.selectors
        with self._context() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.paths.browser_profile), channel="msedge", headless=False, accept_downloads=True
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.config.novofon_calls_url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_selector(selectors.call_rows, timeout=20_000)
                except Exception as exc:
                    raise BrowserInteractionError("novofon_session_or_layout_unavailable", "Novofon session or call list is unavailable", retryable=True) from exc
                rows = page.locator(selectors.call_rows)
                data = rows.evaluate_all(
                    """(items, cfg) => items.map(item => ({
                      session: cfg.sessionSelector ? item.querySelector(cfg.sessionSelector)?.textContent : item.getAttribute(cfg.session),
                      started: cfg.startedSelector ? item.querySelector(cfg.startedSelector)?.textContent : item.getAttribute(cfg.started),
                      duration: cfg.durationSelector ? item.querySelector(cfg.durationSelector)?.textContent : item.getAttribute(cfg.duration)
                    }))""",
                    {
                        "session": selectors.session_id_attribute,
                        "sessionSelector": selectors.session_id_selector,
                        "started": selectors.started_at_attribute,
                        "duration": selectors.duration_attribute,
                        "startedSelector": selectors.started_at_selector,
                        "durationSelector": selectors.duration_selector,
                    },
                )
                return self._rows_to_calls(data)
            finally:
                context.close()

    def inspect_layout(self) -> dict:
        """Return a PII-free description used once to configure a new UI.

        Attribute values and row text are deliberately never returned.  Values
        are reduced to a type/length shape so diagnostics cannot leak calls,
        phone numbers, contacts, or URLs into the local log/terminal.
        """
        if not self.config.novofon_calls_url:
            raise BrowserInteractionError("novofon_url_missing", "Set novofon_calls_url in local config", retryable=False)
        with self._context() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.paths.browser_profile), channel="msedge", headless=False, accept_downloads=True
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.config.novofon_calls_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(10_000)
                button_hints: list[dict[str, object]] = []
                first_row = page.locator("tr[data-row-key]").first
                first_row_buttons = first_row.locator("button")
                for index in range(first_row_buttons.count()):
                    button = first_row_buttons.nth(index)
                    try:
                        button.hover(timeout=2_000)
                        page.wait_for_timeout(350)
                        tooltip_text = " ".join(page.locator('[role="tooltip"]').all_inner_texts()).lower()
                        direct_text = " ".join(filter(None, [button.get_attribute("aria-label"), button.get_attribute("title"), button.inner_text()])).lower()
                        combined = f"{tooltip_text} {direct_text}"
                        button_hints.append({
                            "ordinal": index,
                            "hints": [hint for hint, pattern in {
                                "save": r"сохран", "download": r"скача|download", "listen": r"слуш|play", "menu": r"ещ|more|menu"
                            }.items() if re.search(pattern, combined)],
                        })
                    except Exception:
                        button_hints.append({"ordinal": index, "hints": []})
                layout = page.evaluate(
                    """() => {
                      const shape = (value) => {
                        const text = String(value || '');
                        if (!text) return 'empty';
                        if (/^\\d+$/.test(text)) return `digits:${text.length}`;
                        if (/^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(text)) return 'uuid_like';
                        if (/^[A-Za-z0-9_.:-]+$/.test(text)) return `token:${Math.min(text.length, 200)}`;
                        return `other:${Math.min(text.length, 200)}`;
                      };
                      const rows = [...document.querySelectorAll('tr,[role="row"],li')];
                      const dataAttributes = new Set();
                      const idAttributes = new Set();
                      const rowShapes = rows.slice(0, 12).map(row => {
                        const attrs = {};
                        for (const attr of row.attributes) {
                          if (attr.name.startsWith('data-')) {
                            dataAttributes.add(attr.name);
                            attrs[attr.name] = shape(attr.value);
                          }
                          if (/id|call|session|record/i.test(attr.name)) {
                            idAttributes.add(attr.name);
                            attrs[attr.name] = shape(attr.value);
                          }
                        }
                        const descendants = [...row.querySelectorAll('*')];
                        const descendantIdentifiers = {};
                        for (const node of descendants) for (const attr of node.attributes) {
                          if (attr.name.startsWith('data-')) dataAttributes.add(attr.name);
                          if (/id|call|session|record|key/i.test(attr.name)) {
                            const key = `${node.tagName.toLowerCase()}.${attr.name}`;
                            idAttributes.add(key);
                            descendantIdentifiers[key] = shape(attr.value);
                          }
                        }
                        const actionControls = descendants.filter(node => ['button', 'a'].includes(node.tagName.toLowerCase()) || node.getAttribute('role') === 'button').map(node => {
                          const label = [node.getAttribute('aria-label'), node.getAttribute('title'), node.textContent].filter(Boolean).join(' ').toLowerCase();
                          return {
                            tag: node.tagName.toLowerCase(), role: node.getAttribute('role') || null,
                            data_attributes: [...node.attributes].map(attr => attr.name).filter(name => name.startsWith('data-')).sort(),
                            action_hints: ['save', 'download', 'listen', 'menu'].filter(hint => ({save: /сохран/.test(label), download: /скача|download/.test(label), listen: /слуш|play/.test(label), menu: /ещ|more|menu/.test(label)}[hint]))
                          };
                        });
                        return {tag: row.tagName.toLowerCase(), role: row.getAttribute('role') || null, id_value_shapes: {...attrs, ...descendantIdentifiers}, controls: actionControls};
                      });
                      const controls = [...document.querySelectorAll('button,a,[role="button"]')].map(node => {
                        const label = [node.getAttribute('aria-label'), node.getAttribute('title'), node.textContent].filter(Boolean).join(' ').toLowerCase();
                        return {
                          save_like: /сохран|скача|download/.test(label),
                          tag: node.tagName.toLowerCase(), role: node.getAttribute('role') || null,
                          data_attributes: [...node.attributes].map(a => a.name).filter(name => name.startsWith('data-')).sort()
                        };
                      });
                      const firstDataRow = document.querySelector('tr[data-row-key]');
                      const recordingCell = firstDataRow?.querySelector('td:nth-child(15)');
                      const recordingControlShape = recordingCell ? [...recordingCell.querySelectorAll('button,a,[role="button"]')].map(node => ({
                        tag: node.tagName.toLowerCase(),
                        role: node.getAttribute('role') || null,
                        attribute_names: [...node.attributes].map(attribute => attribute.name).sort(),
                        has_svg: Boolean(node.querySelector('svg')),
                        has_text: Boolean(node.textContent.trim())
                      })) : [];
                      const safeUrl = new URL(location.href);
                      return {
                        page: safeUrl.origin + safeUrl.pathname,
                        title: document.title.slice(0, 120),
                        table_headers: [...document.querySelectorAll('th')].map(header => header.innerText.trim().slice(0, 80)),
                        document_state: document.readyState,
                        page_signals: {
                          has_password_field: Boolean(document.querySelector('input[type="password"]')),
                          has_login_like_text: /войти|авториз|login|sign in/i.test(document.body.innerText),
                          has_calls_like_text: /звонк|call|запис/i.test(document.body.innerText),
                          total_buttons: document.querySelectorAll('button,[role="button"]').length,
                          total_links: document.querySelectorAll('a').length
                        },
                        row_counts: {table_rows: document.querySelectorAll('tr').length, aria_rows: document.querySelectorAll('[role="row"]').length, list_items: document.querySelectorAll('li').length},
                        data_attributes: [...dataAttributes].sort(),
                        identifier_attributes: [...idAttributes].sort(),
                        rows: rowShapes,
                        save_controls: controls.filter(control => control.save_like).map(({tag, role, data_attributes}) => ({tag, role, data_attributes}))
                        ,recording_control_shape: recordingControlShape
                      };
                    }"""
                )
                layout["first_row_button_hints"] = button_hints
                return layout
            finally:
                context.close()

    def download(self, call_session_id: str) -> DownloadedAudio:
        if not _SAFE_SESSION_ID.fullmatch(call_session_id):
            raise StableIdentifierMissing()
        selectors = self.config.selectors
        with self._context() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.paths.browser_profile), channel="msedge", headless=False, accept_downloads=True
            )
            temporary: Path | None = None
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.config.novofon_calls_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_selector(selectors.call_rows, timeout=20_000)
                rows = page.locator(selectors.call_rows)
                matched_row = None
                for index in range(rows.count()):
                    candidate = rows.nth(index)
                    candidate_session = (
                        candidate.locator(selectors.session_id_selector).inner_text().strip()
                        if selectors.session_id_selector
                        else candidate.get_attribute(selectors.session_id_attribute)
                    )
                    if candidate_session == call_session_id:
                        matched_row = candidate
                        break
                if matched_row is None:
                    raise BrowserInteractionError("call_not_visible", "The exact call is not visible in the Novofon list", retryable=True)
                button = matched_row.locator(selectors.download_button)
                if button.count() != 1 or not button.is_enabled():
                    raise BrowserInteractionError("download_button_unavailable", "Novofon save button is unavailable", retryable=True)
                try:
                    with page.expect_download(timeout=60_000) as expected:
                        button.click()
                    download = expected.value
                except Exception as exc:
                    raise BrowserInteractionError("download_interrupted", "Novofon download did not complete", retryable=True) from exc
                extension = Path(download.suggested_filename).suffix.lower()
                if extension not in _SAFE_EXTENSIONS:
                    raise BrowserInteractionError("audio_format_invalid", "Novofon returned an unsupported audio format", retryable=False)
                temporary = self.paths.staging / f"{os.urandom(16).hex()}{extension}.part"
                download.save_as(str(temporary))
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise BrowserInteractionError("audio_empty", "Novofon returned an empty download", retryable=True)
                duration = media_duration(temporary)
                digest = sha256_file(temporary)
                final = self.paths.audio / f"{digest}{extension}"
                if final.exists():
                    temporary.unlink(missing_ok=True)
                else:
                    temporary.replace(final)
                temporary = None
                return DownloadedAudio(final, digest, duration)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
                context.close()
