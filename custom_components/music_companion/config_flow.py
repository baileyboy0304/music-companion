import voluptuous as vol
import logging
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

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
    # Types / domains
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
        device_registry = dr.async_get(hass)

        # Find the assist satellite entity in the registry
        assist_entity = entity_registry.async_get(assist_satellite_entity)
        if not assist_entity or not assist_entity.device_id:
            return None, f"Assist satellite entity '{assist_satellite_entity}' not found or has no device"

        # Get the device
        device = device_registry.async_get(assist_entity.device_id)
        if not device:
            return None, "Device not found for assist satellite"

        # Find all entities belonging to this device
        device_entities = er.async_entries_for_device(entity_registry, assist_entity.device_id)

        # Look for a switch entity with "tagging_enable" in the name
        for entity in device_entities:
            if (
                entity.domain == "switch"
                and "tagging_enable" in entity.entity_id
                and entity.disabled_by is None
            ):
                # Verify the switch actually exists in the state registry
                if hass.states.get(entity.entity_id):
                    return entity.entity_id, None

        # If we get here, no tagging switch was found on this device
        switch_entities = [e.entity_id for e in device_entities if e.domain == "switch"]
        return None, f"No tagging switch found on device. Available switches: {switch_entities}"

    except Exception as e:
        return None, f"Error looking up device entities: {str(e)}"


def get_devices_for_domain(hass: HomeAssistant, domain: str):
    """Get devices for a specific domain."""
    try:
        device_registry = dr.async_get(hass)
        config_entries_mgr = hass.config_entries

        matching_devices = []

        for device in device_registry.devices.values():
            # device.config_entries contains config entry IDs (strings)
            # We need to look up the actual config entries to check their domains
            for entry_id in device.config_entries:
                config_entry = config_entries_mgr.async_get_entry(entry_id)
                if config_entry and config_entry.domain == domain:
                    matching_devices.append(device)
                    break  # Found a match, no need to check other entries for this device

        return matching_devices
    except Exception as e:
        _LOGGER.error("Error in get_devices_for_domain for domain %s: %s", domain, e)
        return []


