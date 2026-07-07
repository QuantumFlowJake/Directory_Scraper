# Directory Scraper

A generic directory-style listing scraper. Point it at a starting URL and it
auto-detects the repeating "row" block, per-row column selectors (name,
phone, email, address, website, ...), and the next-page link, then paginates
and extracts records. Any detected selector can be overridden explicitly
once `/inspect` shows you what it found.

Two ways to use it:

- **CLI** (`scrape_directory.py`) — one-off scrapes from the command line.
- **Service** (`service.py`) — a FastAPI wrapper with saved profiles, sync
  and async runs, and job polling, backed by SQLite so state survives
  container restarts.

## Setup

### Docker (recommended)

```bash
docker compose up --build -d
```

This builds the image and starts the service on `http://localhost:8000`,
persisting profiles/jobs to a named volume (`scraper-data`) mounted at
`/data`.

**Quickest path to a CSV:** open **http://localhost:8000/ui** in a browser,
paste in the directory's URL, and click Scrape. It auto-detects everything
and downloads a `contacts.csv` — no curl, no profiles, no selectors.

To enable Playwright (for JS-rendered sites, via `render: true`), rebuild
with:

```bash
docker compose build --build-arg WITH_PLAYWRIGHT=true
```

This pulls Chromium + system deps (~400MB), so it's off by default.

### Local Python

```bash
pip install -r requirements.txt
python -m uvicorn service:app --reload
```

Requires Python 3.12+. SQLite path defaults to `/data/scraper.db`; override
with the `SCRAPER_DB` environment variable if `/data` isn't writable on your
machine, e.g. `SCRAPER_DB=./scraper.db`.

## CLI usage

```bash
# Preview detected selectors + a sample of rows without scraping
python scrape_directory.py https://example.com/directory --inspect

# Scrape, following pagination, print JSON
python scrape_directory.py https://example.com/directory

# Scrape to CSV, capping pages and slowing down requests
python scrape_directory.py https://example.com/directory --format csv --max-pages 10 --delay 2

# Override selectors once /inspect shows what it auto-detected wrong
python scrape_directory.py https://example.com/directory \
  --row-selector "div.listing-item" --next-selector "a.pagination-next"

# JS-rendered sites (requires the image built with WITH_PLAYWRIGHT=true)
python scrape_directory.py https://example.com/directory --render --wait ".listing-item"
```

Run `python scrape_directory.py --help` for the full flag list.

## API usage

All endpoints accept/return JSON unless noted.

| Method | Path                | Description                                    |
|--------|---------------------|-------------------------------------------------|
| GET    | `/`                 | Health check                                   |
| GET    | `/ui`                | Single-page form: paste a URL, download a CSV  |
| POST   | `/scrape`            | One-shot: url in, auto-detected CSV out         |
| POST   | `/inspect`           | Fetch a URL, return detected selectors + sample |
| GET    | `/profiles`          | List saved profiles                            |
| GET    | `/profiles/{name}`   | Get one saved profile                          |
| POST   | `/profiles/{name}`   | Save/update a profile                          |
| DELETE | `/profiles/{name}`   | Delete a profile                               |
| POST   | `/run/{name}`        | Run a saved profile, sync or async             |
| GET    | `/jobs/{job_id}`     | Poll an async job                              |
| GET    | `/jobs`              | List recent jobs                               |

### Example: one-shot scrape (no profile needed)

```bash
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/directory"}' \
  -o contacts.csv
```

Auto-detects rows/columns/pagination exactly like `/inspect`, follows all
pages up to `max_pages` (default 25), and always returns a CSV file. This is
what `/ui` calls under the hood. Good for a first try on any given
directory; if the results look wrong (see the `/inspect` caveats below),
switch to a saved profile with explicit overrides instead.

### Example: inspect a site

```bash
curl -X POST http://localhost:8000/inspect \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/directory"}'
```

Returns the auto-detected `row_selector`, `col_selectors`, `next_selector`,
row count, and a 5-row sample so you can check the guess before committing
to a profile. Auto-detection is a heuristic (most-repeated classed tag wins)
— on pages with lots of small repeated inline elements (nav items, tags,
etc.) it can pick the wrong container. If the sample looks wrong, override
`row_selector`/`col_selectors`/`next_selector` explicitly in the next call.

### Example: save and run a profile

```bash
curl -X POST http://localhost:8000/profiles/my-directory \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/directory",
    "row_selector": "div.listing-item",
    "col_selectors": {"name": "h3.title", "phone": "span.phone"},
    "next_selector": "a.pagination-next",
    "max_pages": 10
  }'

curl -X POST http://localhost:8000/run/my-directory -H "Content-Type: application/json" -d '{}'
```

Pass `{"async_job": true}` to run in the background and poll `/jobs/{job_id}`
instead of waiting on the response. Pass `{"format": "csv"}` to get a CSV
response instead of JSON.

## Config fields

| Field           | Default  | Notes                                                        |
|-----------------|----------|---------------------------------------------------------------|
| `url`           | required | Starting page                                                 |
| `format`        | `json`   | `json` or `csv`                                                |
| `max_pages`     | `25`     | Pagination cap                                                 |
| `delay`         | `1.0`    | Seconds between page fetches                                   |
| `min_repeat`    | `5`      | Min repeat count for a tag signature to be considered a row   |
| `render`        | `false`  | Use Playwright instead of plain `requests` (needs the image built with Playwright) |
| `wait`          | `null`   | CSS selector to wait for when `render: true`                   |
| `row_selector`  | `null`   | Override auto-detected row container                            |
| `col_selectors` | `null`   | Override auto-detected `{field_name: selector}` map              |
| `next_selector` | `null`   | Override auto-detected next-page link                           |

## Output columns

Every record is normalized to the same leading column order, regardless of
how the source site is structured:

```
first_name, middle_name, last_name, title, email, phone, full_name, ...everything else detected on the row
```

- **Name splitting** handles both "Last, First Middle" (comma-separated,
  common in institutional directories) and plain "First Middle Last" order,
  and keeps multi-word surname particles together (e.g. "de la Cruz", "van
  der Berg") instead of splitting them at the last space. It works whether
  the site exposes a single full-name field or separate given/family-name
  fields.
- **`email`/`phone` are scrubbed**: sites often glue a label and type onto
  the actual value with no separator (e.g. `Work Email:name@example.eduINTERNET`,
  `Work Phone:555-123-4567ext.9work`). These columns always contain just the
  address/number itself, extracted from wherever on the row it actually
  appears — not the raw, label-prefixed text.
- **`title`** is the person's job title/position, resolved from an
  explicitly-hinted field when one exists, falling back to content matching
  (e.g. "Faculty", "Director", "Coordinator", ...) for sites where the
  detected `title` field actually captured the person's name instead.
- Any additional fields the auto-detection found on the row (department,
  address, a secondary phone type label, etc.) follow after `full_name`,
  unchanged.

## Notes

- Async jobs run via FastAPI `BackgroundTasks` in-process — fine at
  `--workers 1` (the Dockerfile's default), but a job in flight when the
  worker restarts is lost. Move to RQ/Redis before scaling workers.
- Profile and job state live in SQLite (`SCRAPER_DB`), so they survive
  container restarts as long as the `/data` volume persists.
- Respect the target site's `robots.txt` and terms of service before
  scraping.
