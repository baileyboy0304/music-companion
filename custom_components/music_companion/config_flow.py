import voluptuous as vol
import logging
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
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
    CONF_ACRCLOUD_HOST,
    CONF_HOME_ASSISTANT_UDP_PORT,
    CONF_ACRCLOUD_ACCESS_KEY,
    CONF_ACRCLOUD_ACCESS_SECRET,
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    CONF_SPOTIFY_PLAYLIST_ID,
    CONF_SPOTIFY_CREATE_PLAYLIST,
    CONF_SPOTIFY_PLAYLIST_NAME,
    CONF_DEVICE_NAME,
    CONF_ASSIST_SATELLITE_ENTITY,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_DISPLAY_DEVICE,
    CONF_USE_DISPLAY_DEVICE,
    ENTRY_TYPE_MASTER,
    ENTRY_TYPE_DEVICE,
    VIEW_ASSIST_DOMAIN,
    REMOTE_ASSIST_DISPLAY_DOMAIN,
    DEFAULT_SPOTIFY_PLAYLIST_NAME
)

_LOGGER = logging.getLogger(__name__)

def infer_tagging_switch_from_assist_satellite(hass, assist_satellite_entity):
    """Infer tagging switch entity from assist satellite entity ID using device registry."""
    if not assist_satellite_entity.startswith("assist_satellite."):
        return None, "Invalid assist satellite entity format"
    
    try:
        # Get the entity registry and device registry
        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        
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
                # We look for entity_id containing 'tagging_enable'
                if "tagging_enable" in entry.entity_id:
                    candidates.append(entry.entity_id)
        
        # If we found candidates, prefer the one that matches the base name of the satellite
        if candidates:
            # Try to find the best match by comparing base name patterns
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
        return None, "Error inferring tagging switch: %s" % e


def get_devices_for_domain(hass: HomeAssistant, domain: str):
    """Return a list of device entries that have entities from the specified domain."""
    try:
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        
        matching_devices = []
        
        for device in device_registry.devices.values():
            # device.config_entries contains config entry IDs (strings)
            # We need to look up the actual config entries to check their domains
            for entry_id in device.config_entries:
                config_entry = config_entries.async_get_entry(entry_id)
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
            _LOGGER.debug("View Assist data keys: %s", list(view_assist_data.keys()) if isinstance(view_assist_data, dict) else "Not a dict")
            
            # Try different possible keys for browser IDs
            possible_keys = ["va_browser_ids", "browser_ids", "browsers", "devices"]
            for key in possible_keys:
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

    # Fallback: try to discover devices via an internal helper in View Assist
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

    # Try Remote Assist Display domain if present
    if REMOTE_ASSIST_DISPLAY_DOMAIN in hass.data:
        _LOGGER.debug("Remote Assist Display domain found in hass.data")
        try:
            remote_display_data = hass.data[REMOTE_ASSIST_DISPLAY_DOMAIN]
            _LOGGER.debug("Remote Assist Display keys: %s", list(remote_display_data.keys()) if isinstance(remote_display_data, dict) else "Not a dict")
            
            possible_keys = ["devices", "registered_displays", "screens"]
            for key in possible_keys:
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
    
    # Check for any other display-related integrations
    try:
        # Look for entities that might be displays
        display_entity_patterns = [
            "display.",
            "screen.",
            "monitor.",
        ]
        
        for pattern in display_entity_patterns:
            matching_entities = [
                entity_id for entity_id in 
                hass.states.async_entity_ids()
                if entity_id.startswith(pattern)
            ]
            
            if matching_entities:
                _LOGGER.debug("Found %d entities matching pattern '%s': %s", 
                             len(matching_entities), pattern, matching_entities[:3])  # Log first 3
                
                # Add these as potential display devices
                for entity_id in matching_entities[:5]:  # Limit to first 5
                    state = hass.states.get(entity_id)
                    if state:
                        friendly_name = state.attributes.get('friendly_name', entity_id)
                        display_devices[entity_id] = f"Display Entity: {friendly_name}"
                        
    except Exception as e:
        _LOGGER.debug("Error during generic display entity discovery: %s", e)

    if not display_devices:
        # Always include a dummy value so the selector renders
        display_devices["dummy"] = "No display devices found"

    return display_devices


class MusicCompanionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the integration."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Initial step: choose Master or Device configuration."""
        errors = {}

        if user_input is not None:
            _LOGGER.debug("User selected config type: %s", user_input)
            choice = user_input.get("config_type")
            if choice == "master":
                return await self.async_step_master()
            elif choice == "device":
                return await self.async_step_device()
            else:
                errors["base"] = "invalid_selection"

        schema = vol.Schema({
            vol.Required("config_type"): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": "master", "label": "Master Configuration"},
                        {"value": "device", "label": "Add Device"},
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        })

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_master(self, user_input=None):
        """Handle the master configuration step."""
        errors = {}

        if user_input is not None:
            _LOGGER.debug("Master step input: %s", user_input)

            # Before creating another master, remove any duplicates
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
            
            # Build data for the master entry
            data = {
                **user_input,
                "entry_type": ENTRY_TYPE_MASTER
            }
            
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

        schema = vol.Schema({
            vol.Optional(CONF_ACRCLOUD_HOST, default=existing_data.get(CONF_ACRCLOUD_HOST, "")): cv.string,
            vol.Optional(CONF_ACRCLOUD_ACCESS_KEY, default=existing_data.get(CONF_ACRCLOUD_ACCESS_KEY, "")): cv.string,
            vol.Optional(CONF_ACRCLOUD_ACCESS_SECRET, default=existing_data.get(CONF_ACRCLOUD_ACCESS_SECRET, "")): cv.string,
            vol.Optional(CONF_HOME_ASSISTANT_UDP_PORT, default=existing_data.get(CONF_HOME_ASSISTANT_UDP_PORT, 10699)): cv.positive_int,

            vol.Optional(CONF_SPOTIFY_CLIENT_ID, default=existing_data.get(CONF_SPOTIFY_CLIENT_ID, "")): cv.string,
            vol.Optional(CONF_SPOTIFY_CLIENT_SECRET, default=existing_data.get(CONF_SPOTIFY_CLIENT_SECRET, "")): cv.string,
            vol.Optional(CONF_SPOTIFY_PLAYLIST_ID, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_ID, "")): cv.string,
            vol.Optional(CONF_SPOTIFY_CREATE_PLAYLIST, default=existing_data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True)): cv.boolean,
            vol.Optional(CONF_SPOTIFY_PLAYLIST_NAME, default=existing_data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME)): cv.string,
        })

        return self.async_show_form(step_id="master", data_schema=schema, errors=errors)

    async def async_step_device(self, user_input=None):
        """Handle the device configuration step."""
        errors = {}

        # Build dynamic lists for selection
        assist_satellites = []
        media_players = []

        # Collect assist satellites and media players from states
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

            # Basic validation
            if not device_name:
                errors[CONF_DEVICE_NAME] = "required"

            if not errors:
                # Validate assist satellite entity
                if not assist_satellite.startswith("assist_satellite."):
                    errors[CONF_ASSIST_SATELLITE_ENTITY] = "invalid_assist_satellite"
                else:
                    # Try to infer tagging switch from assist satellite
                    tagging_switch, error = infer_tagging_switch_from_assist_satellite(self.hass, assist_satellite)
                    tagging_enabled = tagging_switch is not None
                    
                    if error and not tagging_enabled:
                        _LOGGER.info("Device '%s' will be configured without tagging capability: %s", device_name, error)
                
                # Validate media player entity
                if not self.hass.states.get(media_player):
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "media_player_not_found"
                
                # Validate display device if selected
                if use_display_device and display_device and display_device not in ["none", "dummy"]:
                    # Get current display devices to validate
                    available_devices = get_display_device_options(self.hass)
                    if display_device not in available_devices:
                        errors[CONF_DISPLAY_DEVICE] = "display_device_not_found"
                        _LOGGER.warning("Selected display device not found: %s. Available: %s", 
                                      display_device, list(available_devices.keys()))
                
                if not errors:
                    # Extract base name for storage
                    base_name = assist_satellite[17:-17] if assist_satellite.endswith("_assist_satellite") else ""
                    
                    data = {
                        "entry_type": ENTRY_TYPE_DEVICE,
                        CONF_DEVICE_NAME: device_name,
                        CONF_ASSIST_SATELLITE_ENTITY: assist_satellite,
                        CONF_MEDIA_PLAYER_ENTITY: media_player,
                        # Tagging capability will be set based on inference
                        "tagging_enabled": tagging_switch is not None,
                        "tagging_switch_entity": tagging_switch if tagging_switch else None,
                        # Display options
                        CONF_USE_DISPLAY_DEVICE: use_display_device,
                        CONF_DISPLAY_DEVICE: display_device if use_display_device else None,
                        "base_name": base_name,
                    }

                    title = f"Device: {device_name}"
                    return self.async_create_entry(title=title, data=data)

        # Rebuild lists on every show
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

        data_schema = vol.Schema({
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
        })

        return self.async_show_form(step_id="device", data_schema=data_schema, errors=errors)


class MusicCompanionOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Music Companion."""

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options for the master entry (Spotify/ACR)."""
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

        schema = vol.Schema({
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
            vol.Optional(CONF_SPOTIFY_PLAYLIST_NAME, default=data.get(CONF_SPOTIFY_PLAYLIST_NAME, DEFAULT_SPOTIFY_PLAYLIST_NAME)): str,
        })

        return self.async_show_form(step_id="init", data_schema=schema)

async def async_get_options_flow(config_entry):
    return MusicCompanionOptionsFlowHandler(config_entry)
