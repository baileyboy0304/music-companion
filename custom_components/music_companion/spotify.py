import json
import logging
import urllib.parse
import aiohttp
from aiohttp import web
import asyncio
import time
import os
import base64
import hashlib
from typing import Optional

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
    DEFAULT_SPOTIFY_PLAYLIST_NAME,
)

_LOGGER = logging.getLogger(__name__)

# -------------------------------------------------
# Schemas
# -------------------------------------------------
SPOTIFY_CONFIG_SCHEMA = vol.Schema({
    vol.Required("client_id"): cv.string,
    vol.Required("client_secret"): cv.string,
    vol.Optional("playlist_id"): cv.string,
    vol.Optional("create_playlist", default=True): cv.boolean,
    vol.Optional("playlist_name", default=DEFAULT_SPOTIFY_PLAYLIST_NAME): cv.string,
})

SERVICE_ADD_TO_SPOTIFY_SCHEMA = vol.Schema({
    vol.Optional("title"): cv.string,
    vol.Optional("artist"): cv.string,
    vol.Optional("spotify_id"): cv.string,
})

# ---------------- PKCE helpers (optional; safe even if not used) ----------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _new_code_verifier() -> str:
    # 43–128 chars; 64 bytes → 86 chars
    return _b64url(os.urandom(64))


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


# -------------------------------------------------
# OAuth callback view
# -------------------------------------------------
class SpotifyAuthView(HomeAssistantView):
    url = SPOTIFY_AUTH_CALLBACK_PATH
    name = f"api:{DOMAIN}:spotify_callback"
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request):
        try:
            code = request.query.get("code")
            error = request.query.get("error")
            _LOGGER.warning("Spotify Callback Received - Code: %s, Error: %s", code, error)

            if error:
                _LOGGER.error("Spotify authentication error: %s", error)
                return web.Response(
                    text=f"<html><body><h1>Authentication Error</h1><p>{error}</p></body></html>",
                    content_type="text/html",
                    status=400,
                )

            if not code:
                _LOGGER.error("No authorization code received")
                return web.Response(
                    text="<html><body><h1>Error</h1><p>No authorization code received</p></body></html>",
                    content_type="text/html",
                    status=400,
                )

            spotify_service = self.hass.data.get("spotify_service")
            if not spotify_service:
                _LOGGER.error("Spotify service not initialized")
                return web.Response(
                    text="<html><body><h1>Setup Error</h1><p>Spotify service not initialized</p></body></html>",
                    content_type="text/html",
                    status=500,
                )

            success = await spotify_service.exchange_code(code)
            if success:
                return web.Response(
                    text="<html><body><h1>Authentication Successful</h1><p>You can close this window now</p></body></html>",
                    content_type="text/html",
                    status=200,
                )
            else:
                _LOGGER.error("Failed to exchange authorization code")
                return web.Response(
                    text="<html><body><h1>Authentication Failed</h1><p>Unable to complete Spotify authorization</p></body></html>",
                    content_type="text/html",
                    status=500,
                )
        except Exception as e:
            _LOGGER.exception("Unexpected error in Spotify auth callback: %s", e)
            return web.Response(
                text=f"<html><body><h1>Unexpected Error</h1><p>{str(e)}</p></body></html>",
                content_type="text/html",
                status=500,
            )


