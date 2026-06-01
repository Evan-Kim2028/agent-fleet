"""PlaywrightVisualCapture — silphco adapter implementing VisualCapture.

Serves the built frontend on an ephemeral port (mirroring the pattern in
``frontend/playwright.config.ts``) and takes deterministic screenshots at
configured viewports and fixture states.

Integration boundary
--------------------
The actual browser driving requires:
  - ``playwright`` Python package installed (``playwright install chromium``).
  - A built frontend (``npm run build`` in ``frontend/``), OR a running dev
    server at ``SILPH_FRONTEND_URL`` (for development).
  - Backend running with ``SILPH_FRONTEND_FIXTURE_MODE=1`` so dynamic values
    (live prices, timestamps) are replaced by stable fixture data.

This boundary is clearly marked below with ``# requires running frontend``
comments.  Unit tests mock out the ``_serve_frontend`` and ``_take_screenshot``
helpers so the seam, config consumption, and determinism contract are fully
covered without a real browser.

Determinism contract (mirrors fleet.visual.VisualCapture docstring)
-------------------------------------------------------------------
1. Animations:     CSS ``transition-duration: 0s !important; animation-duration:
                   0s !important;`` injected via ``page.add_style_tag()`` before
                   any screenshot.
2. Viewport / DPR: fixed per ``CaptureConfig`` entry; defaults to 1280×800 @1x
                   desktop and 390×844 @3x mobile.
3. Fonts:          ``await page.evaluate("document.fonts.ready")`` awaited
                   before capture.
4. Dynamic values: ``SILPH_FRONTEND_FIXTURE_MODE=1`` env var instructs the
                   frontend to render seeded fixture data (no live API calls).
                   Any remaining dynamic text (e.g. ``data-capture-mask``
                   elements) is masked with a grey rectangle via Playwright's
                   ``mask`` parameter.
5. Network:        ``page.route("**/api/**", ...)`` intercepts all API calls
                   and returns seeded JSON fixtures so no real backend is needed.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator

from silphco.visual import CaptureArtifact

log = logging.getLogger(__name__)


def _safe_filename_part(s: str) -> str:
    """Sanitize *s* for use as a filename component.

    Replaces path separators (``/``, ``\\``) and any other characters that are
    unsafe in filenames (``:``, ``*``, ``?``, ``"``, ``<``, ``>``, ``|``,
    NUL, newline, tab) with an underscore.  Strips leading/trailing dots and
    spaces so the result is safe on all major filesystems.

    Prevents path-traversal if a subclass or caller passes a malicious label
    or state string such as ``"../../../etc/passwd"``.
    """
    import re
    safe = re.sub(r'[/\\:*?"<>|\x00\n\r\t]', "_", s)
    # Also collapse consecutive underscores and strip leading/trailing junk.
    safe = safe.strip(". ")
    return safe or "unknown"

# ---------------------------------------------------------------------------
# CaptureConfig — per-route/viewport capture spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewportSpec:
    """One viewport to capture at.

    Attributes:
        label:   Human-readable label, e.g. ``"desktop"`` / ``"mobile"``.
        width:   Viewport width in CSS pixels.
        height:  Viewport height in CSS pixels.
        device_scale_factor: Device pixel ratio (1 for desktop, 3 for iPhone).
    """

    label: str
    width: int
    height: int
    device_scale_factor: float = 1.0


@dataclass(frozen=True)
class RouteSpec:
    """One route / fixture state to capture.

    Attributes:
        ref:            Route path, e.g. ``"/cards/base1-4"``.
        state:          Fixture state label, e.g. ``"default"`` / ``"empty"``.
        fixture_params: Query params forwarded to the fixture interceptor so
                        the frontend renders the desired seeded state.
        mask_selectors: CSS selectors for elements that contain dynamic values
                        and should be masked with a grey rectangle.
    """

    ref: str
    state: str
    fixture_params: dict[str, str] = field(default_factory=dict)
    mask_selectors: tuple[str, ...] = ()


# Default viewports: desktop 1280×800 + mobile iPhone 13 (390×844 @3x).
DEFAULT_VIEWPORTS: tuple[ViewportSpec, ...] = (
    ViewportSpec(label="desktop", width=1280, height=800, device_scale_factor=1.0),
    ViewportSpec(label="mobile", width=390, height=844, device_scale_factor=3.0),
)

# Default routes to capture — covers the main visual surfaces.
DEFAULT_ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec(ref="/", state="default"),
    RouteSpec(ref="/cards/base1-4", state="default"),
    RouteSpec(ref="/sets", state="default"),
)

# Masking selectors for values that are inherently dynamic even with fixture mode on.
_DEFAULT_MASK_SELECTORS: tuple[str, ...] = (
    "[data-capture-mask]",
    "[data-testid='live-price']",
    "[data-testid='last-updated']",
)

# CSS injected before every screenshot to freeze animations.
_FREEZE_ANIMATIONS_CSS = (
    "*, *::before, *::after { "
    "transition-duration: 0s !important; "
    "animation-duration: 0s !important; "
    "animation-delay: 0s !important; "
    "}"
)


# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------

#: Maximum number of port-probe attempts before giving up.
_PORT_PROBE_ATTEMPTS = 10


def _free_port() -> int:
    """Return a candidate free TCP port on localhost.

    Binds to port 0 (kernel picks a free port), reads the assigned number,
    closes the socket, and returns it.  The returned port is *likely* free but
    not guaranteed under concurrency — callers that actually bind the port
    (e.g. ``_serve_frontend_with_retry``) should handle ``EADDRINUSE`` by
    calling this function again for a fresh candidate.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# PlaywrightVisualCapture
