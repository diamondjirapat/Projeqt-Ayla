import asyncio
import logging
import os
import re
from typing import Optional, cast
import mimetypes
import urllib.parse

# Fix Windows registry MIME types bug where .js is served as text/plain
mimetypes.init()
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')

import discord
from discord.ext import commands
import httpx
from itsdangerous import Signer
from fastapi import FastAPI, Request, HTTPException, Response, Query
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import Config
from database.models import UserModel, GuildModel
from utils.queue import CustomPlayer, LoopMode
from utils.lastfm import lastfm_handler
import pomice

logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Projeqt-Ayla Glass Music Player API")

# Enable CORS for local testing/development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0"
}

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in NO_CACHE_HEADERS.items():
        response.headers[k] = v
    return response

# Global variables to store bot instance and signer
bot_instance: Optional[commands.Bot] = None
signer: Optional[Signer] = None
user_model = UserModel()
guild_model = GuildModel()

def get_all_voice_clients():
    if not bot_instance:
        return []
    clients = []
    all_bots = getattr(bot_instance, "all_bots", [bot_instance])
    for b in all_bots:
        clients.extend(b.voice_clients)
    return clients

def get_all_guilds():
    if not bot_instance:
        return []
    guilds = []
    seen = set()
    all_bots = getattr(bot_instance, "all_bots", [bot_instance])
    for b in all_bots:
        for g in b.guilds:
            if g.id not in seen:
                guilds.append(g)
                seen.add(g.id)
    return guilds

def get_user_from_request(request: Request):
    if not signer:
        return None
    cookie = request.cookies.get("ayla_session")
    if not cookie:
        return None
    try:
        unsigned = signer.unsign(cookie.encode()).decode()
        parts = unsigned.split(":")
        if len(parts) >= 3:
            return {
                "id": int(parts[0]),
                "username": parts[1],
                "avatar": parts[2]
            }
    except Exception as e:
        logger.warning(f"[WEB] Failed to verify session cookie: {e}")
    return None

@app.get("/login")
async def login():
    if not Config.DISCORD_CLIENT_ID or not Config.DISCORD_REDIRECT_URI:
        return JSONResponse(
            status_code=400,
            content={"error": "Discord OAuth is not configured in .env file. Please check configuration."}
        )
    
    url = (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={Config.DISCORD_CLIENT_ID}"
        f"&redirect_uri={httpx.URL(Config.DISCORD_REDIRECT_URI)}"
        "&response_type=code"
        "&scope=identify"
    )
    return RedirectResponse(url=url)

@app.get("/api/auth/callback")
async def auth_callback(code: str):
    if not Config.DISCORD_CLIENT_ID or not Config.DISCORD_CLIENT_SECRET or not Config.DISCORD_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="OAuth client credentials missing")

    async with httpx.AsyncClient() as client:
        # Exchange code for access token
        resp = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": Config.DISCORD_CLIENT_ID,
                "client_secret": Config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": Config.DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            logger.error(f"[WEB] OAuth token exchange failed: {resp.text}")
            return Response(content="<h1>Authentication Failed</h1><p>Check console logs.</p>", media_type="text/html", status_code=400)
            
        token_data = resp.json()
        access_token = token_data["access_token"]
        
        # Get user profile
        user_resp = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch user profile")
            
        user_data = user_resp.json()
        user_id = int(user_data["id"])
        username = user_data["username"]
        avatar = user_data.get("avatar") or ""
        
        # Upsert user in database
        db_user = await user_model.get_user(user_id)
        if not db_user:
            await user_model.create_user(user_id, username, avatar=avatar)
        else:
            await user_model.update_user(user_id, {"username": username, "avatar": avatar})

        # Set session cookie
        cookie_value = signer.sign(f"{user_id}:{username}:{avatar}".encode()).decode()
        response = RedirectResponse(url="/webplayer")
        response.set_cookie(
            key="ayla_session",
            value=cookie_value,
            httponly=True,
            max_age=30 * 24 * 3600,  # 30 days
            samesite="lax",
            secure=False,
        )
        return response

@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = get_user_from_request(request)
    if not user:
        return {"authenticated": False}
        
    # Get user preferences from DB
    db_user = await user_model.get_user(user["id"])
    settings = {}
    lastfm = None
    if db_user:
        settings = db_user.get("settings", {})
        lfm = db_user.get("lastfm")
        if lfm:
            lastfm = {
                "username": lfm.get("username"),
                "scrobbling": lfm.get("scrobbling", True)
            }
        
    is_admin = int(user["id"]) in Config.OWNER_IDS
    if not is_admin:
        for guild in get_all_guilds():
            member = guild.get_member(user["id"])
            if member:
                perms = member.guild_permissions
                if perms.administrator or perms.manage_guild:
                    is_admin = True
                    break
    user_copy = user.copy()
    user_copy["id"] = str(user_copy["id"])
    return {
        "authenticated": True,
        "user": user_copy,
        "is_admin": is_admin,
        "settings": settings,
        "lastfm": lastfm
    }

# Last.fm endpoints
@app.get("/api/lastfm/auth-url")
async def lastfm_auth_url(request: Request, cb: Optional[str] = None):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not lastfm_handler.enabled:
        return {"enabled": False}
        
    url, token = await lastfm_handler.get_auth_data(cb=cb)
    if not url or not token:
        raise HTTPException(status_code=500, detail="Failed to get auth token from Last.fm")
        
    return {"enabled": True, "url": url, "token": token}

