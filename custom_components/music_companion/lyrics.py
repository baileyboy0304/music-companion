import logging
import datetime
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import lrc_kit
import time
import re
import asyncio
import aiohttp
from .const import (
    DOMAIN, 
    DEVICE_DATA_LYRICS_SYNC, 
    DEVICE_DATA_LAST_MEDIA_CONTENT_ID,
    DEVICE_DATA_LYRICS_ENTITIES,
    CONF_DEVICE_NAME,
    CONF_MEDIA_PLAYER_ENTITY,
)
from homeassistant.helpers.event import async_track_state_change_event
from .media_tracker import MediaTracker

_LOGGER = logging.getLogger(__name__)

_INTEGRATION_JUST_STARTED = True # handles issues around first track lyrics not being displayed

SERVICE_FETCH_LYRICS_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id
})


def get_device_data(hass: HomeAssistant, entry_id: str = None):
    """Get or create device-specific data structure."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    
    # If no entry_id provided, use default
    if not entry_id:
        entry_id = "default"
    
    # Create a separate runtime data key for this device
    runtime_key = f"{entry_id}_runtime"
    
    if runtime_key not in hass.data[DOMAIN]:
        hass.data[DOMAIN][runtime_key] = {}
    
    device_data = hass.data[DOMAIN][runtime_key]
    
    # Initialize device-specific data if not present
    if DEVICE_DATA_LYRICS_SYNC not in device_data:
        device_data[DEVICE_DATA_LYRICS_SYNC] = None
    if DEVICE_DATA_LAST_MEDIA_CONTENT_ID not in device_data:
        device_data[DEVICE_DATA_LAST_MEDIA_CONTENT_ID] = None
    
    return device_data


def get_device_lyrics_entities(hass: HomeAssistant, entry_id: str = None):
    """Get device-specific lyrics entity names using device registry."""
    device_data = get_device_data(hass, entry_id)
    
    if DEVICE_DATA_LYRICS_ENTITIES not in device_data:
        if not entry_id:
            _LOGGER.error("No entry_id provided for lyrics entity lookup")
            return {}
        
        # Get the device registry and entity registry
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        
        # Find the device that belongs to this config entry
        device = None
        for dev in device_registry.devices.values():
            if entry_id in dev.config_entries:
                device = dev
                break
        
        if not device:
            _LOGGER.error("No device found for config entry: %s", entry_id)
            return {}
        
        _LOGGER.debug("Found device: %s (id: %s) for entry: %s", device.name, device.id, entry_id)
        
        # Find ALL entities that belong to this device
        device_entities = er.async_entries_for_device(entity_registry, device.id)
        
        # Filter for the lyrics text entities
        lyrics_entities = {}
        for entity in device_entities:
            if (entity.domain == "text" and 
                entity.platform == DOMAIN and 
                "lyrics" in entity.entity_id and
                not entity.disabled_by):
                
                # Determine which line this is
                if "line1" in entity.entity_id:
                    lyrics_entities["line1"] = entity.entity_id
                elif "line2" in entity.entity_id:
                    lyrics_entities["line2"] = entity.entity_id
                elif "line3" in entity.entity_id:
                    lyrics_entities["line3"] = entity.entity_id
        
        if len(lyrics_entities) != 3:
            _LOGGER.error("Expected 3 lyrics entities for device %s, found %d: %s", 
                         device.name, len(lyrics_entities), list(lyrics_entities.values()))
            return {}
        
        _LOGGER.info("Found lyrics entities for device %s: %s", device.name, lyrics_entities)
        device_data[DEVICE_DATA_LYRICS_ENTITIES] = lyrics_entities
    
    return device_data[DEVICE_DATA_LYRICS_ENTITIES]


def get_device_config_data(hass: HomeAssistant, entry_id: str = None):
    """Get device configuration data."""
    if not entry_id:
        return None
    
    # Go through config entries to find the device
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        if config_entry.entry_id == entry_id and config_entry.data.get("entry_type") == "device":
            return config_entry.data
    
    return None


def find_entry_id_for_media_player(hass: HomeAssistant, media_player_entity_id: str):
    """Find the config entry ID for a given media player entity."""
    
    # Go through ALL Music Companion config entries
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        # Only check device entries (not master)
        if config_entry.data.get("entry_type") == "device":
            # Check if this entry's media player matches
            configured_media_player = config_entry.data.get(CONF_MEDIA_PLAYER_ENTITY)
            if configured_media_player == media_player_entity_id:
                _LOGGER.info("Found config entry %s for media player %s (device: %s)", 
                           config_entry.entry_id, media_player_entity_id, 
                           config_entry.data.get(CONF_DEVICE_NAME))
                return config_entry.entry_id
    
    _LOGGER.error("No Music Companion config entry found for media player: %s", media_player_entity_id)
    return None


class LyricsSynchronizer:
    """Manages lyrics synchronization using MediaTracker."""
    
    def __init__(self, hass: HomeAssistant, entry_id: str = None):
        self.hass = hass
        self.entry_id = entry_id
        self.media_tracker = None
        self.entity_id = None
        
        # Track information for comparison
        self.current_track = ""
        self.current_artist = ""
        
        # Lyrics data
        self.timeline = []
        self.lyrics = []
        self.current_line_index = -1
        
        # Control flags
        self.active = False
        
        # Display update handling
        self.last_update_time = 0
        self.force_update_interval = 3  # Force display update every 3 seconds even without position change
    
    async def start(self, entity_id: str, timeline: list, lyrics: list, pos=None, updated_at=None, is_radio_source=False):
        """Start lyrics synchronization for the given entity."""
        if self.active:
            await self.stop()
            
        self.entity_id = entity_id
        self.timeline = timeline
        self.lyrics = lyrics
        self.current_line_index = -1
        self.last_update_time = datetime.datetime.now().timestamp()
        
        # Store the current track info from the player state
        player_state = self.hass.states.get(entity_id)
        if player_state:
            self.current_track = player_state.attributes.get("media_title", "")
            self.current_artist = player_state.attributes.get("media_artist", "")
            _LOGGER.info("LyricsSynchronizer: Tracking track '%s' by '%s' (device: %s)", 
                        self.current_track, self.current_artist, self.entry_id)
        
        # Immediately show "Loading lyrics..." message
        #await update_lyrics_entities(self.hass, "", "Loading lyrics...", 
        #                            self.lyrics[0] if lyrics and len(lyrics) > 0 else "", self.entry_id)
        
        # Initialize media tracker with callbacks, passing radio source flag
        self.media_tracker = MediaTracker(
            self.hass, 
            self.entity_id,
            self.update_lyrics_position,  # Position update callback
            self.handle_track_change,     # Track change callback
            is_radio_source,              # Flag for radio source
            self.entry_id                 # Device entry ID
        )
        
        # Handle initial position setup
        if pos is not None and updated_at is not None:
            # We have reliable position data - use it
            self.media_tracker.set_initial_position(pos, updated_at)
            
            # Calculate initial position in milliseconds for better lyrics placement
            initial_position_ms = pos * 1000
            _LOGGER.info("LyricsSynchronizer: Initial position is %.2f ms (device: %s)", 
                        initial_position_ms, self.entry_id)
            
            # Find and display appropriate lyrics for this position
            self._sync_to_position(initial_position_ms)
        else:
            # No reliable position data - start from beginning and wait for position updates
            _LOGGER.info("LyricsSynchronizer: No position data - starting from beginning, waiting for fresh position updates (device: %s)", self.entry_id)
            
            # Show first lyrics line
            if len(lyrics) > 1:
                await update_lyrics_entities(self.hass, "", lyrics[0], lyrics[1], self.entry_id)
            elif len(lyrics) > 0:
                await update_lyrics_entities(self.hass, "", lyrics[0], "", self.entry_id)
            
            # Don't set any initial position in MediaTracker - let it get fresh data from state changes
        
        # Start tracking
        await self.media_tracker.start_tracking()
        self.active = True
        
        # Start a periodic force update task
        asyncio.create_task(self._force_update_task())
        
        _LOGGER.info("LyricsSynchronizer: Started for %s with %d lyrics lines (radio source: %s, position sync: %s, device: %s)", 
                    self.entity_id, len(self.lyrics), is_radio_source, pos is not None, self.entry_id)
    
    
    def _sync_to_position(self, position_ms):
        """Sync lyrics to a specific position in milliseconds."""
        line_found = False
        if self.timeline:
            # For radio sources, be more aggressive in finding the right starting point
            if hasattr(self.media_tracker, 'is_radio_source') and self.media_tracker.is_radio_source:
                # Calculate where we expect to be in about 500ms (to account for processing delay)
                target_position_ms = position_ms + 500
                
                # Try to find a line that's AFTER our current position but within a reasonable window
                for i in range(1, len(self.timeline)):
                    if self.timeline[i-1] > position_ms and self.timeline[i-1] < position_ms + 10000:
                        # Found a line coming up soon - use it
                        self.current_line_index = i-1
                        _LOGGER.info("LyricsSynchronizer: Starting at upcoming line index %d at %d ms (device: %s)", 
                                   i-1, self.timeline[i-1], self.entry_id)
                        line_found = True
                        
                        # Display immediately
                        previous_line = self.lyrics[i-2] if i > 1 else ""
                        current_line = self.lyrics[i-1]
                        next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                        
                        asyncio.create_task(update_lyrics_entities(self.hass, previous_line, current_line, next_line, self.entry_id))
                        break
            
            # If we didn't find an upcoming line, or this isn't a radio source,
            # fall back to finding the line that matches our current position
            if not line_found:
                for i in range(1, len(self.timeline)):
                    if self.timeline[i-1] <= position_ms < self.timeline[i]:
                        # We found the right line to start with
                        self.current_line_index = i-1
                        _LOGGER.info("LyricsSynchronizer: Starting at line index %d (device: %s)", 
                                    self.current_line_index, self.entry_id)
                        
                        # Display the initial lyrics
                        previous_line = self.lyrics[i-2] if i > 1 else ""
                        current_line = self.lyrics[i-1]
                        next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                        
                        asyncio.create_task(update_lyrics_entities(self.hass, previous_line, current_line, next_line, self.entry_id))
                        line_found = True
                        break
        
        # If we couldn't find the right position in the timeline, show first lines
        if not line_found and len(self.lyrics) > 0:
            _LOGGER.info("LyricsSynchronizer: No matching position found, showing first lines (device: %s)", self.entry_id)
            if len(self.lyrics) > 1:
                asyncio.create_task(update_lyrics_entities(self.hass, "", self.lyrics[0], self.lyrics[1], self.entry_id))
            else:
                asyncio.create_task(update_lyrics_entities(self.hass, "", self.lyrics[0], "", self.entry_id))

    async def stop(self):
        """Stop lyrics synchronization."""
        if not self.active:
            return
            
        self.active = False
        
        if self.media_tracker:
            await self.media_tracker.stop_tracking()
            self.media_tracker = None
        
        # Clear display
        await update_lyrics_entities(self.hass, "", "", "", self.entry_id)
        
        _LOGGER.info("LyricsSynchronizer: Stopped (device: %s)", self.entry_id)
    
    def update_lyrics_position(self, media_timecode: float):
        """Update lyrics display based on current media position."""
        if not self.active or not self.timeline or not self.lyrics:
            _LOGGER.warning("LyricsSynchronizer: Unable to update lyrics - no active timeline or lyrics (device: %s)", self.entry_id)
            return
            
        # Record update time for force update mechanism
        self.last_update_time = datetime.datetime.now().timestamp()
            
        # Convert to milliseconds for comparison with timeline
        position_ms = media_timecode * 1000
        
        # Log position occasionally for debugging
        if int(media_timecode) % 5 == 0 and abs(media_timecode - int(media_timecode)) < 0.15:  # Log roughly every 5 seconds
            _LOGGER.debug("LyricsSynchronizer: Current position: %.2f seconds (%.2f ms, device: %s)", 
                        media_timecode, position_ms, self.entry_id)
        
        # Check if lyrics finished
        if position_ms >= self.timeline[-1]:
            _LOGGER.info("LyricsSynchronizer: Lyrics finished (device: %s)", self.entry_id)
            asyncio.create_task(self.stop())
            return
            
        # Find current line
        found_position = False
        for i in range(1, len(self.timeline)):
            if self.timeline[i-1] <= position_ms < self.timeline[i]:
                if i-1 != self.current_line_index:
                    self.current_line_index = i-1
                    
                    # Get lines to display
                    previous_line = self.lyrics[i-2] if i > 1 else ""
                    current_line = self.lyrics[i-1]
                    next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                    
                    # Update display
                    asyncio.create_task(
                        update_lyrics_entities(self.hass, previous_line, current_line, next_line, self.entry_id)
                    )
                    
                    _LOGGER.debug("LyricsSynchronizer: Updated to line %d at %f ms (device: %s)", 
                                i-1, position_ms, self.entry_id)
                
                found_position = True
                break
        
        # If position wasn't found in any interval but lyrics exist,
        # it might be before the first line
        if not found_position and position_ms < self.timeline[0]:
            if self.current_line_index != -1:
                self.current_line_index = -1
                #asyncio.create_task(
                #    update_lyrics_entities(self.hass, "", "Waiting for first line...", self.lyrics[0], self.entry_id)
                #)
    
    def handle_track_change(self, is_track_change=True):
        """Handle track changes or seek operations detected by the media tracker.
        
        Args:
            is_track_change: True if actual track changed, False if just a seek operation
        """
        if is_track_change:
            # For actual track changes, stop lyrics entirely
            _LOGGER.info("LyricsSynchronizer: Track change detected, stopping lyrics (device: %s)", self.entry_id)
            asyncio.create_task(self.stop())
        else:
            # For seek operations, just reset the current line index to force resyncing
            _LOGGER.info("LyricsSynchronizer: Seek operation detected, resyncing lyrics (device: %s)", self.entry_id)
            
            # Get current position to find the right lyrics line
            if self.media_tracker and self.media_tracker.media_position is not None:
                current_position = self.media_tracker.calculate_current_position()
                position_ms = current_position * 1000
                
                # Find the appropriate lyrics line for current position
                self.current_line_index = -1  # Reset first
                
                # Look for the right lyrics line
                for i in range(1, len(self.timeline)):
                    if self.timeline[i-1] <= position_ms < self.timeline[i]:
                        # Found the right line
                        self.current_line_index = i-1
                        
                        # Display corresponding lyrics
                        previous_line = self.lyrics[i-2] if i > 1 else ""
                        current_line = self.lyrics[i-1]
                        next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                        
                        asyncio.create_task(
                            update_lyrics_entities(self.hass, previous_line, current_line, next_line, self.entry_id)
                        )
                        
                        _LOGGER.info("LyricsSynchronizer: Resynced to line %d at %f ms (device: %s)", 
                                   i-1, position_ms, self.entry_id)
                        break
                
                # If we couldn't find a matching line, check if we're before the first line
                #if self.current_line_index == -1:
                #    if position_ms < self.timeline[0]:
                #        asyncio.create_task(
                #            update_lyrics_entities(self.hass, "", "Waiting for first line...", self.lyrics[0], self.entry_id)
                #        )
                #    else:
                #        # We might be past the end
                #        asyncio.create_task(
                #            update_lyrics_entities(self.hass, "", "Lyrics finished", "", self.entry_id)
                #        )
    
    async def _force_update_task(self):
        """Periodically force update the lyrics display to ensure it doesn't get stuck."""
        try:
            # Use a shorter interval for the first few updates to ensure quick initial sync
            initial_updates = 0
            max_initial_updates = 5
            initial_interval = 0.5  # Faster initial updates
            
            while self.active:
                # Use shorter interval for initial updates
                if initial_updates < max_initial_updates:
                    await asyncio.sleep(initial_interval)
                    initial_updates += 1
                else:
                    await asyncio.sleep(self.force_update_interval)
                
                # If we're active but no update in a while, force one
                current_time = datetime.datetime.now().timestamp()
                time_since_update = current_time - self.last_update_time
                
                if time_since_update > (0.5 if initial_updates < max_initial_updates else self.force_update_interval):
                    _LOGGER.debug("LyricsSynchronizer: Forcing display update (%.1f seconds since last update, device: %s)", 
                                 time_since_update, self.entry_id)
                    
                    # Recalculate current position
                    if self.media_tracker and self.media_tracker.state == "playing":
                        current_position = self.media_tracker.calculate_current_position()
                        position_ms = current_position * 1000
                        
                        if len(self.timeline) > 1 and len(self.lyrics) > 1:
                            # Find appropriate line for current position
                            found_line = False
                            for i in range(1, len(self.timeline)):
                                if self.timeline[i-1] <= position_ms < self.timeline[i]:
                                    # Get lines to display
                                    previous_line = self.lyrics[i-2] if i > 1 else ""
                                    current_line = self.lyrics[i-1]
                                    next_line = self.lyrics[i] if i < len(self.lyrics) else ""
                                    
                                    # Force update the display
                                    await update_lyrics_entities(self.hass, previous_line, current_line, next_line, self.entry_id)
                                    self.last_update_time = current_time
                                    _LOGGER.debug("LyricsSynchronizer: Force updated to line %d (%.1f ms, device: %s)", 
                                                i-1, position_ms, self.entry_id)
                                    found_line = True
                                    break
                            
                            # If no matching line found, check if we're past the end
                            if not found_line:
                                if position_ms < self.timeline[0]:
                                    # Before first line
                                    #await update_lyrics_entities(self.hass, "", 
                                    #                          "Coming up...", self.lyrics[0], self.entry_id)
                                elif position_ms >= self.timeline[-1]:
                                    # Past the last line
                                    #await update_lyrics_entities(self.hass, 
                                    #                          self.lyrics[-1], "End of lyrics", "", self.entry_id)
                                else:
                                    # We should have found a line - try to recover
                                    # Find the closest line
                                    closest_idx = min(range(len(self.timeline)), 
                                                    key=lambda i: abs(self.timeline[i] - position_ms))
                                    
                                    _LOGGER.info("LyricsSynchronizer: Couldn't find exact line match, using closest: %d (device: %s)", 
                                               closest_idx, self.entry_id)
                                    
                                    previous_line = self.lyrics[closest_idx-1] if closest_idx > 0 else ""
                                    current_line = self.lyrics[closest_idx]
                                    next_line = self.lyrics[closest_idx+1] if closest_idx < len(self.lyrics)-1 else ""
                                    
                                    await update_lyrics_entities(self.hass, previous_line, current_line, next_line, self.entry_id)
                                
                                self.last_update_time = current_time
                        else:
                            # If we have lyrics but no timeline yet, or in initialization
                            if len(self.lyrics) > 0:
                                #await update_lyrics_entities(self.hass, "", "Loading lyrics...", self.lyrics[0], self.entry_id)
                                self.last_update_time = current_time
        except asyncio.CancelledError:
            _LOGGER.debug("LyricsSynchronizer: Force update task cancelled (device: %s)", self.entry_id)
        except Exception as e:
            _LOGGER.error("LyricsSynchronizer: Error in force update task (device: %s): %s", self.entry_id, str(e))


