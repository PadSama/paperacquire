# Responsible use

paperacquire automates what a researcher otherwise does by hand: open an article page, check
access, download the PDF. Automation changes the scale and the visibility of that activity, so
it comes with obligations. This page explains them and what the tool does to help. It is not
legal advice; when in doubt, ask your library or the licence holder.

## Before you start

- **Entitled access only.** Retrieve only content that your institution licenses to you or
  that is openly licensed. The tool has no way past a paywall and must not be adapted to find
  one.
- **Read the terms that apply to you.** Publisher terms of use and library licence agreements
  often restrict automated or systematic downloading, text and data mining, and the use of
  texts with AI or language-model tools. Some publishers declare a text-and-data-mining
  reservation on their pages; some require a separate licence for AI use even of texts you
  obtained lawfully. Check both the retrieval and what you do with the texts afterwards.
- **Tell your library before you enable the browser channel.** The browser channel is off by
  default (`browser_enabled: false`). Before switching it on, let your library know that you
  will retrieve entitled content at low, rate-limited volumes for a research project, and follow
  their guidance: lower limits, publishers to leave out (`browser_skip_publishers`), or an API
  route instead.
- **Prefer official APIs for text and data mining.** Where a publisher offers a TDM API and your
  institution has an agreement for it, use that route (`ELSEVIER_API_KEY`, `WILEY_TDM_TOKEN`).

## What the tool does to stay polite

- **Conservative defaults:** at most 50 requests per publisher per day across all channels,
  at most 150 browser attempts per day, randomized gaps between requests, a metered OpenAlex
  budget, and stop rules that end a publisher's day on 429 responses, repeated 403s or
  challenge pages. Raising the limits or removing stop rules is your responsibility and may
  breach terms; the defaults are deliberately low.
- **A human in the loop:** you sign in through your institution yourself. The tool never solves
  CAPTCHAs or bot challenges; it waits for them to clear or for you, and otherwise stops.
- **No credential handling:** API keys are read from environment variables only; the browser
  login stays in a dedicated local browser profile. Nothing is shared.
- **Honest identification:** API requests carry the tool's name and version, and your contact
  e-mail where services ask for one (OpenAlex, Unpaywall, Crossref polite pools).
- **No shadow libraries, no credential sharing, no use of proxies or VPNs to get around
  licensing or geography.**

## About the patchright driver

The default browser driver is Playwright, a standard automation library that makes the browser
identify itself as automated. The optional patchright driver (`browser_driver: "patchright"`)
is a fork of Playwright that removes such automation markers. Some websites block automated
browsers, and removing the markers can be read as evading bot detection, which publishers'
terms may prohibit. Use patchright only where your library and the publisher's terms allow
automated access to content you are entitled to, and never to get around a block, a challenge
or a refusal.

## Sharing results

- `pack` produces zips for collaborators only after scanning them for the identity markers you
  configured (`privacy_markers`); a hit deletes the zip.
- Full texts remain subject to their licences. Do not redistribute or publish them, and do not
  upload them to services their licence does not cover.

## If you are asked to stop

If a publisher or your library asks you to stop or slow down, do so. If you believe the tool
behaves in a way that is not polite, please open an issue.
