"""Client for the renderer service.

The renderer returns facts and this module turns them into a typed record. No
thresholds live here or there: they live next to the criteria doc in
`app/checks/rendered.py`, so retuning C2 does not mean redeploying a browser.

robots.txt is enforced before this is called. The renderer renders what it is
told to, so the caller is the one that must have asked permission.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from app.config import get_config

MOBILE_VIEWPORT = {"width": 390, "height": 844}

# C2's threshold. Below this and body copy is a squint on a phone.
MIN_BODY_FONT_PX = 16


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    url: str
    final_url: str | None = None
    status: int | None = None
    elapsed_ms: int | None = None
    title: str = ""
    horizontal_scroll: bool | None = None
    document: Mapping[str, Any] = field(default_factory=dict)
    overflowing_elements: Sequence[Mapping[str, Any]] = ()
    fonts: Mapping[str, Any] = field(default_factory=dict)
    tel_links: Sequence[Mapping[str, Any]] = ()
    phone_text: Sequence[Mapping[str, Any]] = ()
    forms: Sequence[Mapping[str, Any]] = ()
    ctas: Sequence[Mapping[str, Any]] = ()
    console_errors: Sequence[str] = ()
    screenshot_b64: str | None = None
    screenshot_bytes: int | None = None
    error: str | None = None

    # ── Derived reads over the font histogram ─────────────────────────────────

    def _histogram(self) -> list[tuple[int, int]]:
        raw = (self.fonts or {}).get("histogram") or {}
        return sorted((int(px), int(chars)) for px, chars in raw.items())

    @property
    def total_chars(self) -> int:
        return int((self.fonts or {}).get("total_chars") or 0)

    def median_font_px(self) -> int | None:
        """Character weighted median.

        The mean is wrong here: one 47px hero headline should not drag the
        reading experience upward, and a 10px legal footer should not drag it
        down. The size most characters are actually set at is the honest answer.
        """
        histogram = self._histogram()
        if not histogram or self.total_chars <= 0:
            return None
        halfway = self.total_chars / 2
        seen = 0
        for px, chars in histogram:
            seen += chars
            if seen >= halfway:
                return px
        return histogram[-1][0]

    def small_text_ratio(self, below: int = MIN_BODY_FONT_PX) -> float | None:
        """Share of characters set below a given size."""
        histogram = self._histogram()
        if not histogram or self.total_chars <= 0:
            return None
        small = sum(chars for px, chars in histogram if px < below)
        return small / self.total_chars

    # ── Above the fold ────────────────────────────────────────────────────────

    def phones_above_fold(self) -> list[Mapping[str, Any]]:
        return [x for x in (*self.tel_links, *self.phone_text) if x.get("above_fold")]

    def forms_above_fold(self) -> list[Mapping[str, Any]]:
        return [x for x in self.forms if x.get("above_fold")]

    def ctas_above_fold(self) -> list[Mapping[str, Any]]:
        return [x for x in self.ctas if x.get("above_fold")]

    def screenshot(self) -> bytes | None:
        return base64.b64decode(self.screenshot_b64) if self.screenshot_b64 else None


def _from_payload(payload: Mapping[str, Any], url: str) -> RenderResult:
    shot = payload.get("screenshot") or {}
    return RenderResult(
        ok=bool(payload.get("ok")),
        url=payload.get("url") or url,
        final_url=payload.get("final_url"),
        status=payload.get("status"),
        elapsed_ms=payload.get("elapsed_ms"),
        title=payload.get("title") or "",
        horizontal_scroll=payload.get("horizontal_scroll"),
        document=payload.get("document") or {},
        overflowing_elements=payload.get("overflowing_elements") or (),
        fonts=payload.get("fonts") or {},
        tel_links=payload.get("tel_links") or (),
        phone_text=payload.get("phone_text") or (),
        forms=payload.get("forms") or (),
        ctas=payload.get("ctas") or (),
        console_errors=payload.get("console_errors") or (),
        screenshot_b64=shot.get("bytes"),
        screenshot_bytes=shot.get("size_bytes"),
        error=payload.get("error"),
    )


async def render(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    screenshot: str = "viewport",
    viewport: Mapping[str, int] | None = None,
    timeout: float = 90.0,
) -> RenderResult:
    """Render one URL. Never raises: an unreachable renderer is a skipped check."""
    cfg = get_config()
    cfg.require("renderer_url")

    payload = {
        "url": url,
        "viewport": dict(viewport or MOBILE_VIEWPORT),
        "screenshot": screenshot,
    }
    endpoint = cfg.renderer_url.rstrip("/") + "/render"

    headers = {"content-type": "application/json"}
    if cfg.renderer_shared_secret:
        headers["x-relay-secret"] = cfg.renderer_shared_secret
    headers.update(_auth_headers(endpoint))
    owned = client is None
    http_client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
    try:
        response = await http_client.post(endpoint, json=payload, headers=headers)
        if response.status_code != 200:
            return RenderResult(ok=False, url=url,
                                error=f"renderer returned {response.status_code}")
        return _from_payload(response.json(), url)
    except httpx.HTTPError as exc:
        return RenderResult(ok=False, url=url, error=f"{type(exc).__name__}: {exc}")
    except ValueError as exc:
        return RenderResult(ok=False, url=url, error=f"bad renderer response: {exc}")
    finally:
        if owned:
            await http_client.aclose()


# ── Cloud Run identity ────────────────────────────────────────────────────────

_ID_TOKEN_CACHE: dict[str, str] = {}


def _cloud_run_id_token(endpoint: str) -> str | None:
    """An identity token for an IAM protected Cloud Run service, or None.

    Two credential shapes have to work. On Cloud Run the metadata server mints a
    token for the service account directly. On a workstation the ADC are user
    credentials, which cannot mint an ID token for an arbitrary audience at all,
    so we ask gcloud, which can. Neither path writes a key file.
    """
    audience = "/".join(endpoint.split("/")[:3])
    if audience in _ID_TOKEN_CACHE:
        return _ID_TOKEN_CACHE[audience]

    token: str | None = None
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token as google_id_token

        token = google_id_token.fetch_id_token(
            google.auth.transport.requests.Request(), audience
        )
    except Exception:  # noqa: BLE001 - fall through to the local path
        token = None

    if not token:
        import shutil
        import subprocess

        gcloud = shutil.which("gcloud")
        if gcloud:
            try:
                token = subprocess.run(
                    [gcloud, "auth", "print-identity-token"],
                    capture_output=True, text=True, timeout=30, check=True,
                ).stdout.strip() or None
            except (subprocess.SubprocessError, OSError):
                token = None

    if token:
        _ID_TOKEN_CACHE[audience] = token
    return token


def _auth_headers(endpoint: str) -> dict[str, str]:
    """Bearer token only for Cloud Run. A localhost renderer needs no identity."""
    if ".run.app" not in endpoint:
        return {}
    token = _cloud_run_id_token(endpoint)
    return {"Authorization": f"Bearer {token}"} if token else {}
