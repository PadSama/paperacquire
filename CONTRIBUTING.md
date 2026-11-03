# Contributing

Thank you for helping. A few rules keep the tool polite and the repository clean.

## Ground rules

- **No paywall circumvention, no challenge solving, no credential handling.** Changes that
  would get past an access control, solve a CAPTCHA, or share or store credentials are out of
  scope. See [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md).
- **Defaults stay conservative.** A change to a default limit or stop rule needs a reason in
  the pull request and matching updates to `tests/test_default_limits.py`,
  `paper_acquire/config.example.json` and the README.
- **Tests never contact real sites.** Use the stubs in `tests/stubs.py` or the browser replica
  in `tests/browser_replica.py`.
- **No real data in the repository.** Use synthetic DOIs, PIIs and titles in tests and
  examples (for example `10.1016/j.example.2021.100001`); never commit full texts, abstracts,
  bibliographic exports, logs or configurations from a real project.
- **One line in `CHANGELOG.md` per user-visible change.**

## Development

```bash
python -m pip install -e ".[test,browser]"
python -m pytest
```

The real-browser suite is opt-in; it needs Chrome:

```bash
PA_BROWSER_CHANNEL=chrome python -m pytest tests/test_browser_replica.py
```

On Linux without a display, use `PA_BROWSER_EXECUTABLE=/path/to/chrome` and `xvfb-run -a`.
`PA_BROWSER_DRIVER=patchright` runs it with the patchright driver.

Continuous integration runs both suites on Windows and Linux for every pull request.
