# paperacquire

A polite, entitlement-respecting tool for acquiring the full texts of scholarly articles for
literature research (systematic reviews, bibliometric and content-analysis studies). For each
record (a DOI and a title) it tries legal open-access copies first, then the channels you are
already entitled to (publisher APIs you hold keys for, your own institutional login in a real
browser), then working-paper versions. Daily limits, randomized gaps and stop rules are built
in, and every PDF that comes from the network is checked against the record's title before it
is accepted.

> **Use it only for content you are entitled to, within your publishers' and your library's
> terms, at the conservative default rates.** It never bypasses a paywall, never solves a
> CAPTCHA, never shares credentials and never uses shadow libraries. Read
> [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) before enabling the browser channel.

## What it does

| Step | Source | Notes |
|---|---|---|
| `inhand` | PDFs you already have (`inputs/inhand/<uid>.pdf`) | no network |
| `oa_content` | open-access PDF locations from OpenAlex and Unpaywall metadata | any version; title-checked |
| `oa_vor` | the published (version-of-record) open-access locations | retried separately |
| `publisher` | Elsevier and Wiley TDM APIs, publisher direct links | needs your API keys; stops after repeated 403s |
| `browser` | your institutional login in Chrome or Edge | off by default; human login, no challenge solving |
| `wp` | working-paper and other copies, OpenAlex cached content, title matches | OpenAlex content is paid and metered; version recorded |

Each step has a retry budget; a step that is stopped for the day defers to the next day
instead of failing the record. A record is "not found" only when every step is exhausted.

## What it does not do

- It does not get past paywalls: without your entitlement there is no PDF.
- It does not solve CAPTCHAs or bot challenges; it waits for them to clear or for you to act,
  and stops the publisher for the day if they persist.
- It does not run at scale: the defaults allow at most 50 requests per publisher per day.
- It does not store or send credentials; API keys stay in environment variables.

## Install

Python 3.10 or newer, and Google Chrome (or Microsoft Edge) for the browser channel.

```bash
pip install "paper-acquire[browser] @ git+https://github.com/PadSama/paperacquire"
```

The `browser` extra installs Playwright; the browser channel uses your installed Chrome, so no
browser download is needed. `paper-acquire[patchright]` installs the optional patchright
driver (see [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md)).

## Quick start

1. Create a project folder with `inputs/records.csv` (columns `uid,DOI,Title`) or
   `inputs/records.jsonl` (one object per line with the same keys).
2. Initialise it; this writes `config.json` from the example on the first run:
   `python -m paper_acquire --project myproject init`
3. Edit `myproject/config.json`: set `contact_email` and `privacy_markers` (see below).
4. Optional API keys, as environment variables: `OPENALEX_API_KEY`, `ELSEVIER_API_KEY`,
   `ELSEVIER_INSTTOKEN` (only when your institution issues one), `WILEY_TDM_TOKEN`.
5. Run one polite pass a day: `python -m paper_acquire --project myproject fetch-day`
   (add `--no-browser` to skip the browser channel).
6. See where things stand: `python -m paper_acquire --project myproject status`
7. Pack new full texts for collaborators, through the privacy gate:
   `python -m paper_acquire --project myproject pack`

Acquired PDFs land in `fulltext/`, every network attempt in `logs/attempts.jsonl`, per-record
state in `work/status.json`, and each day's counts and stops in `work/day/<date>.json`.

## Configuration

`paper_acquire/config.example.json` documents every key. The important ones:

