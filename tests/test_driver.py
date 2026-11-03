"""Browser driver selection (config browser_driver): Playwright by default, patchright only
when asked for, anything else refused. Offline: fake driver modules stand in for both."""
import sys
import types

import pytest

from paper_acquire.browser_channel import BROWSER_DRIVERS, load_driver


@pytest.fixture
def fake_drivers(monkeypatch):
    for name in BROWSER_DRIVERS:
        pkg, api = types.ModuleType(name), types.ModuleType(f"{name}.sync_api")
        api.sync_playwright = f"sync_playwright from {name}"
        pkg.sync_api = api
        monkeypatch.setitem(sys.modules, name, pkg)
        monkeypatch.setitem(sys.modules, f"{name}.sync_api", api)


@pytest.mark.parametrize("configured", [None, "", "playwright", " Playwright "])
def test_default_driver_is_playwright(fake_drivers, configured):
    assert load_driver(configured) == ("playwright", "sync_playwright from playwright")


def test_patchright_only_when_configured(fake_drivers):
    assert load_driver("patchright") == ("patchright", "sync_playwright from patchright")


def test_unknown_driver_is_refused(fake_drivers):
    with pytest.raises(ValueError, match="browser_driver"):
        load_driver("selenium")
