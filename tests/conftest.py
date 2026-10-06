"""Test fixtures for the hasspy integration.

Two layers:

* `pure` - the Home-Assistant-free core (`const`, `lifecycle`), loaded as a
  synthetic package so the debug state machine can be tested without HA.
* `setup_integration` - a real HA config entry, for the service/entity tests
  (only available when `pytest-homeassistant-custom-component` is installed).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "hasspy"
sys.path.insert(0, str(REPO_ROOT))

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:
    _HA_TEST = False
else:
    _HA_TEST = True
    pytest_plugins = ["pytest_homeassistant_custom_component"]


def _load_pure(name: str):
    spec = importlib.util.spec_from_file_location(
        f"hasspy_pure.{name}", INTEGRATION_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"hasspy_pure.{name}"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def pure():
    """A namespace with the HA-free modules: `.const`, `.lifecycle`."""
    package = types.ModuleType("hasspy_pure")
    package.__path__ = [str(INTEGRATION_DIR)]  # type: ignore[attr-defined]
    sys.modules["hasspy_pure"] = package
    return types.SimpleNamespace(
        const=_load_pure("const"),
        lifecycle=_load_pure("lifecycle"),
    )


if _HA_TEST:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        yield

    @pytest.fixture
    async def setup_integration(hass):
        """Set up the hasspy config entry and return (entry, runtime)."""
        from custom_components.hasspy.const import DOMAIN
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry, hass.data[DOMAIN][entry.entry_id]
