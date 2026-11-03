"""paper_acquire: a polite, entitlement-respecting full-text acquisition waterfall for
literature research.

For each bibliographic record (DOI and title) it tries, in order: a PDF the project
already holds, open-access copies (OpenAlex content, Unpaywall and other legal open
repositories), publisher APIs the user holds keys for, the user's own entitled browser
session, and working-paper versions. Every step has a retry budget, every publisher a
daily limit, and every refusal class a stop rule. See README.md and RESPONSIBLE_USE.md.

Design principles:
- Entitlement is explicit: an API key and an institutional browser login are different
  entitlements. A refusal on one never disables the other, and their stop keys live in
  separate namespaces.
- A human stays in the loop: the browser channel uses a dedicated persistent profile and
  one manual institutional login per run (can be disabled for unattended runs). No
  CAPTCHA or bot challenge is ever solved by the program.
- Politeness is structural: per-publisher daily limits, randomized gaps between
  requests, stop rules for refusals and challenge pages, and a network-down probe that
  pauses instead of recording failures.
- Outbound privacy gate: every zip the tool produces is scanned for the operator's
  configured identity markers; a hit deletes the zip and fails closed.
"""

__version__ = "0.6.0"
