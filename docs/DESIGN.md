# Design

This document explains how paperacquire works and why. It describes the tool, not any project
it was used in.

## Goal and constraints

Acquire the full texts of a fixed list of bibliographic records for research, under five
constraints:

1. No paywall circumvention: a PDF is obtained only through open access or the user's own
   entitlement.
2. Polite volumes: daily limits per publisher and per channel, randomized gaps, stop rules.
3. A human in the loop for logins and challenges.
4. Auditable decisions: every network attempt and every verdict is recorded.
5. No lost work: transient failures (network dropouts, daily stops, publisher hiccups) must
   never be recorded as "not found".

## Waterfall and state machine

Each record goes through steps in a fixed order (`inhand`, `oa_content`, `oa_vor`, `publisher`,
`browser`, `wp`; a project adapter may inject its own steps and names). A step returns one of
a small set of results:

- `ok`: a PDF was obtained and accepted (it starts with `%PDF-`, is readable, and its first
  pages match the record's title above a threshold);
- `fail`: a real attempt failed; it spends one try of the step's retry budget;
- `defer`: nothing was attempted because of a daily stop, a daily limit or a backoff; it spends
  nothing and the waterfall moves on to later steps;
- `exhaust` / `na`: the step cannot succeed for this record (for example, no entitlement on
  that channel).

A record is `acquired` when a step succeeds, `not_found` only when every step is exhausted or
not applicable, and `pending` otherwise. Retry budgets mean a step is never closed for good
by one bad day.

## Limits, namespaces and stop rules

- Every request is counted under a key: the publisher for the browser (`elsevier`), and
  channel namespaces for APIs and direct script downloads (`elsevier_api`, `elsevier_direct`).
- The daily per-publisher limit counts the whole family together, so the limit holds across
  channels. Stop keys stay per channel: an API refusal (a subscription question for the API
  key) never stops the browser channel, whose entitlement is the user's login.
- Stop rules end a publisher's day on a 429, repeated 403s from its API, a challenge page on a
  script download, or two unresolved challenges in the browser.
- Metadata calls are serial and spaced; a 429 waits and retries once, a second 429 stops that
  host for the day. Metadata is cached only on a definitive answer, so a refused lookup is
  retried another day instead of leaving a record without candidates.
- Paid OpenAlex calls are charged against a daily budget before they are sent.

## The browser channel

### Entitlement signals

A visible View PDF or Download PDF control on the landing page is the entitlement signal, and
clicking it is the first acquisition path. Citation meta tags are not a signal: publishers
serve them for every article, entitled or not. Definitive no-access text on the landing
("You do not currently have access to this content") is a per-paper verdict, but only when no
entitlement control is visible, and only after an access-state poll, because some pages render
that note late.

### Getting the PDF reliably

Chromium's built-in PDF viewer hides the file from automation: a navigation returns the
viewer's shell and a click opens the viewer without a download event. The dedicated profile
therefore sets `plugins.always_open_pdf_externally`, so PDFs download. A start-up self-test
serves a small PDF from 127.0.0.1 and checks that it arrives as a download; Edge acting as the
system PDF reader ignores the setting, which the self-test reports, and the channel then falls
back to capturing the viewer's stream.

Downloads are caught at context level in any tab, because a View PDF button may open a new
tab. Nothing navigates a tab again before pending downloads are drained: a second navigation
would destroy a PDF that arrives after a JavaScript relay.

For ScienceDirect, the landing is judged only after Elsevier's linkinghub redirect has
committed; the article page's own data (`article.pdfDownload.urlMetadata`) names the PDF link
when the button has not rendered yet; a page without a control is reloaded once, because the
control renders per page load. A block page ("There was a problem providing the content you
requested") is treated as a block (a strike toward the publisher's daily stop), never as "no
access" for the paper.

The request API (a non-browser HTTP client with a different network fingerprint) is not used
as a second path to a publisher while the browser itself is fetching.

### Bounded waits

Playwright's `save_as` blocks until a download finishes. A stalled server can therefore hold an
attempt for many minutes (browsers also resume interrupted downloads several times). The stall
guard enters `save_as` only for a finished download, recognised by Playwright's finished-file
name in the downloads folder, and cancels a download that shows no progress for 20 s or runs
longer than 180 s. Two facts from Windows test runs shaped it: the download event arrives
before Chrome creates the partial file, and a Windows directory listing can report a stale size
for a file that is still growing, so sizes are read with `os.stat`. Saved copies are removed
from the downloads folder.

### Traces

Each browser attempt leaves a trace of event classes and timings (landing settled, control
seen, reload, click, new tab, download, stream, navigation status class, a rendering
heartbeat, a TLS issuer class). Traces contain no URLs, no titles, and no hosts outside a fixed
list of publisher names, so they can be shared for diagnosis.

## Privacy

- API keys come from environment variables only. The attempts log redacts `api_key`, `apikey`,
  `token` and `mailto` values, in URLs and in error texts (which can quote a URL).
- The outbound packer scans every zip for the operator's configured identity markers and
  deletes the zip on a hit.
- The User-Agent names the tool and its version, plus the contact e-mail only when configured;
  it carries no repository or account URL.

## Verification

- **Offline suite:** stubbed HTTP sessions and browsers, the engine's built-in selftest, and
  tests that pin the default limits and stop rules.
- **Replica suite:** the real browser channel runs in a real Chrome against a replica of
  publisher behaviour that answers every request inside the browser. The replica models the
  failure modes seen in real use: a control that renders late or only after a reload, an
  institution activation click, a JavaScript relay that delivers the PDF late, a View PDF link
  that never renders (with and without page data), a relay that answers clicks only, a block
  page, a stalled download, a slow but moving download, and a no-access landing. Every hop is
  an in-page navigation (a redirect fulfilled by the test would let the follow-up request leak
  to the real site), the browser's proxy is a dead local port, and the suite asserts that no
  request escaped.
- **Continuous integration** runs both suites on Windows and Linux, with both drivers on
  Windows. Two defects in earlier versions showed only on Windows, which is why it runs there.
- **What tests cannot show:** whether a publisher still serves its pages the same way, and
  whether a user's institution is entitled to a given article. Traces from real runs answer the
  first; only the user's own access answers the second.

## Decisions and alternatives considered

- **A real browser with a human login**, rather than headless scraping or shared credentials:
  the user's own entitlement is the only legitimate path to subscription content.
- **No remote or cloud browsers, and no language-model browser agents in the runtime path:**
  they would move credentials off the user's machine, expose the run to prompt injection from
  web pages, or try to solve challenges.
- **Playwright by default, patchright opt-in:** standard automation is the honest default;
  patchright is available where automated access is permitted and automation markers alone
  cause trouble (see RESPONSIBLE_USE.md).
- **Conservative limits by default:** the tool serves projects of hundreds or a few thousand
  records over days or weeks, not bulk harvesting.
