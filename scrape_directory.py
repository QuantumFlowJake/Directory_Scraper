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
import xml.etree.ElementTree as ET
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
    # negative lookbehinds keep classes like "cn-email-address" or
    # "phone-address" (already claimed by the email/phone hint above) from
    # also matching here just because "address" is a substring.
    "address": re.compile(r"(?<!email-)(?<!e-mail-)(?<!mail-)(?<!phone-)address|location|city", re.IGNORECASE),
    # "link" alone is excluded: it's a common generic UI/styling class (e.g.
    # Bootstrap's "card-link", "nav-link") shared by unrelated anchors (map,
    # phone, email links), so it false-matches long before a genuinely
    # website-labeled element is reached.
    "website": re.compile(r"website|url", re.IGNORECASE),
}

# --- Output canonicalization -----------------------------------------------
#
# Auto-detected field names vary wildly by site (a plain "name" class here, a
# fallback-named "notranslate_2" there), and raw values are often dirty -
# concatenated with UI labels/type suffixes the source markup never actually
# separated with whitespace (e.g. "Work Email:x@y.eduINTERNET"). Regardless of
# what a given site's markup looks like, every scraped record is normalized
# to lead with the same seven columns - first_name, middle_name, last_name,
# title, email, phone, full_name - with clean values, followed by whatever
# else was detected on the row.

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,6}\b")
PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?:\s*(?:ext\.?|x|extension)\s*\d+)?",
    re.IGNORECASE,
)

FULLNAME_KEY_RE = re.compile(r"full[_ ]?name", re.IGNORECASE)
FAMILY_KEY_RE = re.compile(r"family|sur[_ ]?name|last[_ ]?name", re.IGNORECASE)
GIVEN_KEY_RE = re.compile(r"given|first[_ ]?name", re.IGNORECASE)
JOB_TITLE_KEY_RE = re.compile(r"job[_ ]?title|position|role|designation", re.IGNORECASE)

# Content-based fallback for when a site's "title" field was actually claimed
# by the person's full name (e.g. an unclassed heading link) and the real job
# title ended up under a meaningless fallback key instead.
JOB_TITLE_HINT_RE = re.compile(
    r"\b(faculty|professor|instructor|lecturer|director|dean|chair|coordinator|"
    r"specialist|manager|supervisor|administrator|assistant|associate|analyst|"
    r"engineer|technician|officer|president|executive|counselor|advisor|"
    r"librarian|registrar|provost|principal|representative)\b",
    re.IGNORECASE,
)


def _first_regex_match(record: dict, pattern: re.Pattern) -> str:
    for value in record.values():
        if isinstance(value, str):
            m = pattern.search(value)
            if m:
                return m.group(0)
    return ""


def _find_key(record: dict, pattern: re.Pattern, exclude: set = frozenset()) -> Optional[str]:
    for key in record:
        if key in exclude:
            continue
        if pattern.search(key):
            return key
    return None


def _looks_like_person_name(value: str) -> bool:
    words = value.replace(",", " ").split()
    return 1 < len(words) <= 5 and not any(ch.isdigit() for ch in value)


def canonicalize_record(record: dict) -> dict:
    """Reshape a raw detected record into the fixed output column order,
    scrubbing the leading email/phone columns of any label/type text the
    source markup ran together with the actual value."""
    email = _first_regex_match(record, EMAIL_RE)
    phone = _first_regex_match(record, PHONE_RE)

    full_key = _find_key(record, FULLNAME_KEY_RE)
    family_key = _find_key(record, FAMILY_KEY_RE)
    given_key = _find_key(record, GIVEN_KEY_RE, exclude={family_key} if family_key else set())

    raw_full = ""
    raw_full_key = None
    if full_key and record.get(full_key):
        raw_full = record[full_key]
        raw_full_key = full_key
    elif family_key and record.get(family_key):
        given_value = record.get(given_key) if given_key else record.get("name", "")
        raw_full = f"{given_value or ''} {record[family_key]}".strip()
    elif record.get("name"):
        raw_full = record["name"]
        raw_full_key = "name"
    elif record.get("title") and _looks_like_person_name(record["title"]):
        raw_full = record["title"]
    else:
        # Last resort: no field's *name* hinted at a person's name at all (a
        # sort-key helper span, say, with a class like "hidden-sortable-data"
        # that gives no semantic clue) - fall back to whichever field's
        # *value* is shaped like one, first field wins.
        for key, value in record.items():
            if key in ("email", "phone") or not isinstance(value, str):
                continue
            if _looks_like_person_name(value):
                raw_full = value
                raw_full_key = key
                break

    name_parts = split_person_name(raw_full)

    consumed = {
        k for k in (full_key, family_key, given_key, "name", "email", "phone", raw_full_key) if k and k in record
    }

    title_key = _find_key(record, JOB_TITLE_KEY_RE, exclude=consumed)
    if title_key:
        title = record.get(title_key) or ""
        consumed.add(title_key)
    elif record.get("title") and record["title"] != raw_full:
        title = record["title"]
        consumed.add("title")
    else:
        title = ""
        for key, value in record.items():
            if key in consumed or key == "title" or not isinstance(value, str):
                continue
            if JOB_TITLE_HINT_RE.search(value):
                title = value
                consumed.add(key)
                break
    # whichever branch resolved `title` above, the record's own "title" key
    # (if any) is now fully represented by the canonical field - never let it
    # re-appear as an extra column and clobber that resolved value below.
    consumed.add("title")

    canonical = {
        "first_name": name_parts["first_name"],
        "middle_name": name_parts["middle_name"],
        "last_name": name_parts["last_name"],
        "title": title,
        "email": email,
        "phone": phone,
        "full_name": name_parts["full_name"] or raw_full,
    }
    canonical.update({k: v for k, v in record.items() if k not in consumed})
    return canonical


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

    if row.name == "tr":
        for name, selector in _positional_table_fallback(row, cols).items():
            assign(name, selector)

    return cols


