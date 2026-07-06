"""Generic directory-style listing scraper.

Given a starting URL, auto-detects the repeating "row" block, per-row column
selectors, and the next-page link, then paginates and extracts records. Any
of the detected selectors can be overridden explicitly once /inspect shows
you what it found. Optional Playwright rendering is used for JS-heavy sites
when render=True (requires the image to be built with WITH_PLAYWRIGHT=true).
"""
from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

USER_AGENT = "Mozilla/5.0 (compatible; DirectoryScraper/1.0)"

NEXT_TEXT_PATTERN = re.compile(r"^\s*(next|»|>|more|older)[\s»›>→]*$", re.IGNORECASE)

FIELD_HINTS = {
    "name": re.compile(r"name|title|company|business", re.IGNORECASE),
    "phone": re.compile(r"phone|tel", re.IGNORECASE),
    "email": re.compile(r"e[-]?mail", re.IGNORECASE),
    "address": re.compile(r"address|location|city", re.IGNORECASE),
    # "link" alone is excluded: it's a common generic UI/styling class (e.g.
    # Bootstrap's "card-link", "nav-link") shared by unrelated anchors (map,
    # phone, email links), so it false-matches long before a genuinely
    # website-labeled element is reached.
    "website": re.compile(r"website|url", re.IGNORECASE),
}


@dataclass
class ScrapeConfig:
    url: str
    format: str = "json"
    max_pages: int = 25
    delay: float = 1.0
    min_repeat: int = 5
    render: bool = False
    wait: Optional[str] = None
    row_selector: Optional[str] = None
    col_selectors: Optional[dict] = None
    next_selector: Optional[str] = None


def fetch_html(url: str, render: bool, wait: Optional[str]) -> str:
    if render:
        if not HAS_PLAYWRIGHT:
            raise RuntimeError(
                "render=true requires Playwright, which isn't installed in this image. "
                "Rebuild with --build-arg WITH_PLAYWRIGHT=true."
            )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle")
            if wait:
                page.wait_for_selector(wait, timeout=15000)
            html = page.content()
            browser.close()
        return html

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


INLINE_TAGS = {"a", "span", "small", "strong", "em", "b", "i", "img", "code", "abbr", "u"}


def _signature(tag: Tag) -> str:
    classes = " ".join(sorted(tag.get("class", [])))
    return f"{tag.name}.{classes}"


def _table_row_selector(soup: BeautifulSoup, min_repeat: int) -> Optional[str]:
    """Prefer a semantic <table> body when one has enough data rows. A <tr> is
    a purpose-built "one record per row" signal - stronger than any guessed
    class - and accessible directories (e.g. many .edu sites) often render
    plain, unclassed <tr>/<td> rows with `headers` attributes instead.
    """
    best: Optional[tuple[Tag, int]] = None
    for table in soup.find_all("table"):
        container = table.find("tbody") or table
        rows = [tr for tr in container.find_all("tr", recursive=False) if tr.find("td")]
        if len(rows) < min_repeat:
            continue
        if best is None or len(rows) > best[1]:
            best = (table, len(rows))
    if best is None:
        return None

    table = best[0]
    table_id = table.get("id")
    table_cls = table.get("class")
    if table_id:
        table_part = f"table#{table_id}"
    elif table_cls:
        table_part = f"table.{'.'.join(table_cls)}"
    else:
        table_part = "table"
    return f"{table_part} tbody tr" if table.find("tbody") else f"{table_part} tr"


def detect_row_selector(soup: BeautifulSoup, min_repeat: int) -> Optional[str]:
    """Find the most common classed-tag signature that repeats at least min_repeat times.

    A "row" is expected to be a container of several fields, so plain inline
    tags (a, span, small, ...) are only used as a last resort: a directory
    listing's per-item links/labels usually outnumber the rows that wrap them,
    which would otherwise win on raw repeat count alone.
    """
    table_selector = _table_row_selector(soup, min_repeat)
    if table_selector:
        return table_selector

    counts: dict[str, int] = {}
    first_seen: dict[str, Tag] = {}
    for tag in soup.find_all(True):
        if not tag.get("class"):
            continue
        sig = _signature(tag)
        counts[sig] = counts.get(sig, 0) + 1
        first_seen.setdefault(sig, tag)

    candidates = [(sig, n) for sig, n in counts.items() if n >= min_repeat]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)

    def is_container(sig: str) -> bool:
        tag_name = sig.partition(".")[0]
        if tag_name in INLINE_TAGS:
            return False
        return len(first_seen[sig].find_all(True)) >= 1

    containers = [c for c in candidates if is_container(c[0])]
    sig = (containers or candidates)[0][0]
    tag_name, _, cls = sig.partition(".")
    return f"{tag_name}.{cls.replace(' ', '.')}" if cls else tag_name


