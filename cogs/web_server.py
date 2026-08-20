import asyncio
import json
import logging
import re
from typing import Optional, cast
from html import escape
import mimetypes
import urllib.parse

import discord
from discord.ext import commands
import httpx
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from fastapi import FastAPI, Request, HTTPException, Response, Query
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import Config
from database.models import UserModel, GuildModel, LevelingModel, CustomCommandModel
from utils.queue import CustomPlayer, LoopMode
from utils.lastfm import lastfm_handler
from utils.artwork import artwork_resolver, DEFAULT_ARTWORK
import wavelink

# Fix a Windows registry issue that can map JavaScript files to text/plain.
mimetypes.init()
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')

logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Projeqt-Ayla Glass Music Player API")

# Enable CORS for local testing/development
app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.WEB_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def on_shutdown():
    await artwork_resolver.close()

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0"
}
IMMUTABLE_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}
SESSION_MAX_AGE = 30 * 24 * 3600
LASTFM_AUTH_STATE_MAX_AGE = 15 * 60
STATIC_DIR = (Config.PROJECT_ROOT / 'static').resolve()

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        for key, value in NO_CACHE_HEADERS.items():
            response.headers[key] = value
    return response

# Global variables to store bot instance and signer
bot_instance: Optional[commands.Bot] = None
signer: Optional[TimestampSigner] = None
user_model = UserModel()
guild_model = GuildModel()
leveling_model = LevelingModel()
custom_command_model = CustomCommandModel()


def get_track_artwork(track):
    return artwork_resolver.resolve_track(track).artwork


def serialize_track(track, **extra):
    res = artwork_resolver.resolve_track(track)
    payload = {
        "title": getattr(track, "title", "Unknown Track"),
        "author": getattr(track, "author", "Unknown Artist"),
        "uri": getattr(track, "uri", ""),
        "length": getattr(track, "length", 0),
        "requester": getattr(track, "requester", "Unknown"),
    }
    payload.update(res.as_dict())
    payload.update(extra)
    return payload


async def serialize_track_async(track, **extra):
    payload = serialize_track(track, **extra)
    if payload.get("artwork") == DEFAULT_ARTWORK or not payload.get("artwork"):
        resolved = await artwork_resolver.resolve_track_async(payload)
        payload.update(resolved.as_dict())
    return payload


