# Changelog

User-visible changes, newest first. Versions follow `paper_acquire.__version__`.

## 0.6.0 · 2026-11-03 · first public release

- Browser driver option `browser_driver`: Playwright by default; patchright only on request.
- Chrome is the default browser channel (Edge ignores the download-PDFs setting when it is the
  system PDF reader; the start-up self-test reports that).
- The browser channel is off in the example config until the operator enables it (see
  RESPONSIBLE_USE.md).
- Key-like values (`api_key`, `apikey`, `token`, `mailto`) are redacted in error texts written
  to the attempts log, as they already were in URLs.
- Installable with pip (`pyproject.toml`; extras `browser`, `patchright`, `test`); the example
  config ships inside the package so `init` works from an installed copy.
- PyMuPDF is imported by its current module name, with a fallback for older releases.
- The default per-record state file is `work/status.json`.
- The User-Agent names the tool and its version, plus the contact e-mail only when configured.
- Citation metadata (`CITATION.cff`) and Dependabot configuration for dependency updates.

## Earlier versions (developed before the public release)

- **0.5.3:** download copies are removed from the downloads folder once a PDF is saved.
- **0.5.2:** the download stall guard enters `save_as` only for a finished download and cancels
  one without progress; the landing is judged only after a redirect hop has committed (both
  found by Windows test runs).
- **0.5.1:** ScienceDirect's page data names the PDF link when the button has not rendered;
  block pages count as blocks, never as per-paper verdicts; context-level download capture; no
  non-browser re-fetch while the browser downloads; a start-up PDF self-test; per-attempt
  traces of event classes and timings; API 403s record the publisher's status header.
- **0.5.0:** PDFs download instead of opening the built-in viewer; downloads are caught in every
  tab; nothing re-navigates before the catcher is drained; the landing is judged after the
  linkinghub redirect; a page without a control is reloaded before any PDF navigation; the
  `reopen` repair command.
- **0.4.9:** one daily count per publisher family across channels; an OpenAlex spend meter; the
  metadata 429 rule (wait, retry once, then stop the host for the day); metadata cached only on
  a definitive answer.
- **0.4.x:** the retry-budget state machine with fall-through defers; separate stop keys for the
  browser, API and direct channels; the login gate and storage-state re-injection; adaptive
  challenge waits; entitlement-signal discipline on landing pages; abandoned sources; a
  transport-failure fallback to the browser identity; network-dropout pause and probe.