@app.get("/api/lastfm/callback", response_class=HTMLResponse)
async def lastfm_callback(token: str):
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Last.fm Authorization Success</title>
        <script>
            if (window.opener) {{
                window.opener.postMessage({{
                    source: 'lastfm-auth',
                    status: 'success',
                    token: {repr(token)}
                }}, '*');
            }}
            window.close();
        </script>
    </head>
    <body>
        <p>Authorization successful! You can close this window now.</p>
    </body>
    </html>
    """

@app.post("/api/lastfm/verify")
async def lastfm_verify(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    body = await request.json()
    token = body.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Missing auth token")
        
    session_key = await lastfm_handler.get_session_from_token(token)
    if not session_key:
        return {"success": False, "error": "Last.fm authorization not completed or token expired."}
        
    username = await lastfm_handler.get_username_from_session(session_key)
    if not username:
        return {"success": False, "error": "Failed to fetch Last.fm username info."}
        
    await user_model.update_lastfm(user["id"], username, session_key)
    return {"success": True, "username": username}

@app.post("/api/lastfm/disconnect")
async def lastfm_disconnect(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    await user_model.remove_lastfm(user["id"])
    return {"success": True}

@app.post("/api/lastfm/toggle-scrobble")
async def lastfm_toggle_scrobble(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    body = await request.json()
    enabled = body.get("enabled", True)
    await user_model.toggle_lastfm_scrobbling(user["id"], enabled)
    return {"success": True}

@app.get("/api/lastfm/recent")
async def lastfm_recent(request: Request, limit: int = 50):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    db_user = await user_model.get_user(user["id"])
    if not db_user or not db_user.get("lastfm") or not db_user.get("lastfm", {}).get("username"):
        return {"success": False, "error": "Last.fm account not connected"}
        
    username = db_user["lastfm"]["username"]
    recent_tracks = await lastfm_handler.get_recent_tracks(username, limit=limit)
    if not recent_tracks or "recenttracks" not in recent_tracks:
        return {"success": False, "error": "Failed to fetch recent tracks from Last.fm"}
        
    tracks_list = []
    raw_tracks = recent_tracks["recenttracks"].get("track", [])
    if isinstance(raw_tracks, dict):
        raw_tracks = [raw_tracks]
        
    for t in raw_tracks:
        title = t.get("name", "Unknown Track")
        
        artist_obj = t.get("artist", {})
        artist = artist_obj.get("#text", "Unknown Artist") if isinstance(artist_obj, dict) else artist_obj
        
        album_obj = t.get("album", {})
        album = album_obj.get("#text", "") if isinstance(album_obj, dict) else album_obj
        
        artwork = ""
        images = t.get("image", [])
        if isinstance(images, list):
            for img in images:
                if img.get("size") == "large" or img.get("size") == "extralarge":
                    artwork = img.get("#text", "")
            if not artwork and images:
                artwork = images[-1].get("#text", "")
                
        date_obj = t.get("date", {})
        date_text = date_obj.get("#text", "") if isinstance(date_obj, dict) else ""
        
        attr_obj = t.get("@attr", {})
        is_now_playing = attr_obj.get("nowplaying") == "true" if isinstance(attr_obj, dict) else False
        
        tracks_list.append({
            "title": title,
            "author": artist,
            "album": album,
            "artwork": artwork or "/default_artwork.jpg",
            "date": "Now Playing" if is_now_playing else date_text,
            "now_playing": is_now_playing
        })
        
    return {"success": True, "data": await enrich_artworks(tracks_list)}

def extract_lastfm_artwork(images):
    artwork = ""
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and img.get("size") in ("extralarge", "large", "medium"):
                artwork = img.get("#text", "")
        if not artwork and images and isinstance(images[-1], dict):
            artwork = images[-1].get("#text", "")
    return artwork or "/default_artwork.jpg"

ITUNES_ARTWORK_CACHE = {}

async def fetch_itunes_artwork(term: str) -> str:
    cache_key = term
    if cache_key in ITUNES_ARTWORK_CACHE:
        return ITUNES_ARTWORK_CACHE[cache_key]
        
    try:
        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}&entity=song&limit=1"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("results"):
                    url = data["results"][0].get("artworkUrl100", "")
                    if url:
                        url = url.replace("100x100bb", "500x500bb")
                        ITUNES_ARTWORK_CACHE[cache_key] = url
                        return url
    except Exception as e:
        logger.error(f"iTunes API error for {term}: {e}")
        pass
    
    ITUNES_ARTWORK_CACHE[cache_key] = ""
    return ""

async def enrich_artworks(items, is_artist=False):
    async def process_item(item):
        if is_artist:
            term = item.get("name", "")
        else:
            title = item.get("title", "")
            if title.startswith("Top Hits by "):
                term = title.replace("Top Hits by ", "")
            else:
                term = f"{title} {item.get('author', '')}"
            
        artwork = item.get("artwork", "")
        if not artwork or "2a96cbd8b46e442fc41c2b86b821562f" in artwork or artwork == "/default_artwork.jpg":
            itunes_art = await fetch_itunes_artwork(term)
            if itunes_art:
                item["artwork"] = itunes_art
            else:
                item["artwork"] = "/default_artwork.jpg"
        return item
        
    if items:
        await asyncio.gather(*(process_item(i) for i in items))
    return items

@app.get("/api/lastfm/top-tracks")
async def lastfm_top_tracks(request: Request, limit: int = 20, period: str = "overall"):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    db_user = await user_model.get_user(user["id"])
    if not db_user or not db_user.get("lastfm") or not db_user.get("lastfm", {}).get("username"):
        return {"success": False, "error": "Last.fm account not connected"}
        
    username = db_user["lastfm"]["username"]
    top_tracks_resp = await lastfm_handler.get_top_tracks(username, limit=limit, period=period)
    if not top_tracks_resp or "toptracks" not in top_tracks_resp:
        return {"success": False, "error": "Failed to fetch top tracks from Last.fm"}
        
    tracks_list = []
    raw_tracks = top_tracks_resp["toptracks"].get("track", [])
    if isinstance(raw_tracks, dict):
        raw_tracks = [raw_tracks]
        
    for t in raw_tracks:
        title = t.get("name", "Unknown Track")
        artist_obj = t.get("artist", {})
        artist = artist_obj.get("name", "Unknown Artist") if isinstance(artist_obj, dict) else str(artist_obj)
        playcount = t.get("playcount", "0")
        artwork = extract_lastfm_artwork(t.get("image", []))
        
        tracks_list.append({
            "title": title,
            "author": artist,
            "artwork": artwork,
            "playcount": playcount,
            "uri": f"ytsearch:{title} {artist}"
        })
        
    return {"success": True, "data": await enrich_artworks(tracks_list)}

@app.get("/api/lastfm/top-artists")
async def lastfm_top_artists(request: Request, limit: int = 20, period: str = "overall"):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    db_user = await user_model.get_user(user["id"])
    if not db_user or not db_user.get("lastfm") or not db_user.get("lastfm", {}).get("username"):
        return {"success": False, "error": "Last.fm account not connected"}
        
    username = db_user["lastfm"]["username"]
    top_artists_resp = await lastfm_handler.get_top_artists(username, limit=limit, period=period)
    if not top_artists_resp or "topartists" not in top_artists_resp:
        return {"success": False, "error": "Failed to fetch top artists from Last.fm"}
        
    artists_list = []
    raw_artists = top_artists_resp["topartists"].get("artist", [])
    if isinstance(raw_artists, dict):
        raw_artists = [raw_artists]
        
    for a in raw_artists:
        name = a.get("name", "Unknown Artist")
        playcount = a.get("playcount", "0")
        artwork = extract_lastfm_artwork(a.get("image", []))
        
        artists_list.append({
            "name": name,
            "playcount": playcount,
            "artwork": artwork,
            "query": f"ytsearch:{name} top songs"
        })
        
    return {"success": True, "data": await enrich_artworks(artists_list, is_artist=True)}

@app.get("/api/lastfm/recommendations")
async def lastfm_recommendations(request: Request, limit: int = 15):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    db_user = await user_model.get_user(user["id"])
    if not db_user or not db_user.get("lastfm") or not db_user.get("lastfm", {}).get("username"):
        return {"success": False, "error": "Last.fm account not connected"}
        
    username = db_user["lastfm"]["username"]
    top_artists_resp = await lastfm_handler.get_top_artists(username, limit=5)
    
    recommendations = []
    if top_artists_resp and "topartists" in top_artists_resp:
        raw_artists = top_artists_resp["topartists"].get("artist", [])
        if isinstance(raw_artists, dict):
            raw_artists = [raw_artists]
            
        artist_names = [a.get("name") for a in raw_artists[:3] if a.get("name")]
        if artist_names:
            similar_results = await asyncio.gather(*[lastfm_handler.get_similar_artists(name, limit=5) for name in artist_names])
            for artist_name, similar_resp in zip(artist_names, similar_results):
                if similar_resp and "similarartists" in similar_resp:
                    sim_artists = similar_resp["similarartists"].get("artist", [])
                    if isinstance(sim_artists, dict):
                        sim_artists = [sim_artists]
                    for sa in sim_artists:
                        sname = sa.get("name")
                        if not sname or sname == artist_name:
                            continue
                        recommendations.append({
                            "title": f"Top Hits by {sname}",
                            "author": f"Based on your interest in {artist_name}",
                            "artwork": extract_lastfm_artwork(sa.get("image", [])),
                            "uri": f"ytsearch:{sname} popular songs"
                        })
                        if len(recommendations) >= limit:
                            break
                if len(recommendations) >= limit:
                    break
                
    return {"success": True, "data": await enrich_artworks(recommendations)}

@app.get("/api/lastfm/chart")
async def lastfm_chart(request: Request, limit: int = 20):
    chart_resp = await lastfm_handler.get_chart_tracks(limit=limit)
    if not chart_resp or "tracks" not in chart_resp:
        return {"success": False, "error": "Failed to fetch Last.fm chart"}
        
    tracks_list = []
    raw_tracks = chart_resp["tracks"].get("track", [])
    if isinstance(raw_tracks, dict):
        raw_tracks = [raw_tracks]
        
    for t in raw_tracks:
        title = t.get("name", "Unknown Track")
        artist_obj = t.get("artist", {})
        artist = artist_obj.get("name", "Unknown Artist") if isinstance(artist_obj, dict) else str(artist_obj)
        listeners = t.get("listeners", "0")
        artwork = extract_lastfm_artwork(t.get("image", []))
        
        tracks_list.append({
            "title": title,
            "author": artist,
            "artwork": artwork,
            "listeners": listeners,
            "uri": f"ytsearch:{title} {artist}"
        })
        
    return {"success": True, "data": await enrich_artworks(tracks_list)}

@app.get("/api/lastfm/genre-tracks")
async def lastfm_genre_tracks(request: Request, genre: str, limit: int = 15):
    tracks_resp = await lastfm_handler.get_tag_top_tracks(genre, limit=limit)
    if not tracks_resp or "tracks" not in tracks_resp:
        return {"success": False, "error": f"Failed to fetch Last.fm tracks for genre: {genre}"}
        
    tracks_list = []
    raw_tracks = tracks_resp["tracks"].get("track", [])
    if isinstance(raw_tracks, dict):
        raw_tracks = [raw_tracks]
        
    for t in raw_tracks:
        title = t.get("name", "Unknown Track")
        artist_obj = t.get("artist", {})
        artist = artist_obj.get("name", "Unknown Artist") if isinstance(artist_obj, dict) else str(artist_obj)
        artwork = extract_lastfm_artwork(t.get("image", []))
        
        tracks_list.append({
            "title": title,
            "author": artist,
            "artwork": artwork,
            "uri": f"ytsearch:{title} {artist}"
        })
        
    return {"success": True, "data": await enrich_artworks(tracks_list)}

@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("ayla_session")
    return {"success": True}

@app.get("/api/player/list")
async def player_list(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not bot_instance:
        return []
        
    user_id = user["id"]
    is_admin = int(user_id) in Config.OWNER_IDS
    players = []
    for player in get_all_voice_clients():
        if not isinstance(player, CustomPlayer):
            continue
        member = player.guild.get_member(user_id)
        if not member and not is_admin:
            try:
                member = await player.guild.fetch_member(user_id)
            except Exception:
                continue
        user_has_perm = is_admin or (member and (member.guild_permissions.administrator or member.guild_permissions.manage_guild))
        if user_has_perm:
            players.append({
                "guild_id": str(player.guild.id),
                "guild_name": player.guild.name,
                "channel_name": player.channel.name,
                "current_track": player.current.title if player.current else None,
                "is_playing": player.is_playing
            })
            
    return players

@app.get("/api/player/state")
async def player_state(request: Request, guild_id: Optional[int] = None):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not bot_instance:
        return {"connected": False, "msg": "Bot is starting..."}
        
    user_id = user["id"]
    is_admin = int(user_id) in Config.OWNER_IDS
    active_player: Optional[CustomPlayer] = None
    
    # 1. Try to find player in the voice channel the user is currently in
    for player in get_all_voice_clients():
        if not isinstance(player, CustomPlayer):
            continue
        voice_state = player.guild._voice_states.get(user_id)
        if voice_state and voice_state.channel and voice_state.channel.id == player.channel.id:
            active_player = player
            break
            
    # 2. Only if user is admin, check target guild or fallback to first active player
    if not active_player and is_admin:
        if guild_id:
            for player in get_all_voice_clients():
                if not isinstance(player, CustomPlayer):
                    continue
                if player.guild.id == guild_id:
                    active_player = player
                    break
                    
        if not active_player:
            for player in get_all_voice_clients():
                if not isinstance(player, CustomPlayer):
                    continue
                active_player = player
                break
                
    if not active_player:
        # Check if user is in any voice channel at all to offer connection
        user_voice_channel = None
        for guild in get_all_guilds():
            voice_state = guild._voice_states.get(user_id)
            if voice_state and voice_state.channel:
                user_voice_channel = {
                    "guild_id": str(guild.id),
                    "guild_name": guild.name,
                    "channel_id": str(voice_state.channel.id),
                    "channel_name": voice_state.channel.name
                }
                break
        return {
            "connected": False,
            "guild_id": None,
            "guild_name": "",
            "channel_id": None,
            "channel_name": "",
            "bitrate": None,
            "is_playing": False,
            "is_paused": False,
            "volume": 50,
            "position": 0,
            "loop_mode": "OFF",
            "autoplay": False,
            "current_track": None,
            "queue": [],
            "history": [],
            "user_voice": user_voice_channel
        }
        
    # Get current track details
    track_info = None
    if active_player.current:
        track = active_player.current
        # Try to form artwork url
        artwork = getattr(track, "artwork_url", None) or getattr(track, "artwork", None)
        if not artwork:
            if "youtube" in track.uri or "youtu.be" in track.uri:
                artwork = f"https://img.youtube.com/vi/{track.identifier}/mqdefault.jpg"
            else:
                artwork = "/default_artwork.jpg"
                
        track_info = {
            "title": track.title,
            "author": track.author,
            "uri": track.uri,
            "length": track.length,
            "requester": getattr(track, "requester", "Unknown"),
            "artwork": artwork
        }
        
    # Get queue details
    queue_tracks = []
    for track in active_player.queue._queue:
        artwork = getattr(track, "artwork_url", None) or getattr(track, "artwork", None)
        if not artwork and ("youtube" in track.uri or "youtu.be" in track.uri):
            artwork = f"https://img.youtube.com/vi/{track.identifier}/mqdefault.jpg"
            
        queue_tracks.append({
            "title": track.title,
            "author": track.author,
            "uri": track.uri,
            "length": track.length,
            "requester": getattr(track, "requester", "Unknown"),
            "artwork": artwork or "/default_artwork.jpg"
        })

    # Get history details
    history_tracks = []
    player_history = getattr(active_player, "history", [])
    for track in player_history:
        artwork = getattr(track, "artwork_url", None) or getattr(track, "artwork", None)
        if not artwork:
            if "youtube" in track.uri or "youtu.be" in track.uri:
                artwork = f"https://img.youtube.com/vi/{track.identifier}/mqdefault.jpg"
            else:
                artwork = "/default_artwork.jpg"
        history_tracks.append({
            "title": track.title,
            "author": track.author,
            "uri": track.uri,
            "length": track.length,
            "requester": getattr(track, "requester", "Unknown"),
            "artwork": artwork
        })
        
    bitrate = min(getattr(active_player.channel, "bitrate", 64000) // 1000, 160)
    return {
        "connected": True,
        "guild_id": str(active_player.guild.id),
        "guild_name": active_player.guild.name,
        "channel_id": str(active_player.channel.id),
        "channel_name": active_player.channel.name,
        "bitrate": bitrate,
        "is_playing": active_player.is_playing,
        "is_paused": active_player.is_paused,
        "volume": active_player.volume,
        "position": active_player.position,
        "loop_mode": active_player.queue.loop_mode.name,
        "autoplay": getattr(active_player, "autoplay_enabled", False),
        "current_track": track_info,
        "queue": queue_tracks,
        "history": history_tracks
    }

@app.post("/api/player/control")
async def player_control(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not bot_instance:
        raise HTTPException(status_code=503, detail="Bot is starting")
        
    user_id = user["id"]
    data = await request.json()
    action = data.get("action")
    value = data.get("value")
    
    # Locate active player
    active_player: Optional[CustomPlayer] = None
    for player in get_all_voice_clients():
        if not isinstance(player, CustomPlayer):
            continue
        voice_state = player.guild._voice_states.get(user_id)
        if voice_state and voice_state.channel and voice_state.channel.id == player.channel.id:
            active_player = player
            break
            
    guild_id = data.get("guild_id")
    is_admin = int(user_id) in Config.OWNER_IDS
    if not active_player and is_admin and guild_id:
        for player in get_all_voice_clients():
            if not isinstance(player, CustomPlayer):
                continue
            if str(player.guild.id) == str(guild_id) or player.guild.id == guild_id:
                active_player = player
                break
                
    if not active_player and action != "play":
        raise HTTPException(status_code=400, detail="You must be in the bot's voice channel to control playback.")
        
    try:
        if action == "play":
            if not value:
                raise HTTPException(status_code=400, detail="No track or URL provided to play.")
            # Find user voice channel to connect if not already connected
            voice_channel = None
            searched_guilds = []
            for guild in get_all_guilds():
                searched_guilds.append(f"{guild.name}({guild.id})")
                voice_state = guild._voice_states.get(user_id)
                if voice_state and voice_state.channel:
                    voice_channel = voice_state.channel
                    break
                    
            if not voice_channel:
                logger.warning(f"[WEB] Voice channel not found for user_id={user_id}. Searched guilds: {searched_guilds}")
                for guild in get_all_guilds():
                    if guild._voice_states:
                        logger.warning(f"[WEB] Guild {guild.name} ({guild.id}) voice states: {list(guild._voice_states.keys())}")
                raise HTTPException(status_code=400, detail="You must be in a Discord voice channel to play music.")
                
            if not active_player:
                # Find if already in that guild across all bots
                for pc in get_all_voice_clients():
                    if pc.guild.id == voice_channel.guild.id:
                        active_player = cast(CustomPlayer, pc)
                        break
                        
            if not active_player:
                all_bots = getattr(bot_instance, "all_bots", [bot_instance])
                guild_bots = [b for b in all_bots if b.get_guild(voice_channel.guild.id) is not None]
                if not guild_bots:
                    raise HTTPException(status_code=400, detail="No bots are present in this server.")
                
                selected_bot = None
                for b in guild_bots:
                    is_busy = any(vc.guild.id == voice_channel.guild.id for vc in b.voice_clients)
                    if not is_busy:
                        selected_bot = b
                        break
                
                if not selected_bot:
                    selected_bot = guild_bots[0]
                
                bot_guild = selected_bot.get_guild(voice_channel.guild.id)
                bot_voice_channel = bot_guild.get_channel(voice_channel.id) if bot_guild else None
                
                if not bot_voice_channel:
                    raise HTTPException(status_code=400, detail="Could not connect bot to your voice channel.")
                
                logger.info(f"[WEB] Connecting {selected_bot.user} to voice channel {bot_voice_channel.name} in guild {bot_guild.name}")
                active_player = await bot_voice_channel.connect(cls=CustomPlayer)
                active_player.home_channel = bot_voice_channel
                
                # Initialize autoplay_enabled from user settings
                db_user = await user_model.get_user(user["id"])
                if db_user and db_user.get("settings", {}).get("autoplay") is not None:
                    active_player.autoplay_enabled = bool(db_user["settings"]["autoplay"])
                
            tracks = await active_player.get_tracks(value)
            if not tracks:
                raise HTTPException(status_code=404, detail="No results found for query")
                
            if isinstance(tracks, pomice.Playlist):
                for track in tracks.tracks:
                    track.requester = f"@{user['username']} (Web)"
                    active_player.queue.put(track)
            else:
                track = tracks[0]
                track.requester = f"@{user['username']} (Web)"
                active_player.queue.put(track)
                
            if not active_player.is_playing:
                await active_player.set_volume(50)
                await active_player.play(active_player.queue.get())
                
        elif action == "pause":
            await active_player.set_pause(True)
            
        elif action == "resume":
            await active_player.set_pause(False)
            
        elif action == "skip":
            if active_player.queue.is_empty:
                # Save current to history before stopping
                if active_player.current:
                    if not hasattr(active_player, "history"):
                        active_player.history = []
                    if not active_player.history or active_player.history[-1] is not active_player.current:
                        active_player.history.append(active_player.current)
                await active_player.stop()
            else:
                # Save current to history before playing next
                if active_player.current:
                    if not hasattr(active_player, "history"):
                        active_player.history = []
                    if not active_player.history or active_player.history[-1] is not active_player.current:
                        active_player.history.append(active_player.current)
                next_track = active_player.queue.get(force_next=True)
                await active_player.play(next_track)
                
        elif action == "previous":
            if active_player.history:
                prev_track = active_player.history.pop()
                if active_player.current:
                    active_player.queue.put_at_front(active_player.current)
                await active_player.play(prev_track)
            else:
                raise HTTPException(status_code=400, detail="No history to go back to")
                
        elif action == "volume":
            vol = int(value)
            vol = max(0, min(vol, 150))
            await active_player.set_volume(vol)
            
        elif action == "seek":
            pos = int(value)
            if active_player.current and 0 <= pos <= active_player.current.length:
                await active_player.seek(pos)
                
        elif action == "shuffle":
            active_player.queue.shuffle()
            
        elif action == "repeat":
            if value == "track":
                active_player.queue.set_loop_mode(LoopMode.TRACK)
            elif value == "queue":
                active_player.queue.set_loop_mode(LoopMode.QUEUE)
            else:
                active_player.queue.set_loop_mode(LoopMode.OFF)
                
        elif action == "autoplay":
            if value is not None:
                active_player.autoplay_enabled = bool(value)
            else:
                active_player.autoplay_enabled = not getattr(active_player, "autoplay_enabled", False)
            # Sync back to DB settings
            db_user = await user_model.get_user(user["id"])
            if db_user:
                settings = db_user.get("settings", {})
                settings["autoplay"] = active_player.autoplay_enabled
                await user_model.update_user(user["id"], {"settings": settings})
                
        elif action == "queue_remove":
            active_player.queue.remove_at(int(value))
            
        elif action == "queue_jump":
            # Jump to a specific index in the upcoming queue.
            # All tracks before the target index are skipped and saved to history.
            target_index = int(value)
            if not 0 <= target_index < len(active_player.queue._queue):
                raise HTTPException(status_code=400, detail="Queue index out of range")
            if not hasattr(active_player, "history"):
                active_player.history = []
            # Save current track to history
            if active_player.current:
                if not active_player.history or active_player.history[-1] is not active_player.current:
                    active_player.history.append(active_player.current)
            # Save all tracks being skipped over to history
            for _ in range(target_index):
                skipped = active_player.queue._queue.pop(0)
                if not active_player.history or active_player.history[-1] is not skipped:
                    active_player.history.append(skipped)
            # Play the target track
            target_track = active_player.queue.get(force_next=True)
            await active_player.play(target_track)

        elif action == "history_jump":
            # Replay a track from history by its index.
            # History tracks after the target + current track are pushed back to queue front.
            target_index = int(value)
            if not hasattr(active_player, "history"):
                active_player.history = []
            if not 0 <= target_index < len(active_player.history):
                raise HTTPException(status_code=400, detail="History index out of range")
            # Grab the target track before mutating history
            target_track = active_player.history[target_index]
            # Tracks in history after the target go back to the queue front (in order)
            tracks_to_requeue = active_player.history[target_index + 1:]
            # Trim history to everything before the target
            active_player.history = active_player.history[:target_index]
            # Push current track to queue front first (it will play after target)
            if active_player.current:
                active_player.queue.put_at_front(active_player.current)
            # Push orphaned history tracks back to queue front in reverse so order is preserved
            for t in reversed(tracks_to_requeue):
                active_player.queue.put_at_front(t)
            # Play the target history track
            await active_player.play(target_track)

        elif action == "queue_clear":
            active_player.queue.clear()

        elif action == "queue_move":
            # Reorder: move track from index_from to index_to within the upcoming queue
            index_from = int(value["from"])
            index_to = int(value["to"])
            active_player.queue.move(index_from, index_to)

        elif action == "history_move":
            # Reorder: move a track within the history list
            index_from = int(value["from"])
            index_to = int(value["to"])
            if not hasattr(active_player, "history"):
                active_player.history = []
            history = active_player.history
            if not (0 <= index_from < len(history) and 0 <= index_to < len(history)):
                raise HTTPException(status_code=400, detail="History index out of range")
            track = history.pop(index_from)
            history.insert(index_to, track)

        elif action == "history_to_queue":
            # Move a history track into the upcoming queue at a specific position
            history_index = int(value["history_index"])
            queue_index = int(value["queue_index"])
            if not hasattr(active_player, "history"):
                active_player.history = []
            if not (0 <= history_index < len(active_player.history)):
                raise HTTPException(status_code=400, detail="History index out of range")
            track = active_player.history.pop(history_index)
            insert_at = min(queue_index, len(active_player.queue._queue))
            active_player.queue.put_at_index(insert_at, track)

        elif action == "queue_to_history":
            # Move an upcoming track back into history at a specific position
            queue_index = int(value["queue_index"])
            history_index = int(value["history_index"])
            if not (0 <= queue_index < len(active_player.queue._queue)):
                raise HTTPException(status_code=400, detail="Queue index out of range")
            if not hasattr(active_player, "history"):
                active_player.history = []
            track = active_player.queue.remove_at(queue_index)
            insert_at = min(history_index, len(active_player.history))
            active_player.history.insert(insert_at, track)

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WEB] Control error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/player/search")
async def player_search(request: Request, q: str = Query(..., min_length=1)):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not bot_instance:
        return []
        
    try:
        # Use Pomice Node to resolve search query without player requirement
        node = pomice.NodePool.get_node()
        tracks = await node.get_tracks(q)
        results = []
        
        if isinstance(tracks, pomice.Playlist):
            # Return ALL playlist tracks (no cap), with playlist metadata
            for t in tracks.tracks:
                artwork = getattr(t, "artwork_url", None) or getattr(t, "artwork", None)
                if not artwork:
                    artwork = f"https://img.youtube.com/vi/{t.identifier}/mqdefault.jpg" if "youtube" in t.uri or "youtu.be" in t.uri else "/default_artwork.jpg"
                results.append({
                    "title": t.title,
                    "author": t.author,
                    "uri": t.uri,
                    "length": t.length,
                    "artwork": artwork
                })
            return {
                "is_playlist": True,
                "playlist_name": getattr(tracks, "name", None) or "Playlist",
                "playlist_url": getattr(tracks, "uri", None) or q,
                "tracks": results
            }
        elif tracks:
            for t in tracks[:15]:
                artwork = getattr(t, "artwork_url", None) or getattr(t, "artwork", None)
                if not artwork:
                    artwork = f"https://img.youtube.com/vi/{t.identifier}/mqdefault.jpg" if "youtube" in t.uri or "youtu.be" in t.uri else "/default_artwork.jpg"
                results.append({
                    "title": t.title,
                    "author": t.author,
                    "uri": t.uri,
                    "length": t.length,
                    "artwork": artwork
                })
        return {"is_playlist": False, "playlist_name": None, "tracks": results}
    except Exception as e:
        logger.error(f"[WEB] Search error: {e}")
        return {"is_playlist": False, "playlist_name": None, "tracks": []}

@app.get("/api/playlists")
async def list_playlists(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    user_id = user["id"]
    playlists = await user_model.get_all_playlists(user_id)
    
    # Format playlists for frontend
    formatted = []
    for key, data in playlists.items():
        formatted.append({
            "key": key,
            "name": data.get("name", "Unknown Playlist"),
            "track_count": len(data.get("tracks", [])),
            "tracks": data.get("tracks", []),
            "created_at": str(data.get("created_at", ""))
        })
    return formatted

@app.post("/api/playlists/create")
async def create_playlist(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    data = await request.json()
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
        
    success = await user_model.create_playlist(user["id"], name)
    return {"success": success}

@app.post("/api/playlists/delete")
async def delete_playlist(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    data = await request.json()
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
        
    success = await user_model.delete_playlist(user["id"], name)
    return {"success": success}

@app.post("/api/playlists/add-track")
async def playlist_add_track(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    data = await request.json()
    playlist_name = data.get("playlist_name")
    track_info = data.get("track")
    if not playlist_name or not track_info:
        raise HTTPException(status_code=400, detail="Playlist name and track are required")
        
    success = await user_model.add_track_to_playlist(user["id"], playlist_name, track_info)
    return {"success": success}

@app.post("/api/playlists/remove-track")
async def playlist_remove_track(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    data = await request.json()
    playlist_name = data.get("playlist_name")
    index = data.get("index")
    if not playlist_name or index is None:
        raise HTTPException(status_code=400, detail="Playlist name and index are required")
        
    success, _ = await user_model.remove_track_from_playlist(user["id"], playlist_name, int(index))
    return {"success": success}

@app.post("/api/settings")
async def update_settings(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    data = await request.json()
    # Update settings fields in UserModel
    success = await user_model.update_user(user["id"], {"settings": data})
    
    # Sync autoplay setting with the active player if connected
    if "autoplay" in data:
        autoplay_val = bool(data["autoplay"])
        for player in get_all_voice_clients():
            if not isinstance(player, CustomPlayer):
                continue
            voice_state = player.guild._voice_states.get(user["id"])
            if voice_state and voice_state.channel and voice_state.channel.id == player.channel.id:
                player.autoplay_enabled = autoplay_val
                break
                
    return {"success": success}

async def check_user_guild_permission(user_id: int, guild_id: int) -> bool:
    if int(user_id) in Config.OWNER_IDS:
        return True
    if not bot_instance:
        return False
    all_bots = getattr(bot_instance, "all_bots", [bot_instance])
    for b in all_bots:
        guild = b.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    continue
            if member:
                perms = member.guild_permissions
                if perms.administrator or perms.manage_guild:
                    return True
    return False

@app.get("/api/guilds/manageable")
async def get_manageable_guilds(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]
    is_admin = int(user_id) in Config.OWNER_IDS
    
    manageable = []
    for guild in get_all_guilds():
        has_perm = is_admin
        if not has_perm:
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    continue
            if member:
                perms = member.guild_permissions
                if perms.administrator or perms.manage_guild:
                    has_perm = True
        if has_perm:
            manageable.append({
                "id": str(guild.id),
                "name": guild.name
            })
    return manageable

@app.get("/api/guild/settings")
async def get_guild_settings(request: Request, guild_id: int):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]
    
    can_edit = await check_user_guild_permission(user_id, guild_id)
    
    guild = None
    for b in getattr(bot_instance, "all_bots", [bot_instance]):
        guild = b.get_guild(guild_id)
        if guild:
            break
            
    if not guild:
        raise HTTPException(status_code=404, detail="Server not found or bot is not in it.")
        
    is_global_admin = int(user_id) in Config.OWNER_IDS
    member = guild.get_member(user_id)
    if not member and not is_global_admin:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            raise HTTPException(status_code=403, detail="You are not a member of this server.")
            
    from database.models import GuildModel
    guild_model = GuildModel()
    
    default_vol = await guild_model.get_default_volume(guild_id)
    music_chan = await guild_model.get_music_channel(guild_id)
    
    from utils.prefix_manager import prefix_manager
    prefix = await prefix_manager.get_guild_prefix(guild_id) or prefix_manager.default_prefix
    
    db_guild = await guild_model.get_guild(guild_id)
    locale = db_guild.get("locale", "en") if db_guild else "en"
    auto_role_id = db_guild.get("auto_role_id") if db_guild else None
    
    channels = []
    for ch in guild.text_channels:
        channels.append({"id": str(ch.id), "name": ch.name})
        
    roles = []
    for r in guild.roles:
        if r.name != "@everyone":
            roles.append({"id": str(r.id), "name": r.name, "color": str(r.color)})
            
    guild_emojis = []
    for e in guild.emojis:
        guild_emojis.append({"id": str(e.id), "name": e.name, "url": e.url})
        
    reaction_roles = db_guild.get("reaction_roles", {}) if db_guild else {}
    reaction_role_titles = {}
    
    import asyncio
    async def fetch_message_title(guild, msg_id_str):
        try:
            msg_id = int(msg_id_str)
        except:
            return "Invalid ID"
            
        # Check cache first
        import discord
        for b in getattr(bot_instance, "all_bots", [bot_instance]):
            msg = discord.utils.get(b.cached_messages, id=msg_id)
            if msg:
                return msg.embeds[0].title if msg.embeds and msg.embeds[0].title else "Reaction Roles"
                
        # Search channels concurrently
        async def check_channel(ch):
            try:
                return await ch.fetch_message(msg_id)
            except:
                return None
                
        results = await asyncio.gather(*(check_channel(ch) for ch in guild.text_channels))
        for msg in results:
            if msg:
                return msg.embeds[0].title if msg.embeds and msg.embeds[0].title else "Reaction Roles"
        return "Unknown Message"

    if reaction_roles:
        tasks = [fetch_message_title(guild, msg_id) for msg_id in reaction_roles.keys()]
        titles = await asyncio.gather(*tasks)
        for msg_id, title in zip(reaction_roles.keys(), titles):
            reaction_role_titles[msg_id] = title
        
    return {
        "guild_id": str(guild_id),
        "guild_name": guild.name,
        "can_edit": can_edit,
        "prefix": prefix,
        "default_volume": default_vol,
        "music_channel_id": str(music_chan) if music_chan else None,
        "locale": locale,
        "auto_role_id": str(auto_role_id) if auto_role_id else None,
        "text_channels": channels,
        "roles": roles,
        "guild_emojis": guild_emojis,
        "reaction_roles": reaction_roles,
        "reaction_role_titles": reaction_role_titles
    }

@app.post("/api/guild/settings")
async def save_guild_settings(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]
    
    data = await request.json()
    guild_id = int(data.get("guild_id"))
    
    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to edit settings for this server.")
        
    from database.models import GuildModel
    guild_model = GuildModel()
    
    # 1. Prefix
    prefix = data.get("prefix")
    if prefix:
        from utils.prefix_manager import prefix_manager
        success, msg = await prefix_manager.set_guild_prefix(guild_id, prefix)
        if not success:
            raise HTTPException(status_code=400, detail=msg)
            
    # 2. Volume
    default_vol = data.get("default_volume")
    if default_vol is not None:
        await guild_model.set_default_volume(guild_id, int(default_vol))
        
    # 3. Channel
    music_chan_id = data.get("music_channel_id")
    if music_chan_id is not None and music_chan_id != "" and music_chan_id != "null":
        await guild_model.set_music_channel(guild_id, int(music_chan_id))
    else:
        await guild_model.remove_music_channel(guild_id)
        
    # 4. Locale
    locale = data.get("locale")
    if locale:
        await guild_model.update_guild(guild_id, {"locale": locale})
        
    # 5. AutoRole
    if "auto_role_id" in data:
        auto_role_id_raw = data.get("auto_role_id")
        auto_role_id = int(auto_role_id_raw) if auto_role_id_raw and str(auto_role_id_raw).lower() not in ("none", "null", "") else None
        await guild_model.update_guild(guild_id, {"auto_role_id": auto_role_id})
        
        # Update AutoRole cog cache if it exists
        for b in getattr(bot_instance, "all_bots", [bot_instance]):
            autorole_cog = b.get_cog('AutoRole')
            if autorole_cog:
                if auto_role_id:
                    autorole_cog._role_cache[guild_id] = auto_role_id
                else:
                    autorole_cog._role_cache.pop(guild_id, None)
                    
    # 6. Reaction Roles
    if "reaction_roles" in data:
        reaction_roles = data.get("reaction_roles")
        await guild_model.update_guild(guild_id, {"reaction_roles": reaction_roles})
        
        # Update ReactionRoles cog cache
        for b in getattr(bot_instance, "all_bots", [bot_instance]):
            rr_cog = b.get_cog('ReactionRolesCog')
            if rr_cog:
                rr_cog.reaction_roles[guild_id] = {}
                for msg_id_str, emoji_roles in reaction_roles.items():
                    msg_id = int(msg_id_str)
                    rr_cog.reaction_roles[guild_id][msg_id] = {}
                    for emoji, role_id in emoji_roles.items():
                        rr_cog.reaction_roles[guild_id][msg_id][emoji] = role_id
                        
                # Sync live embeds and reactions
                import asyncio
                async def sync_messages(cog, gid, r_roles):
                    guild = cog.bot.get_guild(gid)
                    if not guild: return
                    for m_id_str, e_roles in r_roles.items():
                        m_id = int(m_id_str)
                        for ch in guild.text_channels:
                            try:
                                msg = await ch.fetch_message(m_id)
                                if msg:
                                    # Update embed text
                                    await cog._update_message_embed(msg, gid, m_id)
                                    # Add reactions
                                    for emoji_str in e_roles.keys():
                                        if emoji_str.startswith('<'):
                                            import re
                                            match = re.match(r'<a?:.+?:(\d+)>', emoji_str)
                                            if match:
                                                e_id = int(match.group(1))
                                                custom_emoji = cog.bot.get_emoji(e_id)
                                                if custom_emoji:
                                                    await msg.add_reaction(custom_emoji)
                                        else:
                                            await msg.add_reaction(emoji_str)
                                    break
                            except:
                                pass
                
                asyncio.create_task(sync_messages(rr_cog, guild_id, reaction_roles))
        
    return {"success": True}

@app.post("/api/guild/reaction-roles/generate")
async def generate_reaction_role_message(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]
    
    data = await request.json()
    guild_id = int(data.get("guild_id"))
    channel_id = int(data.get("channel_id"))
    
    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to edit settings for this server.")
        
    guild = None
    for b in getattr(bot_instance, "all_bots", [bot_instance]):
        guild = b.get_guild(guild_id)
        if guild:
            break
            
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
        
    channel = guild.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
        
    title = data.get("title") or "Reaction Roles"
    description = data.get("description") or "React to this message to get your roles!"
        
    import discord
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blue()
    )
    try:
        msg = await channel.send(embed=embed)
        return {"message_id": str(msg.id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send message: {e}")

# Serves Frontend SPA
@app.get("/{path:path}")
async def serve_frontend(request: Request, path: str):
    # If API call, proceed as usual (should fall under routing above, but catches fallthroughs)
    if path.startswith("api/") or path.startswith("login"):
        raise HTTPException(status_code=404, detail="Not Found")
        
    # Check if file exists in static folder
    static_file = os.path.join("static", path)
    if path and os.path.exists(static_file) and os.path.isfile(static_file):
        return FileResponse(static_file, headers=NO_CACHE_HEADERS)
        
    # Serve landing page for root
    if not path or path == "" or path == "landing":
        landing_index = os.path.join("static", "index.html")
        if os.path.exists(landing_index):
            return FileResponse(landing_index, headers=NO_CACHE_HEADERS)
            
    # Fallback to serving SPA webplayer.html for frontend routing
    spa_index = os.path.join("static", "webplayer.html")
    if os.path.exists(spa_index):
        return FileResponse(spa_index, headers=NO_CACHE_HEADERS)
        
    # Standard fallback if frontend isn't generated yet
    return Response(content="<h1>Glass Music Player Web Service</h1><p>Frontend static files not found in '/static'.</p>", media_type="text/html", headers=NO_CACHE_HEADERS)


class WebServer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.server_task = None
        
    async def cog_load(self):
        global bot_instance, signer
        if not getattr(self.bot, "is_main", True):
            logger.info("[WEB] Non-main bot instance; skipping web server startup.")
            return

        bot_instance = self.bot
        signer = Signer(Config.SESSION_SECRET_KEY)
        
        # Start uvicorn server in asyncio loop
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=Config.WEB_PORT,
            log_level="info",
            loop="asyncio"
        )
        server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(server.serve())
        logger.info(f"[WEB] Web player backend server started on port {Config.WEB_PORT}")
        
    async def cog_unload(self):
        if self.server_task:
            self.server_task.cancel()
            logger.info("[WEB] Web player backend server stopped")


async def setup(bot):
    await bot.add_cog(WebServer(bot))
