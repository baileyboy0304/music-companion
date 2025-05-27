# Music Companion

Your complete music companion for Home Assistant - recognize any song and sync lyrics in real-time!

## 🎵 What it does

**Music Companion** provides two main features:
- **🔍 Music Recognition**: Identifies songs playing from any audio source using ACRCloud
- **📝 Lyrics Sync**: Shows synchronized lyrics that follow along with your media players

## ✨ Features

- 🎧 **Audio Fingerprinting**: Identify songs from radio, TV, streaming services, or any audio
- 📱 **Multiple Devices**: Set up different devices (Living Room, Kitchen, etc.) with shared credentials  
- 🎤 **Synchronized Lyrics**: Real-time lyrics that sync with your music playback
- 🎶 **Spotify Integration**: Automatically add discovered songs to your Spotify playlists
- 🏠 **Home Assistant Native**: Full integration with Home Assistant's UI and automations

## 🚀 Quick Start

1. **Install via HACS**: Add this repository to HACS
2. **Setup Master Config**: Configure your ACRCloud and Spotify credentials once
3. **Add Devices**: Create devices for each location (Living Room, Kitchen, etc.)
4. **Start Discovering**: Use the `music_companion.fetch_audio_tag` service to identify songs

## 📋 Requirements

- **ACRCloud Account**: For music recognition ([sign up here](https://www.acrcloud.com/))
- **Spotify Developer App**: For playlist integration ([create app here](https://developer.spotify.com/))
- **UDP Audio Source**: Device/app that can send audio via UDP

## 🎯 Perfect For

- Identifying songs on radio stations
- Creating playlists from TV/movie soundtracks  
- Auto-tagging music from any audio source
- Showing lyrics for currently playing music
- Building music discovery automations

Transform your Home Assistant into the ultimate music companion! 🎵