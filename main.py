from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import os
import json
import base64
import urllib.parse

# --- STARTUP COOKIE LOADING ---
COOKIE_PATH = "/tmp/cookies.txt"

def setup_cookies():
    raw_b64 = os.environ.get("YOUTUBE_COOKIES_B64")
    if not raw_b64:
        print("DEBUG: YOUTUBE_COOKIES_B64 environment variable not found.")
        return
    try:
        # Deep clean: remove any hidden spaces, tabs or newlines
        clean_data = "".join(raw_b64.split())
        decoded = base64.b64decode(clean_data)
        with open(COOKIE_PATH, "wb") as f:
            f.write(decoded)
        print(f"DEBUG: ✅ Cookies saved to {COOKIE_PATH} ({len(decoded)} bytes)")
    except Exception as e:
        print(f"ERROR: ❌ Cookie setup failed: {e}")

# Load immediately on module load
setup_cookies()

app = FastAPI()

@app.get("/")
async def health_check():
    cookie_preview = "None"
    file_exists = os.path.exists(COOKIE_PATH)
    file_size = 0
    if file_exists:
        try:
            file_size = os.path.getsize(COOKIE_PATH)
            with open(COOKIE_PATH, "r") as f:
                cookie_preview = f.read(50)
        except Exception as e:
            cookie_preview = f"Error: {str(e)}"
            
    return {
        "status": "online",
        "cookies_loaded": file_exists,
        "cookie_file_size": file_size,
        "cookie_file_preview": cookie_preview,
        "is_netscape_format": "# Netscape" in cookie_preview,
        "env_var_found": os.environ.get("YOUTUBE_COOKIES_B64") is not None
    }

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Disposition"],
)

def get_ydl_opts(format_type: str = "video", quality: str = "best"):
    # Add local path for ffmpeg (macOS dev support)
    if os.path.exists("/opt/homebrew/bin"):
        os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin"
        
    common_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'default_search': 'auto',
        'force_ipv4': True,
        'cachedir': False,
        'user_agent': os.environ.get("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    }

    # Inject Cookies
    if os.path.exists(COOKIE_PATH):
        common_opts['cookiefile'] = COOKIE_PATH
        # If we have cookies, we should use the web client often associated with browser cookies
        common_opts['extractor_args'] = {
            'youtube': {
                'player_client': ['web', 'web_creator'],
                'skip': ['hls', 'dash']
            }
        }
    else:
        # Fallback to mobile clients if no cookies
        common_opts['extractor_args'] = {
            'youtube': {
                'player_client': ['android', 'ios'],
                'skip': ['hls', 'dash']
            }
        }

    if format_type == "audio":
        common_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality,
            }],
            'outtmpl': '%(title)s.%(ext)s',
        })
    else:
        if quality == "best":
            f_str = 'bestvideo+bestaudio/best'
        else:
            f_str = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        
        common_opts.update({
            'format': f_str,
            'outtmpl': '%(title)s.%(ext)s',
            'merge_output_format': 'mp4',
        })

    return common_opts

@app.post("/api/info")
async def get_video_info(request: Request):
    data = await request.json()
    url = data.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    
    try:
        ydl_opts = get_ydl_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"DEBUG: Extracting info for {url}")
            info = ydl.extract_info(url, download=False)
            if not info:
                raise Exception("YouTube returned no info. This usually means the IP is blocked or cookies have expired.")
            
            return {
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
            }
    except Exception as e:
        err_msg = str(e)
        print(f"ERROR: {err_msg}")
        raise HTTPException(status_code=500, detail=err_msg)

@app.post("/api/download")
async def download_video(request: Request):
    data = await request.json()
    url = data.get("url")
    format_type = data.get("format", "video")
    quality = data.get("quality", "1080" if format_type == "video" else "192")
    
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    try:
        ydl_opts = get_ydl_opts(format_type, quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Merge extension check
            if not os.path.exists(filename):
                base = os.path.splitext(filename)[0]
                for ext in [".mp4", ".mkv", ".mp3"]:
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break

        if not os.path.exists(filename):
            raise Exception("File not found after download completion.")

        def iterfile():
            try:
                with open(filename, mode="rb") as f:
                    yield from f
            finally:
                if os.path.exists(filename):
                    os.remove(filename)

        media_type = "audio/mpeg" if format_type == "audio" else "video/mp4"
        safe_name = os.path.basename(filename)
        try:
            safe_name.encode('latin-1')
        except:
            safe_name = urllib.parse.quote(safe_name)

        return StreamingResponse(
            iterfile(),
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
                "Content-Length": str(os.path.getsize(filename))
            }
        )
    except Exception as e:
        print(f"ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
