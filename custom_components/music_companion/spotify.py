import json
import logging
import urllib.parse
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
import voluptuous as vol
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    SPOTIFY_AUTH_CALLBACK_PATH,
    SPOTIFY_STORAGE_VERSION,
    SPOTIFY_STORAGE_KEY,
    SPOTIFY_SCOPE,
    DEFAULT_SPOTIFY_PLAYLIST_NAME
)

_LOGGER = logging.getLogger(__name__)

# Configuration schema
SPOTIFY_CONFIG_SCHEMA = vol.Schema({
    vol.Required("client_id"): cv.string,
    vol.Required("client_secret"): cv.string,
    vol.Optional("playlist_id"): cv.string,
    vol.Optional("create_playlist", default=True): cv.boolean,
    vol.Optional("playlist_name", default=DEFAULT_SPOTIFY_PLAYLIST_NAME): cv.string,
})

# Schema for the add_to_spotify service call
SERVICE_ADD_TO_SPOTIFY_SCHEMA = vol.Schema({
    vol.Optional("title"): cv.string,
    vol.Optional("artist"): cv.string,
    vol.Optional("spotify_id"): cv.string
})

class SpotifyAuthView(HomeAssistantView):
    """Handle Spotify authentication callbacks."""
    url = SPOTIFY_AUTH_CALLBACK_PATH
    name = f"api:{DOMAIN}:spotify_callback"
    requires_auth = False

    def __init__(self, hass):
        """Initialize the Spotify auth callback view."""
        self.hass = hass

    async def get(self, request):
        """Handle Spotify auth callback requests."""
        try:
            # Extract query parameters
            code = request.query.get("code")
            error = request.query.get("error")
            
            # Log all incoming parameters for debugging
            _LOGGER.warning(f"Spotify Callback Received - Code: {code}, Error: {error}")
            
            # Handle error scenario
            if error:
                _LOGGER.error(f"Spotify authentication error: {error}")
                return aiohttp.web.Response(
                    text=f"<html><body><h1>Authentication Error</h1><p>{error}</p></body></html>",
                    content_type="text/html",
                    status=400
                )
            
            # Ensure code is present
            if not code:
                _LOGGER.error("No authorization code received")
                return aiohttp.web.Response(
                    text="<html><body><h1>Error</h1><p>No authorization code received</p></body></html>",
                    content_type="text/html",
                    status=400
                )
            
            # Retrieve Spotify service
            spotify_service = self.hass.data.get("spotify_service")
            if not spotify_service:
                _LOGGER.error("Spotify service not initialized")
                return aiohttp.web.Response(
                    text="<html><body><h1>Setup Error</h1><p>Spotify service not initialized</p></body></html>",
                    content_type="text/html",
                    status=500
                )
            
            # Attempt to exchange code
            success = await spotify_service.exchange_code(code)
            
            if success:
                return aiohttp.web.Response(
                    text="<html><body><h1>Authentication Successful</h1><p>You can close this window now</p></body></html>",
                    content_type="text/html",
                    status=200
                )
            else:
                _LOGGER.error("Failed to exchange authorization code")
                return aiohttp.web.Response(
                    text="<html><body><h1>Authentication Failed</h1><p>Unable to complete Spotify authorization</p></body></html>",
                    content_type="text/html",
                    status=500
                )
        
        except Exception as e:
            # Catch-all for any unexpected errors
            _LOGGER.exception(f"Unexpected error in Spotify auth callback: {e}")
            return aiohttp.web.Response(
                text=f"<html><body><h1>Unexpected Error</h1><p>{str(e)}</p></body></html>",
                content_type="text/html",
                status=500
            )
        
