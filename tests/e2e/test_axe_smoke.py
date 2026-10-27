"""Accessibility smoke test: no serious or critical axe violations on the main screens.

axe-core is a frontend devDependency, the engine visual-kit's audit records, here as a gate.
CI (``.github/workflows/a11y.yml``) runs it against ``make demo``, so it audits seeded content;
against an empty database it covers the shell and the empty states only. ``/login`` and
``/signup`` need the demo's login screen on and stay in ``make screenshots-audit-auth``.

``E2E_REQUIRED=1`` (set in CI) turns a missing axe-core or an unreachable server into a failure
instead of a skip, so the gate cannot go green by not running.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

AXE_JS = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "node_modules"
    / "axe-core"
    / "axe.min.js"
)
BLOCKING = ("serious", "critical")
ROUTES = ["/", "/triage", "/cases", "/costs", "/settings/account"]


def _blocking_violations(page: Page) -> list[dict]:
    page.add_script_tag(path=str(AXE_JS))
    result = page.evaluate("axe.run(document, { resultTypes: ['violations'] })")
    return [v for v in result["violations"] if v["impact"] in BLOCKING]


def _describe(violations: list[dict]) -> str:
    lines = []
    for v in violations:
        lines.append(f"{v['id']} ({v['impact']}): {v['help']}")
        lines.extend(f"    {n['target']}" for n in v["nodes"][:5])
    return "\n".join(lines)


@pytest.mark.skipif(
    not AXE_JS.exists(), reason="run `npm --prefix frontend ci` for axe-core"
)
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("route", ROUTES)
def test_no_serious_axe_violations(page: Page, route: str, theme: str):
    # `site-theme` is the SPA's own choice (frontend/src/theme.ts); it wins over the OS scheme.
    if not AXE_JS.exists():
        message = "axe-core is missing: run `npm --prefix frontend ci`"
        if os.getenv("E2E_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    page.add_init_script(f"localStorage.setItem('site-theme', '{theme}')")
    page.goto(route)
    page.wait_for_load_state("networkidle")
    # A redirect (to /login, say) would otherwise make this audit the wrong screen.
    landed = urlparse(page.url).path.rstrip("/")
    assert landed == route.rstrip("/"), f"{route} redirected to {page.url}"
    violations = _blocking_violations(page)
    assert not violations, f"{route} ({theme})\n{_describe(violations)}"
