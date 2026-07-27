"""W3-E proof against the real dashboard bundle in headless Chromium."""

from __future__ import annotations

# Preload the nested package before pytest imports this module from a checkout
# directory named ``shipfactory``.  This is import-only and does not alter
# production package loading; it prevents the repository root plugin from
# circular-importing itself during focused collection.
from shipfactory import store as _shipfactory_store

import asyncio
import functools
import http.server
import json
import os
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

import pytest


ROOT = Path(__file__).resolve().parents[1]
VITE_CONFIG = ROOT / "dashboard" / "conformance" / "browser-vite.mjs"
DEFAULT_HERMES_MOBILE = Path("/Volumes/MainData/Developer/products/hermes-mobile")


def _no_board(value: object) -> bool:
    if isinstance(value, dict):
        return all(key != "board" and _no_board(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_no_board(item) for item in value)
    return True


def test_projects_and_graph_rendered_browser_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _shipfactory_store.__name__ == "shipfactory.store"
    try:
        from playwright.async_api import async_playwright
    except Exception as error:  # pragma: no cover - exact environment blocker
        pytest.fail(f"BLOCKED: installed Playwright/Chromium runtime unavailable: {error}")

    hermes_mobile = Path(os.environ.get("HERMES_MOBILE_PATH", DEFAULT_HERMES_MOBILE))
    original_home = Path(os.environ.get("HOME", str(Path.home())))
    browser_candidates = sorted(
        (original_home / "Library/Caches/ms-playwright").glob(
            "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell"
        )
    )
    if not browser_candidates:
        pytest.fail(
            f"BLOCKED: installed Playwright Chromium executable not found under {original_home / 'Library/Caches/ms-playwright'}"
        )
    chromium_executable = browser_candidates[-1]
    browser_host = Path(os.environ.get("HERMES_BROWSER_HOST_PATH", DEFAULT_HERMES_MOBILE))
    vite = browser_host / "node_modules" / ".bin" / "vite"
    if not vite.is_file() and (hermes_mobile / "node_modules" / ".bin" / "vite").is_file():
        browser_host = hermes_mobile
        vite = browser_host / "node_modules" / ".bin" / "vite"
    if not vite.is_file():
        pytest.fail(f"BLOCKED: Vite runtime unavailable at {vite}")
    if not VITE_CONFIG.is_file():
        pytest.fail(f"BLOCKED: browser Vite config missing at {VITE_CONFIG}")

    isolated_home = tmp_path / "browser-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_home / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(isolated_home / "data"))

    build_dir = tmp_path / "browser-build"
    build_env = os.environ.copy()
    build_env["HERMES_MOBILE_PATH"] = str(browser_host)
    result = subprocess.run(
        [str(vite), "build", "--config", str(VITE_CONFIG), "--outDir", str(build_dir)],
        cwd=ROOT,
        env=build_env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"BLOCKED: Vite browser build failed:\n{result.stdout}\n{result.stderr}")
    html_files = list(build_dir.rglob("conformance-harness.html"))
    if len(html_files) != 1:
        pytest.fail(f"BLOCKED: Vite browser build did not produce one harness HTML: {html_files}")

    class QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

    try:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(QuietStaticHandler, directory=str(build_dir)),
        )
    except OSError as error:  # pragma: no cover - managed sandbox blocker
        pytest.fail(f"BLOCKED: local HTTP server bind failed on 127.0.0.1 port 0: {error}")
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="w3e-browser-static-server",
    )
    server_thread_started = False
    browser_diagnostics: dict[str, list[str]] = {"console": [], "pageerror": []}

    try:
        server_thread.start()
        server_thread_started = True

        async def run() -> None:
            relative_html = html_files[0].relative_to(build_dir).as_posix()
            base_url = f"http://127.0.0.1:{server.server_port}/{quote(relative_html, safe='/')}"

            browser = None
            context = None
            async with async_playwright() as playwright:
                try:
                    try:
                        browser = await playwright.chromium.launch(
                            headless=True, executable_path=str(chromium_executable)
                        )
                    except Exception as error:  # pragma: no cover - host sandbox blocker
                        raise RuntimeError(f"BLOCKED: headless Chromium launch failed: {error}") from error
                    context = await browser.new_context(
                        viewport={"width": 1440, "height": 1000},
                        storage_state=None,
                    )
                    context.set_default_timeout(8_000)
                    context.set_default_navigation_timeout(12_000)
                    page = await context.new_page()
                    page.on(
                        "console",
                        lambda message: browser_diagnostics["console"].append(
                            f"{message.type}: {message.text}"
                        ),
                    )
                    page.on(
                        "pageerror",
                        lambda error: browser_diagnostics["pageerror"].append(str(error)),
                    )

                    async def wait_for_request(method: str, path: str) -> None:
                        await page.wait_for_function(
                            "([expectedMethod, expectedPath]) => window.__SHIPFACTORY_CONFORMANCE_REQUESTS__.some(item => item.method === expectedMethod && item.url === expectedPath)",
                            arg=[method, path],
                        )

                    await page.goto(base_url, wait_until="domcontentloaded")
                    await page.wait_for_selector(".factory-projects-view", state="visible")
                    await page.wait_for_selector('[data-project-id="p_bound"].is-selected', state="visible")
                    await page.wait_for_selector('[data-recipe-key="dev-pipeline@14"]', state="visible")
                    assert await page.locator(".factory-tabs button").first.text_content() == "Projects"
                    assert await page.locator('[data-project-id="p_bound"]').count() == 1

                    await page.locator('[data-recipe-detach="creative-video@1"]').click()
                    await wait_for_request("PUT", "/api/plugins/shipfactory/projects/p_bound/recipe-policy")
                    await page.wait_for_selector('[data-recipe-attach="creative-video@1"]', state="visible")

                    await page.locator('[data-recipe-attach="creative-video@1"]').click()
                    await page.wait_for_function(
                        "() => window.__SHIPFACTORY_CONFORMANCE_REQUESTS__.filter(item => item.method === 'PUT').length >= 2"
                    )
                    await page.wait_for_selector('[data-recipe-detach="creative-video@1"]', state="visible")

                    await page.locator('[data-recipe-default="creative-video@1"]').click()
                    await page.wait_for_function(
                        "() => window.__SHIPFACTORY_CONFORMANCE_REQUESTS__.filter(item => item.method === 'PUT').length >= 3"
                    )
                    await page.wait_for_selector('[data-recipe-default="dev-pipeline@14"]', state="visible")
                    await page.locator('[data-recipe-key="dev-pipeline@14"] button[aria-pressed]').click()
                    parameter = page.locator("#project-dev-pipeline-14-parameter-request")
                    await parameter.fill("Ship the frozen browser proof")
                    await page.wait_for_selector(".factory-graph svg", state="visible")

                    nodes = page.locator("[data-graph-node]")
                    edges = page.locator("[data-graph-edge]")
                    assert await nodes.count() == 5
                    assert await edges.count() == 5
                    assert await nodes.first.get_attribute("tabindex") == "0"
                    assert await nodes.first.get_attribute("data-step-id") == "start"
                    assert await edges.first.get_attribute("data-edge-from")
                    assert await edges.first.get_attribute("data-edge-to")
                    assert await edges.first.get_attribute("data-edge-kind") == "needs"
                    approval = page.locator('[data-step-id="approve"]')
                    assert "factory-graph-node--operator-only" in (await approval.get_attribute("class") or "")
                    assert "operator-only" in (await approval.text_content() or "")

                    await nodes.first.focus()
                    await nodes.first.press("Enter")
                    inspector = page.locator('[role="dialog"][aria-label="Graph node inspector"]')
                    await inspector.wait_for(state="visible")
                    await inspector.get_by_role("button", name="Close graph node inspector").click()
                    await inspector.wait_for(state="hidden")

                    await page.locator('[data-project-launch="p_bound"]').click()
                    await wait_for_request("POST", "/api/plugins/shipfactory/projects/p_bound/flights")
                    await page.wait_for_selector('[data-flight-instance-id="fixture-flight-created"]', state="visible")
                    await wait_for_request("GET", "/api/plugins/shipfactory/instances/fixture-flight-created/graph")
                    await page.wait_for_selector('[data-step-id="start"].factory-graph-node--state-done', state="visible")
                    await page.wait_for_selector('[data-step-id="approve"].factory-graph-node--state-waiting', state="visible")
                    overlay = page.locator('[aria-label="Live overlay summary"]')
                    await overlay.wait_for(state="visible")
                    overlay_text = await overlay.text_content()
                    assert "Next actor: Operator" in (overlay_text or "")
                    assert "Blocker: human action required" in (overlay_text or "")
                    assert "Human operator approval required." in (overlay_text or "")
                    await page.locator('[data-step-id="approve"]').click()
                    inspector = page.locator('[role="dialog"][aria-label="Graph node inspector"]')
                    await inspector.wait_for(state="visible")
                    inspector_text = await inspector.text_content()
                    assert "Activation history" in (inspector_text or "")
                    assert "#1 · waiting" in (inspector_text or "")
                    assert "Receipts: unavailable" in (inspector_text or "")
                    assert "Evidence: unavailable" in (inspector_text or "")
                    requests = await page.evaluate("window.__SHIPFACTORY_CONFORMANCE_REQUESTS__")
                    launch_requests = [
                        item for item in requests
                        if item["method"] == "POST" and item["url"] == "/api/plugins/shipfactory/projects/p_bound/flights"
                    ]
                    assert len(launch_requests) == 1
                    launch_body = json.loads(launch_requests[0]["body"] or "{}")
                    assert launch_body["recipe"] == "dev-pipeline"
                    assert launch_body["version"] == 14
                    assert launch_body["parameters"]["request"] == "Ship the frozen browser proof"
                    assert isinstance(launch_body["idempotency_key"], str) and launch_body["idempotency_key"]
                    assert _no_board(launch_body)
                    assert await page.locator('[data-flight-instance-id="fixture-flight-created"]').text_content() == "Started flight fixture-flight-created for dev-pipeline@14."

                    unclassified_project = page.locator('[data-project-id="unclassified"]')
                    await page.wait_for_selector('.factory-projects-view', state="visible")
                    await unclassified_project.wait_for(state="visible")
                    await unclassified_project.click()
                    await page.wait_for_selector('[data-unclassified="true"]', state="visible")
                    assert await page.get_by_text("No recent flights.").count() == 1
                    assert await page.locator("[data-project-launch]").count() == 0

                    await page.goto(base_url + "?graph=hostile", wait_until="domcontentloaded")
                    await page.wait_for_selector('[data-graph-node="start"]', state="visible")
                    hostile = '<img src=x onerror="window.__w3e_pwned=1">'
                    assert hostile in await page.locator("svg").text_content()
                    assert await page.locator("img").count() == 0
                    assert await page.evaluate("window.__w3e_pwned === undefined")

                    await page.goto(base_url + "?graph=unsupported", wait_until="domcontentloaded")
                    await page.wait_for_selector(".factory-graph-error", state="visible")
                    assert "Graph unavailable" in (await page.locator(".factory-graph-error").text_content() or "")

                    await page.goto(base_url + "?graph=cycle", wait_until="domcontentloaded")
                    await page.wait_for_selector(".factory-graph-error", state="visible")
                    assert "cyclic" in (await page.locator(".factory-graph-error").text_content() or "")

                    await page.goto(base_url, wait_until="domcontentloaded")
                    await page.wait_for_selector(".factory-graph", state="visible")
                    await page.set_viewport_size({"width": 700, "height": 900})
                    columns = await page.locator(".factory-projects-layout").evaluate("element => getComputedStyle(element).gridTemplateColumns")
                    assert len(columns.split()) == 1
                    await page.emulate_media(reduced_motion="reduce")
                    transition = await page.locator(".factory-graph-node").first.evaluate("element => getComputedStyle(element).transitionDuration")
                    assert transition == "0s"
                finally:
                    if context is not None:
                        try:
                            await context.close()
                        finally:
                            if browser is not None:
                                await browser.close()
                    elif browser is not None:
                        await browser.close()

        try:
            asyncio.run(asyncio.wait_for(run(), timeout=90))
        except asyncio.TimeoutError as error:
            pytest.fail(
                "BLOCKED: rendered browser proof exceeded its 90-second bound: "
                f"{error}\n{_browser_diagnostics(browser_diagnostics)}"
            )
        except Exception as error:
            pytest.fail(f"{error}\n{_browser_diagnostics(browser_diagnostics)}")
    finally:
        if server_thread_started:
            server.shutdown()
        server.server_close()
        if server_thread_started:
            server_thread.join()


def _browser_diagnostics(diagnostics: dict[str, list[str]]) -> str:
    return (
        "Browser console:\n"
        + ("\n".join(diagnostics["console"]) or "<none>")
        + "\nBrowser pageerror:\n"
        + ("\n".join(diagnostics["pageerror"]) or "<none>")
    )