class SpotifyService:
    """Service to add tracks to Spotify playlists."""
    def __init__(self, hass: HomeAssistant, config):
        """Initialize the Spotify service."""
        self.hass = hass
        self.client_id = config["client_id"]
        self.client_secret = config["client_secret"]
        self.playlist_id = config.get("playlist_id")
        self.create_playlist = config.get("create_playlist", True)
        self.playlist_name = config.get("playlist_name", DEFAULT_SPOTIFY_PLAYLIST_NAME)
        self.session = async_get_clientsession(hass)
        self.user_id = None
        self.authorized = False
        
        # Set up storage for tokens
        self.store = Store(hass, SPOTIFY_STORAGE_VERSION, f"{DOMAIN}_{SPOTIFY_STORAGE_KEY}")
        self.access_token = None
        self.refresh_token = None
        self.expires_at = 0
    
    async def async_setup(self):
        """Set up the Spotify service."""
        await self.load_tokens()
        
        # If we have a refresh token, try to use it
        if self.refresh_token:
            await self.refresh_access_token()
        
        # Set up authentication callback
        self.hass.http.register_view(SpotifyAuthView(self.hass))
        
        # Log current playlist ID status - but don't try to create one yet
        _LOGGER.debug(f"Current Playlist ID: {self.playlist_id}")
        if not self.playlist_id:
            _LOGGER.info("No playlist ID configured - will create or find one when needed")
        else:
            _LOGGER.info(f"Using existing playlist: {self.playlist_id}")
            
        return self.authorized
    
    async def load_tokens(self):
        """Load tokens and playlist ID from storage."""
        data = await self.store.async_load()
        if data:
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self.expires_at = data.get("expires_at", 0)
            self.user_id = data.get("user_id")
            self.playlist_id = data.get("playlist_id")  # Load playlist ID

    async def save_tokens(self):
        """Save tokens and playlist ID to storage."""
        data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
            "playlist_id": self.playlist_id,  # Ensure playlist ID is saved
        }
        await self.store.async_save(data)
    
    def get_authorize_url(self):
        """Get the authorization URL for Spotify."""
        # Hardcoded base URL as specified
        base_url = 'http://homeassistant.local:8123/'
        
        # Ensure base_url does not end with a slash
        base_url = base_url.rstrip('/')
        
        # Construct the full redirect URI
        redirect_uri = f"{base_url}{SPOTIFY_AUTH_CALLBACK_PATH}"
        
        # Construct the authorization URL parameters
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_SCOPE,
            "show_dialog": "true",
        }
        
        # Generate the full authorization URL
        auth_url = f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"
        
        # Log the generated URL for debugging
        _LOGGER.info(f"Generated Spotify Authorization URL: {auth_url}")
        _LOGGER.info(f"Redirect URI used: {redirect_uri}")
        
        return auth_url
    
    async def exchange_code(self, code):
        """Exchange authorization code for tokens."""
        # Hardcoded redirect URI to match the one used in authorization
        redirect_uri = f'http://homeassistant.local:8123{SPOTIFY_AUTH_CALLBACK_PATH}'
        
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        
        try:
            _LOGGER.warning(f"Attempting to exchange code. Redirect URI: {redirect_uri}")
            
            async with self.session.post("https://accounts.spotify.com/api/token", data=payload) as resp:
                # Log the full response for debugging
                resp_text = await resp.text()
                _LOGGER.warning(f"Token exchange response status: {resp.status}")
                _LOGGER.warning(f"Token exchange response body: {resp_text}")
                
                if resp.status != 200:
                    _LOGGER.error(f"Failed to exchange code: {resp.status} - {resp_text}")
                    return False
                
                try:
                    tokens = await resp.json()
                except Exception as json_error:
                    _LOGGER.error(f"Failed to parse JSON response: {json_error}")
                    _LOGGER.error(f"Response text: {resp_text}")
                    return False
                
                # Log token details (be careful with sensitive information)
                _LOGGER.warning("Successfully retrieved tokens")
                
                # Store tokens
                self.access_token = tokens.get("access_token")
                self.refresh_token = tokens.get("refresh_token")
                
                # Calculate expiration time
                expires_in = tokens.get("expires_in", 3600)  # Default to 1 hour if not provided
                self.expires_at = int(self.hass.loop.time()) + expires_in
                
                # Fetch user info
                user_info_success = await self._fetch_user_info()
                
                # Save tokens
                await self.save_tokens()
                
                self.authorized = True
                
                # No playlist creation here - it will be created when needed
                
                return True
        
        except Exception as e:
            _LOGGER.exception(f"Unexpected error in token exchange: {e}")
            return False
    
    async def refresh_access_token(self):
        """Refresh the access token."""
        if not self.refresh_token:
            _LOGGER.error("No refresh token available")
            self.authorized = False
            return False
        
        # Check if token is still valid
        if self.expires_at > int(self.hass.loop.time()) + 300:  # 5 minute margin
            self.authorized = True
            return True
        
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        
        try:
            async with self.session.post("https://accounts.spotify.com/api/token", data=payload) as resp:
                if resp.status != 200:
                    resp_json = await resp.json()
                    _LOGGER.error(f"Failed to refresh token: {resp.status} - {resp_json}")
                    self.authorized = False
                    return False
                
                tokens = await resp.json()
                self.access_token = tokens["access_token"]
                self.expires_at = tokens["expires_in"] + int(self.hass.loop.time())
                
                # Refresh token might be returned
                if "refresh_token" in tokens:
                    self.refresh_token = tokens["refresh_token"]
                
                # Save tokens
                await self.save_tokens()
                
                self.authorized = True
                return True
        except Exception as e:
            _LOGGER.error(f"Error refreshing token: {e}")
            self.authorized = False
            return False
    
    async def _fetch_user_info(self):
        """Fetch user information from Spotify."""
        try:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            async with self.session.get("https://api.spotify.com/v1/me", headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"Failed to fetch user info: {resp.status}")
                    return False
                
                user_info = await resp.json()
                self.user_id = user_info["id"]
                _LOGGER.info(f"Spotify authenticated for user: {self.user_id}")
                return True
        except Exception as e:
            _LOGGER.error(f"Error fetching user info: {e}")
            return False
    
    async def _ensure_playlist_exists(self):
        """Ensure that a playlist exists by checking for an existing one with the same name or creating a new one."""
        # First, refresh access token
        await self.refresh_access_token()
        if not self.authorized:
            _LOGGER.error("Not authorized with Spotify")
            return False

        if self.playlist_id:
            # Check if the playlist ID still exists and is accessible
            try:
                headers = {"Authorization": f"Bearer {self.access_token}"}
                async with self.session.get(f"https://api.spotify.com/v1/playlists/{self.playlist_id}", headers=headers) as resp:
                    if resp.status == 200:
                        _LOGGER.debug(f"Confirmed playlist exists: {self.playlist_id}")
                        # Playlist exists and is accessible
                        return True
                    elif resp.status == 404:
                        _LOGGER.warning(f"Playlist {self.playlist_id} does not exist")
                        # Continue to next step - create or find a playlist
                    else:
                        _LOGGER.error(f"Error checking playlist: HTTP {resp.status}")
                        # Some other error occurred
                        return False
            except Exception as e:
                _LOGGER.error(f"Error checking playlist existence: {e}")
                return False

        # If no valid playlist_id or playlist doesn't exist, try to find an existing one or create a new one
        if self.create_playlist:
            try:
                headers = {
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                }
                
                # First look for an existing playlist with the same name
                _LOGGER.debug(f"Looking for existing playlist with name: {self.playlist_name}")
                found_playlist_id = None
                offset = 0
                limit = 50
                
                while True:
                    async with self.session.get(
                        f"https://api.spotify.com/v1/me/playlists",
                        headers=headers,
                        params={"limit": limit, "offset": offset}
                    ) as resp:
                        if resp.status != 200:
                            _LOGGER.error(f"Failed to fetch playlists: {resp.status}")
                            return False
                        
                        playlists_data = await resp.json()
                        playlists = playlists_data["items"]
                        
                        # Check if a playlist with the same name already exists
                        for playlist in playlists:
                            if playlist["name"] == self.playlist_name:
                                _LOGGER.info(f"Found existing playlist: {self.playlist_name} (ID: {playlist['id']})")
                                found_playlist_id = playlist["id"]
                                break
                        
                        if found_playlist_id or len(playlists) < limit:
                            break
                        
                        offset += limit
                
                # If we found a matching playlist, use it
                if found_playlist_id:
                    self.playlist_id = found_playlist_id
                    # Save the found playlist ID
                    await self.save_tokens()
                    return True
                
                # If we get here, no playlist was found with that name, so create a new one
                _LOGGER.info(f"No existing playlist found with name '{self.playlist_name}', creating a new one")
                payload = {
                    "name": self.playlist_name,
                    "public": False,
                    "description": "Tracks identified by Home Assistant ACR",
                }
                
                async with self.session.post(
                    f"https://api.spotify.com/v1/users/{self.user_id}/playlists",
                    headers=headers,
                    json=payload
                ) as resp:
                    if resp.status not in (200, 201):
                        _LOGGER.error(f"Failed to create playlist: {resp.status}")
                        return False
                    
                    playlist = await resp.json()
                    self.playlist_id = playlist["id"]
                    _LOGGER.info(f"Created new Spotify playlist: {self.playlist_name} (ID: {self.playlist_id})")
                    
                    # Save the new playlist ID
                    await self.save_tokens()
                    
                    # Show notification
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Spotify Playlist Created",
                            "message": f"Created new playlist '{self.playlist_name}' for discovered tracks.",
                            "notification_id": "spotify_playlist_created"
                        }
                    )
                    
                    return True
            except Exception as e:
                _LOGGER.error(f"Error creating/finding playlist: {e}")
                return False
        else:
            _LOGGER.error("No valid playlist ID and playlist creation is disabled")
            return False
    
    async def search_track(self, title, artist):
        """Search for a track and return its Spotify URI, name, artist, and ID."""
        await self.refresh_access_token()
        if not self.authorized:
            _LOGGER.error("Not authorized with Spotify")
            return None, None, None, None
        
        try:
            # Format the search query
            query = f"track:{title} artist:{artist}"
            query_params = {
                "q": query,
                "type": "track",
                "limit": 1
            }
            
            headers = {"Authorization": f"Bearer {self.access_token}"}
            
            async with self.session.get(
                f"https://api.spotify.com/v1/search?{urllib.parse.urlencode(query_params)}",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(f"Failed to search for track: {resp.status}")
                    return None, None, None, None
                
                results = await resp.json()
                
                if results["tracks"]["items"]:
                    track = results["tracks"]["items"][0]
                    return (
                        track["uri"], 
                        track["name"], 
                        track["artists"][0]["name"],
                        track["id"]  # Return the track ID
                    )
                else:
                    _LOGGER.warning(f"No Spotify track found for: {title} - {artist}")
                    return None, None, None, None
        except Exception as e:
            _LOGGER.error(f"Error searching for track: {e}")
            return None, None, None, None
    
    async def check_track_in_playlist(self, track_uri):
        """Check if the track is already in the playlist."""
        await self.refresh_access_token()
        if not self.authorized or not self.playlist_id:
            return False
        
        try:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            
            # Log before checking playlist
            _LOGGER.warning(f"Attempting to verify playlist: {self.playlist_id}")
            
            # First, get the playlist's total tracks
            playlist_url = f"https://api.spotify.com/v1/playlists/{self.playlist_id}"
            _LOGGER.warning(f"Making API call to: {playlist_url}")
            
            async with self.session.get(playlist_url, headers=headers) as resp:
                _LOGGER.warning(f"Playlist check response status: {resp.status}")
                
                if resp.status == 404:
                    _LOGGER.error(f"PLAYLIST DOES NOT EXIST: {self.playlist_id}")
                    return False
                elif resp.status != 200:
                    _LOGGER.error(f"Failed to get playlist info: {resp.status}")
                    return False
                
                # Get response body for detailed inspection
                resp_body = await resp.text()
                _LOGGER.warning(f"Playlist API response first 100 chars: {resp_body[:100]}")
                
                try:
                    playlist_info = json.loads(resp_body)
                    playlist_name = playlist_info.get("name", "Unknown")
                    playlist_owner = playlist_info.get("owner", {}).get("display_name", "Unknown")
                    _LOGGER.warning(f"Playlist verified: '{playlist_name}' owned by '{playlist_owner}'")
                    total_tracks = playlist_info["tracks"]["total"]
                    _LOGGER.warning(f"Playlist contains {total_tracks} tracks")
                except Exception as e:
                    _LOGGER.error(f"Error parsing playlist response: {e}")
                    return False
            
            # Continue with checking if the track is in the playlist
            # [rest of the function remains unchanged]
            
            # Now check if the track is in the playlist
            offset = 0
            limit = 100
            
            while offset < total_tracks:
                params = {
                    "fields": "items(track(uri))",
                    "limit": limit,
                    "offset": offset
                }
                
                async with self.session.get(
                    f"https://api.spotify.com/v1/playlists/{self.playlist_id}/tracks?{urllib.parse.urlencode(params)}",
                    headers=headers
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.error(f"Failed to get playlist tracks: {resp.status}")
                        return False
                    
                    tracks_data = await resp.json()
                    
                    # Check if the track is in this batch
                    track_uris = [item["track"]["uri"] for item in tracks_data["items"] if item["track"]]
                    if track_uri in track_uris:
                        return True
                    
                    offset += limit
            
            # Track not found in playlist
            return False
        except Exception as e:
            _LOGGER.error(f"Error checking track in playlist: {e}")
            return False
    
    async def add_track_to_playlist(self, title, artist, spotify_id=None):
        """Add a track to the specified playlist."""

        _LOGGER.warning(f"Adding track to playlist: {title} - {artist}, Spotify ID: {spotify_id}")

        if not self.authorized:
            _LOGGER.warning("Not authorized with Spotify, generating auth URL")

            # Show notification for user to authorize
            auth_url = self.get_authorize_url()
            _LOGGER.warning(f"Auth URL: {auth_url}")
            
            # Get the notification message
            message = f"Spotify authorization required to add tracks to playlists. " \
                    f"[Click here to authorize]({auth_url})"
            
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Authorization Required",
                    "message": message,
                    "notification_id": "spotify_auth_required"
                }
            )
            return False
        
        # Ensure we have a valid playlist - this will check for existing playlist or create one if needed
        if not await self._ensure_playlist_exists():
            _LOGGER.error("Failed to ensure playlist exists")
            return False
        
        # Try to use Spotify ID first
        if spotify_id:
            track_uri = f"spotify:track:{spotify_id}"
            spotify_title = title
            spotify_artist = artist
        else:
            # Fallback to search if no Spotify ID
            search_result = await self.search_track(title, artist)
            if not search_result or not search_result[0]:
                # Show notification
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Spotify Track Not Found",
                        "message": f"Could not find '{title}' by {artist} on Spotify.",
                        "notification_id": "spotify_track_status"
                    }
                )
                return False
            
            track_uri, spotify_title, spotify_artist, spotify_track_id = search_result
        
        # Check if track is already in playlist
        in_playlist = await self.check_track_in_playlist(track_uri)
        if in_playlist:
            # Show notification
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Track Already Saved",
                    "message": f"The track '{spotify_title}' by {spotify_artist} is already in your playlist.",
                    "notification_id": "spotify_track_status"
                }
            )
            return True
        
        # Add track to playlist
        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            
            payload = {"uris": [track_uri]}
            
            async with self.session.post(
                f"https://api.spotify.com/v1/playlists/{self.playlist_id}/tracks",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status not in (200, 201):
                    resp_json = await resp.json()
                    _LOGGER.error(f"Failed to add track to playlist: {resp.status} - {resp_json}")
                    
                    # Show error notification
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Spotify Error",
                            "message": f"Failed to add track to playlist: HTTP {resp.status}",
                            "notification_id": "spotify_track_status"
                        }
                    )
                    return False
                
                # Show success notification
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Added Track to Spotify",
                        "message": f"Successfully added '{spotify_title}' by {spotify_artist} to your Spotify playlist.",
                        "notification_id": "spotify_track_status"
                    }
                )
                return True
        except Exception as e:
            _LOGGER.error(f"Error adding track to playlist: {e}")
            
            # Show error notification
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Error",
                    "message": f"Failed to add track to playlist: {str(e)}",
                    "notification_id": "spotify_track_status"
                }
            )
            return False

    async def add_track_to_playlist_by_uri(self, track_uri):
        """Add a track to the playlist using its Spotify URI."""
        if not self.authorized:
            # Handle authorization similar to add_track_to_playlist method
            _LOGGER.warning("Not authorized with Spotify, generating auth URL")
            auth_url = self.get_authorize_url()
            
            # Get the notification message
            message = f"Spotify authorization required to add tracks to playlists. " \
                    f"[Click here to authorize]({auth_url})"
            
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Authorization Required",
                    "message": message,
                    "notification_id": "spotify_auth_required"
                }
            )
            return False
        
        # Ensure we have a valid playlist
        if not await self._ensure_playlist_exists():
            _LOGGER.error("Failed to ensure playlist exists")
            return False
        
        # Check if track is already in playlist
        in_playlist = await self.check_track_in_playlist(track_uri)
        if in_playlist:
            # Show notification about existing track
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Spotify Track Already Saved",
                    "message": "The track is already in your playlist.",
                    "notification_id": "spotify_track_status"
                }
            )
            return True
        
        # Add track to playlist
        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            
            payload = {"uris": [track_uri]}
            
            async with self.session.post(
                f"https://api.spotify.com/v1/playlists/{self.playlist_id}/tracks",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status not in (200, 201):
                    resp_json = await resp.json()
                    _LOGGER.error(f"Failed to add track to playlist: {resp.status} - {resp_json}")
                    return False
                
                # Show success notification
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Added Track to Spotify",
                        "message": "Successfully added track to your Spotify playlist.",
                        "notification_id": "spotify_track_status"
                    }
                )
                return True
        except Exception as e:
            _LOGGER.error(f"Error adding track to playlist: {e}")
            return False