def _parse_integer(value, field: str, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be an integer.") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        allowed = f"{minimum} to {maximum}" if maximum is not None else f"at least {minimum}"
        raise HTTPException(status_code=400, detail=f"{field} must be {allowed}.")
    return parsed


def _find_guild(guild_id: int):
    return next((guild for guild in get_all_guilds() if guild.id == guild_id), None)


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
        unsigned = signer.unsign(cookie, max_age=SESSION_MAX_AGE)
        session_data = json.loads(unsigned)
        return {
            "id": int(session_data["id"]),
            "username": str(session_data["username"]),
            "avatar": str(session_data.get("avatar", "")),
        }
    except SignatureExpired:
        logger.info("[WEB] Session cookie expired")
    except (BadSignature, KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("[WEB] Rejected an invalid session cookie")
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
        session_data = json.dumps(
            {"id": user_id, "username": username, "avatar": avatar},
            separators=(',', ':'),
        )
        cookie_value = signer.sign(session_data.encode()).decode()
        response = RedirectResponse(url="/webplayer")
        response.set_cookie(
            key="ayla_session",
            value=cookie_value,
            httponly=True,
            max_age=SESSION_MAX_AGE,
            samesite="lax",
            secure=Config.SESSION_COOKIE_SECURE,
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
    user_locale = "en"
    lastfm = None
    if db_user:
        settings = db_user.get("settings", {})
        user_locale = db_user.get("locale") or settings.get("language") or settings.get("locale") or "en"
        settings["language"] = user_locale
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
        "locale": user_locale,
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

    if not signer:
        raise HTTPException(status_code=503, detail="Web authentication is not ready")

    # Put a short-lived, signed user id in the callback path. This lets the
    # callback finish linking the account even when Last.fm/browser security
    # severs window.opener during the cross-origin authorization round trip.
    state = signer.sign(str(user["id"]).encode()).decode()
    callback_base = cb or f"{str(request.base_url).rstrip('/')}/api/lastfm/callback"
    callback_url = f"{callback_base.rstrip('/')}/{urllib.parse.quote(state, safe='')}"

    # Web authentication must not call auth.getToken first. Last.fm creates
    # the token only after the user clicks Allow, then appends it to this
    # callback URL. Supplying a desktop-flow token here prevents that redirect.
    url = lastfm_handler.get_web_auth_url(callback_url)
    if not url:
        raise HTTPException(status_code=500, detail="Failed to create Last.fm authorization URL")

    logger.info("[WEB] Created Last.fm web authorization for user_id=%s", user["id"])

    return {"enabled": True, "url": url}

@app.get("/api/lastfm/callback/{state}", response_class=HTMLResponse)
@app.get("/api/lastfm/callback", response_class=HTMLResponse)
async def lastfm_callback(
    request: Request,
    state: Optional[str] = None,
    token: Optional[str] = None,
    error: Optional[str] = None,
):
    callback_origin = f"{request.url.scheme}://{request.url.netloc}"
    username: Optional[str] = None
    error_text: Optional[str] = error

    if not error_text and not token:
        error_text = "Last.fm did not return an authorization token."

    user_id: Optional[int] = None
    if not error_text and state:
        if not signer:
            error_text = "Web authentication is not ready."
        else:
            try:
                user_id = int(signer.unsign(state, max_age=LASTFM_AUTH_STATE_MAX_AGE).decode())
            except (BadSignature, SignatureExpired, TypeError, ValueError):
                error_text = "This Last.fm authorization request is invalid or expired."
    elif not error_text:
        # Backwards compatibility for callbacks opened before signed-state
        # support was added. A same-origin session cookie is required here.
        callback_user = get_user_from_request(request)
        if callback_user:
            user_id = callback_user["id"]
        else:
            error_text = "Your web session could not be matched to this Last.fm request."

    if not error_text and token and user_id is not None:
        logger.info("[WEB] Completing Last.fm authorization for user_id=%s", user_id)
        session_key = await lastfm_handler.get_session_from_token(token)
        if not session_key:
            error_text = "Last.fm did not create a session. Please start the connection again."
        else:
            username = await lastfm_handler.get_username_from_session(session_key)
            if not username:
                error_text = "Last.fm created a session, but the username could not be loaded."
            else:
                await user_model.update_lastfm(user_id, username, session_key)
                logger.info(
                    "[WEB] Last.fm account linked for user_id=%s username=%s",
                    user_id,
                    username,
                )

    if username:
        message = {
            "source": "lastfm-auth",
            "status": "success",
            "username": username,
        }
        heading = "Last.fm Authorization Success"
        body = f"Connected to Last.fm as {escape(username)}. This window will close automatically."
    else:
        safe_error = escape(error_text or "Authorization was cancelled.", quote=True)
        logger.warning("[WEB] Last.fm authorization callback failed: %s", safe_error)
        message = {
            "source": "lastfm-auth",
            "status": "error",
            "error": safe_error,
        }
        heading = "Last.fm Authorization Failed"
        body = f"Last.fm authorization was not completed: {safe_error} You can close this window now."

    serialized_message = json.dumps(message).replace("</", "<\\/")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{heading}</title>
        <script>
            if (window.opener) {{
                window.opener.postMessage({serialized_message}, {json.dumps(callback_origin)});
            }}
            window.setTimeout(() => window.close(), 150);
        </script>
    </head>
    <body>
        <p>{body}</p>
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
        artist = artist_obj.get("#text", "Unknown Artist") if isinstance(artist_obj, dict) else str(artist_obj or "Unknown Artist")
        
        album_obj = t.get("album", {})
        album = album_obj.get("#text", "") if isinstance(album_obj, dict) else str(album_obj or "")
        
        artwork = extract_lastfm_artwork(t.get("image", []))
        if artwork == DEFAULT_ARTWORK or "2a96cbd8b46e442fc41c2b86b821562f" in artwork:
            artwork = ""
                
        date_obj = t.get("date", {})
        date_text = date_obj.get("#text", "") if isinstance(date_obj, dict) else ""
        
        attr_obj = t.get("@attr", {})
        is_now_playing = attr_obj.get("nowplaying") == "true" if isinstance(attr_obj, dict) else False
        
        tracks_list.append({
            "title": title,
            "author": artist,
            "album": album,
            "artwork": artwork,
            "uri": f"ytsearch:{title} {artist}",
            "date": "Now Playing" if is_now_playing else date_text,
            "now_playing": is_now_playing
        })
        
    return {"success": True, "data": await enrich_artworks(tracks_list)}

def extract_lastfm_artwork(images):
    if isinstance(images, list):
        for size in ("extralarge", "large", "medium", "small"):
            for img in images:
                if isinstance(img, dict) and img.get("size") == size:
                    text = str(img.get("#text", "")).strip()
                    if text and "2a96cbd8b46e442fc41c2b86b821562f" not in text:
                        return text
        for img in images:
            if isinstance(img, dict):
                text = str(img.get("#text", "")).strip()
                if text and "2a96cbd8b46e442fc41c2b86b821562f" not in text:
                    return text
    return DEFAULT_ARTWORK


async def enrich_artworks(items, is_artist=False):
    if not items:
        return []
    if is_artist:
        results = await artwork_resolver.resolve_artists_batch(items)
    else:
        results = await artwork_resolver.resolve_tracks_batch(items)

    enriched_items = []
    for item, res in zip(items, results):
        enriched = dict(item)
        enriched.update(res.as_dict())
        enriched_items.append(enriched)
    return enriched_items

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
    track_info = serialize_track(active_player.current) if active_player.current else None

    # Get queue details
    queue_tracks = [serialize_track(track) for track in active_player.queue._queue]

    # Get history details
    player_history = getattr(active_player, "history", [])
    history_tracks = [serialize_track(track) for track in player_history]
        
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
            query_str = None
            if isinstance(value, dict):
                query_str = value.get("uri") or value.get("url") or value.get("query")
                if not query_str:
                    title = value.get("title")
                    author = value.get("author")
                    if title and author:
                        query_str = f"ytsearch:{title} {author}"
                    elif title:
                        query_str = f"ytsearch:{title}"
                    elif author:
                        query_str = f"ytsearch:{author}"
            elif isinstance(value, str):
                query_str = value.strip()
            elif value is not None:
                query_str = str(value).strip()

            if not query_str:
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
                
            tracks = await active_player.get_tracks(query_str)
            if not tracks:
                raise HTTPException(status_code=404, detail="No results found for query")
                
            if isinstance(tracks, wavelink.Playlist):
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
async def player_search(request: Request, q: str = Query(..., min_length=1), mode: str = "youtube"):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not bot_instance:
        return {"mode": mode, "is_playlist": False, "playlist_name": None, "tracks": [], "playlists": []}
        
    try:
        explicit_search_prefixes = (
            'ytsearch:',
            'ytmsearch:',
            'scsearch:',
            'spsearch:',
            'dzsearch:',
            'amsearch:',
            'bcsearch:',
        )
        try:
            node = wavelink.Pool.get_node()
        except Exception:
            node = None

        url_pattern = re.compile(r'https?://(?:www\.)?.+')
        is_url = bool(url_pattern.match(q))

        if is_url or q.lower().startswith(explicit_search_prefixes):
            tracks = await wavelink.Pool.fetch_tracks(q, node=node)
        elif mode in ("youtubemusic", "ytm"):
            tracks = await wavelink.Pool.fetch_tracks(f"ytmsearch:{q}", node=node)
        else:
            # Default to regular YouTube search
            tracks = await wavelink.Pool.fetch_tracks(f"ytsearch:{q}", node=node)

        results = []
        if isinstance(tracks, wavelink.Playlist):
            # Return ALL playlist tracks (no cap), with playlist metadata
            for t in tracks.tracks:
                results.append(serialize_track(t))
            return {
                "mode": mode,
                "is_playlist": True,
                "playlist_name": getattr(tracks, "name", None) or "Playlist",
                "playlist_url": getattr(tracks, "uri", None) or getattr(tracks, "url", None) or q,
                "tracks": results,
                "playlists": []
            }
        elif tracks:
            for t in tracks[:15]:
                results.append(serialize_track(t))
        return {"mode": mode, "is_playlist": False, "playlist_name": None, "tracks": results, "playlists": []}
    except Exception as e:
        logger.error(f"[WEB] Search error: {e}")
        return {"mode": mode, "is_playlist": False, "playlist_name": None, "tracks": [], "playlists": []}

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
            "cover": data.get("cover"),
            "track_count": len(data.get("tracks", [])),
            "tracks": data.get("tracks", []),
            "created_at": str(data.get("created_at", ""))
        })
    return formatted

@app.post("/api/playlists/set-cover")
async def playlist_set_cover(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    data = await request.json()
    playlist_name = data.get("playlist_name")
    cover_url = (data.get("cover_url") or "").strip() if data.get("cover_url") else ""
    if not playlist_name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
        
    success = await user_model.set_playlist_cover(user["id"], playlist_name, cover_url)
    return {"success": success}

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
    update_data = {"settings": data}
    if "language" in data:
        update_data["locale"] = data["language"]
    elif "locale" in data:
        update_data["locale"] = data["locale"]

    # Update settings fields in UserModel
    success = await user_model.update_user(user["id"], update_data)
    
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
    level_chan = await guild_model.get_level_channel(guild_id)
    
    from utils.prefix_manager import prefix_manager
    prefix = await prefix_manager.get_guild_prefix(guild_id) or prefix_manager.default_prefix
    
    db_guild = await guild_model.get_guild(guild_id)
    locale = db_guild.get("locale", "en") if db_guild else "en"
    auto_role_id = db_guild.get("auto_role_id") if db_guild else None
    
    channels = []
    for ch in guild.text_channels:
        channels.append({"id": str(ch.id), "name": ch.name})
        
    roles = []
    role_names_by_id = {}
    for r in guild.roles:
        if r.name != "@everyone":
            role_id = str(r.id)
            roles.append({"id": role_id, "name": r.name, "color": str(r.color)})
            role_names_by_id[role_id] = r.name
            
    guild_emojis = []
    for e in guild.emojis:
        guild_emojis.append({"id": str(e.id), "name": e.name, "url": e.url, "animated": getattr(e, 'animated', False)})
        
    raw_reaction_roles = db_guild.get("reaction_roles", {}) if db_guild else {}
    reaction_roles = {
        str(message_id): {
            str(emoji): str(role_id)
            for emoji, role_id in mappings.items()
        }
        for message_id, mappings in raw_reaction_roles.items()
        if isinstance(mappings, dict)
    }
    stored_role_names = db_guild.get("reaction_role_names", {}) if db_guild else {}
    reaction_role_names = dict(stored_role_names) if isinstance(stored_role_names, dict) else {}
    for mappings in reaction_roles.values():
        for raw_role_id in mappings.values():
            role_id = str(raw_role_id)
            if role_id in role_names_by_id:
                reaction_role_names[role_id] = role_names_by_id[role_id]
    stored_reaction_role_embeds = db_guild.get("reaction_role_embeds", {}) if db_guild else {}
    reaction_role_embeds = {
        str(message_id): dict(embed_settings)
        for message_id, embed_settings in stored_reaction_role_embeds.items()
        if isinstance(embed_settings, dict)
    }
    stored_reaction_role_inline = db_guild.get("reaction_role_inline", {}) if db_guild else {}
    reaction_role_inline = {
        str(message_id): {
            str(emoji): bool(is_inline)
            for emoji, is_inline in inline_settings.items()
        }
        for message_id, inline_settings in stored_reaction_role_inline.items()
        if isinstance(inline_settings, dict)
    }
    reaction_role_titles = {}

    import asyncio
    async def fetch_message_embed(guild, msg_id_str):
        try:
            msg_id = int(msg_id_str)
        except (TypeError, ValueError):
            return {
                "title": "Invalid ID",
                "description": "",
                "image_url": "",
                "thumbnail_url": "",
                "footer_text": "",
                "footer_url": ""
            }

        def serialize_message_embed(message):
            embed = message.embeds[0] if message.embeds else None
            if not embed:
                return {
                    "title": "Reaction Roles",
                    "description": "",
                    "image_url": "",
                    "thumbnail_url": "",
                    "footer_text": "",
                    "footer_url": ""
                }
            return {
                "title": embed.title or "Reaction Roles",
                "description": embed.description or "",
                "image_url": getattr(embed.image, "url", "") or "",
                "thumbnail_url": getattr(embed.thumbnail, "url", "") or "",
                "footer_text": getattr(embed.footer, "text", "") or "",
                "footer_url": getattr(embed.footer, "icon_url", "") or ""
            }
            
        # Check cache first
        for b in getattr(bot_instance, "all_bots", [bot_instance]):
            msg = discord.utils.get(b.cached_messages, id=msg_id)
            if msg:
                return serialize_message_embed(msg)
                
        # Search channels concurrently
        async def check_channel(ch):
            try:
                return await ch.fetch_message(msg_id)
            except discord.HTTPException:
                return None
                
        results = await asyncio.gather(*(check_channel(ch) for ch in guild.text_channels))
        for msg in results:
            if msg:
                return serialize_message_embed(msg)
        return {
            "title": "Unknown Message",
            "description": "",
            "image_url": "",
            "thumbnail_url": "",
            "footer_text": "",
            "footer_url": ""
        }

    if reaction_roles:
        tasks = [fetch_message_embed(guild, msg_id) for msg_id in reaction_roles.keys()]
        fetched_embeds = await asyncio.gather(*tasks)
        for msg_id, fetched_embed in zip(reaction_roles.keys(), fetched_embeds):
            stored_embed = reaction_role_embeds.setdefault(msg_id, {})
            for key, value in fetched_embed.items():
                stored_embed.setdefault(key, value)
            reaction_role_titles[msg_id] = stored_embed.get("title") or "Reaction Roles"
        
    leveling_on = await guild_model.is_leveling_enabled(guild_id)
    level_alert_cfg = await guild_model.get_level_alert_config(guild_id)
    if level_alert_cfg.get("channel_id"):
        level_alert_cfg["channel_id"] = str(level_alert_cfg["channel_id"])
    else:
        level_alert_cfg["channel_id"] = str(level_chan) if level_chan else None
    level_alert_cfg["leveling_enabled"] = leveling_on

    return {
        "guild_id": str(guild_id),
        "guild_name": guild.name,
        "can_edit": can_edit,
        "prefix": prefix,
        "default_volume": default_vol,
        "music_channel_id": str(music_chan) if music_chan else None,
        "level_channel_id": str(level_chan) if level_chan else None,
        "leveling_enabled": leveling_on,
        "level_alert_config": level_alert_cfg,
        "locale": locale,
        "auto_role_id": str(auto_role_id) if auto_role_id else None,
        "text_channels": channels,
        "roles": roles,
        "guild_emojis": guild_emojis,
        "reaction_roles": reaction_roles,
        "reaction_role_names": reaction_role_names,
        "reaction_role_titles": reaction_role_titles,
        "reaction_role_embeds": reaction_role_embeds,
        "reaction_role_inline": reaction_role_inline
    }

@app.post("/api/guild/settings")
async def save_guild_settings(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]
    
    data = await request.json()
    raw_guild_id = data.get("guild_id")
    if raw_guild_id is None:
        raise HTTPException(status_code=400, detail="Missing guild_id in request body.")
    guild_id = _parse_integer(raw_guild_id, "guild_id", minimum=1)
    
    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to edit settings for this server.")
        
    guild = _find_guild(guild_id)
    if guild is None:
        raise HTTPException(status_code=404, detail="Server not found.")
    
    # 1. Prefix
    if "prefix" in data:
        prefix_val = data.get("prefix")
        from utils.prefix_manager import prefix_manager
        if prefix_val is not None and str(prefix_val).strip() != "":
            success, msg = await prefix_manager.set_guild_prefix(guild_id, str(prefix_val))
            if not success:
                raise HTTPException(status_code=400, detail=msg)
        else:
            await prefix_manager.remove_guild_prefix(guild_id)
            
    # 2. Volume
    if "default_volume" in data:
        default_vol = data.get("default_volume")
        if default_vol is not None:
            volume = _parse_integer(default_vol, "default_volume", minimum=1, maximum=100)
            await guild_model.set_default_volume(guild_id, volume)
        
    # 3. Channel
    if "music_channel_id" in data:
        music_chan_id = data.get("music_channel_id")
        if music_chan_id is not None and music_chan_id != "" and str(music_chan_id).lower() != "null":
            channel_id = _parse_integer(music_chan_id, "music_channel_id", minimum=1)
            channel = guild.get_channel(channel_id)
            if channel is None or not isinstance(channel, discord.TextChannel):
                raise HTTPException(status_code=400, detail="music_channel_id must identify a text channel in this server.")
            await guild_model.set_music_channel(guild_id, channel_id)
        else:
            await guild_model.remove_music_channel(guild_id)

    # 4. Leveling Master & Alert Config / Level Channel
    if "leveling_enabled" in data:
        await guild_model.set_leveling_enabled(guild_id, bool(data["leveling_enabled"]))

    if "level_alert_config" in data:
        cfg = data.get("level_alert_config")
        if isinstance(cfg, dict):
            raw_c = cfg.get("channel_id")
            if raw_c is not None and str(raw_c).lower() not in ("null", "none", ""):
                c_id = _parse_integer(raw_c, "level_alert_config.channel_id", minimum=1)
                ch = guild.get_channel(c_id)
                if ch is None or not isinstance(ch, discord.TextChannel):
                    raise HTTPException(status_code=400, detail="level_alert_config.channel_id must identify a text channel in this server.")
                cfg["channel_id"] = c_id
            else:
                cfg["channel_id"] = None
            await guild_model.set_level_alert_config(guild_id, cfg)
    elif "level_channel_id" in data:
        level_chan_id = data.get("level_channel_id")
        if level_chan_id is not None and level_chan_id != "" and str(level_chan_id).lower() not in ("null", "none"):
            channel_id = _parse_integer(level_chan_id, "level_channel_id", minimum=1)
            channel = guild.get_channel(channel_id)
            if channel is None or not isinstance(channel, discord.TextChannel):
                raise HTTPException(status_code=400, detail="level_channel_id must identify a text channel in this server.")
            await guild_model.set_level_channel(guild_id, channel_id)
        else:
            await guild_model.remove_level_channel(guild_id)
        
    # 5. Locale
    if "locale" in data:
        locale = data.get("locale")
        if locale:
            from utils.i18n import i18n
            if not await i18n.set_guild_locale(guild_id, str(locale)):
                raise HTTPException(status_code=400, detail="Unsupported locale.")
        
    # 5. AutoRole
    if "auto_role_id" in data:
        auto_role_id_raw = data.get("auto_role_id")
        auto_role_id = None
        if auto_role_id_raw and str(auto_role_id_raw).lower() not in ("none", "null", ""):
            auto_role_id = _parse_integer(auto_role_id_raw, "auto_role_id", minimum=1)
            if guild.get_role(auto_role_id) is None:
                raise HTTPException(status_code=400, detail="auto_role_id must identify a role in this server.")
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
        if not isinstance(reaction_roles, dict):
            raise HTTPException(status_code=400, detail="reaction_roles must be an object.")

        stored_guild = await guild_model.get_guild(guild_id)
        stored_role_names = stored_guild.get("reaction_role_names", {}) if stored_guild else {}
        if not isinstance(stored_role_names, dict):
            stored_role_names = {}
        provided_role_names = data.get("reaction_role_names", {})
        if not isinstance(provided_role_names, dict):
            provided_role_names = {}
        stored_embed_settings = stored_guild.get("reaction_role_embeds", {}) if stored_guild else {}
        if not isinstance(stored_embed_settings, dict):
            stored_embed_settings = {}
        provided_embed_settings = data.get("reaction_role_embeds", {})
        if not isinstance(provided_embed_settings, dict):
            provided_embed_settings = {}
        stored_inline_settings = stored_guild.get("reaction_role_inline", {}) if stored_guild else {}
        if not isinstance(stored_inline_settings, dict):
            stored_inline_settings = {}
        provided_inline_settings = data.get("reaction_role_inline", {})
        if not isinstance(provided_inline_settings, dict):
            provided_inline_settings = {}

        normalized_reaction_roles = {}
        reaction_role_names = {}
        reaction_role_embeds = {}
        reaction_role_inline = {}
        for message_id, emoji_roles in reaction_roles.items():
            if not isinstance(emoji_roles, dict):
                continue
            normalized_emoji_roles = {}
            for raw_role_id in emoji_roles.values():
                try:
                    role_id = str(_parse_integer(raw_role_id, "role_id", minimum=1))
                except HTTPException:
                    raise HTTPException(status_code=400, detail="reaction role IDs must be positive integers.")
                try:
                    role = guild.get_role(int(role_id))
                except (TypeError, ValueError):
                    role = None
                role_name = role.name if role else provided_role_names.get(role_id) or stored_role_names.get(role_id)
                if role_name:
                    reaction_role_names[role_id] = str(role_name)

            for emoji, raw_role_id in emoji_roles.items():
                role_id = str(_parse_integer(raw_role_id, "role_id", minimum=1))
                normalized_emoji_roles[str(emoji)] = int(role_id)
            message_key = str(message_id)
            normalized_reaction_roles[message_key] = normalized_emoji_roles

            provided_embed = provided_embed_settings.get(message_key, {})
            stored_embed = stored_embed_settings.get(message_key, {})
            if not isinstance(provided_embed, dict):
                provided_embed = {}
            if not isinstance(stored_embed, dict):
                stored_embed = {}
            embed_settings = {}
            for field in ("title", "description", "image_url", "thumbnail_url", "footer_text", "footer_url"):
                if field in provided_embed:
                    embed_settings[field] = str(provided_embed[field] or "")
                elif field in stored_embed:
                    embed_settings[field] = str(stored_embed[field] or "")
            if embed_settings:
                reaction_role_embeds[message_key] = embed_settings

            provided_inline = provided_inline_settings.get(message_key, {})
            stored_inline = stored_inline_settings.get(message_key, {})
            if not isinstance(provided_inline, dict):
                provided_inline = {}
            if not isinstance(stored_inline, dict):
                stored_inline = {}
            reaction_role_inline[message_key] = {
                emoji: bool(provided_inline[emoji]) if emoji in provided_inline
                else bool(stored_inline.get(emoji, True))
                for emoji in normalized_emoji_roles
            }

        await guild_model.update_guild(guild_id, {
            "reaction_roles": normalized_reaction_roles,
            "reaction_role_names": reaction_role_names,
            "reaction_role_embeds": reaction_role_embeds,
            "reaction_role_inline": reaction_role_inline
        })
        
        # Update ReactionRoles cog cache
        for b in getattr(bot_instance, "all_bots", [bot_instance]):
            rr_cog = b.get_cog('ReactionRolesCog')
            if rr_cog:
                rr_cog.reaction_roles[guild_id] = {}
                for msg_id_str, emoji_roles in normalized_reaction_roles.items():
                    msg_id = int(msg_id_str)
                    rr_cog.reaction_roles[guild_id][msg_id] = {}
                    for emoji, role_id in emoji_roles.items():
                        rr_cog.reaction_roles[guild_id][msg_id][emoji] = role_id
                        
                # Sync live embeds and reactions
                import asyncio
                async def sync_messages(cog, gid, r_roles):
                    guild = cog.bot.get_guild(gid)
                    if not guild:
                        return
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
                            except discord.HTTPException:
                                pass
                
                asyncio.create_task(sync_messages(rr_cog, guild_id, normalized_reaction_roles))
        
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


# --- Leveling & Leaderboard Endpoints ---
@app.get("/api/leveling/guilds")
async def list_leveling_guilds(request: Request):
    """List all Discord servers the bot is currently in for the leaderboard selector."""
    guilds_list = []
    seen_ids = set()
    if bot_instance:
        for b in getattr(bot_instance, "all_bots", [bot_instance]):
            for g in getattr(b, "guilds", []):
                if g.id not in seen_ids:
                    seen_ids.add(g.id)
                    icon_url = str(g.icon.url) if g.icon else ""
                    guilds_list.append({
                        "id": str(g.id),
                        "name": g.name,
                        "icon": icon_url
                    })
    guilds_list.sort(key=lambda x: x["name"].lower())
    return guilds_list


@app.get("/api/leveling/leaderboard/global")
async def get_global_leaderboard_endpoint(request: Request, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=50)):
    """Get paginated global XP leaderboard across all servers."""
    return await leveling_model.get_global_leaderboard(page=page, limit=limit)


@app.get("/api/leveling/leaderboard/server/{guild_id}")
async def get_server_leaderboard_endpoint(request: Request, guild_id: int, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=50)):
    """Get paginated server XP leaderboard for a specific guild."""
    data = await leveling_model.get_guild_leaderboard(guild_id=guild_id, page=page, limit=limit)
    guild = None
    if bot_instance:
        for b in getattr(bot_instance, "all_bots", [bot_instance]):
            guild = b.get_guild(guild_id)
            if guild:
                break
    if guild:
        data["guild_name"] = guild.name
        data["guild_icon"] = str(guild.icon.url) if guild.icon else ""
    return data