def lyricSplit(lyrics):
    """Split lyrics into a timeline and corresponding lines."""
    timeline = []
    lrc = []

    for line in lyrics.splitlines():
        if line.startswith(("[0", "[1", "[2", "[3")):
            # Match timestamp in square brackets (e.g., [01:15.35])
            regex = re.compile(r'\[.+?\]')
            match = re.match(regex, line)

            if not match:
                continue  # Skip lines with no timestamp

            # Extract and clean the timestamp
            _time = match.group(0)[1:-1]  # Remove square brackets
            line = regex.sub('', line).strip()  # Remove timestamp from the line

            if not line:  # Skip if the line is empty after removing the timestamp
                continue

            # Convert the timestamp to milliseconds
            try:
                time_parts = _time.split(':')
                minutes = int(time_parts[0])
                seconds = float(time_parts[1])
                milliseconds = int((minutes * 60 + seconds) * 1000)

                timeline.append(milliseconds)
                lrc.append(line)
            except (ValueError, IndexError) as e:
                _LOGGER.warning("Invalid timestamp format: %s", _time)
                continue

    return timeline, lrc


async def update_lyrics_entities(hass: HomeAssistant, previous_line: str, current_line: str, next_line: str, entry_id: str = None):
    """Update the text entities with the current lyrics lines."""
    if not entry_id:
        _LOGGER.error("No entry_id provided for lyrics update")
        return
    
    # Get the lyrics entities for this device
    lyrics_entities = get_device_lyrics_entities(hass, entry_id)
    
    if not lyrics_entities:
        _LOGGER.error("Could not find lyrics entities for entry_id: %s", entry_id)
        return
    
    # Verify all entities exist and are available
    missing_entities = []
    for line_name, entity_id in lyrics_entities.items():
        state = hass.states.get(entity_id)
        if not state:
            missing_entities.append(entity_id)
        elif state.state == "unavailable":
            _LOGGER.warning("Lyrics entity %s is unavailable", entity_id)
    
    if missing_entities:
        _LOGGER.error("Lyrics entities missing: %s", missing_entities)
        return
    
    # Update the entities
    try:
        await hass.services.async_call("text", "set_value", {
            "entity_id": lyrics_entities["line1"], 
            "value": previous_line
        })
        await hass.services.async_call("text", "set_value", {
            "entity_id": lyrics_entities["line2"], 
            "value": current_line
        })
        await hass.services.async_call("text", "set_value", {
            "entity_id": lyrics_entities["line3"], 
            "value": next_line
        })
        
        _LOGGER.debug("Successfully updated lyrics for entry_id: %s", entry_id)
        
    except Exception as e:
        _LOGGER.error("Error updating lyrics entities for entry_id %s: %s", entry_id, e)