def _name_from_class(cls: list[str]) -> str:
    """Fall back to the element's own class as a field name, e.g. ["tag"] -> "tag"."""
    token = re.sub(r"[^a-z0-9]+", "_", cls[-1].lower()).strip("_")
    return token or "field"


def _selector_for(child: Tag) -> str:
    """Build a selector for a descendant. Unclassed tags are qualified by their
    parent so lookups don't accidentally match an unrelated sibling with the
    same tag name (e.g. two unclassed <a> tags in one row)."""
    cls = child.get("class")
    if cls:
        return f"{child.name}.{'.'.join(cls)}"
    parent = child.parent
    if isinstance(parent, Tag) and parent.name:
        parent_cls = parent.get("class")
        parent_part = f"{parent.name}.{'.'.join(parent_cls)}" if parent_cls else parent.name
        return f"{parent_part} > {child.name}"
    return child.name


def _table_col_selectors(row: Tag) -> dict:
    """A <td headers="name"> cell already carries its own authoritative field
    name (used for accessible header/cell association) - use it verbatim
    instead of guessing from class/text, and skip the FIELD_HINTS pass
    entirely so distinct concepts that happen to share a hint keyword (e.g.
    a person's "name" vs. their job "title") aren't conflated.
    """
    cols: dict[str, str] = {}
    used_names: set[str] = set()
    for td in row.find_all("td"):
        headers = td.get("headers")
        text = td.get_text(strip=True)
        if not headers or not text:
            continue
        # bs4 treats `headers` as multi-valued (like `class`), always a list
        headers_str = " ".join(headers) if isinstance(headers, list) else headers
        name = re.sub(r"[^a-z0-9]+", "_", headers_str.strip().lower()).strip("_") or "field"
        base_name = name
        suffix = 2
        while name in used_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name)
        cols[name] = f'td[headers="{headers_str}"]'
    return cols


def detect_col_selectors(row: Tag) -> dict:
    """Guess field names for descendants of a row using common naming hints,
    falling back to the element's own class name (e.g. "author") rather than a
    meaningless "field_N" when no hint matches. Unclassed elements are only
    considered when they carry a `title` attribute - a common pattern for
    truncated labels, e.g. <a title="Full Name">Full N...</a>.
    """
    table_cols = _table_col_selectors(row)
    if table_cols:
        return table_cols

    candidates = []
    used_selectors = set()
    for child in row.find_all(True):
        text = child.get_text(strip=True)
        cls = child.get("class")
        title_attr = child.get("title")
        if not text or len(text) > 200 or not (cls or title_attr):
            continue

        selector = _selector_for(child)
        if selector in used_selectors:
            # e.g. several sibling a.tag - select_one would only ever return the
            # first anyway, so extra columns for the same selector are just noise.
            continue
        used_selectors.add(selector)

        # own_text excludes descendant text, so a wrapping container (e.g. a
        # card body that includes the business's own name, like "... Business
        # Services") can't accidentally match a child's hint keyword.
        own_text = "".join(child.find_all(string=True, recursive=False)).strip()
        candidates.append({"cls": cls, "own_text": own_text, "title_attr": title_attr, "selector": selector})

    cols: dict[str, str] = {}
    used_names = set()

    def assign(name: str, selector: str) -> None:
        base_name = name
        suffix = 2
        while name in used_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name)
        cols[name] = selector

    # Round 1: class-name hint matches take priority over text-based ones - a
    # class is a deliberate structural label, while text is content that can
    # coincidentally contain a hint keyword (e.g. a business's own name
    # containing the word "business", which would otherwise race an actual
    # title element for the "name" slot depending on DOM order).
    remaining = []
    for c in candidates:
        cls_haystack = " ".join(c["cls"] or [])
        name = next(
            (f for f, pattern in FIELD_HINTS.items() if f not in used_names and pattern.search(cls_haystack)),
            None,
        )
        if name:
            assign(name, c["selector"])
        else:
            remaining.append(c)

    # Round 2: fall back to the element's own text, then to its class name.
    for c in remaining:
        haystack = c["own_text"] + (" title" if c["title_attr"] else "")
        name = next(
            (f for f, pattern in FIELD_HINTS.items() if f not in used_names and pattern.search(haystack)),
            None,
        )
        name = name or (_name_from_class(c["cls"]) if c["cls"] else "title")
        assign(name, c["selector"])

    return cols


