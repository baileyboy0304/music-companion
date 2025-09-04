DOMAIN = "music_companion"

# Master Configuration Constants - These appear as field labels in UI
CONF_MASTER_CONFIG = "master_config"
CONF_ACRCLOUD_HOST = "acrcloud_host_url"
CONF_ACRCLOUD_ACCESS_KEY = "acrcloud_access_key"
CONF_ACRCLOUD_ACCESS_SECRET = "acrcloud_access_secret"
CONF_HOME_ASSISTANT_UDP_PORT = "home_assistant_udp_port"
CONF_SPOTIFY_CLIENT_ID = "spotify_client_id"
CONF_SPOTIFY_CLIENT_SECRET = "spotify_client_secret"
CONF_SPOTIFY_PLAYLIST_ID = "spotify_playlist_id_optional"
CONF_SPOTIFY_CREATE_PLAYLIST = "spotify_create_playlist"
CONF_SPOTIFY_PLAYLIST_NAME = "spotify_playlist_name"

# Device Configuration Constants
CONF_DEVICE_NAME = "device_name"
CONF_ASSIST_SATELLITE_ENTITY = "assist_satellite_entity"
CONF_MEDIA_PLAYER_ENTITY = "media_player_entity"
CONF_TAGGING_ENABLED = "tagging_enabled"
CONF_TAGGING_SWITCH_ENTITY = "tagging_switch_entity"
CONF_DISPLAY_DEVICE = "display_device"
CONF_USE_DISPLAY_DEVICE = "use_display_device"

# Entry Types
ENTRY_TYPE_MASTER = "master"
ENTRY_TYPE_DEVICE = "device"

# Device-specific entity templates
DEVICE_LYRICS_LINE1_TEMPLATE = "text.{}_lyrics_line1"
DEVICE_LYRICS_LINE2_TEMPLATE = "text.{}_lyrics_line2"
DEVICE_LYRICS_LINE3_TEMPLATE = "text.{}_lyrics_line3"

# View Assist integration constants
VIEW_ASSIST_DOMAIN = "view_assist"
REMOTE_ASSIST_DISPLAY_DOMAIN = "remote_assist_display"

# Spotify Auth Constants
SPOTIFY_AUTH_CALLBACK_PATH = "/api/music_companion/spotify_callback"
SPOTIFY_STORAGE_VERSION = 1
SPOTIFY_STORAGE_KEY = "spotify_tokens"
#SPOTIFY_SCOPE = "playlist-modify-private playlist-modify-public user-read-private"
SPOTIFY_SCOPE = "playlist-modify-private playlist-modify-public playlist-read-private user-read-private"
DEFAULT_SPOTIFY_PLAYLIST_NAME = "Home Assistant Music Discoveries"

# Device data structure keys
DEVICE_DATA_LYRICS_SYNC = "lyrics_sync"
DEVICE_DATA_LAST_MEDIA_CONTENT_ID = "last_media_content_id"
DEVICE_DATA_LYRICS_ENTITIES = "lyrics_entities"

# Device capability constants
CAPABILITY_LYRICS_DISPLAY = "lyrics_display"
CAPABILITY_AUDIO_TAGGING = "audio_tagging"