def clean_track_name(track):
    """Improved function to clean up track names."""
    if not track:
        return ""
    
    original_track = track

    _LOGGER.info("Pre-cleaned up track = %s", track)
    
    # 1. Handle nested brackets by recursively removing them from the outermost in
    while re.search(r'\s*[\(\[\{\<].*?[\)\]\}\>]', track):
        track = re.sub(r'\s*[\(\[\{\<].*?[\)\]\}\>]', '', track)
    
    # 2. Remove dates in various formats (1999, '99, etc.)
    track = re.sub(r'\b\d{4}\b', '', track)
    track = re.sub(r'\b\'\d{2}\b', '', track)
    
    # 3. More careful handling of dashes - only split on dashes surrounded by spaces
    # or dashes followed by specific version-related words
    dash_pattern = r'\s+-\s+|\s*-\s*(?:remaster|version|edit|mix|single|live|from)\b'
    parts = re.split(dash_pattern, track, flags=re.IGNORECASE)
    track = parts[0]
    
    # 4. Remove common phrases
    common_phrases = [
        r'\b(?:from|on)\s+(?:the\s+)?(?:"[^"]*"|\'[^\']*\'|\S+)?\s*(?:soundtrack|album|movie|film|series|show)\b',
        r'\b(?:original|movie|film|radio|single|album|instrumental|acoustic|live|studio|extended|shortened)\s+(?:version|edit|mix|cut|recording)\b',
        r'\b(?:remaster(?:ed)?|remix(?:ed)?|feat\.?|ft\.?|featuring)\b',
        r'\b(?:bonus\s+track|deluxe\s+edition|digital\s+exclusive)\b',
        r'\b(?:explicit|clean)\s+(?:version|edit)?\b',
        r'\d+(?:th|st|nd|rd)?\s+(?:anniversary|edition)\b',
        r'\b(?:anthology|world\s+wildlife\s+fund)\s+(?:version)?\b'
    ]
    
    for phrase in common_phrases:
        track = re.sub(phrase, '', track, flags=re.IGNORECASE)
    
    # 5. Remove non-Latin characters while preserving accented characters
    track = re.sub(r'[^\x00-\x7F\xC0-\xFF\u2000-\u206F]', '', track)
    
    # 6. Normalize quotes and apostrophes
    track = track.replace("'", "'").replace(""", '"').replace(""", '"').replace("´", "'").replace("`", "'")
    
    # 7. Replace multiple spaces with a single space
    track = re.sub(r'\s+', ' ', track)
    
    # 8. Trim whitespace and remove trailing punctuation
    track = track.strip()
    track = re.sub(r'[.,;:!?]+$', '', track).strip()
    
    # 9. Check if we've removed everything - if so, return at least part of the original
    if len(track) < 2 and len(original_track) > 0:
        # Try to extract any word characters from the original
        words = re.findall(r'\b[A-Za-z]+\b', original_track)
        if words:
            return ' '.join(words)
        # If no words, return the original but cleaned of special characters
        return re.sub(r'[^\w\s]', '', original_track).strip()
    
    _LOGGER.info("Cleaned up track = %s", track)
    
    return track


