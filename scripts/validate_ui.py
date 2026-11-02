"""Browser smoke test using only a temporary archive and synthetic data."""
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from experiment_archive.api import ExperimentRecord
from experiment_archive.app import AppService, make_server


def main():
    errors = []
    with tempfile.TemporaryDirectory(prefix="myarchiver-ui-") as temporary:
        service = AppService(Path(temporary) / "archive")
        for day in (1, 2):
            service.client.publish(ExperimentRecord(metadata={
                "schema_version": 1, "experiment_id": f"demo_{day}",
                "title": f"合成测试记录 {day}", "experiment_date": f"2026-01-0{day}",
                "source_refs": [{"id": "synthetic", "description": "Synthetic UI fixture"}],
                "domains": ["demo"], "evaluations": {},
                "lifecycle": {"run": "completed", "evaluation": "pending", "conclusion": "inconclusive"}
            }, note="合成测试，关键词检索示例，无实际实验数据。"))
        server = make_server(service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#{service.token}")
                page.locator(".experiment-card").first.wait_for()
                assert page.locator(".experiment-card").first.get_attribute("data-open") == "demo_2"
                page.locator("#library-sort").select_option("time_asc")
                page.wait_for_function("() => document.querySelector('.experiment-card').dataset.open === 'demo_1'")
                page.locator('[data-page="search"]').first.click()
                page.locator("#search-mode").select_option("lexical")
                page.locator("#semantic-query").fill("合成")
                page.locator("#search-submit").click()
                page.locator(".result-card").first.wait_for()
                page.locator("#search-sort").select_option("time_desc")
                page.wait_for_function("() => state.searchResults?.sort === 'time_desc'")
                assert page.locator('.result-card [data-open]').first.get_attribute('data-open') == 'demo_2'
                page.locator('[data-page="settings"]').first.click()
                page.locator('[data-pick="config"]').wait_for()
                page.route('**/api/paths/pick', lambda route: route.fulfill(json={"path": None}))
                page.locator('[data-pick="config"]').click()
                page.wait_for_function("() => !document.querySelector('[data-pick=\"config\"]').disabled")
                page.set_viewport_size({"width": 700, "height": 900})
                page.locator('[data-page="library"]').first.click()
                page.locator(".experiment-card").first.wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                browser.close()
            assert not errors, errors
            print("Synthetic UI smoke test passed: catalog, sorting, lexical search, settings, cancellation, mobile layout.")
        finally:
            server.shutdown()
            server.server_close()
            service.close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
