import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    DOMAIN,
    # ACRCloud
    CONF_ACRCLOUD_HOST,
    CONF_HOME_ASSISTANT_UDP_PORT,
    CONF_ACRCLOUD_ACCESS_KEY,
    CONF_ACRCLOUD_ACCESS_SECRET,
    # Spotify
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    CONF_SPOTIFY_PLAYLIST_ID,
    CONF_SPOTIFY_CREATE_PLAYLIST,
    CONF_SPOTIFY_PLAYLIST_NAME,
    DEFAULT_SPOTIFY_PLAYLIST_NAME,
    # Device fields
    CONF_DEVICE_NAME,
    CONF_ASSIST_SATELLITE_ENTITY,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_DISPLAY_DEVICE,
    CONF_USE_DISPLAY_DEVICE,
    # Types / other
    ENTRY_TYPE_MASTER,
    ENTRY_TYPE_DEVICE,
    VIEW_ASSIST_DOMAIN,
    REMOTE_ASSIST_DISPLAY_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def infer_tagging_switch_from_assist_satellite(hass, assist_satellite_entity):
    """Infer tagging switch entity from assist satellite entity ID using device registry."""
    if not assist_satellite_entity.startswith("assist_satellite."):
        return None, "Invalid assist satellite entity format"

    try:
        entity_registry = er.async_get(hass)

        # Find the assist satellite entity in the registry
        assist_entity = entity_registry.async_get(assist_satellite_entity)
        if not assist_entity:
            return None, "Assist satellite entity not found in registry"

        # Get device ID for the assist satellite
        assist_device_id = assist_entity.device_id
        if not assist_device_id:
            return None, "Assist satellite entity has no device association"

        # Search for a switch entity on the same device that looks like a tagging enable switch
        candidates = []
        for entry in entity_registry.entities.values():
            if entry.platform == "switch" and entry.device_id == assist_device_id:
                if "tagging_enable" in entry.entity_id:
                    candidates.append(entry.entity_id)

        # If we found candidates, prefer the one that matches the base name of the satellite
        if candidates:
            try:
                base = assist_satellite_entity.split(".", 1)[1]
                base = base.replace("_assist_satellite", "")
            except Exception:
                base = None

            if base:
                for c in candidates:
                    if base in c:
                        return c, None

            # Otherwise return the first candidate
            return candidates[0], None

        return None, "No tagging switch found on the same device"
    except Exception as e:
        _LOGGER.warning("Error inferring tagging switch: %s", e)
        return None, f"Error inferring tagging switch: {e}"


def get_devices_for_domain(hass: HomeAssistant, domain: str):
    """Return a list of device entries that have entities from the specified domain."""
    try:
        device_registry = dr.async_get(hass)
        matching_devices = []

        # Cross-check device.config_entries against HA's config_entries manager
        for device in device_registry.devices.values():
            for entry_id in device.config_entries:
                ce = hass.config_entries.async_get_entry(entry_id)
                if ce and ce.domain == domain:
                    matching_devices.append(device)
                    break

        return matching_devices
    except Exception as e:
        _LOGGER.error("Error in get_devices_for_domain for domain %s: %s", domain, e)
        return []


