from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import os
import json

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
        # 'ffmpeg_location': '/opt/homebrew/bin/ffmpeg', # Removed for Docker/Render compatibility
        # Removing manual player_client to let yt-dlp use defaults (e.g. android_sdkless) which works better
    }

    print(f"DEBUG: download_video opts (default clients)")

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
        format_str = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]'
        
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
        ydl_opts = {
            'quiet': True,
            'nocheckcertificate': True,
            # Let defaults handle clients
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"DEBUG: Fetching info for {url} (default clients)")
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

