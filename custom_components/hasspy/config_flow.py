"""Config flow for the hasspy integration.

Single config entry, no user input needed: the integration is a local sink for
a hasspy runtime that connects out to Home Assistant. The flow exists so the
integration can be added and removed from the UI like any other.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant

from .const import DOMAIN, NAME


class HasspyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the hasspy config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single hasspy entry."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})

        return self.async_show_form(step_id="user")


async def async_migrate_entry(hass: HomeAssistant, entry) -> bool:  # noqa: ANN001
    """Placeholder for future schema migrations."""
    return True
