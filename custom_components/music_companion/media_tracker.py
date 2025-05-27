import logging
import datetime
import asyncio
from typing import Optional, Callable
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)

class MediaTracker:
    """Tracks media player state and handles position synchronization."""
    
    def __init__(self, hass: HomeAssistant, entity_id: str, 
                 position_callback=None, track_change_callback=None,
                 is_radio_source=False, entry_id: str = None):
        """Initialize the MediaTracker.
        
        Args:
            hass: HomeAssistant instance
            entity_id: Media player entity ID to monitor
            position_callback: Called when position updates with current timecode
            track_change_callback: Called when track changes or seek detected
            is_radio_source: True if source is radio/fingerprinted audio
            entry_id: Device entry ID for logging purposes
        """
        self.hass = hass
        self.entity_id = entity_id
        self.position_callback = position_callback
        self.track_change_callback = track_change_callback
        self.is_radio_source = is_radio_source  # Flag for radio sources
        self.entry_id = entry_id  # Device identifier for logging
        
        # Media state tracking
        self.current_track = ""
        self.current_artist = ""
        self.media_content_id = ""
        self.state = "idle"
        
        # Position tracking
        self.media_position = None
        self.position_updated_at = None
        self.last_calculated_position = 0
        
        # Control flags
        self.tracking_active = False
        self.monitor_task = None
        self.position_update_interval = 0.1  # seconds
        self.seek_threshold = 6.0  # seconds - difference that indicates a seek operation
        
        # Always assume we're ready to display lyrics (no stabilization)
        self.initialization_complete = True
        
        # Pause handling
        self.pause_start_time = None
        self.paused_duration = 0  # Total time spent paused
    
    async def start_tracking(self):
        """Start tracking the media player state."""
        if self.tracking_active:
            return
            
        self.tracking_active = True
        self.update_from_state()
        self.monitor_task = asyncio.create_task(self._position_monitor_loop())
        
        # Register for state change events using the new method
        self._state_listener = async_track_state_change_event(
            self.hass,
            [self.entity_id],
            self._handle_state_change
        )
        
        _LOGGER.info("MediaTracker: Started tracking %s (radio source: %s, device: %s)", 
                    self.entity_id, self.is_radio_source, self.entry_id)
    
    async def stop_tracking(self):
        """Stop tracking the media player state."""
        self.tracking_active = False
        
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            self.monitor_task = None
            
        _LOGGER.info("MediaTracker: Stopped tracking %s (device: %s)", self.entity_id, self.entry_id)
    
    def update_from_state(self) -> bool:
        """Update tracker state from media player entity state.
        Returns True if significant state change detected.
        """
        player_state = self.hass.states.get(self.entity_id)
        if not player_state:
            _LOGGER.error("MediaTracker: Media player entity not found: %s (device: %s)", self.entity_id, self.entry_id)
            return False
            
        # Get current state attributes
        new_state = player_state.state
        attrs = player_state.attributes
        
        new_track = attrs.get("media_title", "")
        new_artist = attrs.get("media_artist", "")
        new_media_id = attrs.get("media_content_id", "")
        new_position = attrs.get("media_position")
        new_position_updated = attrs.get("media_position_updated_at")
        
        # Detect track changes
        track_changed = (
            (new_track != self.current_track) or 
            (new_artist != self.current_artist) or
            (new_media_id != self.media_content_id)
        )
        
        # Detect state changes (play/pause/stop)
        state_changed = new_state != self.state
        
        # Handle pausing/resuming
        if state_changed:
            if new_state == "paused" and self.state == "playing":
                self.pause_start_time = datetime.datetime.now(datetime.timezone.utc)
                _LOGGER.debug("MediaTracker: Playback paused at %s (device: %s)", self.pause_start_time, self.entry_id)
            
            elif new_state == "playing" and self.state == "paused":
                if self.pause_start_time:
                    pause_duration = (datetime.datetime.now(datetime.timezone.utc) - 
                                     self.pause_start_time).total_seconds()
                    self.paused_duration += pause_duration
                    
                    # If not a radio source, adjust the position_updated_at time
                    # For radio sources, we continue from where we left off
                    if not self.is_radio_source and self.position_updated_at:
                        if isinstance(self.position_updated_at, str):
                            self.position_updated_at = datetime.datetime.fromisoformat(self.position_updated_at)
                        
                        self.position_updated_at += datetime.timedelta(seconds=pause_duration)
                        _LOGGER.debug("MediaTracker: Adjusted position_updated_at by %s seconds after pause (device: %s)", 
                                     pause_duration, self.entry_id)
        
        # For position changes, only update if not a radio source
        position_changed = False
        if not self.is_radio_source and new_position is not None and self.media_position is not None and new_position != self.media_position:
            position_changed = True
            _LOGGER.debug("MediaTracker: Position changed from %.2f to %.2f (device: %s)", self.media_position, new_position, self.entry_id)
        
        # Update current state
        self.state = new_state
        self.current_track = new_track
        self.current_artist = new_artist
        self.media_content_id = new_media_id
        
        # Update position info when available (only for non-radio sources)
        if not self.is_radio_source and new_position is not None:
            old_position = self.media_position
            self.media_position = new_position
            self.position_updated_at = new_position_updated
            
            # If significant position change, treat as a seek for resyncing lyrics
            if old_position is not None and abs(new_position - old_position) > 2.0:
                position_changed = True
        
        return track_changed or position_changed

    def set_initial_position(self, position, updated_at):
        """Set the initial position and timestamp.
        This works for both radio and non-radio sources.
        """
        self.media_position = position
        self.position_updated_at = updated_at
        self.paused_duration = 0  # Reset paused duration
        _LOGGER.info("MediaTracker: Set initial position to %.2f (device: %s)", position, self.entry_id)

    def calculate_current_position(self) -> float:
        """Calculate the current media position based on last known position and time elapsed."""
        if self.media_position is None or self.position_updated_at is None:
            return 0.0
            
        if self.state != "playing":
            return self.media_position
        
        # Convert position_updated_at to datetime if it's a string
        if isinstance(self.position_updated_at, str):
            try:
                last_update_time = datetime.datetime.fromisoformat(self.position_updated_at)
            except ValueError:
                _LOGGER.error("MediaTracker: Error parsing position_updated_at timestamp: %s (device: %s)", 
                             self.position_updated_at, self.entry_id)
                return self.last_calculated_position
        else:
            last_update_time = self.position_updated_at
        
        # Calculate elapsed time since position update
        current_time = datetime.datetime.now(datetime.timezone.utc)
        elapsed_time = (current_time - last_update_time).total_seconds()
        
        # For radio sources, add a small acceleration factor to catch up if we started late
        # This helps compensate for delays in identification and processing
        acceleration = 1.0
        if self.is_radio_source and elapsed_time > 2.0:
            # Gradually increase acceleration based on elapsed time, up to 1.05x
            acceleration = min(1.05, 1.0 + (elapsed_time / 200.0))
            
        # Calculate current position with optional acceleration
        current_position = round(self.media_position + (elapsed_time * acceleration), 2)
        self.last_calculated_position = current_position
        
        return current_position
    
    async def _position_monitor_loop(self):
        """Continuously monitor media position and update lyrics."""
        try:
            update_count = 0
            while self.tracking_active:
                if self.state == "playing" and self.media_position is not None:
                    current_position = self.calculate_current_position()
                    update_count += 1
                    
                    # Call the update callback with current position
                    if self.position_callback:
                        self.position_callback(current_position)
                    
                    # Occasionally log the current position for debugging
                    if update_count % 100 == 0:
                        _LOGGER.debug("MediaTracker: Current position: %.2f seconds (device: %s)", current_position, self.entry_id)
                
                await asyncio.sleep(self.position_update_interval)
        except asyncio.CancelledError:
            _LOGGER.debug("MediaTracker: Position monitor loop cancelled (device: %s)", self.entry_id)
            raise
        except Exception as e:
            _LOGGER.error("MediaTracker: Error in position monitor loop (device: %s): %s", self.entry_id, str(e))
    
    async def _handle_state_change(self, event):
        """Handle media player state changes."""
        entity_id = event.data.get('entity_id')
        old_state = event.data.get('old_state')
        new_state = event.data.get('new_state')
        
        _LOGGER.debug("MediaTracker: State change detected for %s (device: %s)", entity_id, self.entry_id)
        
        # Handle case when state is None
        if not new_state:
            return
        
        # Get player attributes
        attrs = new_state.attributes if new_state else {}
        old_attrs = old_state.attributes if old_state else {}
        
        # Get media IDs to check for actual track changes
        old_media_id = old_attrs.get("media_content_id", "")
        new_media_id = attrs.get("media_content_id", "")
        
        # First update state to detect changes
        state_changed = self.update_from_state()
        
        # Differentiate between track changes and position changes
        if state_changed:
            # Only treat it as a track change if the media_content_id changed or 
            # track/artist changed while media_id is the same
            if new_media_id != old_media_id:
                _LOGGER.info("MediaTracker: Media content ID changed - treating as track change (device: %s)", self.entry_id)
                if self.track_change_callback:
                    self.track_change_callback(True)  # True = actual track change
            else:
                # This is just a position change, trigger a resync
                _LOGGER.debug("MediaTracker: Position changed but same track - resyncing (device: %s)", self.entry_id)
                if self.track_change_callback:
                    self.track_change_callback(False)  # False = just a position change