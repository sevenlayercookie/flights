"""Inspect Google Flights controls and capture protobuf-bearing URLs.

This is research tooling, not part of the fast-flights package. It intentionally
uses an installed Chrome executable so Playwright does not download a browser.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, sync_playwright

from fast_flights import FlightQuery, create_query


def _default_url() -> str:
    departure = (date.today() + timedelta(days=14)).isoformat()
    return create_query(
        flights=[
            FlightQuery(
                date=departure,
                from_airport="MSP",
                to_airport="DEN",
            )
        ],
        currency="USD",
        language="en",
    ).url()


def _accept_consent(page: Page) -> None:
    for label in ("Accept all", "I agree"):
        button = page.get_by_role("button", name=label, exact=True)
        if button.count():
            button.first.click()
            page.wait_for_timeout(1_000)
            return


def _control_inventory(page: Page) -> list[dict[str, str | None]]:
    return page.locator("button, [role=button], input, [role=combobox]").evaluate_all(
        """elements => {
            const seen = new Set();
            return elements.filter((element) =>
                element.getClientRects().length &&
                getComputedStyle(element).visibility !== "hidden"
            ).map((element) => ({
                tag: element.tagName.toLowerCase(),
                role: element.getAttribute("role"),
                aria_label: element.getAttribute("aria-label"),
                text: (element.innerText || element.value || "").trim(),
                context: (element.parentElement?.innerText || "").trim().slice(0, 160),
                name: element.getAttribute("name"),
                type: element.getAttribute("type"),
            })).filter((item) => {
                if (!(item.aria_label || item.text || item.name)) return false;
                const key = JSON.stringify(item);
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            });
        }"""
    )


def _tfs_record(url: str, source: str) -> dict[str, str] | None:
    query = parse_qs(urlparse(url).query)
    values = query.get("tfs")
    if not values:
        return None
    return {"source": source, "url": url, "tfs": values[0]}


def inspect(
    url: str,
    chrome: str,
    headed: bool,
    wait_ms: int,
    clicks: list[str],
    text_clicks: list[str],
    range_values: list[str],
    add_origin: str | None,
    add_destination: str | None,
) -> dict[str, object]:
    captured: list[dict[str, str]] = []
    seen: set[str] = set()

    def capture(candidate: str, source: str) -> None:
        record = _tfs_record(candidate, source)
        if record is not None and record["tfs"] not in seen:
            seen.add(record["tfs"])
            captured.append(record)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chrome,
            headless=not headed,
            args=["--no-sandbox"],
        )
        page = browser.new_page(locale="en-US")
        page.on("request", lambda request: capture(request.url, "request"))
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        _accept_consent(page)
        page.wait_for_timeout(wait_ms)
        for label in clicks:
            page.get_by_role("button", name=label, exact=True).last.click()
            page.wait_for_timeout(1_000)
        for label in text_clicks:
            matches = page.get_by_text(label, exact=True)
            visible = next(
                (matches.nth(index) for index in range(matches.count()) if matches.nth(index).is_visible()),
                None,
            )
            if visible is None:
                raise ValueError(f"no visible exact text match for {label!r}")
            visible.click(force=True)
            page.wait_for_timeout(2_000)
        for setting in range_values:
            label, value = setting.split("=", 1)
            control = page.locator(f'input[type="range"][aria-label="{label}"]').last
            control.evaluate(
                """(element, value) => {
                    element.value = value;
                    element.dispatchEvent(new Event("input", {bubbles: true}));
                    element.dispatchEvent(new Event("change", {bubbles: true}));
                }""",
                value,
            )
            page.wait_for_timeout(2_000)
        if add_origin:
            page.locator('input[aria-label^="Where from?"]').first.click()
            page.get_by_role(
                "button", name="Origin, Select multiple airports", exact=True
            ).click()
            extra = page.locator(
                '[role="dialog"] input[role="combobox"]:visible'
            ).last
            extra.fill(add_origin)
            page.wait_for_timeout(1_500)
            extra.press("ArrowDown")
            extra.press("Enter")
            page.wait_for_timeout(1_000)
            page.get_by_role("button", name="Done", exact=True).last.click()
            page.wait_for_timeout(3_000)
        if add_destination:
            page.locator('input[aria-label^="Where to?"]').first.click()
            page.get_by_role(
                "button", name="Destination, Select multiple airports", exact=True
            ).click()
            extra = page.locator(
                '[role="dialog"] input[role="combobox"]:visible'
            ).last
            extra.fill(add_destination)
            page.wait_for_timeout(1_500)
            extra.press("ArrowDown")
            extra.press("Enter")
            page.wait_for_timeout(1_000)
            page.get_by_role("button", name="Done", exact=True).last.click()
            page.wait_for_timeout(3_000)
        capture(page.url, "page")
        result = {
            "title": page.title(),
            "page_url": page.url,
            "controls": _control_inventory(page),
            "dialogs": page.locator('[role="dialog"]').all_inner_texts(),
            "captured_tfs": captured,
        }
        browser.close()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=_default_url())
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait-ms", type=int, default=8_000)
    parser.add_argument(
        "--click",
        action="append",
        default=[],
        help="Exact accessible button name to click after loading; repeatable.",
    )
    parser.add_argument(
        "--click-text",
        action="append",
        default=[],
        help="Exact visible text to click after button actions; repeatable.",
    )
    parser.add_argument(
        "--set-range",
        action="append",
        default=[],
        metavar="LABEL=VALUE",
        help="Set a range input by exact aria-label; repeatable.",
    )
    parser.add_argument(
        "--add-origin",
        help="Add an airport code through the UI's origin multi-select mode.",
    )
    parser.add_argument(
        "--add-destination",
        help="Add an airport code through the UI's destination multi-select mode.",
    )
    args = parser.parse_args(argv)

    print(
        json.dumps(
            inspect(
                args.url,
                args.chrome,
                args.headed,
                args.wait_ms,
                args.click,
                args.click_text,
                args.set_range,
                args.add_origin,
                args.add_destination,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
