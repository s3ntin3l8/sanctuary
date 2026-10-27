"""Accessibility smoke test: no serious or critical axe violations on the main screens.

axe-core ships in ``frontend/node_modules`` (a dependency of ``@axe-core/playwright``), so the
page runs the same engine the visual-kit audit records, here as a gate. The CI database is empty,
so this covers the shell, the empty states and the forms, not seeded content: the demo audit
(``make screenshots-audit``) is the wider sweep.
"""

from pathlib import Path

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
    page.add_init_script(f"localStorage.setItem('site-theme', '{theme}')")
    page.goto(route)
    page.wait_for_load_state("networkidle")
    violations = _blocking_violations(page)
    assert not violations, f"{route} ({theme})\n{_describe(violations)}"
