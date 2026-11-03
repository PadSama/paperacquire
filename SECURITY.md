# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub: open the repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue for them.

Useful reports include: API keys, tokens or contact details reaching a log, a trace or a zip;
a way past the outbound privacy gate; a path traversal in the packer or the repair command;
or any behaviour that would contact a site more often than the configured limits allow.

This is research software maintained on a best-effort basis; there is no bug bounty. You will
get an acknowledgement, and a fix or an explanation, as soon as practical.

## Scope reminders

- Keys belong in environment variables. If you find one in a file of this repository or in
  its history, report it as above.
- Tests must never contact real publisher sites; the browser tests run against a local replica
  behind a dead proxy.