def get_media_player_info(hass: HomeAssistant, entity_id: str, entry_id: str = None):
    """Retrieve track, artist, media position, and last update time from media player."""
    player_state = hass.states.get(entity_id)

    if not player_state:
        _LOGGER.error("Get Media Info: Media player entity not found (device: %s).", entry_id)
        #hass.async_create_task(update_lyrics_entities(hass, "Media player entity not found", "", "", entry_id))
        return None, None, None, None  # Return empty values

    if player_state.state != "playing":
        _LOGGER.info("Get Media Info: Media player is not playing. Waiting... (device: %s)", entry_id)
        #hass.async_create_task(update_lyrics_entities(hass, "Waiting for playback to start", "", "", entry_id))
        return None, None, None, None

    track = clean_track_name(player_state.attributes.get("media_title", ""))
    artist = player_state.attributes.get("media_artist", "")
    pos = player_state.attributes.get("media_position")
    updated_at = player_state.attributes.get("media_position_updated_at")

    if not track or not artist:
        _LOGGER.warning("Get Media Info: Missing track or artist information (device: %s).", entry_id)
        #hass.async_create_task(update_lyrics_entities(hass, "Missing track or artist", "", "", entry_id))
        return None, None, None, None

    return track, artist, pos, updated_at


async def fetch_lyrics_for_track(hass: HomeAssistant, track: str, artist: str, pos, updated_at, entity_id, audiofingerprint, entry_id: str = None):
    """Fetch lyrics for a given track and synchronize with playback."""
    global _INTEGRATION_JUST_STARTED
    
    device_data = get_device_data(hass, entry_id)
    
    _LOGGER.info("Fetch: Fetching lyrics for: %s %s (device: %s)", artist, track, entry_id)
    _LOGGER.info("Fetch: pos=%s, updated_at=%s, audiofingerprint=%s (device: %s)", pos, updated_at, audiofingerprint, entry_id)

    # Check if this is a radio station and not from audio fingerprinting
    player_state = hass.states.get(entity_id)
    current_media_id = player_state.attributes.get("media_content_id", "") if player_state else ""
    
    if current_media_id.startswith("library://radio") and not audiofingerprint:
        #_LOGGER.info("Fetch: Radio station detected, skipping automatic lyrics fetch. Use audio tagging to identify specific songs (device: %s)", entry_id)
        #await update_lyrics_entities(hass, "", "Radio playing - use tagging to identify songs", "", entry_id)
        return

    # Reset the current display first to show we're working on it
    await update_lyrics_entities(hass, "", "Searching for lyrics...", "", entry_id)

    # Handle first track after startup - ignore position data
    if _INTEGRATION_JUST_STARTED:
        _LOGGER.info("Fetch: First track after startup - ignoring position data (device: %s)", entry_id)
        pos = None
        updated_at = None
        _INTEGRATION_JUST_STARTED = False

    # Get current track info
    current_track = player_state.attributes.get("media_title", "") if player_state else ""
    current_artist = player_state.attributes.get("media_artist", "") if player_state else ""
    
    # Always stop existing lyrics if this is a fingerprint-based identification
    # This allows for correction of misidentified tracks
    should_stop_existing = True
    
    # For non-fingerprint calls, check if we already have lyrics running for this track
    active_lyrics_sync = device_data.get(DEVICE_DATA_LYRICS_SYNC)
    if not audiofingerprint and active_lyrics_sync and active_lyrics_sync.active:
        # Check if we're already displaying lyrics for this track/artist
        if (active_lyrics_sync.media_tracker and 
            active_lyrics_sync.media_tracker.current_track == current_track and
            active_lyrics_sync.media_tracker.current_artist == current_artist):
            _LOGGER.info("Fetch: Already displaying lyrics for this track. Skipping (device: %s).", entry_id)
            should_stop_existing = False
            return
    
    # Stop any existing lyrics synchronization if needed
    if should_stop_existing and active_lyrics_sync and active_lyrics_sync.active:
        _LOGGER.info("Fetch: Stopping current lyrics session for new request (device: %s).", entry_id)
        await active_lyrics_sync.stop()
    
    # Update the last media content ID
    device_data[DEVICE_DATA_LAST_MEDIA_CONTENT_ID] = current_media_id
    
    _LOGGER.info("Fetch: Start new session (device: %s)", entry_id)
    
    # Load lyrics provider
    lyrics_provider = [lrc_kit.QQProvider]
    provider = lrc_kit.ComboLyricsProvider(lyrics_provider)
    
    # Try with the combined artist name first
    _LOGGER.info("Fetch: Searching for lyrics with combined artist name (device: %s).", entry_id)
    search_request = await hass.async_add_executor_job(lrc_kit.SearchRequest, artist, track)
    lyrics_result = await hass.async_add_executor_job(provider.search, search_request)
    
    # If no lyrics found and artist contains separators, try with individual artists
    if not lyrics_result:
        # Define common artist separators
        separators = ["/", "|", "&", ",", " and ", " with ", " feat ", " feat. ", " ft ", " ft. ", " featuring "]
        
        # Check if any separator is in the artist name
        contains_separator = any(sep in artist for sep in separators)
        
        if contains_separator:
            _LOGGER.info("Fetch: No lyrics found with combined artist name. Trying individual artists (device: %s).", entry_id)
            
            # Split the artist string using multiple possible separators
            individual_artists = artist
            for sep in separators:
                if sep in individual_artists:
                    individual_artists = individual_artists.replace(sep, "|")  # Normalize to one separator
            
            # Split by the normalized separator and strip whitespace
            artist_list = [a.strip() for a in individual_artists.split("|") if a.strip()]
            
            # Try each individual artist
            for single_artist in artist_list:
                _LOGGER.info("Fetch: Trying with artist: %s (device: %s)", single_artist, entry_id)
                search_request = await hass.async_add_executor_job(lrc_kit.SearchRequest, single_artist, track)
                lyrics_result = await hass.async_add_executor_job(provider.search, search_request)
                
                if lyrics_result:
                    _LOGGER.info("Fetch: Lyrics found with artist: %s (device: %s)", single_artist, entry_id)
                    break
    
    # If still no lyrics found
    if not lyrics_result:
        _LOGGER.warning("Fetch: No lyrics found for '%s' (device: %s).", track, entry_id)
        await update_lyrics_entities(hass, "", "No lyrics found", "", entry_id)
        return

    _LOGGER.info("Fetch: Processing lyrics into timeline (device: %s)", entry_id)
    timeline, lrc = lyricSplit(str(lyrics_result))

    if not timeline:
        _LOGGER.error("Fetch: Lyrics have no timeline (device: %s).", entry_id)
        await update_lyrics_entities(hass, "", "Lyrics not synced", "", entry_id)
        return
        
    # Debug information
    _LOGGER.info("Fetch: Found %d lines of lyrics (device: %s)", len(lrc), entry_id)
    if len(lrc) > 0:
        _LOGGER.info("Fetch: First line: %s (device: %s)", lrc[0], entry_id)
        _LOGGER.info("Fetch: Last line: %s (device: %s)", lrc[-1], entry_id)

    # Create lyrics synchronizer if it doesn't exist
    if not device_data.get(DEVICE_DATA_LYRICS_SYNC):
        device_data[DEVICE_DATA_LYRICS_SYNC] = LyricsSynchronizer(hass, entry_id)
    
    # Start synchronized lyrics display, passing the audiofingerprint flag
    await device_data[DEVICE_DATA_LYRICS_SYNC].start(entity_id, timeline, lrc, pos, updated_at, audiofingerprint)