def detect_next_selector(soup: BeautifulSoup) -> Optional[str]:
    a = soup.find("a", rel="next")
    if a and a.get("href"):
        return "a[rel='next']"
    for a in soup.find_all("a", href=True):
        if not NEXT_TEXT_PATTERN.match(a.get_text(strip=True)):
            continue
        cls = a.get("class")
        if cls:
            return f"a.{'.'.join(cls)}"
        parent = a.parent
        if parent and parent.get("class"):
            return f"{parent.name}.{'.'.join(parent['class'])} a"
    return None


def extract_record(row: Tag, col_selectors: dict) -> dict:
    record = {}
    for field_name, selector in col_selectors.items():
        el = row.select_one(selector) if selector else None
        if el is None:
            record[field_name] = None
            continue
        href = el.get("href")
        if href and href.startswith("mailto:"):
            record[field_name] = href[len("mailto:"):]
        elif href and href.startswith("tel:"):
            record[field_name] = href[len("tel:"):]
        elif field_name == "website":
            # the matched element is often a wrapper (e.g. <li>) around the
            # actual <a>, so fall back to a descendant anchor's href - the
            # visible text alone (e.g. "Visit Website") carries no data.
            if not href:
                nested = el.find("a", href=True)
                href = nested["href"] if nested else None
            record[field_name] = href or el.get_text(strip=True)
        elif el.get("title"):
            # `title` commonly holds the untruncated label when visible text is elided
            record[field_name] = el["title"].strip()
        else:
            record[field_name] = el.get_text(strip=True)
    return record


def get_next_url(soup: BeautifulSoup, next_selector: Optional[str], current_url: str) -> Optional[str]:
    if not next_selector:
        return None
    el = soup.select_one(next_selector)
    if not el or not el.get("href"):
        return None
    return urljoin(current_url, el["href"])


def inspect(config: ScrapeConfig) -> dict:
    """Fetch a single page and report detected/overridden selectors plus a sample of rows."""
    html = fetch_html(config.url, config.render, config.wait)
    soup = BeautifulSoup(html, "html.parser")

    row_selector = config.row_selector or detect_row_selector(soup, config.min_repeat)
    rows = soup.select(row_selector) if row_selector else []

    col_selectors = config.col_selectors or (detect_col_selectors(rows[0]) if rows else {})
    next_selector = config.next_selector or detect_next_selector(soup)

    return {
        "url": config.url,
        "row_selector": row_selector,
        "row_count": len(rows),
        "col_selectors": col_selectors,
        "next_selector": next_selector,
        "sample": [extract_record(r, col_selectors) for r in rows[:5]],
    }


def scrape(config: ScrapeConfig) -> dict:
    """Paginate from config.url following next_selector, extracting one record per row."""
    if config.row_selector and config.col_selectors:
        row_selector = config.row_selector
        col_selectors = config.col_selectors
        next_selector = config.next_selector
    else:
        preview = inspect(config)
        row_selector = config.row_selector or preview["row_selector"]
        col_selectors = config.col_selectors or preview["col_selectors"]
        next_selector = config.next_selector or preview["next_selector"]

    if not row_selector:
        raise ValueError("Could not detect a row_selector; provide one explicitly (see /inspect).")

    records = []
    url = config.url
    seen_urls = {url}
    pages = 0

    while url and pages < config.max_pages:
        html = fetch_html(url, config.render, config.wait)
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select(row_selector):
            records.append(extract_record(row, col_selectors))
        pages += 1

        next_url = get_next_url(soup, next_selector, url)
        if not next_url or next_url in seen_urls:
            break
        seen_urls.add(next_url)
        url = next_url
        if pages < config.max_pages:
            time.sleep(config.delay)

    return {
        "count": len(records),
        "pages": pages,
        "mode": "playwright" if config.render else "requests",
        "records": records,
        "row_selector": row_selector,
        "col_selectors": col_selectors,
        "next_selector": next_selector,
    }


def records_to_csv(records: list[dict]) -> str:
    if not records:
        return ""
    fieldnames = list(records[0].keys())
    for r in records[1:]:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Scrape a directory-style listing site.")
    parser.add_argument("url")
    parser.add_argument("--format", default="json", choices=["json", "csv"])
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--min-repeat", type=int, default=5)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--wait")
    parser.add_argument("--row-selector")
    parser.add_argument("--next-selector")
    parser.add_argument("--inspect", action="store_true", help="preview detected selectors instead of scraping")
    args = parser.parse_args()

    config = ScrapeConfig(
        url=args.url,
        format=args.format,
        max_pages=args.max_pages,
        delay=args.delay,
        min_repeat=args.min_repeat,
        render=args.render,
        wait=args.wait,
        row_selector=args.row_selector,
        next_selector=args.next_selector,
    )

    if args.inspect:
        print(json.dumps(inspect(config), indent=2))
        return

    result = scrape(config)
    print(records_to_csv(result["records"]) if args.format == "csv" else json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
