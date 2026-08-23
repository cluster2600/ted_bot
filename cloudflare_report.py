#!/usr/bin/env python3
"""Publish the static TED J+30 report to a Cloudflare Pages FQDN."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import patch

import requests
from blake3 import blake3


API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_PROJECT = "ted-bot-j30-report"
REPORT_FILENAME = "ted-alertes-j30.html"
CHART_FILENAME = "ted-alertes-j30.png"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PROJECT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?$")

PAGES_HEADERS = """/*
  Cache-Control: no-store
  Content-Security-Policy: default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  X-Robots-Tag: noindex, nofollow
"""

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"


class PublishError(RuntimeError):
    """A report-publication error that never includes credential material."""


def _safe_project(value: str) -> str:
    project = value.strip().lower()
    if not PROJECT_RE.fullmatch(project):
        raise PublishError("TED_REPORT_PROJECT has an invalid Pages project name")
    return project


def _safe_account_id(value: str) -> str:
    account_id = value.strip()
    if len(account_id) != 32 or any(char not in "0123456789abcdefABCDEF" for char in account_id):
        raise PublishError("CLOUDFLARE_ACCOUNT_ID has an invalid format")
    return account_id


def _result(response: object, source: str, *, allow_not_found: bool = False):
    status = getattr(response, "status_code", 0)
    if allow_not_found and status == 404:
        return None
    if status < 200 or status >= 300:
        raise PublishError(f"{source} returned HTTP {status}")
    content = getattr(response, "content", b"")
    if len(content) > MAX_RESPONSE_BYTES:
        raise PublishError(f"{source} returned an unexpectedly large response")
    try:
        payload = response.json()
    except (AttributeError, ValueError) as exc:
        raise PublishError(f"{source} returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise PublishError(f"{source} returned an unsuccessful result")
    return payload.get("result")


def _account_request(
    session: requests.Session,
    method: str,
    account_id: str,
    api_token: str,
    path: str,
    *,
    allow_not_found: bool = False,
    **kwargs,
):
    try:
        response = session.request(
            method,
            f"{API_BASE}/accounts/{account_id}{path}",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise PublishError("Cloudflare account API is unavailable") from exc
    return _result(response, "Cloudflare account API", allow_not_found=allow_not_found)


def _asset_request(
    session: requests.Session,
    method: str,
    jwt: str,
    path: str,
    payload: dict | list,
):
    try:
        response = session.request(
            method,
            f"{API_BASE}/pages/assets/{path}",
            headers={"Authorization": f"Bearer {jwt}"},
            json=payload,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise PublishError("Cloudflare Pages asset API is unavailable") from exc
    return _result(response, "Cloudflare Pages asset API")


def _asset_hash(name: str, content: bytes) -> str:
    """Match Wrangler's BLAKE3(base64(content) + file extension) asset key."""
    extension = Path(name).suffix.lstrip(".")
    encoded = base64.b64encode(content) + extension.encode("utf-8")
    return blake3(encoded).hexdigest()[:32]


def _assets(directory: Path) -> dict[str, dict]:
    report = directory / REPORT_FILENAME
    chart = directory / CHART_FILENAME
    for path in (report, chart):
        if not path.is_file() or path.stat().st_size == 0:
            raise PublishError(f"report asset is missing or empty: {path.name}")
    source = {
        "index.html": report.read_bytes(),
        CHART_FILENAME: chart.read_bytes(),
        "robots.txt": ROBOTS_TXT.encode("utf-8"),
    }
    assets: dict[str, dict] = {}
    for name, content in source.items():
        assets[name] = {
            "content": content,
            "hash": _asset_hash(name, content),
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        }
    return assets


def _ensure_project(
    session: requests.Session,
    account_id: str,
    api_token: str,
    project: str,
) -> dict:
    encoded = requests.utils.quote(project, safe="")
    existing = _account_request(
        session,
        "GET",
        account_id,
        api_token,
        f"/pages/projects/{encoded}",
        allow_not_found=True,
    )
    if existing is None:
        existing = _account_request(
            session,
            "POST",
            account_id,
            api_token,
            "/pages/projects",
            json={"name": project, "production_branch": "main"},
        )
    if not isinstance(existing, dict):
        raise PublishError("Cloudflare Pages project has an invalid response")
    return existing


def _upload_assets(
    session: requests.Session,
    account_id: str,
    api_token: str,
    project: str,
    assets: dict[str, dict],
) -> dict[str, str]:
    encoded = requests.utils.quote(project, safe="")
    upload_token = _account_request(
        session,
        "GET",
        account_id,
        api_token,
        f"/pages/projects/{encoded}/upload-token",
    )
    jwt = upload_token.get("jwt") if isinstance(upload_token, dict) else None
    if not isinstance(jwt, str) or not jwt:
        raise PublishError("Cloudflare Pages did not return an upload token")

    hashes = [asset["hash"] for asset in assets.values()]
    missing = _asset_request(session, "POST", jwt, "check-missing", {"hashes": hashes})
    if not isinstance(missing, list) or any(not isinstance(value, str) for value in missing):
        raise PublishError("Cloudflare Pages returned an invalid missing-asset list")
    missing_set = set(missing)
    unknown = missing_set - set(hashes)
    if unknown:
        raise PublishError("Cloudflare Pages requested an unknown asset hash")
    if missing_set:
        upload = []
        for asset in assets.values():
            if asset["hash"] not in missing_set:
                continue
            upload.append(
                {
                    "key": asset["hash"],
                    "value": base64.b64encode(asset["content"]).decode("ascii"),
                    "metadata": {"contentType": asset["content_type"]},
                    "base64": True,
                }
            )
        _asset_request(session, "POST", jwt, "upload", upload)
    try:
        _asset_request(session, "POST", jwt, "upsert-hashes", {"hashes": hashes})
    except PublishError:
        # Wrangler also treats this cache optimisation as non-fatal. The
        # deployment manifest remains valid; a later run may re-upload files.
        pass
    return {f"/{name}": asset["hash"] for name, asset in assets.items()}


def _deploy(
    session: requests.Session,
    account_id: str,
    api_token: str,
    project: str,
    manifest: dict[str, str],
) -> dict:
    encoded = requests.utils.quote(project, safe="")
    data = {
        "manifest": json.dumps(manifest, separators=(",", ":")),
        "branch": "main",
        "commit_message": "Publish TED J+30 report",
        "commit_dirty": "false",
    }
    files = {"_headers": ("_headers", PAGES_HEADERS.encode("utf-8"), "text/plain")}
    result = _account_request(
        session,
        "POST",
        account_id,
        api_token,
        f"/pages/projects/{encoded}/deployments",
        data=data,
        files=files,
    )
    if not isinstance(result, dict):
        raise PublishError("Cloudflare Pages deployment has an invalid response")
    return result


def _project_url(project: dict, fallback_name: str) -> str:
    subdomain = project.get("subdomain")
    if isinstance(subdomain, str) and subdomain:
        hostname = subdomain.removeprefix("https://").rstrip("/")
    else:
        hostname = f"{fallback_name}.pages.dev"
    if not hostname.endswith(".pages.dev") or "/" in hostname:
        raise PublishError("Cloudflare Pages returned an invalid project subdomain")
    return f"https://{hostname}"


def _verify(
    session: requests.Session,
    url: str,
    assets: dict[str, dict],
    *,
    sleep: Callable[[float], None],
) -> None:
    expected_html = assets["index.html"]["content"]
    expected_chart = assets[CHART_FILENAME]["content"]
    marker = hashlib.sha256(expected_html).hexdigest()[:12]
    for attempt in range(20):
        try:
            page = session.get(f"{url}/?v={marker}", timeout=20)
            chart = session.get(f"{url}/{CHART_FILENAME}?v={marker}", timeout=20)
            headers = {key.lower(): value for key, value in getattr(page, "headers", {}).items()}
            if (
                page.status_code == 200
                and chart.status_code == 200
                and page.content == expected_html
                and chart.content == expected_chart
                and "noindex" in headers.get("x-robots-tag", "").lower()
                and headers.get("x-content-type-options", "").lower() == "nosniff"
            ):
                return
        except requests.RequestException:
            pass
        if attempt < 19:
            sleep(3)
    raise PublishError("the public Pages report did not match the uploaded assets and headers")


def publish_report(
    directory: Path,
    *,
    account_id: str,
    api_token: str,
    project_name: str = DEFAULT_PROJECT,
    session: Optional[requests.Session] = None,
    public_session: Optional[requests.Session] = None,
    verify: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Create/update one Direct Upload project and return its stable HTTPS URL."""
    safe_account = _safe_account_id(account_id)
    project = _safe_project(project_name)
    if not api_token.strip():
        raise PublishError("CLOUDFLARE_API_TOKEN is empty")
    client = session or requests.Session()
    assets = _assets(directory)
    project_data = _ensure_project(client, safe_account, api_token, project)
    manifest = _upload_assets(client, safe_account, api_token, project, assets)
    _deploy(client, safe_account, api_token, project, manifest)
    url = _project_url(project_data, project)
    if verify:
        # Keep public propagation checks independent from the API client's
        # retry adapter. Otherwise each of the 20 bounded checks can itself
        # expand into several long DNS/HTTP retries.
        _verify(public_session or requests.Session(), url, assets, sleep=sleep)
    return url


def publish_from_environment(
    directory: Path,
    *,
    require_config: bool = False,
    session: Optional[requests.Session] = None,
) -> Optional[str]:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not token and not account_id:
        if require_config:
            raise PublishError("Cloudflare report publication is not configured")
        return None
    if not token or not account_id:
        raise PublishError("Cloudflare report publication requires both token and account ID")
    return publish_report(
        directory,
        account_id=account_id,
        api_token=token,
        project_name=os.environ.get("TED_REPORT_PROJECT", DEFAULT_PROJECT),
        session=session,
    )


def selftest() -> None:
    class FakeResponse:
        def __init__(self, status_code=200, result=None, content=None, headers=None):
            self.status_code = status_code
            self._payload = {"success": True, "result": result}
            self.content = content if content is not None else json.dumps(self._payload).encode()
            self.headers = headers or {}

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.assets = {}
            self.project_created = False
            self.deployed = False
            self.expected_html = b""
            self.expected_chart = b""

        def request(self, method, url, **kwargs):
            if url.endswith("/pages/projects/ted-bot-j30-report") and method == "GET":
                if not self.project_created:
                    return FakeResponse(status_code=404)
                return FakeResponse(result={"subdomain": "ted-bot-j30-report.pages.dev"})
            if url.endswith("/pages/projects") and method == "POST":
                self.project_created = True
                return FakeResponse(result={"subdomain": "ted-bot-j30-report.pages.dev"})
            if url.endswith("/upload-token"):
                return FakeResponse(result={"jwt": "short-lived-upload-token"})
            if url.endswith("/check-missing"):
                return FakeResponse(result=kwargs["json"]["hashes"])
            if url.endswith("/pages/assets/upload"):
                self.assets = {item["key"]: item for item in kwargs["json"]}
                return FakeResponse(result={})
            if url.endswith("/upsert-hashes"):
                return FakeResponse(result={})
            if url.endswith("/deployments"):
                manifest = json.loads(kwargs["data"]["manifest"])
                assert set(manifest) == {"/index.html", f"/{CHART_FILENAME}", "/robots.txt"}
                assert "_headers" in kwargs["files"]
                self.deployed = True
                return FakeResponse(result={"url": "https://deployment.pages.dev"})
            raise AssertionError((method, url))

        def get(self, url, **_kwargs):
            assert self.deployed
            if CHART_FILENAME in url:
                return FakeResponse(content=self.expected_chart)
            return FakeResponse(
                content=self.expected_html,
                headers={"X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff"},
            )

    with tempfile.TemporaryDirectory() as raw_temp:
        directory = Path(raw_temp)
        html_content = b"<!doctype html><title>TED Bot report</title>"
        chart_content = b"\x89PNG\r\n\x1a\nsynthetic"
        (directory / REPORT_FILENAME).write_bytes(html_content)
        (directory / CHART_FILENAME).write_bytes(chart_content)
        fake = FakeSession()
        fake.expected_html = html_content
        fake.expected_chart = chart_content
        url = publish_report(
            directory,
            account_id="a" * 32,
            api_token="not-a-real-token",
            session=fake,
            public_session=fake,
            sleep=lambda _seconds: None,
        )
        assert url == "https://ted-bot-j30-report.pages.dev"
        assert len(fake.assets) == 3
        with patch.dict(
            os.environ,
            {"CLOUDFLARE_API_TOKEN": "", "CLOUDFLARE_ACCOUNT_ID": ""},
        ):
            assert publish_from_environment(directory, session=fake) is None
    print("cloudflare_report selftest: OK")


if __name__ == "__main__":
    selftest()