# --- Custom Commands Endpoints ---
def serialize_custom_command(cmd: Optional[dict]) -> dict:
    if not cmd:
        return {}
    res = dict(cmd)
    if "_id" in res:
        res["_id"] = str(res["_id"])
    if hasattr(res.get("created_at"), "isoformat"):
        res["created_at"] = res["created_at"].isoformat()
    if hasattr(res.get("updated_at"), "isoformat"):
        res["updated_at"] = res["updated_at"].isoformat()
    return res


@app.get("/api/guild/custom-commands")
async def get_custom_commands(request: Request, guild_id: int):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]

    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to view custom commands for this server.")

    commands_list = await custom_command_model.get_guild_commands(guild_id)
    return {"commands": [serialize_custom_command(cmd) for cmd in commands_list]}


@app.post("/api/guild/custom-commands/preview")
async def preview_custom_command(request: Request):
    user = get_user_from_request(request)
    data = await request.json()

    author_name = user.get("username", "User") if user else "User"
    user_id_str = str(user.get("id", "123456789")) if user else "123456789"
    args_text = str(data.get("args_text", "sample argument"))

    def parse_preview_str(template: str) -> str:
        if not template:
            return ""
        args_list = args_text.split() if args_text else []
        from datetime import datetime, UTC
        placeholders = {
            "{user}": author_name,
            "{author}": author_name,
            "{username}": author_name,
            "{author_mention}": f"@{author_name}",
            "{user_mention}": f"@{author_name}",
            "{mention}": f"@{author_name}",
            "{author_id}": user_id_str,
            "{user_id}": user_id_str,
            "{server}": "Ayla Community",
            "{guild}": "Ayla Community",
            "{server_id}": "100000000000000000",
            "{guild_id}": "100000000000000000",
            "{channel}": "general",
            "{channel_mention}": "#general",
            "{channel_id}": "200000000000000000",
            "{member_count}": "150",
            "{date}": datetime.now(UTC).strftime('%Y-%m-%d'),
            "{time}": datetime.now(UTC).strftime('%H:%M:%S UTC'),
            "{args}": args_text,
        }
        for i in range(1, 10):
            placeholders[f"{{arg{i}}}"] = args_list[i - 1] if i - 1 < len(args_list) else ""
        res = template
        for k, v in placeholders.items():
            res = res.replace(k, v)
        return res

    raw_response = data.get("response", "")
    parsed_text = parse_preview_str(raw_response) if raw_response else ""

    is_embed = bool(data.get("is_embed", False))
    raw_embed = data.get("embed_config") or {}

    parsed_embed = None
    if is_embed:
        parsed_embed = {
            "title": parse_preview_str(raw_embed.get("title", "")),
            "description": parse_preview_str(raw_embed.get("description", "")),
            "color": raw_embed.get("color", "#5865F2"),
            "image_url": parse_preview_str(raw_embed.get("image_url", "")),
            "thumbnail_url": parse_preview_str(raw_embed.get("thumbnail_url", "")),
            "footer_text": parse_preview_str(raw_embed.get("footer_text", "")),
            "footer_icon": parse_preview_str(raw_embed.get("footer_icon", ""))
        }

    return {
        "parsed_text": parsed_text,
        "parsed_embed": parsed_embed
    }


