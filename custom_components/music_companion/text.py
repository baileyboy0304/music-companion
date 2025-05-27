"""Text entities for Music Companion lyrics display."""
import logging
from homeassistant.components.text import TextEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN, 
    CONF_DEVICE_NAME, 
    CONF_MEDIA_PLAYER_ENTITY, 
    CONF_ASSIST_SATELLITE_ENTITY,
    CONF_DISPLAY_DEVICE,
    CONF_USE_DISPLAY_DEVICE
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lyrics text entities and device info sensor for a device."""
    device_name = config_entry.data.get(CONF_DEVICE_NAME, "Music Companion Device")
    
    # Only create entities for device entries, not master entries
    if config_entry.data.get("entry_type") != "device":
        return
    
    safe_name = device_name.lower().replace(" ", "_").replace("-", "_")
    
    # Check if device is configured to use display device
    use_display_device = config_entry.data.get(CONF_USE_DISPLAY_DEVICE, False)
    display_device = config_entry.data.get(CONF_DISPLAY_DEVICE) if use_display_device else None
    
    entities = []
    
    # Always create text entities (they serve as fallback and for manual access)
    # But they may not be actively used if display device is configured
    text_entities = [
        LyricsTextEntity(
            config_entry,
            "line1",
            f"{device_name} Lyrics Line 1",
            f"{safe_name}_lyrics_line1",
            use_display_device
        ),
        LyricsTextEntity(
            config_entry,
            "line2", 
            f"{device_name} Lyrics Line 2",
            f"{safe_name}_lyrics_line2",
            use_display_device
        ),
        LyricsTextEntity(
            config_entry,
            "line3",
            f"{device_name} Lyrics Line 3", 
            f"{safe_name}_lyrics_line3",
            use_display_device
        ),
    ]
    
    entities.extend(text_entities)
    
    # Create device info sensor
    device_info_sensor = MusicCompanionDeviceSensor(config_entry)
    entities.append(device_info_sensor)
    
    # Add all entities
    async_add_entities(entities)
    
    # Log appropriate message based on configuration
    if use_display_device and display_device and display_device != "none":
        _LOGGER.info("Created lyrics text entities (fallback) and device sensor for device: %s (primary display: %s)", 
                    device_name, display_device)
    else:
        _LOGGER.info("Created lyrics text entities and device sensor for device: %s", device_name)


class LyricsTextEntity(TextEntity):
    """Text entity for displaying lyrics lines."""
    
    def __init__(self, config_entry: ConfigEntry, line_type: str, name: str, unique_id: str, use_display_device: bool = False):
        """Initialize the lyrics text entity."""
        self._config_entry = config_entry
        self._line_type = line_type
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_{unique_id}"
        self._attr_native_value = ""
        self._attr_icon = "mdi:music-note"
        self._attr_mode = "text"
        self._attr_native_max = 255
        self._attr_native_min = 0
        self._use_display_device = use_display_device
        
        # Set the entity ID we want
        device_name = config_entry.data.get(CONF_DEVICE_NAME, "Music Companion Device")
        safe_name = device_name.lower().replace(" ", "_").replace("-", "_")
        self._entity_id = f"text.{safe_name}_lyrics_{line_type}"
        
        # Device information
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=f"Music Companion - {device_name}",
            manufacturer="Music Companion",
            model="Lyrics Display",
            sw_version="1.0.0",
        )
    
    @property
    def entity_id(self) -> str:
        """Return the entity ID."""
        return self._entity_id
    
    @entity_id.setter
    def entity_id(self, value: str) -> None:
        """Set the entity ID."""
        self._entity_id = value
    
    @property
    def extra_state_attributes(self) -> dict:
        """Return additional state attributes."""
        attrs = {}
        
        # Add information about display device usage
        if self._use_display_device:
            display_device = self._config_entry.data.get(CONF_DISPLAY_DEVICE)
            attrs["display_mode"] = "display_device"
            attrs["display_device"] = display_device
            attrs["primary_output"] = False  # Text entities are fallback when display device is used
        else:
            attrs["display_mode"] = "text_entities"
            attrs["primary_output"] = True  # Text entities are primary output
        
        return attrs
    
    async def async_set_value(self, value: str) -> None:
        """Set the text value."""
        self._attr_native_value = value
        self.async_write_ha_state()
        
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True


class MusicCompanionDeviceSensor(SensorEntity):
    """Sensor that exposes device-specific entity mappings for dashboard integration."""
    
    def __init__(self, config_entry: ConfigEntry):
        """Initialize the device info sensor."""
        self._config_entry = config_entry
        device_name = config_entry.data.get(CONF_DEVICE_NAME, "Music Companion Device")
        safe_name = device_name.lower().replace(" ", "_").replace("-", "_")
        
        self._attr_name = f"Music Companion {device_name}"
        self._attr_unique_id = f"{DOMAIN}_{config_entry.entry_id}_device_info"
        self._attr_icon = "mdi:music"
        self._attr_native_value = "active"
        
        # Set the entity ID we want
        self._entity_id = f"sensor.music_companion_{safe_name}"
        
        # Check display device configuration
        use_display_device = config_entry.data.get(CONF_USE_DISPLAY_DEVICE, False)
        display_device = config_entry.data.get(CONF_DISPLAY_DEVICE) if use_display_device else None
        
        # Expose the lyrics entities and other device info as attributes
        self._attr_extra_state_attributes = {
            "lyrics_line1": f"text.{safe_name}_lyrics_line1",
            "lyrics_line2": f"text.{safe_name}_lyrics_line2", 
            "lyrics_line3": f"text.{safe_name}_lyrics_line3",
            "media_player": config_entry.data.get(CONF_MEDIA_PLAYER_ENTITY),
            "assist_satellite": config_entry.data.get(CONF_ASSIST_SATELLITE_ENTITY),
            "device_name": device_name,
            "safe_name": safe_name,
            "entry_id": config_entry.entry_id,
            "use_display_device": use_display_device,
            "display_device": display_device,
            "display_mode": "display_device" if use_display_device and display_device else "text_entities",
            "tagging_enabled": config_entry.data.get("tagging_enabled", False),
        }
        
        # Device information
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=f"Music Companion - {device_name}",
            manufacturer="Music Companion",
            model="Device Info",
            sw_version="1.0.0",
        )
    
    @property
    def entity_id(self) -> str:
        """Return the entity ID."""
        return self._entity_id
    
    @entity_id.setter
    def entity_id(self, value: str) -> None:
        """Set the entity ID."""
        self._entity_id = value
    
    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True
    
    @property
    def should_poll(self) -> bool:
        """No need to poll."""
        return False