def get_display_device_options(hass: HomeAssistant):
    """Get available View Assist display devices for selection."""
    display_devices = {}

    _LOGGER.debug("Starting display device discovery...")

    # Check if View Assist is loaded
    if VIEW_ASSIST_DOMAIN in hass.data:
        _LOGGER.debug("View Assist domain found in hass.data")
        try:
            # Check View Assist domain data for browser IDs - this is the main source
            view_assist_data = hass.data[VIEW_ASSIST_DOMAIN]
            _LOGGER.debug(
                "View Assist data keys: %s",
                list(view_assist_data.keys()) if isinstance(view_assist_data, dict) else "Not a dict",
            )

            # Try different possible keys for browser IDs
            possible_keys = ["va_browser_ids", "browser_ids", "browsers", "devices"]
            va_browser_ids = {}

            for key in possible_keys:
                if isinstance(view_assist_data, dict) and key in view_assist_data:
                    va_browser_ids = view_assist_data[key]
                    _LOGGER.debug("Found browser IDs under key '%s': %s", key, va_browser_ids)
                    break

            if va_browser_ids:
                try:
                    keys_list = list(va_browser_ids.keys())
                except Exception:
                    keys_list = []
                _LOGGER.debug("View Assist browser IDs found: %s", keys_list)
                if isinstance(va_browser_ids, dict):
                    for device_id, device_name in va_browser_ids.items():
                        display_devices[str(device_id)] = f"View Assist: {device_name}"
                elif isinstance(va_browser_ids, list):
                    for device_id in va_browser_ids:
                        display_devices[str(device_id)] = f"View Assist: {device_id}"
            else:
                _LOGGER.debug("No browser IDs found in View Assist data")

        except Exception as e:
            _LOGGER.debug("Error getting View Assist browser IDs: %s", e)
    else:
        _LOGGER.debug(
            "View Assist domain not found in hass.data. Available domains: %s",
            [key for key in hass.data.keys() if not key.startswith("_")],
        )

    # Check for View Assist entities in the entity registry
    try:
        entity_registry = er.async_get(hass)
        view_assist_entities = [
            entity for entity in entity_registry.entities.values() if entity.platform == VIEW_ASSIST_DOMAIN
        ]

        _LOGGER.debug("Found %d View Assist entities", len(view_assist_entities))

        if view_assist_entities:
            device_registry = dr.async_get(hass)
            for entity in view_assist_entities:
                if entity.device_id:
                    device = device_registry.async_get(entity.device_id)
                    if device and device.id not in display_devices:
                        device_name = device.name or f"View Assist Device {entity.device_id[:8]}"
                        display_devices[device.id] = f"View Assist: {device_name}"
                        _LOGGER.debug(
                            "Added View Assist device from entity registry: %s -> %s",
                            device.id,
                            device_name,
                        )

    except Exception as e:
        _LOGGER.debug("Error checking View Assist entities: %s", e)

    # Add Remote Assist Display devices from device registry
    try:
        remote_display_devices = get_devices_for_domain(hass, REMOTE_ASSIST_DISPLAY_DOMAIN)
        _LOGGER.debug("Found %d Remote Assist Display devices", len(remote_display_devices))
        for device in remote_display_devices:
            if device.id not in display_devices:
                device_name = device.name or f"Remote Display {device.id[:8]}"
                display_devices[device.id] = f"Remote Display: {device_name}"
                _LOGGER.debug("Added Remote Assist Display device: %s -> %s", device.id, device_name)
    except Exception as e:
        _LOGGER.debug("Error getting Remote Assist Display devices: %s", e)

    # Check for any other display-related integrations
    try:
        # Look for entities that might be displays
        display_entity_patterns = [
            "display.",
            "screen.",
            "monitor.",
        ]

        for pattern in display_entity_patterns:
            matching_entities = [entity_id for entity_id in hass.states.async_entity_ids() if entity_id.startswith(pattern)]

            if matching_entities:
                _LOGGER.debug(
                    "Found %d entities matching pattern '%s': %s",
                    len(matching_entities),
                    pattern,
                    matching_entities[:3],
                )

                # Add these as potential display devices
                for entity_id in matching_entities[:5]:  # Limit to first 5
                    state = hass.states.get(entity_id)
                    if state:
                        friendly_name = state.attributes.get("friendly_name", entity_id)
                        display_devices[entity_id] = f"Display Entity: {friendly_name}"

    except Exception as e:
        _LOGGER.debug("Error checking for display entities: %s", e)

    # Always add none option first
    ordered_devices = {"none": "None (use text entities only)"}

    # Add found devices
    ordered_devices.update(display_devices)

    # Add dummy if no real devices found
    if len(ordered_devices) == 1:  # Only "none" option
        ordered_devices["dummy"] = "dummy (no display devices found)"

    _LOGGER.debug("Final available display devices: %s", list(ordered_devices.keys()))
    return ordered_devices


class MusicCompanionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = "local_push"

    def __init__(self):
        """Initialize the config flow."""
        self._master_config_exists = False

    # ----- Options Flow hook (adds the cog) -----
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return MusicCompanionOptionsFlowHandler()

    def _check_master_config(self):
        """Check if master configuration already exists."""
        if not self.hass:
            return

        self._master_config_exists = False
        for entry in self._async_current_entries():
            if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                self._master_config_exists = True
                break

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        self._check_master_config()

        if not self._master_config_exists:
            return await self.async_step_master_config()
        else:
            return await self.async_step_menu()

    async def async_step_menu(self, user_input=None):
        """Show menu for choosing setup type."""
        if user_input is not None:
            if user_input["setup_type"] == "master":
                return await self.async_step_master_config()
            elif user_input["setup_type"] == "device":
                return await self.async_step_device()

        self._check_master_config()

        return self.async_show_menu(
            step_id="menu",
            menu_options={
                "device": "Add Device",
                "master": "Update Master Configuration" if self._master_config_exists else "Setup Master Configuration",
            },
        )

    async def async_step_master_config(self, user_input=None):
        """Configure master settings."""
        errors = {}

        if user_input is not None:
            # Check for existing master configuration
            existing_master = None
            all_entries = self._async_current_entries()

            _LOGGER.debug("Checking for existing master config. Total entries: %d", len(all_entries))

            for entry in all_entries:
                entry_type = entry.data.get("entry_type")
                _LOGGER.debug("Entry: %s, Type: %s", entry.entry_id, entry_type)
                if entry_type == ENTRY_TYPE_MASTER:
                    if existing_master is not None:
                        # Found multiple master configs - this shouldn't happen!
                        _LOGGER.error("Multiple master configurations found! Deleting duplicate.")
                        await self.hass.config_entries.async_remove(entry.entry_id)
                    else:
                        existing_master = entry

            data = {**user_input, "entry_type": ENTRY_TYPE_MASTER}

            if existing_master:
                _LOGGER.info("Updating existing master configuration: %s", existing_master.entry_id)
                self.hass.config_entries.async_update_entry(existing_master, data=data)
                return self.async_abort(reason="master_updated")
            else:
                _LOGGER.info("Creating new master configuration")
                return self.async_create_entry(title="Master Configuration", data=data)

        # Get existing values if updating
        existing_data = {}
        for entry in self._async_current_entries():
            if entry.data.get("entry_type") == ENTRY_TYPE_MASTER:
                existing_data = entry.data
                break

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ACRCLOUD_HOST, default=existing_data.get(CONF_ACRCLOUD_HOST, "")): cv.string,
                vol.Required(
                    CONF_HOME_ASSISTANT_UDP_PORT, default=existing_data.get(CONF_HOME_ASSISTANT_UDP_PORT, 6056)
                ): cv.port,
                vol.Required(CONF_ACRCLOUD_ACCESS_KEY, default=existing_data.get(CONF_ACRCLOUD_ACCESS_KEY, "")): cv.string,
                vol.Required(
                    CONF_ACRCLOUD_ACCESS_SECRET, default=existing_data.get(CONF_ACRCLOUD_ACCESS_SECRET, "")
                ): cv.string,
                vol.Required(CONF_SPOTIFY_CLIENT_ID, default=existing_data.get(CONF_SPOTIFY_CLIENT_ID, "")): cv.string,
                vol.Required(CONF_SPOTIFY_CLIENT_SECRET, default=existing_data.get(CONF_SPOTIFY_CLIENT_SECRET, "")): cv.string,
                vol.Optional(CONF_SPOTIFY_PLAYLIST_ID, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_ID, "")): cv.string,
                vol.Optional(CONF_SPOTIFY_CREATE_PLAYLIST, default=existing_data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True)): cv.boolean,
                vol.Optional(
                    CONF_SPOTIFY_PLAYLIST_NAME,
                    default=existing_data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME),
                ): cv.string,
            }
        )

        return self.async_show_form(step_id="master_config", data_schema=data_schema, errors=errors)

    async def async_step_device(self, user_input=None):
        """Configure individual device."""
        errors = {}

        self._check_master_config()
        if not self._master_config_exists:
            return self.async_abort(reason="master_required")

        if user_input is not None:
            device_name = user_input[CONF_DEVICE_NAME]
            assist_satellite = user_input[CONF_ASSIST_SATELLITE_ENTITY]
            media_player = user_input[CONF_MEDIA_PLAYER_ENTITY]
            use_display_device = user_input.get(CONF_USE_DISPLAY_DEVICE, False)
            display_device = user_input.get(CONF_DISPLAY_DEVICE) if use_display_device else None

            # Check for duplicate device names
            for entry in self._async_current_entries():
                if (
                    entry.data.get("entry_type") == ENTRY_TYPE_DEVICE
                    and entry.data.get(CONF_DEVICE_NAME) == device_name
                ):
                    errors[CONF_DEVICE_NAME] = "name_exists"
                    break

            if not errors:
                # Validate assist satellite entity
                if not assist_satellite.startswith("assist_satellite."):
                    errors[CONF_ASSIST_SATELLITE_ENTITY] = "invalid_assist_satellite"
                else:
                    # Try to infer tagging switch from assist satellite
                    tagging_switch, error = infer_tagging_switch_from_assist_satellite(self.hass, assist_satellite)
                    tagging_enabled = tagging_switch is not None

                    if error and not tagging_enabled:
                        _LOGGER.info(
                            "Device '%s' will be configured without tagging capability: %s", device_name, error
                        )

                # Validate media player entity
                if not self.hass.states.get(media_player):
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "media_player_not_found"

                # Validate display device if selected
                if use_display_device and display_device and display_device not in ["none", "dummy"]:
                    # Get current display devices to validate
                    available_devices = get_display_device_options(self.hass)
                    if display_device not in available_devices:
                        errors[CONF_DISPLAY_DEVICE] = "display_device_not_found"
                        _LOGGER.warning(
                            "Selected display device not found: %s. Available: %s",
                            display_device,
                            list(available_devices.keys()),
                        )

                if not errors:
                    # Extract base name for storage
                    base_name = (
                        assist_satellite[17:-17] if assist_satellite.endswith("_assist_satellite") else ""
                    )

                    data = {
                        "device_name": device_name,
                        "assist_satellite_entity": assist_satellite,
                        "media_player_entity": media_player,
                        "base_name": base_name,
                        "tagging_enabled": tagging_enabled,
                        "use_display_device": use_display_device,
                        "entry_type": ENTRY_TYPE_DEVICE,
                    }

                    # Only add tagging switch if it exists
                    if tagging_enabled and tagging_switch:
                        data["tagging_switch_entity"] = tagging_switch

                    # Only add display device if enabled and valid
                    if use_display_device and display_device and display_device != "none":
                        data[CONF_DISPLAY_DEVICE] = display_device

                    # Log the device creation for debugging
                    _LOGGER.info(
                        "Creating device entry: %s with tagging enabled: %s, display device: %s",
                        device_name,
                        tagging_enabled,
                        display_device if use_display_device else "None",
                    )
                    if tagging_enabled:
                        _LOGGER.info("Tagging switch: %s", tagging_switch)
                    else:
                        _LOGGER.info("Device will support lyrics display only (no audio tagging)")

                    return self.async_create_entry(title=device_name, data=data)

        # Get available assist satellites and media players
        assist_satellites = []
        media_players = []

        for state in self.hass.states.async_all():
            if state.entity_id.startswith("assist_satellite."):
                assist_satellites.append(state.entity_id)
            elif state.entity_id.startswith("media_player."):
                media_players.append(state.entity_id)

        # Sort the lists for better user experience
        assist_satellites.sort()
        media_players.sort()

        # Get display device options with enhanced discovery
        display_devices = get_display_device_options(self.hass)
        display_options = [{"value": key, "label": value} for key, value in display_devices.items()]

        data_schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_NAME): cv.string,
                vol.Required(CONF_ASSIST_SATELLITE_ENTITY): vol.In(assist_satellites),
                vol.Required(CONF_MEDIA_PLAYER_ENTITY): vol.In(media_players),
                vol.Optional(CONF_USE_DISPLAY_DEVICE, default=False): cv.boolean,
                vol.Optional(CONF_DISPLAY_DEVICE): SelectSelector(
                    SelectSelectorConfig(
                        options=display_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="device", data_schema=data_schema, errors={})


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
                vol.Optional(CONF_HOME_ASSISTANT_UDP_PORT, default=data.get(CONF_HOME_ASSISTANT_UDP_PORT, 6056)): int,
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