def _positional_table_fallback(row: Tag, already_covered: dict) -> dict:
    """Plain semantic tables (bare <td> cells, no `headers` attributes, no
    classes at all) leave the descendant scan above with nothing to grab for
    columns like a bare `<td>Some Dept</td>` or an unclassed mailto link -
    there's no class/title hook anywhere in the cell. When a <thead> is
    present, borrow its <th> labels and map them to <td>s by position,
    skipping any <td> a selector above already reaches into (so an
    already-correct match - e.g. a classed sort-key span - isn't replaced by
    the noisier whole-cell text, which on some sites duplicates that same
    value right next to it)."""
    table = row.find_parent("table")
    if table is None:
        return {}
    thead = table.find("thead")
    header_cells = thead.find_all("th") if thead else []
    tds = row.find_all("td", recursive=False)
    if not header_cells or len(header_cells) != len(tds):
        return {}

    covered_indices = set()
    for selector in already_covered.values():
        el = row.select_one(selector)
        if el is None:
            continue
        td_ancestor = el if el.name == "td" else el.find_parent("td")
        if td_ancestor in tds:
            covered_indices.add(tds.index(td_ancestor))

    cols: dict[str, str] = {}
    for idx, th in enumerate(header_cells):
        if idx in covered_indices:
            continue
        label = th.get_text(strip=True)
        if not label:
            continue
        name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "field"
        cols[name] = f"td:nth-child({idx + 1})"
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


# Common multi-word surname particles (Spanish/Portuguese, Dutch/German,
# French, "Saint") that should stay glued to the last name rather than being
# treated as a middle name, e.g. "de la Cruz", "van der Berg", "St. John".
NAME_PARTICLES = {
    "de", "del", "dela", "della", "der", "den", "des", "di", "do", "dos", "du",
    "la", "las", "le", "les", "los", "van", "von", "st", "ter", "ten",
}


def split_person_name(raw: Optional[str]) -> dict:
    """Split a person's name into first/middle/last, plus a normalized
    "First Middle Last" full name. Handles both "Last, First Middle"
    (comma-separated, common in institutional directories like a faculty
    listing) and plain "First Middle Last" order, and keeps multi-word
    "particle" surnames together (e.g. "de la Cruz") instead of splitting
    them apart at the last space.
    """
    raw = (raw or "").strip()
    if not raw:
        return {"first_name": "", "middle_name": "", "last_name": "", "full_name": ""}

    if "," in raw:
        # "Last, First Middle" - the part before the comma is already the
        # full (possibly multi-word) last name, no particle-scanning needed.
        last_part, _, rest = raw.partition(",")
        last_name = last_part.strip()
        given_tokens = rest.split()
    else:
        tokens = raw.split()
        if len(tokens) == 1:
            return {"first_name": tokens[0], "middle_name": "", "last_name": "", "full_name": tokens[0]}
        split_idx = len(tokens) - 1
        while split_idx > 0 and tokens[split_idx - 1].lower().rstrip(".") in NAME_PARTICLES:
            split_idx -= 1
        last_name = " ".join(tokens[split_idx:])
        given_tokens = tokens[:split_idx]

    first_name = given_tokens[0] if given_tokens else ""
    middle_name = " ".join(given_tokens[1:]) if len(given_tokens) > 1 else ""
    full_name = " ".join(part for part in (first_name, middle_name, last_name) if part)
    return {"first_name": first_name, "middle_name": middle_name, "last_name": last_name, "full_name": full_name}


def _extract_raw_record(row: Tag, col_selectors: dict) -> dict:
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


def extract_record(row: Tag, col_selectors: dict) -> dict:
    return canonicalize_record(_extract_raw_record(row, col_selectors))


def get_next_url(soup: BeautifulSoup, next_selector: Optional[str], current_url: str) -> Optional[str]:
    if not next_selector:
        return None
    el = soup.select_one(next_selector)
    if not el or not el.get("href"):
        return None
    return urljoin(current_url, el["href"])


