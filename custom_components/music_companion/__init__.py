import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.config_entries import ConfigEntry
from .tagging import async_setup_tagging_service
from .lyrics import async_setup_lyrics_service
from .spotify import async_setup_spotify_service
from .const import (
    DOMAIN,
    CONF_MASTER_CONFIG,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_ACRCLOUD_ACCESS_KEY,
    CONF_ACRCLOUD_ACCESS_SECRET,
    CONF_HOME_ASSISTANT_UDP_PORT,
    CONF_ACRCLOUD_HOST,
    CONF_DEVICE_NAME,
    CONF_SPOTIFY_CLIENT_ID,
    CONF_SPOTIFY_CLIENT_SECRET,
    CONF_SPOTIFY_PLAYLIST_ID,
    CONF_SPOTIFY_CREATE_PLAYLIST,
    CONF_SPOTIFY_PLAYLIST_NAME,
    CONF_DISPLAY_DEVICE,
    CONF_USE_DISPLAY_DEVICE,
    ENTRY_TYPE_MASTER,
    ENTRY_TYPE_DEVICE,
)

_LOGGER = logging.getLogger(__name__)

def get_master_config(hass: HomeAssistant):
    """Get the master configuration entry."""
    if DOMAIN not in hass.data:
        return None
    
    for entry_id, data in hass.data[DOMAIN].items():
        if hasattr(data, 'get') and data.get("entry_type") == ENTRY_TYPE_MASTER:
            return data
    return None

def get_device_configs(hass: HomeAssistant):
    """Get all device configuration entries."""
    if DOMAIN not in hass.data:
        return []
    
    devices = []
    for entry_id, data in hass.data[DOMAIN].items():
        if hasattr(data, 'get') and data.get("entry_type") == ENTRY_TYPE_DEVICE:
            devices.append((entry_id, data))
    return devices

def get_device_safe_name(device_name: str) -> str:
    """Convert device name to entity-friendly format."""
    if not device_name:
        return "default"
    return device_name.lower().replace(" ", "_").replace("-", "_")

async def setup_device_notification(hass: HomeAssistant, device_name: str, entry_id: str, config_data: dict):
    """Show notification that device setup is complete."""
    safe_name = get_device_safe_name(device_name)
    
    # Expected entity names
    expected_entities = [
        f"text.{safe_name}_lyrics_line1",
        f"text.{safe_name}_lyrics_line2", 
        f"text.{safe_name}_lyrics_line3"
    ]
    
    # Check display device configuration
    use_display_device = config_data.get("use_display_device", False)
    display_device = config_data.get("display_device") if use_display_device else None
    tagging_enabled = config_data.get("tagging_enabled", False)
    
    # Build message based on configuration
    message = f"✅ **{device_name}** setup complete!\n\n"
    
    if use_display_device and display_device and display_device != "none":
        message += f"**Display Device:** {display_device}\n"
        message += "Lyrics will be shown on the configured display device.\n\n"
        message += "**Fallback text entities created:**\n" + \
                  "\n".join([f"• `{entity}`" for entity in expected_entities])
    else:
        message += "**Lyrics entities created:**\n" + \
                  "\n".join([f"• `{entity}`" for entity in expected_entities])
    
    if tagging_enabled:
        message += "\n\n🎤 **Audio tagging enabled** - Device can identify songs from audio"
    else:
        message += "\n\n📺 **Lyrics display only** - Device shows lyrics but cannot identify audio"
    
    message += "\n\nYour device is ready to display synchronized lyrics!"
    
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": f"Device Ready: {device_name}",
            "message": message,
            "notification_id": f"device_setup_success_{entry_id}"
        }
    )