# -------------------------------------------------
# Spotify service
# -------------------------------------------------
class SpotifyService:
    """Service to add tracks to Spotify playlists."""

    def __init__(self, hass: HomeAssistant, config):
        self.hass = hass
        self.client_id = config["client_id"]
        self.client_secret = config["client_secret"]
        self.playlist_id = config.get("playlist_id")
        self.create_playlist = config.get("create_playlist", True)
        self.playlist_name = config.get("playlist_name", DEFAULT_SPOTIFY_PLAYLIST_NAME)
        self.session = async_get_clientsession(hass)
        self.user_id: Optional[str] = None
        self.authorized = False
        self._lock = asyncio.Lock()
        self._pkce_verifier: Optional[str] = None

        # token storage
        self.store = Store(hass, SPOTIFY_STORAGE_VERSION, f"{DOMAIN}_{SPOTIFY_STORAGE_KEY}")
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at = 0  # epoch seconds

    def _get_base_url(self) -> str:
        """Return best base URL for OAuth redirect."""
        # Prefer HA External URL if set; else Internal URL; else a last-ditch fallback.
        base = self.hass.config.external_url or self.hass.config.internal_url
        if not base:
            # Final fallback keeps old behavior; change if your network can’t resolve this.
            base = "http://homeassistant.local:8123"
        return base.rstrip("/")

    async def async_setup(self):
        """Initialize from storage and verify by talking to Spotify."""
        await self.load_tokens()

        # register callback early
        self.hass.http.register_view(SpotifyAuthView(self.hass))

        # Try to ensure we have a valid token (refresh if needed)
        if self.refresh_token and not await self._token_is_valid():
            await self.refresh_access_token()

        # Final verification: actually hit Spotify /me
        ok = await self._probe_current_user()
        if ok:
            _LOGGER.info(
                "Spotify auth verified with live API; user=%s, playlist_id=%s",
                self.user_id,
                self.playlist_id,
            )
            self.authorized = True
        else:
            self.authorized = False
            _LOGGER.warning(
                "Spotify not authorized. Use the authorize link when prompted before adding tracks."
            )

        # Log current playlist state without implying success
        if self.playlist_id:
            _LOGGER.info(
                "Using existing playlist id (not yet validated this run): %s",
                self.playlist_id,
            )
        else:
            _LOGGER.info("No playlist ID configured; will create/find one on first use.")

        return self.authorized

    # ---------------- Token helpers ----------------
    async def load_tokens(self):
        data = await self.store.async_load()
        if data:
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self.expires_at = data.get("expires_at", 0)
            self.user_id = data.get("user_id")
            self.playlist_id = data.get("playlist_id", self.playlist_id)

    async def save_tokens(self):
        await self.store.async_save(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "user_id": self.user_id,
                "playlist_id": self.playlist_id,
            }
        )

    async def _token_is_valid(self) -> bool:
        return bool(self.access_token and self.expires_at > int(time.time()) + 60)

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    # ---------------- OAuth URLs ----------------
    def get_authorize_url(self):
        base_url = self._get_base_url()
        redirect_uri = f"{base_url}{SPOTIFY_AUTH_CALLBACK_PATH}"

        # PKCE: generate on each auth URL build
        self._pkce_verifier = _new_code_verifier()
        challenge = _code_challenge(self._pkce_verifier)

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_SCOPE,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "show_dialog": "true",
        }
        auth_url = f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"
        _LOGGER.info("Generated Spotify Authorization URL: %s", auth_url)
        _LOGGER.info("Redirect URI used: %s", redirect_uri)
        return auth_url

    async def exchange_code(self, code: str) -> bool:
        redirect_uri = f"{self._get_base_url()}{SPOTIFY_AUTH_CALLBACK_PATH}"
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            # PKCE
            "code_verifier": self._pkce_verifier or "",
        }
        try:
            _LOGGER.warning("Attempting to exchange code. Redirect URI: %s", redirect_uri)
            async with self.session.post("https://accounts.spotify.com/api/token", data=payload) as resp:
                text = await resp.text()
                _LOGGER.warning("Token exchange response status: %s", resp.status)
                _LOGGER.warning("Token exchange response body: %s", text)
                if resp.status != 200:
                    return False
                data = await resp.json()
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token", self.refresh_token)
                self.expires_at = int(time.time()) + int(data.get("expires_in", 3600))
                # fetch user to confirm
                ok = await self._probe_current_user()
                await self.save_tokens()
                return ok
        except Exception as e:
            _LOGGER.exception("Error exchanging auth code: %s", e)
            return False

    async def refresh_access_token(self) -> bool:
        if not self.refresh_token:
            return False
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            async with self.session.post("https://accounts.spotify.com/api/token", data=payload) as resp:
                text = await resp.text()
                _LOGGER.debug("Refresh token response %s: %s", resp.status, text)
                if resp.status != 200:
                    _LOGGER.error("Failed to refresh Spotify token: %s", resp.status)
                    return False
                data = await resp.json()
                self.access_token = data.get("access_token", self.access_token)
                # Some refresh responses omit refresh_token
                self.refresh_token = data.get("refresh_token", self.refresh_token)
                self.expires_at = int(time.time()) + int(data.get("expires_in", 3600))
                await self.save_tokens()
                return True
        except Exception as e:
            _LOGGER.exception("Exception refreshing Spotify token: %s", e)
            return False

    # ---------------- Probes & helpers ----------------
    async def _probe_current_user(self) -> bool:
        """Verify tokens by calling /me. Sets user_id on success."""
        if not self.access_token:
            return False
        try:
            async with self.session.get("https://api.spotify.com/v1/me", headers=self._auth_headers()) as resp:
                if resp.status == 401:
                    # try one refresh once
                    if await self.refresh_access_token():
                        async with self.session.get("https://api.spotify.com/v1/me", headers=self._auth_headers()) as resp2:
                            if resp2.status == 200:
                                user = await resp2.json()
                                self.user_id = user.get("id")
                                await self.save_tokens()
                                return True
                            _LOGGER.error("/me after refresh failed: %s", resp2.status)
                            return False
                if resp.status != 200:
                    _LOGGER.error("Spotify /me failed: %s", resp.status)
                    return False
                user = await resp.json()
                self.user_id = user.get("id")
                await self.save_tokens()
                return True
        except Exception as e:
            _LOGGER.exception("Error probing Spotify /me: %s", e)
            return False

    async def _ensure_playlist_exists(self) -> bool:
        """Confirm playlist exists; create if missing and allowed. Retries once on 401."""
        if not await self._token_is_valid():
            await self.refresh_access_token()
        if not self.access_token:
            return False

        # If we already have an ID, validate it
        if self.playlist_id:
            for attempt in (1, 2):
                async with self.session.get(
                    f"https://api.spotify.com/v1/playlists/{self.playlist_id}", headers=self._auth_headers()
                ) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status == 401 and attempt == 1:
                        _LOGGER.warning("Playlist check returned 401; refreshing and retrying once")
                        if not await self.refresh_access_token():
                            return False
                        continue
                    if resp.status == 404:
                        _LOGGER.warning("Stored playlist id %s not found", self.playlist_id)
                        self.playlist_id = None
                        break
                    # Any other error
                    text = await resp.text()
                    _LOGGER.error("Error checking playlist: %s - %s", resp.status, text)
                    return False

        # Need to find or create one
        if not self.playlist_id:
            # Ensure we know the user
            if not self.user_id and not await self._probe_current_user():
                return False
            # Try to find by name (requires read scope if private)
            for attempt in (1, 2):
                params = {"limit": 50}
                async with self.session.get(
                    "https://api.spotify.com/v1/me/playlists",
                    headers=self._auth_headers(),
                    params=params,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for pl in data.get("items", []):
                            if pl.get("name") == self.playlist_name:
                                self.playlist_id = pl.get("id")
                                await self.save_tokens()
                                return True
                        # Not found
                        break
                    if resp.status == 401 and attempt == 1:
                        if not await self.refresh_access_token():
                            return False
                        continue
                    # Other errors (403 if missing read scope)
                    _LOGGER.warning(
                        "Could not list playlists (status %s). Will try to create.",
                        resp.status,
                    )
                    break

            if self.create_playlist:
                payload = {
                    "name": self.playlist_name,
                    "public": False,
                    "description": "Added by Home Assistant",
                }
                for attempt in (1, 2):
                    async with self.session.post(
                        f"https://api.spotify.com/v1/users/{self.user_id}/playlists",
                        headers={**self._auth_headers(), "Content-Type": "application/json"},
                        json=payload,
                    ) as resp:
                        if resp.status in (200, 201):
                            data = await resp.json()
                            self.playlist_id = data.get("id")
                            await self.save_tokens()
                            return True
                        if resp.status == 401 and attempt == 1:
                            if not await self.refresh_access_token():
                                return False
                            continue
                        text = await resp.text()
                        _LOGGER.error("Failed to create playlist: %s - %s", resp.status, text)
                        return False
            else:
                _LOGGER.error("Playlist not found and auto-create disabled")
                return False

        return bool(self.playlist_id)

    async def check_track_in_playlist(self, track_uri: str) -> bool:
        """Return True if track already in playlist. Retries once on 401."""
        if not self.playlist_id:
            return False
        for attempt in (1, 2):
            params = {"fields": "items(track(uri)),next", "limit": 100}
            async with self.session.get(
                f"https://api.spotify.com/v1/playlists/{self.playlist_id}/tracks",
                headers=self._auth_headers(),
                params=params,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("items", []):
                        if item.get("track", {}).get("uri") == track_uri:
                            return True
                    return False
                if resp.status == 401 and attempt == 1:
                    if not await self.refresh_access_token():
                        return False
                    continue
                text = await resp.text()
                _LOGGER.error("Failed to read playlist tracks: %s - %s", resp.status, text)
                return False

    # ---------------- Public API ----------------
    async def add_track_to_playlist(self, title: str, artist: str, spotify_id: Optional[str]) -> bool:
        """Add a track by ID or by search. Only report success after Spotify confirms."""
        async with self._lock:
            # Auth guard
            if not await self._token_is_valid():
                await self.refresh_access_token()

            if not await self._token_is_valid():
                # Prompt user to authorize
                auth_url = self.get_authorize_url()
                message = (
                    "Spotify authorization required. "
                    f"[Click here to authorize]({auth_url})"
                )
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Spotify Authorization Required",
                        "message": message,
                        "notification_id": "spotify_auth_required",
                    },
                )
                return False

            # If we only have title/artist, search track
            track_uri = f"spotify:track:{spotify_id}" if spotify_id else None
            if not track_uri:
                q = f"track:{title} artist:{artist}"
                for attempt in (1, 2):
                    async with self.session.get(
                        "https://api.spotify.com/v1/search",
                        headers=self._auth_headers(),
                        params={"q": q, "type": "track", "limit": 1},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get("tracks", {}).get("items", [])
                            if not items:
                                _LOGGER.error("Spotify search returned no results for %s", q)
                                return False
                            track_uri = items[0].get("uri")
                            break
                        if resp.status == 401 and attempt == 1:
                            if not await self.refresh_access_token():
                                return False
                            continue
                        text = await resp.text()
                        _LOGGER.error("Spotify search failed: %s - %s", resp.status, text)
                        return False

            # Ensure playlist exists/valid
            if not await self._ensure_playlist_exists():
                _LOGGER.error("Failed to ensure playlist exists")
                return False

            # Already present?
            if await self.check_track_in_playlist(track_uri):
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Spotify Track Already Saved",
                        "message": "The track is already in your playlist.",
                        "notification_id": "spotify_track_status",
                    },
                )
                return True

            # Add track
            for attempt in (1, 2):
                async with self.session.post(
                    f"https://api.spotify.com/v1/playlists/{self.playlist_id}/tracks",
                    headers={**self._auth_headers(), "Content-Type": "application/json"},
                    json={"uris": [track_uri]},
                ) as resp:
                    if resp.status in (200, 201):
                        await self.hass.services.async_call(
                            "persistent_notification",
                            "create",
                            {
                                "title": "Added Track to Spotify",
                                "message": "Successfully added track to your Spotify playlist.",
                                "notification_id": "spotify_track_status",
                            },
                        )
                        return True
                    if resp.status == 401 and attempt == 1:
                        if not await self.refresh_access_token():
                            return False
                        continue
                    text = await resp.text()
                    _LOGGER.error("Failed to add track to playlist: %s - %s", resp.status, text)
                    return False