def get_display_device_options(hass: HomeAssistant):
    """Get available View Assist / Remote Assist display devices for selection."""
    display_devices: dict[str, str] = {}

    _LOGGER.debug("Starting display device discovery...")

    # View Assist: try domain data
    if VIEW_ASSIST_DOMAIN in hass.data:
        _LOGGER.debug("View Assist domain found in hass.data")
        try:
            view_assist_data = hass.data[VIEW_ASSIST_DOMAIN]
            _LOGGER.debug(
                "View Assist data keys: %s",
                list(view_assist_data.keys()) if isinstance(view_assist_data, dict) else "Not a dict",
            )
            for key in ("va_browser_ids", "browser_ids", "browsers", "devices"):
                if isinstance(view_assist_data, dict) and key in view_assist_data:
                    browsers = view_assist_data[key]
                    _LOGGER.debug("Found browsers list under key '%s': %s", key, browsers)
                    if isinstance(browsers, dict):
                        for browser_id, info in browsers.items():
                            name = info.get("name") or info.get("friendly_name") or browser_id
                            display_devices[browser_id] = f"View Assist: {name}"
                    elif isinstance(browsers, list):
                        for browser_id in browsers:
                            display_devices[browser_id] = f"View Assist: {browser_id}"
                    break
        except Exception as e:
            _LOGGER.debug("Error reading View Assist data: %s", e)

    # View Assist: optional helper
    try:
        view_assist = hass.data.get(VIEW_ASSIST_DOMAIN)
        if view_assist and hasattr(view_assist, "get_registered_browsers"):
            browsers = view_assist.get_registered_browsers()
            _LOGGER.debug("View Assist get_registered_browsers returned: %s", browsers)
            if isinstance(browsers, dict):
                for browser_id, name in browsers.items():
                    display_devices[browser_id] = f"View Assist: {name}"
    except Exception as e:
        _LOGGER.debug("Error calling View Assist helper: %s", e)

    # Remote Assist Display
    if REMOTE_ASSIST_DISPLAY_DOMAIN in hass.data:
        _LOGGER.debug("Remote Assist Display domain found in hass.data")
        try:
            remote_display_data = hass.data[REMOTE_ASSIST_DISPLAY_DOMAIN]
            _LOGGER.debug(
                "Remote Assist Display keys: %s",
                list(remote_display_data.keys()) if isinstance(remote_display_data, dict) else "Not a dict",
            )
            for key in ("devices", "registered_displays", "screens"):
                if isinstance(remote_display_data, dict) and key in remote_display_data:
                    displays = remote_display_data[key]
                    if isinstance(displays, dict):
                        for display_id, info in displays.items():
                            name = info.get("name") or info.get("friendly_name") or display_id
                            display_devices[display_id] = f"Remote Assist Display: {name}"
                    elif isinstance(displays, list):
                        for display_id in displays:
                            display_devices[display_id] = f"Remote Assist Display: {display_id}"
                    break
        except Exception as e:
            _LOGGER.debug("Error getting Remote Assist Display devices: %s", e)

    # Generic fallbacks: look for display-like entities
    try:
        display_entity_patterns = ("display.", "screen.", "monitor.")
        for pattern in display_entity_patterns:
            matching_entities = [
                entity_id for entity_id in hass.states.async_entity_ids() if entity_id.startswith(pattern)
            ]
            if matching_entities:
                _LOGGER.debug(
                    "Found %d entities matching pattern '%s': %s",
                    len(matching_entities),
                    pattern,
                    matching_entities[:3],
                )
                for entity_id in matching_entities[:5]:
                    state = hass.states.get(entity_id)
                    if state:
                        friendly_name = state.attributes.get("friendly_name", entity_id)
                        display_devices[entity_id] = f"Display Entity: {friendly_name}"
    except Exception as e:
        _LOGGER.debug("Error during generic display entity discovery: %s", e)

    if not display_devices:
        display_devices["dummy"] = "No display devices found"

    return display_devices


class MusicCompanionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    # ----- Options Flow hook (View Assist pattern) -----
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return MusicCompanionOptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Entry point: choose Master or Device configuration."""
        errors = {}

        if user_input is not None:
            _LOGGER.debug("User selected config type: %s", user_input)
            choice = user_input.get("config_type")
            if choice == "master":
                return await self.async_step_master()
            if choice == "device":
                return await self.async_step_device()
            errors["base"] = "invalid_selection"

        schema = vol.Schema(
            {
                vol.Required("config_type"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": "master", "label": "Master Configuration"},
                            {"value": "device", "label": "Add Device"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_master(self, user_input=None):
        """Configure or update the Master entry."""
        errors = {}

        if user_input is not None:
            _LOGGER.debug("Master step input: %s", user_input)

            # Keep only one master entry
            existing_master = None
            for entry in self._async_current_entries():
                if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                    existing_master = entry
                    break

            data = {
                **user_input,
                "entry_type": ENTRY_TYPE_MASTER,
            }

            if existing_master:
                _LOGGER.info("Updating existing master configuration: %s", existing_master.entry_id)
                self.hass.config_entries.async_update_entry(existing_master, data=data)
                return self.async_abort(reason="master_updated")

            _LOGGER.info("Creating new master configuration")
            return self.async_create_entry(title="Master Configuration", data=data)

        # Prefill from existing master if present
        existing_data = {}
        for entry in self._async_current_entries():
            if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                existing_data = entry.data
                break

        schema = vol.Schema(
            {
                # ACRCloud
                vol.Optional(CONF_ACRCLOUD_HOST, default=existing_data.get(CONF_ACRCLOUD_HOST, "")): cv.string,
                vol.Optional(
                    CONF_ACRCLOUD_ACCESS_KEY, default=existing_data.get(CONF_ACRCLOUD_ACCESS_KEY, "")
                ): cv.string,
                vol.Optional(
                    CONF_ACRCLOUD_ACCESS_SECRET, default=existing_data.get(CONF_ACRCLOUD_ACCESS_SECRET, "")
                ): cv.string,
                vol.Optional(
                    CONF_HOME_ASSISTANT_UDP_PORT, default=existing_data.get(CONF_HOME_ASSISTANT_UDP_PORT, 10699)
                ): cv.positive_int,
                # Spotify
                vol.Optional(CONF_SPOTIFY_CLIENT_ID, default=existing_data.get(CONF_SPOTIFY_CLIENT_ID, "")): cv.string,
                vol.Optional(
                    CONF_SPOTIFY_CLIENT_SECRET, default=existing_data.get(CONF_SPOTIFY_CLIENT_SECRET, "")
                ): cv.string,
                vol.Optional(CONF_SPOTIFY_PLAYLIST_ID, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_ID, "")): cv.string,
                vol.Optional(
                    CONF_SPOTIFY_CREATE_PLAYLIST, default=existing_data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True)
                ): cv.boolean,
                vol.Optional(
                    CONF_SPOTIFY_PLAYLIST_NAME,
                    default=existing_data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME),
                ): cv.string,
            }
        )
        return self.async_show_form(step_id="master", data_schema=schema, errors=errors)

    async def async_step_device(self, user_input=None):
        """Configure a Device entry (Assist Satellite + Media Player + optional display)."""
        errors = {}

        # Build dynamic lists for selection
        assist_satellites: list[str] = []
        media_players: list[str] = []

        for state in self.hass.states.async_all():
            try:
                if state.entity_id.startswith("assist_satellite."):
                    assist_satellites.append(state.entity_id)
                elif state.entity_id.startswith("media_player."):
                    media_players.append(state.entity_id)
            except Exception:
                continue

        if user_input is not None:
            device_name = user_input.get(CONF_DEVICE_NAME, "")
            assist_satellite = user_input.get(CONF_ASSIST_SATELLITE_ENTITY, "")
            media_player = user_input.get(CONF_MEDIA_PLAYER_ENTITY, "")
            use_display_device = user_input.get(CONF_USE_DISPLAY_DEVICE, False)
            display_device = user_input.get(CONF_DISPLAY_DEVICE)

            if not device_name:
                errors[CONF_DEVICE_NAME] = "required"

            if not errors:
                # Assist satellite validation + tagging switch inference
                if not assist_satellite.startswith("assist_satellite."):
                    errors[CONF_ASSIST_SATELLITE_ENTITY] = "invalid_assist_satellite"
                else:
                    tagging_switch, err = infer_tagging_switch_from_assist_satellite(self.hass, assist_satellite)
                    tagging_enabled = tagging_switch is not None
                    if err and not tagging_enabled:
                        _LOGGER.info(
                            "Device '%s' will be configured without tagging capability: %s", device_name, err
                        )

                # Media player must exist
                if not self.hass.states.get(media_player):
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "media_player_not_found"

                # Display device validation
                if use_display_device and display_device and display_device not in ["none", "dummy"]:
                    available_devices = get_display_device_options(self.hass)
                    if display_device not in available_devices:
                        errors[CONF_DISPLAY_DEVICE] = "display_device_not_found"
                        _LOGGER.warning(
                            "Selected display device not found: %s. Available: %s",
                            display_device,
                            list(available_devices.keys()),
                        )

                if not errors:
                    base_name = (
                        assist_satellite[17:-17] if assist_satellite.endswith("_assist_satellite") else ""
                    )

                    data = {
                        "entry_type": ENTRY_TYPE_DEVICE,
                        CONF_DEVICE_NAME: device_name,
                        CONF_ASSIST_SATELLITE_ENTITY: assist_satellite,
                        CONF_MEDIA_PLAYER_ENTITY: media_player,
                        # Tagging
                        "tagging_enabled": tagging_switch is not None,
                        "tagging_switch_entity": tagging_switch if tagging_switch else None,
                        # Display
                        CONF_USE_DISPLAY_DEVICE: use_display_device,
                        CONF_DISPLAY_DEVICE: display_device if use_display_device else None,
                        "base_name": base_name,
                    }

                    title = f"Device: {device_name}"
                    return self.async_create_entry(title=title, data=data)

        # Sort for nicer UX
        assist_satellites.sort()
        media_players.sort()

        display_devices = get_display_device_options(self.hass)
        display_options = [{"value": key, "label": value} for key, value in display_devices.items()]

        data_schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_NAME): cv.string,
                vol.Required(CONF_ASSIST_SATELLITE_ENTITY): vol.In(assist_satellites),
                vol.Required(CONF_MEDIA_PLAYER_ENTITY): vol.In(media_players),
                vol.Optional(CONF_USE_DISPLAY_DEVICE, default=False): cv.boolean,
                vol.Optional(CONF_DISPLAY_DEVICE): SelectSelector(
                    SelectSelectorConfig(options=display_options, mode=SelectSelectorMode.DROPDOWN)
                ),
            }
        )
        return self.async_show_form(step_id="device", data_schema=data_schema, errors=errors)


class MusicCompanionOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Music Companion (master entry only)."""
    # Note: no __init__; HA provides self.config_entry

    async def async_step_init(self, user_input=None):
        """Show/update the options for the Master entry (Spotify/ACR)."""
        # Only expose options on the MASTER entry
        entry_type = self.config_entry.data.get("entry_type")
        if entry_type != ENTRY_TYPE_MASTER:
            # Nothing to configure for device entries
            return self.async_create_entry(title="", data={})

        data = dict(self.config_entry.data)

        if user_input is not None:
            # Merge user_input into data (this integration reads from data, not options)
            data.update(user_input)
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="Updated", data={})

        schema = vol.Schema(
            {
                # ACRCloud
                vol.Optional(CONF_ACRCLOUD_HOST, default=data.get(CONF_ACRCLOUD_HOST, "")): str,
                vol.Optional(CONF_ACRCLOUD_ACCESS_KEY, default=data.get(CONF_ACRCLOUD_ACCESS_KEY, "")): str,
                vol.Optional(CONF_ACRCLOUD_ACCESS_SECRET, default=data.get(CONF_ACRCLOUD_ACCESS_SECRET, "")): str,
                vol.Optional(CONF_HOME_ASSISTANT_UDP_PORT, default=data.get(CONF_HOME_ASSISTANT_UDP_PORT, 10699)): int,
                # Spotify
                vol.Optional(CONF_SPOTIFY_CLIENT_ID, default=data.get(CONF_SPOTIFY_CLIENT_ID, "")): str,
                vol.Optional(CONF_SPOTIFY_CLIENT_SECRET, default=data.get(CONF_SPOTIFY_CLIENT_SECRET, "")): str,
                vol.Optional(CONF_SPOTIFY_PLAYLIST_ID, default=data.get(CONF_SPOTIFY_PLAYLIST_ID, "")): str,
                vol.Optional(CONF_SPOTIFY_CREATE_PLAYLIST, default=data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True)): bool,
                vol.Optional(
                    CONF_SPOTIFY_PLAYLIST_NAME,
                    default=data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