async def handle_add_to_spotify(call):
    """Handle the service call to add a track to Spotify."""
    hass = call.hass

    _LOGGER.warning("ADD TO SPOTIFY SERVICE CALLED")

    # Try to get Spotify ID first from the call data
    spotify_id = call.data.get("spotify_id")
    title = call.data.get("title")
    artist = call.data.get("artist")
    
    # If no Spotify ID or title/artist, try to get from last tagged song
    if not (spotify_id and title and artist):
        last_song = hass.states.get("sensor.last_tagged_song")
        if last_song and last_song.attributes:
            # Prioritize call data, fall back to sensor attributes
            spotify_id = spotify_id or last_song.attributes.get("spotify_id")
            title = title or last_song.attributes.get("title")
            artist = artist or last_song.attributes.get("artist")
    
    if not title or not artist:
        _LOGGER.error("No title or artist provided")
        _LOGGER.error(f"Received data - Title: {title}, Artist: {artist}, Spotify ID: {spotify_id}")
        return
    
    _LOGGER.info(f"add_to_spotify service called for: {title} - {artist}, Spotify ID: {spotify_id}")
    
    spotify_service = hass.data.get("spotify_service")
    if not spotify_service:
        _LOGGER.error("Spotify service not initialized")
        return
    
    await spotify_service.add_track_to_playlist(title, artist, spotify_id)

async def async_setup_spotify_service(hass, config):
    """Set up the Spotify integration and register service."""
    if "spotify" not in config:
        _LOGGER.info("No Spotify configuration found - skipping setup")
        return
    
    try:
        spotify_config = config["spotify"]
        spotify_service = SpotifyService(hass, spotify_config)
        hass.data["spotify_service"] = spotify_service
        
        # Initialize the service
        await spotify_service.async_setup()
        
        # Register the service
        hass.services.async_register(
            DOMAIN,
            "add_to_spotify",
            handle_add_to_spotify,
            schema=SERVICE_ADD_TO_SPOTIFY_SCHEMA
        )
        
        _LOGGER.info("Spotify service registered successfully")
    except Exception as e:
        _LOGGER.error(f"Failed to setup Spotify service: {e}")