# -------------------------------------------------
# HA service registration
# -------------------------------------------------
async def handle_add_to_spotify(call):
    hass = call.hass
    _LOGGER.warning("ADD TO SPOTIFY SERVICE CALLED")

    spotify_id = call.data.get("spotify_id")
    title = call.data.get("title")
    artist = call.data.get("artist")

    if not (spotify_id and title and artist):
        last_song = hass.states.get("sensor.last_tagged_song")
        if last_song and last_song.attributes:
            spotify_id = spotify_id or last_song.attributes.get("spotify_id")
            title = title or last_song.attributes.get("title")
            artist = artist or last_song.attributes.get("artist")

    if not title or not artist:
        _LOGGER.error("No title or artist provided")
        _LOGGER.error(
            "Received data - Title: %s, Artist: %s, Spotify ID: %s",
            title,
            artist,
            spotify_id,
        )
        return

    _LOGGER.info(
        "add_to_spotify service called for: %s - %s, Spotify ID: %s",
        title,
        artist,
        spotify_id,
    )

    spotify_service = hass.data.get("spotify_service")
    if not spotify_service:
        _LOGGER.error("Spotify service not initialized")
        return

    await spotify_service.add_track_to_playlist(title, artist, spotify_id)


async def async_setup_spotify_service(hass, config):
    if "spotify" not in config:
        _LOGGER.info("No Spotify configuration found - skipping setup")
        return
    try:
        spotify_config = config["spotify"]
        spotify_service = SpotifyService(hass, spotify_config)
        hass.data["spotify_service"] = spotify_service

        # Initialize & verify
        await spotify_service.async_setup()

        # Register the service regardless (adds will prompt to authorize if needed)
        hass.services.async_register(
            DOMAIN,
            "add_to_spotify",
            handle_add_to_spotify,
            schema=SERVICE_ADD_TO_SPOTIFY_SCHEMA,
        )

        if spotify_service.authorized:
            _LOGGER.info("Spotify service registered and verified with Spotify API")
        else:
            _LOGGER.warning(
                "Spotify service registered; waiting for user authorization to complete"
            )
    except Exception as e:
        _LOGGER.error("Failed to setup Spotify service: %s", e)