@app.post("/api/guild/custom-commands")
async def create_custom_command_endpoint(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]

    data = await request.json()
    raw_guild_id = data.get("guild_id")
    if raw_guild_id is None:
        raise HTTPException(status_code=400, detail="Missing guild_id.")
    try:
        guild_id = int(raw_guild_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid guild_id.")

    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to create custom commands for this server.")

    name = str(data.get("name", "")).strip().lower()
    if not name or not re.match(r'^[a-z0-9_-]+$', name):
        raise HTTPException(status_code=400, detail="Invalid command trigger name. Only alphanumeric characters, dashes, and underscores are allowed.")

    if bot_instance and bot_instance.get_command(name):
        raise HTTPException(status_code=400, detail=f"Command '{name}' conflicts with a built-in bot command.")

    existing = await custom_command_model.get_command(guild_id, name)
    if existing:
        raise HTTPException(status_code=400, detail=f"Custom command '{name}' already exists.")

    max_commands = await guild_model.get_max_custom_commands(guild_id)
    guild_cmds = await custom_command_model.get_guild_commands(guild_id)
    if len(guild_cmds) >= max_commands:
        raise HTTPException(status_code=400, detail=f"Maximum custom commands limit ({max_commands}) reached for this server.")

    created = await custom_command_model.create_command(
        guild_id=guild_id,
        name=name,
        response=str(data.get("response", "")),
        description=str(data.get("description", "")),
        is_embed=bool(data.get("is_embed", False)),
        embed_config=data.get("embed_config") or {},
        created_by=int(user_id),
        created_by_name=user.get("username", "Unknown"),
        enabled=bool(data.get("enabled", True))
    )
    if not created:
        raise HTTPException(status_code=500, detail="Failed to create custom command.")

    return {"success": True, "command": serialize_custom_command(created)}


@app.post("/api/guild/custom-commands/update")
async def update_custom_command_endpoint(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]

    data = await request.json()
    raw_guild_id = data.get("guild_id")
    if raw_guild_id is None:
        raise HTTPException(status_code=400, detail="Missing guild_id.")
    try:
        guild_id = int(raw_guild_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid guild_id.")

    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to update custom commands for this server.")

    name = str(data.get("name", "")).strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Command name is required.")

    existing = await custom_command_model.get_command(guild_id, name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Custom command '{name}' not found.")

    update_data = {
        "response": str(data.get("response", "")),
        "description": str(data.get("description", "")),
        "is_embed": bool(data.get("is_embed", False)),
        "embed_config": data.get("embed_config") or {},
        "enabled": bool(data.get("enabled", True))
    }

    await custom_command_model.update_command(guild_id, name, update_data)
    updated = await custom_command_model.get_command(guild_id, name)
    return {"success": True, "command": serialize_custom_command(updated)}


@app.post("/api/guild/custom-commands/delete")
async def delete_custom_command_endpoint(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]

    data = await request.json()
    raw_guild_id = data.get("guild_id")
    if raw_guild_id is None:
        raise HTTPException(status_code=400, detail="Missing guild_id.")
    try:
        guild_id = int(raw_guild_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid guild_id.")

    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to delete custom commands for this server.")

    name = str(data.get("name", "")).strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Command name is required.")

    existing = await custom_command_model.get_command(guild_id, name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Custom command '{name}' not found.")

    success = await custom_command_model.delete_command(guild_id, name)
    return {"success": success}


@app.post("/api/guild/custom-commands/toggle")
async def toggle_custom_command_endpoint(request: Request):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    user_id = user["id"]

    data = await request.json()
    raw_guild_id = data.get("guild_id")
    if raw_guild_id is None:
        raise HTTPException(status_code=400, detail="Missing guild_id.")
    try:
        guild_id = int(raw_guild_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid guild_id.")

    can_edit = await check_user_guild_permission(user_id, guild_id)
    if not can_edit:
        raise HTTPException(status_code=403, detail="You do not have permission to toggle custom commands for this server.")

    name = str(data.get("name", "")).strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Command name is required.")

    existing = await custom_command_model.get_command(guild_id, name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Custom command '{name}' not found.")

    new_status = bool(data.get("enabled", not existing.get("enabled", True)))
    success = await custom_command_model.update_command(guild_id, name, {"enabled": new_status})
    return {"success": success, "enabled": new_status}


# Serves Frontend SPA
@app.get("/{path:path}")
async def serve_frontend(request: Request, path: str):
    # If API call, proceed as usual (should fall under routing above, but catches fallthroughs)
    if path.startswith("api/") or path.startswith("login"):
        raise HTTPException(status_code=404, detail="Not Found")
        
    # Check if file exists in static folder
    static_file = (STATIC_DIR / path).resolve()
    if path and static_file.is_relative_to(STATIC_DIR) and static_file.is_file():
        headers = IMMUTABLE_CACHE_HEADERS if static_file.parent.name == 'assets' else NO_CACHE_HEADERS
        return FileResponse(static_file, headers=headers)
        
    # Serve landing page for root
    if not path or path == "" or path == "landing":
        landing_index = STATIC_DIR / "index.html"
        if landing_index.is_file():
            return FileResponse(landing_index, headers=NO_CACHE_HEADERS)
            
    # Fallback to serving SPA webplayer.html for frontend routing
    spa_index = STATIC_DIR / "webplayer.html"
    if spa_index.is_file():
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
        signer = TimestampSigner(Config.SESSION_SECRET_KEY)
        
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