async def trigger_lyrics_lookup(hass: HomeAssistant, title: str, artist: str, play_offset_ms: int, process_begin: str, entry_id=None):
    """Trigger lyrics lookup based on a recognized song."""

    if not title or not artist:
        _LOGGER.warning("Trigger Lyrics: Cannot trigger lyrics lookup: Missing title or artist (device: %s).", entry_id)
        return

    _LOGGER.info("Trigger Lyrics (from tagging) -> Artist: %s Title: %s, Entry ID: %s", artist, title, entry_id)

    # Get the configured media player entity ID
    from .tagging import get_tagging_config
    conf = get_tagging_config(hass, entry_id)
    if not conf:
        _LOGGER.error("No configuration found for entry_id: %s", entry_id)
        return
        
    media_player = conf["media_player"]

    clean_track = clean_track_name(title)
    await fetch_lyrics_for_track(hass, clean_track, artist, play_offset_ms/1000, process_begin, media_player, True, entry_id)


async def handle_fetch_lyrics(hass: HomeAssistant, call: ServiceCall):
    """Main service handler: gets media info and fetches lyrics."""
    entity_id = call.data.get("entity_id")
    
    # Find which Music Companion device this media player belongs to
    entry_id = find_entry_id_for_media_player(hass, entity_id)
    
    if not entry_id:
        _LOGGER.error("Cannot process lyrics request - media player %s is not configured in any Music Companion device", entity_id)
        return
    
    # Verify we can find the lyrics entities for this device
    lyrics_entities = get_device_lyrics_entities(hass, entry_id)
    
    if not lyrics_entities:
        _LOGGER.error("Cannot process lyrics request - no lyrics entities found for entry_id: %s", entry_id)
        return
    
    _LOGGER.info("Processing lyrics request for entry_id: %s, media_player: %s, lyrics_entities: %s", 
                entry_id, entity_id, list(lyrics_entities.values()))
    
    # Define the monitoring function first
    async def monitor_playback_event(event):
        """Monitor media player state changes."""
        entity = event.data.get('entity_id')
        old_state = event.data.get('old_state')
        new_state = event.data.get('new_state')
        
        device_data = get_device_data(hass, entry_id)

        _LOGGER.debug("Monitor Playback: Media player state changed: %s -> %s (device: %s)", 
                     old_state.state if old_state else "None", new_state.state, entry_id)

        media_content_id = hass.states.get(entity).attributes.get("media_content_id", "")

        # Ignore updates if the state remains unchanged (e.g., volume changes)
        if old_state and new_state and old_state.state == new_state.state:
            if old_state.attributes.get("media_content_id") == media_content_id:
                _LOGGER.debug("Monitor Playback: State unchanged and media_content_id unchanged. Ignoring attribute-only update (device: %s).", entry_id)
                return
            
        # Only act if the player changes to 'playing' and it's not a radio station
        if new_state.state == "playing" and not media_content_id.startswith("library://radio"):
            
            last_media_content_id = device_data.get(DEVICE_DATA_LAST_MEDIA_CONTENT_ID)
            _LOGGER.debug("Monitor Playback: LAST_MEDIA_CONTENT_ID: %s (device: %s)", last_media_content_id, entry_id)
            _LOGGER.debug("Monitor Playback: media_content_id: %s (device: %s)", media_content_id, entry_id)

            # Check if the media_content_id is different from the last one processed
            if media_content_id and media_content_id != last_media_content_id:
                _LOGGER.info("Monitor Playback: Content has changed, not a radio station (device: %s).", entry_id)
                
                # Stop any existing lyrics display
                active_lyrics_sync = device_data.get(DEVICE_DATA_LYRICS_SYNC)
                if active_lyrics_sync and active_lyrics_sync.active:
                    await active_lyrics_sync.stop()
                
                await update_lyrics_entities(hass, "", "", "", entry_id)
                track, artist, pos, updated_at = get_media_player_info(hass, entity, entry_id)
                _LOGGER.info("Monitor Playback: New Info -> Artist %s, Track %s, media_content_id %s (device: %s)", 
                            artist, track, media_content_id, entry_id)
                _LOGGER.info("Monitor Playback: New Info -> pos %s, updated_at %s (device: %s)", pos, updated_at, entry_id)

                # Call the lyrics function and update the last processed ID
                if track and artist:
                    _LOGGER.debug("Monitor Playback: Fetching lyrics for new track (device: %s)", entry_id)
                    device_data[DEVICE_DATA_LAST_MEDIA_CONTENT_ID] = media_content_id
                    hass.async_create_task(fetch_lyrics_for_track(hass, track, artist, pos, updated_at, entity, False, entry_id))
            else:
                _LOGGER.info("Monitor Playback: Track already processed. Skipping lyrics fetch (device: %s).", entry_id)
        # Playing, radio - Show message instead of fetching lyrics
        elif new_state.state == "playing" and media_content_id.startswith("library://radio"):
            device_data[DEVICE_DATA_LAST_MEDIA_CONTENT_ID] = media_content_id
            _LOGGER.info("Monitor Playback: Radio station detected - not fetching lyrics automatically (device: %s).", entry_id)
            
            # Stop any existing lyrics display
            active_lyrics_sync = device_data.get(DEVICE_DATA_LYRICS_SYNC)
            if active_lyrics_sync and active_lyrics_sync.active:
                await active_lyrics_sync.stop()
                
            # Show radio message instead of empty display
            #await update_lyrics_entities(hass, "", "Radio playing - use tagging to identify songs", "", entry_id)
        else:
            # Not playing, but lyrics display will be handled by MediaTracker
            _LOGGER.info("Monitor Playback: Media player is not playing (device: %s).", entry_id)

    # Register listener for state change events FIRST - before checking current state
    async_track_state_change_event(hass, [entity_id], monitor_playback_event)
    _LOGGER.debug("Registered state change listener for: %s (device: %s)", entity_id, entry_id)
    
    # Now check current state and fetch lyrics if something is already playing
    track, artist, pos, updated_at = get_media_player_info(hass, entity_id, entry_id)
    
    if track and artist:
        _LOGGER.info("Music already playing on startup - fetching lyrics immediately")
        # Fetch and display lyrics for currently playing track
        await fetch_lyrics_for_track(hass, track, artist, pos, updated_at, entity_id, False, entry_id)
    else:
        _LOGGER.info("No music currently playing - waiting for playback to start (monitoring enabled)")


async def async_setup_lyrics_service(hass: HomeAssistant):
    """Register the fetch_lyrics service."""
    _LOGGER.debug("Registering the fetch_lyrics service.")

    async def async_wrapper(call):
        await handle_fetch_lyrics(hass, call)

    hass.services.async_register(
        DOMAIN,  # Use DOMAIN constant instead of hardcoded string
        "fetch_lyrics",
        async_wrapper,
        schema=SERVICE_FETCH_LYRICS_SCHEMA
    )

    _LOGGER.info("fetch_lyrics service registered successfully.")