async def async_setup(hass: HomeAssistant, config) -> bool:
    """Set up the Music Companion integration from yaml configuration."""
    # No YAML configuration support anymore - only config flow
    return True

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the Music Companion integration from a config entry."""
    entry_type = config_entry.data.get("entry_type", ENTRY_TYPE_DEVICE)
    
    _LOGGER.warning("Setting up config entry: %s, type: %s, data: %s", config_entry.entry_id, entry_type, config_entry.data)
    
    # Initialize the domain data structure if it doesn't exist
    hass.data.setdefault(DOMAIN, {})
    
    # Store this entry's data using the entry ID as the key
    hass.data[DOMAIN][config_entry.entry_id] = config_entry.data
    
    _LOGGER.warning("Stored config entry in hass.data[%s][%s]", DOMAIN, config_entry.entry_id)
    _LOGGER.warning("Current hass.data[%s] keys: %s", DOMAIN, list(hass.data[DOMAIN].keys()))

    try:
        if entry_type == ENTRY_TYPE_MASTER:
            result = await async_setup_master_entry(hass, config_entry)
            _LOGGER.warning("Master setup result: %s", result)
            return result
        else:
            result = await async_setup_device_entry(hass, config_entry)
            _LOGGER.warning("Device setup result: %s", result)
            return result
    except Exception as e:
        _LOGGER.warning("Exception in setup: %s", e)
        raise

async def async_setup_master_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the master configuration entry."""
    _LOGGER.info("Setting up Music Companion Master Configuration")

    # Register the tagging and lyrics services (only once)
    if not hass.data[DOMAIN].get('_services_registered'):
        await async_setup_tagging_service(hass)
        await async_setup_lyrics_service(hass)
        hass.data[DOMAIN]['_services_registered'] = True
    
    try:
        # Set up Spotify service using master config credentials
        if "spotify_service" not in hass.data.get(DOMAIN, {}):
            spotify_config = {
                "client_id": config_entry.data.get(CONF_SPOTIFY_CLIENT_ID),
                "client_secret": config_entry.data.get(CONF_SPOTIFY_CLIENT_SECRET),
                "playlist_id": config_entry.data.get(CONF_SPOTIFY_PLAYLIST_ID),
                "create_playlist": config_entry.data.get(CONF_SPOTIFY_CREATE_PLAYLIST, True),
                "playlist_name": config_entry.data.get(CONF_SPOTIFY_PLAYLIST_NAME, "Home Assistant Discovered Tracks")
            }
            
            # Log spotify config (but mask secret)
            safe_config = {**spotify_config}
            if "client_secret" in safe_config:
                safe_config["client_secret"] = "****"
            _LOGGER.debug("Spotify configuration prepared: %s", safe_config)
            
            # Create a config dictionary with the spotify section
            modified_config = {"spotify": spotify_config}
            
            # Call the Spotify setup service
            _LOGGER.info("Initializing Spotify service from master configuration...")
            await async_setup_spotify_service(hass, modified_config)
            _LOGGER.info("Spotify service initialization completed")
            
            # Create a notification to confirm Spotify is set up
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Master Configuration Setup",
                    "message": "Master configuration with Spotify integration has been initialized.",
                    "notification_id": "master_config_setup"
                }
            )
    except Exception as e:
        _LOGGER.error("Failed to initialize Spotify service from master config: %s", e)
        # Create error notification
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Master Configuration Error",
                "message": f"Failed to initialize master configuration: {str(e)}\n\nCheck logs for more details.",
                "notification_id": "master_config_error"
            }
        )

    # Ensure logging level is set to debug for troubleshooting
    logging.getLogger(f"custom_components.{DOMAIN}").setLevel(logging.DEBUG)

    return True

async def async_setup_device_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up a device entry."""
    device_name = config_entry.data.get(CONF_DEVICE_NAME, "Music Companion Device")
    _LOGGER.info("Setting up Music Companion device: %s", device_name)

    # Check if master configuration exists
    master_config = get_master_config(hass)
    if not master_config:
        _LOGGER.error("Master configuration not found for device: %s", device_name)
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Device Setup Error",
                "message": f"Device '{device_name}' cannot be set up without master configuration. Please set up master configuration first.",
                "notification_id": f"device_setup_error_{config_entry.entry_id}"
            }
        )
        return False

    # Forward the entry to the text platform to create lyrics entities
    await hass.config_entries.async_forward_entry_setups(config_entry, ["text"])

    # Show success notification
    await setup_device_notification(hass, device_name, config_entry.entry_id, config_entry.data)

    _LOGGER.info("Device '%s' configured successfully (using event-based tagging)", device_name)

    # Autostart the fetch_lyrics service for this device
    async def autostart(event):
        _LOGGER.debug("Autostarting fetch_lyrics service for device: %s", device_name)
        try:
            entity_id = config_entry.data[CONF_MEDIA_PLAYER_ENTITY]
            
            # Always autostart since there's no enable/disable switch anymore
            await hass.services.async_call(
                DOMAIN,
                "fetch_lyrics",
                {"entity_id": entity_id}
            )
            _LOGGER.info("Autostarted fetch_lyrics service for entity: %s (device: %s)", entity_id, device_name)
                
        except Exception as e:
            _LOGGER.error("Error in autostarting fetch_lyrics service for device %s: %s", device_name, e)

    # Listen for Home Assistant start event
    hass.bus.async_listen_once("homeassistant_start", autostart)
    _LOGGER.debug("Registered autostart listener for device: %s", device_name)

    return True

async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    entry_type = config_entry.data.get("entry_type", ENTRY_TYPE_DEVICE)
    
    if entry_type == ENTRY_TYPE_MASTER:
        _LOGGER.info("Unloading Music Companion Master Configuration")
        # Don't remove shared services as devices might still need them
    else:
        device_name = config_entry.data.get(CONF_DEVICE_NAME, "Music Companion Device")
        _LOGGER.info("Unloading Music Companion device: %s", device_name)
        
        # Stop any active lyrics sync for this device
        from .const import DEVICE_DATA_LYRICS_SYNC
        if (DOMAIN in hass.data and 
            config_entry.entry_id in hass.data[DOMAIN] and
            DEVICE_DATA_LYRICS_SYNC in hass.data[DOMAIN][config_entry.entry_id]):
            
            lyrics_sync = hass.data[DOMAIN][config_entry.entry_id][DEVICE_DATA_LYRICS_SYNC]
            if lyrics_sync and lyrics_sync.active:
                await lyrics_sync.stop()
                _LOGGER.info("Stopped lyrics sync for device: %s", device_name)
        
        # Unload the text platform
        await hass.config_entries.async_forward_entry_unload(config_entry, "text")
    
    # Remove this entry's data
    if DOMAIN in hass.data and config_entry.entry_id in hass.data[DOMAIN]:
        del hass.data[DOMAIN][config_entry.entry_id]
    
    return True

async def async_reload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)