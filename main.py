from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import os
import json
import base64

# Write cookies from Env Var to file at startup (for Render/backend compatibility)
if os.environ.get("YOUTUBE_COOKIES_B64"):
    try:
        with open("/tmp/cookies.txt", "wb") as f:
            f.write(base64.b64decode(os.environ["YOUTUBE_COOKIES_B64"]))
        print("DEBUG: YOUTUBE_COOKIES_B64 found and written to /tmp/cookies.txt")
    except Exception as e:
        print(f"ERROR: Failed to decode YOUTUBE_COOKIES_B64: {e}")

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Disposition"],
)

def get_ydl_opts(format_type: str = "video", quality: str = "best"):
    # Ensure ffmpeg available in PATH for yt-dlp
    os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin"
    
    
    common_opts = {
        'quiet': True,
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'logtostderr': False,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
        'force_ipv4': True,
        'cachedir': False,
    }

    # Dynamic Path Config (Local vs Docker)
    if os.path.exists('/opt/homebrew/bin/ffmpeg'):
        common_opts['ffmpeg_location'] = '/opt/homebrew/bin/ffmpeg'

    # Smart Auth Strategy
    # 1. Try Cookies First (Best for bypassing 'Sign in to confirm...')
    if os.path.exists("/tmp/cookies.txt"):
        common_opts['cookiefile'] = "/tmp/cookies.txt"
        print("DEBUG: Encrypted Cookies Loaded - Using Authentication")
    
    # 2. Advanced Bypass Clients (Always apply these as they augment auth)
    common_opts.update({
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android', 'web_creator', 'mweb', 'tv'],
                'skip': ['hls', 'dash']
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.google.com/',
        }
    })

    print(f"DEBUG: download_video opts (Using forced iOS/Android clients)")

    if format_type == "audio":
        return {
            **common_opts,
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': quality, 
            }],
            'outtmpl': '%(title)s.%(ext)s',
        }
    else:
        # Strict format selection
        format_str = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best'
        
        return {
            **common_opts,
            'format': format_str, 
            'outtmpl': '%(title)s.%(ext)s',
            'merge_output_format': 'mp4',
        }

@app.post("/api/info")
async def get_video_info(request: Request):
    data = await request.json()
    url = data.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    
    try:
        # Use the same robust options for info fetching
        ydl_opts = get_ydl_opts()
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"DEBUG: Fetching info for {url} (forced clients)")
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
            }
    except Exception as e:
        print(f"ERROR in get_video_info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/download")
async def download_video(request: Request):
    data = await request.json()
    url = data.get("url")
    format_type = data.get("format", "video") # video or audio
    quality = data.get("quality", "1080" if format_type == "video" else "192")
    
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    # Note: robust streaming download is complex with yt-dlp directly to response.
    # For now, we will download to a temp file and stream it back, then delete.
    # Ideally, we should use a proper job queue or stream directly if possible.
    
    try:
        ydl_opts = get_ydl_opts(format_type, quality)
        # Use a temporary directory or specific output path
        # For simplicity in this demo, downloading to current dir then streaming
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"DEBUG: Starting download for {url} with quality {quality}")
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Post-download verification:
            # When merging video+audio (e.g. into mp4), the extension might change.
            # We need to find the actual file that exists.
            
            if not os.path.exists(filename):
                # Check for merged file (video+audio usually results in .mp4 if we requested merge_output_format='mp4')
                base_name = os.path.splitext(filename)[0]
                potential_file = base_name + ".mp4"
                if os.path.exists(potential_file):
                    filename = potential_file
                else:
                     # Check with mkv if mp4 not found (default merge format sometimes)
                    potential_file = base_name + ".mkv"
                    if os.path.exists(potential_file):
                        filename = potential_file

            if format_type == "audio":
                 # For audio, we post-processed to mp3, so check that
                 filename = os.path.splitext(filename)[0] + ".mp3"

        if not os.path.exists(filename):
             raise Exception(f"Downloaded file not found at expected path: {filename}")

        print(f"DEBUG: Serving file {filename}, size: {os.path.getsize(filename)} bytes")

        def iterfile():
            try:
                with open(filename, mode="rb") as file_like:
                    yield from file_like
            finally:
                if os.path.exists(filename):
                    os.remove(filename)

        media_type = "audio/mpeg" if format_type == "audio" else "video/mp4"
        
        # Handle non-ascii filenames by url-encoding just in case, or creating a safe name
        safe_filename = os.path.basename(filename)
        try:
             safe_filename.encode('latin-1')
        except UnicodeEncodeError:
            # Fallback for filenames with special characters
            import urllib.parse
            safe_filename = urllib.parse.quote(safe_filename)

        file_size = os.path.getsize(filename)
        return StreamingResponse(
            iterfile(), 
            media_type=media_type, 
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}",
                "Content-Length": str(file_size)
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