| Key | Default | Meaning |
|---|---|---|
| `contact_email` | empty | polite-pool contact sent to OpenAlex, Unpaywall and Crossref |
| `privacy_markers` | empty | strings the outbound privacy gate scans every zip for (your name, e-mail, institution) |
| `browser_enabled` | `false` | the browser channel; enable only as described in RESPONSIBLE_USE.md |
| `browser_driver` | `playwright` | `playwright` or `patchright` |
| `browser_channel` | `chrome` | `chrome` or `msedge` |
| `browser_login_gate` | `true` | wait for your institutional login before the first browser attempt |
| `browser_skip_publishers` | empty | publishers whose content should not be retrieved automatically |
| `publisher_entitlement` | empty | publishers you have no subscription to (skipped honestly) |
| `limits.per_publisher_daily` | 50 | requests per publisher per day, all channels together |
| `limits.browser_daily_total` | 150 | browser attempts per day |
| `limits.download_gap_s` | 15-30 | randomized seconds between downloads |
| `limits.browser_gap_s` | 25-40 | randomized seconds between browser attempts |
| `limits.api_gap_s` | 1.5 | seconds between metadata API calls |
| `limits.openalex_daily_usd` | 0.95 | OpenAlex spend per day, metered before each paid call |
| `limits.consecutive_403_stop` | 5 | a publisher API stops for the day after this many 403s in a row |

Further stop rules: a metadata 429 waits 60-90 s and retries once, and a second 429 stops that
host for the day; a 429, a challenge page or a non-API 403 from a publisher stops that
publisher for the day; two unresolved challenge pages in a row stop the browser channel for
that publisher for the day.

## The browser channel

- A dedicated browser profile under `work/browser_profile`; your everyday browser profile is
  never touched.
- The login gate opens the browser and waits until you have signed in through your
  institution. Nothing about the login is stored outside that profile.
- PDFs download instead of opening the built-in viewer. A start-up self-test on 127.0.0.1
  prints `pdf self-test: PDFs download (verified)`; Edge ignores the setting when it is the
  system PDF reader, which the self-test reports.
- Each browser attempt appends event classes and timings (no URLs, no titles) to
  `work/day/<date>.browser_trace.json`, for diagnosis (config `browser_trace`).

## Repairing retry budgets

If a defect in the tool (not the paper) used up a step's tries, `reopen` gives that step a fresh
budget on chosen records. It is a dry run unless `--apply`, keeps a backup of the status file,
and refuses to change any other record:

`python -m paper_acquire --project myproject reopen --status work/status.json --step browser --uids A1,A2`

## Verification

- `python -m pytest` runs the offline suite: stubbed HTTP and browser, no network.
- `tests/test_browser_replica.py` drives the real browser channel in a real Chrome against a
  replica of publisher behaviour that answers every request inside the browser (no site is
  contacted). It is opt-in: set `PA_BROWSER_CHANNEL=chrome` (or `PA_BROWSER_EXECUTABLE`) and,
  optionally, `PA_BROWSER_DRIVER=patchright`.
- GitHub Actions runs both on Windows and Linux for every push and pull request.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture and the reasoning behind it.

## Limitations

Publisher pages change; the replica tests show that the tool's behaviour did not regress, not
that a publisher still serves its pages the same way. Entitlement depends on your institution
and network. Beyond the generic open-access steps, publisher-specific handling exists for
ScienceDirect, Wiley, SAGE, Taylor & Francis, Springer and Emerald.

## Citation

If you use paperacquire in research, please cite it. [CITATION.cff](CITATION.cff) holds the
citation metadata; GitHub's "Cite this repository" button formats it as APA or BibTeX.

## Third-party software

None of these is included in this repository; pip installs them as dependencies.

| Package | Used for | License |
|---|---|---|
| requests | HTTP for APIs and open-access downloads | Apache-2.0 |
| PyMuPDF | reading PDFs to check them against the record's title | GNU AGPL-3.0 (or a commercial licence from Artifex) |
| Playwright (extra `browser`) | the browser channel's default driver | Apache-2.0 |
| patchright (extra `patchright`) | the optional browser driver | Apache-2.0 |

## License

The code is licensed under the Apache License 2.0 ([LICENSE](LICENSE)). The documentation (the
Markdown files, including `docs/`) is licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