DATA_FEED_URL_RE = re.compile(
    r"""(?:fetch|axios\.get|\$\.get|\.ajax)\(\s*["']([^"']+\.(?:xml|json))["']"""
    r"""|url\s*[:=]\s*["']([^"']+\.(?:xml|json))["']""",
    re.IGNORECASE,
)


def _iter_data_feed_urls(html: str, base_url: str):
    """Some directories render an empty table client-side (DataTables and
    similar) that fetches the real records from a companion XML/JSON feed
    instead of ever putting them in the page's HTML - no amount of row/column
    detection on the page itself will find them. Yield candidate feed URLs
    found inline first, then in any linked <script src> - unrelated scripts
    (analytics, trackers, ...) can coincidentally match the same pattern, so
    callers should validate a candidate's actual content rather than trust
    the first hit."""
    seen: set = set()

    def _emit(match: re.Match):
        url = urljoin(base_url, match.group(1) or match.group(2))
        if url not in seen:
            seen.add(url)
            return url
        return None

    for m in DATA_FEED_URL_RE.finditer(html):
        url = _emit(m)
        if url:
            yield url

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", src=True):
        script_url = urljoin(base_url, script["src"])
        try:
            resp = requests.get(script_url, headers={"User-Agent": USER_AGENT}, timeout=15)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        for m in DATA_FEED_URL_RE.finditer(resp.text):
            url = _emit(m)
            if url:
                yield url


def _parse_xml_listing(xml_text: str) -> list[dict]:
    """Flatten the most-repeated child element in an XML feed (e.g.
    <listing><person>...</person>...) into one flat dict per item, keyed by
    child tag name."""
    root = ET.fromstring(xml_text)
    tag_counts: dict[str, int] = {}
    for el in root.iter():
        if el is root:
            continue
        tag_counts[el.tag] = tag_counts.get(el.tag, 0) + 1
    if not tag_counts:
        return []
    item_tag = max(tag_counts, key=tag_counts.get)
    if tag_counts[item_tag] < 2:
        return []

    records = []
    for item in root.iter(item_tag):
        record = {}
        for child in item:
            key = re.sub(r"[^a-z0-9]+", "_", child.tag.lower()).strip("_") or "field"
            record[key] = (child.text or "").strip()
        if record:
            records.append(record)
    return records


def _fetch_data_feed_records(feed_url: str) -> list[dict]:
    resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    if feed_url.lower().endswith(".json"):
        data = resp.json()
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        return [dict(item) for item in data if isinstance(item, dict)]
    return _parse_xml_listing(resp.text)


def inspect(config: ScrapeConfig) -> dict:
    """Fetch a single page and report detected/overridden selectors plus a sample of rows."""
    html = fetch_html(config.url, config.render, config.wait)
    soup = BeautifulSoup(html, "html.parser")

    row_selector = config.row_selector or detect_row_selector(soup, config.min_repeat)
    rows = soup.select(row_selector) if row_selector else []

    col_selectors = config.col_selectors or (detect_col_selectors(rows[0]) if rows else {})
    next_selector = config.next_selector or detect_next_selector(soup)
    sample = [extract_record(r, col_selectors) for r in rows[:5]]

    # Row detection found nothing usable (either no rows, or rows that carry
    # no email/phone anywhere - a strong signal it locked onto page chrome
    # rather than real listing content). Only worth the extra requests when
    # the caller hasn't already pinned down selectors themselves.
    looks_empty = not rows or not any(r.get("email") or r.get("phone") for r in sample)
    if looks_empty and not config.row_selector and not config.col_selectors:
        for feed_url in _iter_data_feed_urls(html, config.url):
            try:
                feed_records = _fetch_data_feed_records(feed_url)
            except (requests.RequestException, ET.ParseError, ValueError):
                continue
            feed_sample = [canonicalize_record(r) for r in feed_records[:5]]
            if any(r.get("email") or r.get("phone") for r in feed_sample):
                return {
                    "url": config.url,
                    "row_selector": None,
                    "row_count": len(feed_records),
                    "col_selectors": None,
                    "next_selector": None,
                    "data_feed_url": feed_url,
                    "sample": feed_sample,
                }

    return {
        "url": config.url,
        "row_selector": row_selector,
        "row_count": len(rows),
        "col_selectors": col_selectors,
        "next_selector": next_selector,
        "sample": sample,
    }


def scrape(config: ScrapeConfig) -> dict:
    """Paginate from config.url following next_selector, extracting one record per row."""
    if config.row_selector and config.col_selectors:
        row_selector = config.row_selector
        col_selectors = config.col_selectors
        next_selector = config.next_selector
    else:
        preview = inspect(config)
        if preview.get("data_feed_url"):
            records = [canonicalize_record(r) for r in _fetch_data_feed_records(preview["data_feed_url"])]
            return {
                "count": len(records),
                "pages": 1,
                "mode": "data_feed",
                "records": records,
                "row_selector": None,
                "col_selectors": None,
                "next_selector": None,
                "data_feed_url": preview["data_feed_url"],
            }
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