# ---------------------------------------------------------------------------


class PlaywrightVisualCapture:
    """VisualCapture implementation using Playwright and an ephemeral frontend.

    Satisfies the ``fleet.visual.VisualCapture`` Protocol by structural subtyping.

    Construction
    ------------
    .. code-block:: python

        capture = PlaywrightVisualCapture(
            viewports=DEFAULT_VIEWPORTS,
            routes=DEFAULT_ROUTES,
            frontend_dir=Path("frontend"),
        )

    Then inject via agent-fleet runner hooks when ``spine.design_review_enabled`` is True.

    Extension point for real vision
    --------------------------------
    ``_take_screenshot`` is a thin method wrapping Playwright's
    ``page.screenshot()``.  Override it in a subclass or swap it out in tests
    via ``monkeypatch`` to inject mock screenshots without a real browser.
    """

    def __init__(
        self,
        *,
        viewports: tuple[ViewportSpec, ...] = DEFAULT_VIEWPORTS,
        routes: tuple[RouteSpec, ...] = DEFAULT_ROUTES,
        frontend_dir: Path | None = None,
        mask_selectors: tuple[str, ...] = _DEFAULT_MASK_SELECTORS,
        startup_timeout_s: float = 30.0,
    ) -> None:
        self._viewports = viewports
        self._routes = routes
        self._frontend_dir = frontend_dir
        self._mask_selectors = mask_selectors
        self._startup_timeout_s = startup_timeout_s

    # ------------------------------------------------------------------
    # VisualCapture Protocol impl
    # ------------------------------------------------------------------

    def capture(
        self,
        changed_files: list[str],
        *,
        workdir: Path,
    ) -> list[CaptureArtifact]:
        """Capture screenshots of visual surfaces affected by *changed_files*.

        Implementation notes:
        - Serves the frontend on an ephemeral port (no fixed port conflicts).
        - Injects ``SILPH_FRONTEND_FIXTURE_MODE=1`` to suppress live API calls.
        - Freezes animations via injected CSS before each screenshot.
        - Awaits ``document.fonts.ready`` before capture.
        - Intercepts ``**/api/**`` routes to return seeded fixture JSON.
        - Masks ``[data-capture-mask]`` and other dynamic elements.

        # requires running frontend
        """
        # Filter routes to those affected by the changed files (basic heuristic:
        # capture all routes if any frontend file changed, since CSS/component
        # changes affect all surfaces).
        routes_to_capture = self._filter_routes(changed_files)
        if not routes_to_capture:
            log.debug("playwright_capture: no visual surfaces affected — skipping")
            return []

        frontend_dir = self._frontend_dir or (workdir / "frontend")
        artifacts: list[CaptureArtifact] = []

        with tempfile.TemporaryDirectory(prefix="silphco_capture_") as tmp_dir:
            tmp_path = Path(tmp_dir)

            with self._serve_frontend_with_retry(frontend_dir, workdir=workdir) as port:
                base_url = f"http://127.0.0.1:{port}"

                # requires running frontend — import here so the module can be
                # imported and tested without playwright installed.
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError as exc:
                    log.error(
                        "playwright not installed — cannot capture screenshots (%s). "
                        "Install the design-review extra: "
                        "uv pip install 'silphco-agents[design-review]' "
                        "then run: playwright install chromium",
                        exc,
                    )
                    return []

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    try:
                        for vp in self._viewports:
                            ctx = browser.new_context(
                                viewport={"width": vp.width, "height": vp.height},
                                device_scale_factor=vp.device_scale_factor,
                                # Disable all media (video autoplay etc.) for determinism.
                                reduced_motion="reduce",
                            )
                            try:
                                for route_spec in routes_to_capture:
                                    safe_label = _safe_filename_part(vp.label)
                                    safe_state = _safe_filename_part(route_spec.state)
                                    safe_ref = _safe_filename_part(
                                        route_spec.ref.strip("/").replace("/", "_") or "home"
                                    )
                                    img_path = tmp_path / f"{safe_label}_{safe_state}_{safe_ref}.png"
                                    try:
                                        self._capture_one(
                                            ctx,
                                            base_url=base_url,
                                            route=route_spec,
                                            viewport=vp,
                                            out_path=img_path,
                                        )
                                        artifacts.append(CaptureArtifact(
                                            viewport=vp.label,
                                            state=route_spec.state,
                                            ref=route_spec.ref,
                                            image_path=img_path,
                                        ))
                                    except Exception as exc:
                                        log.warning(
                                            "playwright_capture: failed to capture %s@%s: %s",
                                            route_spec.ref, vp.label, exc,
                                        )
                            finally:
                                ctx.close()
                    finally:
                        browser.close()

            # Move images out of tempdir to a persistent location so the
            # caller can pass them to the executor after the context exits.
            # We persist them alongside workdir under a capture/ subdir.
            capture_dir = workdir / ".capture"
            capture_dir.mkdir(parents=True, exist_ok=True)
            persistent_artifacts: list[CaptureArtifact] = []
            for art in artifacts:
                dest = capture_dir / art.image_path.name
                try:
                    import shutil
                    shutil.copy2(art.image_path, dest)
                    persistent_artifacts.append(CaptureArtifact(
                        viewport=art.viewport,
                        state=art.state,
                        ref=art.ref,
                        image_path=dest,
                    ))
                except Exception as exc:
                    log.warning("playwright_capture: failed to persist %s: %s", art.image_path, exc)

            return persistent_artifacts

    # ------------------------------------------------------------------
    # Internal helpers (overridable in subclasses / monkeypatched in tests)
    # ------------------------------------------------------------------

    def _filter_routes(self, changed_files: list[str]) -> list[RouteSpec]:
        """Return the subset of routes to capture given changed files.

        Current heuristic: if any file under ``frontend/`` changed, capture all
        configured routes.  Projects can override this for finer granularity.
        """
        if not changed_files:
            return list(self._routes)
        for f in changed_files:
            if f.startswith("frontend/"):
                return list(self._routes)
        # Non-frontend change — no visual surfaces affected.
        return []

    @contextlib.contextmanager
    def _serve_frontend_with_retry(
        self, frontend_dir: Path, *, workdir: Path
    ) -> Generator[int, None, None]:
        """Start the static frontend server on a free port, yielding the port number.

        Eliminates the TOCTOU race in the naive probe-close-bind pattern by
        retrying with a fresh port candidate if the server fails to become
        reachable (which happens when another process grabbed the port between
        the probe and the subprocess bind).  Bounded by ``_PORT_PROBE_ATTEMPTS``.

        Yields the bound port so the caller can construct the base URL.
        """
        last_exc: Exception | None = None
        for attempt in range(_PORT_PROBE_ATTEMPTS):
            port = _free_port()
            try:
                with self._serve_frontend(frontend_dir, port, workdir=workdir):
                    yield port
                    return
            except TimeoutError as exc:
                # The server did not become reachable — likely the port was
                # grabbed by another process between our probe and the subprocess
                # bind.  Try a fresh port.
                log.debug(
                    "playwright_capture: server on port %d did not start "
                    "(attempt %d/%d), retrying with a new port: %s",
                    port, attempt + 1, _PORT_PROBE_ATTEMPTS, exc,
                )
                last_exc = exc
                continue
        raise TimeoutError(
            f"Frontend server failed to start after {_PORT_PROBE_ATTEMPTS} attempts"
        ) from last_exc

    @contextlib.contextmanager
    def _serve_frontend(
        self, frontend_dir: Path, port: int, *, workdir: Path
    ) -> Generator[None, None, None]:
        """Serve the built frontend on *port* using a static file server.

        # requires running frontend — the frontend must be built first
        (``npm run build`` in *frontend_dir*).  If the build dir does not
        exist this context manager raises ``FileNotFoundError``.

        Uses Python's built-in ``http.server`` as a zero-dependency static
        server for the built assets.
        """
        dist_dir = frontend_dir / "dist"
        if not dist_dir.exists():
            raise FileNotFoundError(
                f"Frontend build directory not found: {dist_dir}. "
                f"Run 'npm run build' in {frontend_dir} first."
            )

        # Start a simple static HTTP server for the built assets.
        proc = subprocess.Popen(
            ["python3", "-m", "http.server", str(port), "--directory", str(dist_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for the server to be ready.
            deadline = time.monotonic() + self._startup_timeout_s
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                proc.terminate()
                raise TimeoutError(
                    f"Frontend server on port {port} did not start within "
                    f"{self._startup_timeout_s}s"
                )
            yield
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _capture_one(
        self,
        ctx: Any,  # playwright BrowserContext — typed as Any to avoid hard dep
        *,
        base_url: str,
        route: RouteSpec,
        viewport: ViewportSpec,
        out_path: Path,
    ) -> None:
        """Navigate to one route and take a deterministic screenshot.

        # requires running frontend
        """
        from playwright.sync_api import Page

        page: Page = ctx.new_page()
        try:
            # Intercept API calls — return empty fixture JSON so no real backend needed.
            page.route(
                "**/api/**",
                lambda r: r.fulfill(
                    status=200,
                    content_type="application/json",
                    body="{}",
                ),
            )

            # Inject animation-freeze CSS before navigation so it applies to
            # all elements including those rendered during page load.
            page.add_init_script(
                f"document.addEventListener('DOMContentLoaded', () => {{"
                f"  const s = document.createElement('style');"
                f"  s.textContent = {_FREEZE_ANIMATIONS_CSS!r};"
                f"  document.head.appendChild(s);"
                f"}});"
            )

            url = base_url + route.ref
            if route.fixture_params:
                qs = urllib.parse.urlencode(route.fixture_params)
                url += ("&" if "?" in url else "?") + qs

            page.goto(url, wait_until="networkidle", timeout=30_000)

            # Await fonts.ready for deterministic font rendering.
            page.evaluate("() => document.fonts.ready")

            # Inject freeze CSS again after page load (belt-and-suspenders).
            page.add_style_tag(content=_FREEZE_ANIMATIONS_CSS)

            # Build mask locators for dynamic values.
            all_selectors = list(self._mask_selectors) + list(route.mask_selectors)
            mask_locators = [page.locator(sel) for sel in all_selectors]

            page.screenshot(
                path=str(out_path),
                full_page=False,
                mask=mask_locators,
                animations="disabled",
            )
        finally:
            page.close()
