from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from datetime import datetime, timezone, timedelta  # Add timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
import time 
import yt_dlp
import os
import re
import uuid
import shutil
import sqlite3
import cloudinary
import cloudinary.uploader
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://ytigdown.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()
COOKIES_FILE = "cookies.txt"

PUBLIC_URL = os.getenv("PUBLIC_URL")
IG_ID = os.getenv("IG_ID")
ACCESS_TOKEN = os.getenv("LongLived_AccessToken")

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)
TUNIS_TZ = ZoneInfo("Africa/Tunis")

def local_to_utc(naive_local_str: str) -> datetime:
    """Convert local time string to UTC datetime object"""
    dt = datetime.fromisoformat(naive_local_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TUNIS_TZ)
    return dt.astimezone(timezone.utc)

def utc_now() -> datetime:
    """Return current UTC datetime"""
    return datetime.now(timezone.utc)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
REQUEST_TIMEOUT = 30  # seconds, applies to each individual HTTP call
MAX_PUBLISH_RETRIES = 3
DB = sqlite3.connect("videos.db", check_same_thread=False)

DB.execute("""
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    youtube_url TEXT NOT NULL,
    title TEXT NOT NULL,
    video_path TEXT NOT NULL,
    cloudinary_url TEXT,
    cloudinary_public_id TEXT,
    published INTEGER DEFAULT 0,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
DB.commit()

# platform column kept for backward compatibility with existing rows,
# but every new insert always writes 'instagram'
DB.execute("""
CREATE TABLE IF NOT EXISTS scheduled_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'instagram',
    publish_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES videos(id)
)
""")
DB.commit()

FFMPEG_PATH = shutil.which("ffmpeg")

def sanitize_filename(title: str, max_length: int = 150) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "video"
    return cleaned[:max_length]

# ============= DOWNLOAD ENDPOINT =============
@app.post("/download")
async def download_video(url: str = Form(...)):
    if not FFMPEG_PATH:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg not found on this system. Install it and ensure it's on PATH.",
        )

    video_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_PATH,
        "cookiefile": COOKIES_FILE,  # Add this line
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            final_path = ydl.prepare_filename(info)
            base, _ = os.path.splitext(final_path)
            final_path = base + ".mp4"

        if not os.path.exists(final_path):
            raise HTTPException(
                status_code=500,
                detail="Merged file not found after download.",
            )

        title = info.get("title", "video")
        description = info.get("description") or ""

        upload = cloudinary.uploader.upload(
            final_path,
            resource_type="video"
        )

        cloudinary_url = upload["secure_url"]
        cloudinary_public_id = upload["public_id"]

        print("Cloudinary URL:", cloudinary_url)
        print("Cloudinary ID:", cloudinary_public_id)

        DB.execute(
            """
            INSERT INTO videos (
                id,
                youtube_url,
                title,
                video_path,
                description,
                cloudinary_url,
                cloudinary_public_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                url,
                title,
                final_path,
                description,
                cloudinary_url,
                cloudinary_public_id,
            ),
        )
        DB.commit()

        hashtags = re.findall(r"#\w+", description)
        hashtags = list(dict.fromkeys(hashtags))

        return {
            "id": video_id,
            "title": title,
            "description": description,
            "hashtags": hashtags,
            "cloudinary_url": cloudinary_url
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============= GET VIDEO ENDPOINT =============
@app.api_route("/video/{video_id}", methods=["GET", "HEAD"])
async def get_video(video_id: str):
    cursor = DB.execute(
        "SELECT video_path, title FROM videos WHERE id = ?",
        (video_id,),
    )

    row = cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Video not found")

    video_path, title = row

    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video file missing")

    return FileResponse(
        path=video_path,
        media_type="video/mp4",
    )

# ============= INTERNAL: PUBLISH TO INSTAGRAM =============
def _publish_to_instagram(video_url: str, caption: str) -> dict:
    def _post_with_retry(url, data, attempts=MAX_PUBLISH_RETRIES):
        last_err = None
        for i in range(attempts):
            try:
                return requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as e:
                last_err = e
                print(f"Network error (attempt {i+1}/{attempts}): {e}")
                if i < attempts - 1:
                    time.sleep(2 ** i)  # 1s, 2s, 4s backoff
        raise HTTPException(500, f"Network error contacting Instagram: {last_err}")

    def _get_with_retry(url, params, attempts=MAX_PUBLISH_RETRIES):
        last_err = None
        for i in range(attempts):
            try:
                return requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as e:
                last_err = e
                print(f"Network error (attempt {i+1}/{attempts}): {e}")
                if i < attempts - 1:
                    time.sleep(2 ** i)
        raise HTTPException(500, f"Network error contacting Instagram: {last_err}")

    create = _post_with_retry(
        f"https://graph.facebook.com/v26.0/{IG_ID}/media",
        {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": ACCESS_TOKEN,
        },
    )

    print("Create response:", create.text)
    if create.status_code != 200:
        raise HTTPException(500, create.text)

    container_id = create.json()["id"]
    print("Container ID:", container_id)

    attempts = 0
    max_attempts = 30
    while attempts < max_attempts:
        status = _get_with_retry(
            f"https://graph.facebook.com/v26.0/{container_id}",
            {"fields": "status_code,status", "access_token": ACCESS_TOKEN},
        )
        body = status.json()
        print(f"Status check {attempts + 1}:", body)
        code = body.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise HTTPException(500, f"Container processing error: {body}")
        attempts += 1
        time.sleep(5)

    if attempts >= max_attempts:
        raise HTTPException(500, "Container processing timed out")

    publish = _post_with_retry(
        f"https://graph.facebook.com/v26.0/{IG_ID}/media_publish",
        {"creation_id": container_id, "access_token": ACCESS_TOKEN},
    )

    print("Publish response:", publish.text)
    if publish.status_code != 200:
        raise HTTPException(500, publish.text)

    return publish.json()


@app.get("/scheduled-posts")
async def get_scheduled_posts(status: str = None, limit: int = 20, offset: int = 0):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    base_query = """
        SELECT sp.*, v.title, v.cloudinary_url
        FROM scheduled_posts sp
        LEFT JOIN videos v ON sp.video_id = v.id
    """
    count_query = "SELECT COUNT(*) FROM scheduled_posts sp"
    params, count_params = [], []

    if status:
        base_query += " WHERE sp.status = ?"
        count_query += " WHERE sp.status = ?"
        params.append(status)
        count_params.append(status)

    base_query += " ORDER BY sp.publish_at ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = DB.execute(base_query, params).fetchall()
    total = DB.execute(count_query, count_params).fetchone()[0]

    scheduled_posts = [
        {
            "id": row[0],
            "video_id": row[1],
            "platform": row[2],
            "publish_at": row[3],
            "status": row[4],
            "created_at": row[5],
            "title": row[6] if row[6] is not None else "Deleted Video",
            "cloudinary_url": row[7],
        }
        for row in rows
    ]

    return {
        "items": scheduled_posts,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(scheduled_posts) < total,
    }

# ============= PUBLISH TEST ENDPOINT =============
@app.post("/publish-test/{video_id}")
async def publish_test(video_id: str, description: str = Form(None)):
    cursor = DB.execute(
        "SELECT video_path, description, cloudinary_url, cloudinary_public_id FROM videos WHERE id = ?",
        (video_id,),
    )

    row = cursor.fetchone()

    if row is None:
        raise HTTPException(404, "Video not found")

    video_path, stored_description, cloudinary_url, cloudinary_public_id = row

    if not os.path.exists(video_path):
        raise HTTPException(404, "Video file missing")

    # ✅ THE FIX: if the caller sends an (edited) description, persist it
    # and use it for the caption. Previously this endpoint only ever read
    # whatever was in the DB, which was still the original YouTube
    # description because nothing had written the edited text back yet.
    if description is not None and description != stored_description:
        DB.execute(
            "UPDATE videos SET description = ? WHERE id = ?",
            (description, video_id),
        )
        DB.commit()
        stored_description = description

    video_url = cloudinary_url
    print("Video URL:", video_url)

    result = _publish_to_instagram(video_url, stored_description)

    DB.execute(
        "UPDATE videos SET published = 1 WHERE id = ?",
        (video_id,)
    )

    cursor = DB.execute(
        "SELECT video_path, cloudinary_public_id FROM videos WHERE id = ?",
        (video_id,)
    )
    row = cursor.fetchone()

    if row:
        video_path, cloudinary_public_id = row

        if cloudinary_public_id:
            cloudinary.uploader.destroy(
                cloudinary_public_id,
                resource_type="video"
            )

        if os.path.exists(video_path):
            os.remove(video_path)

        DB.execute("DELETE FROM videos WHERE id = ?", (video_id,))

    DB.commit()

    return result

# ============= SCHEDULE POST ENDPOINT (Instagram only) =============
@app.post("/schedule")
async def schedule_post(
    video_id: str = Form(...),
    description: str = Form(...),
    publish_at: str = Form(...),
):
    cursor = DB.execute(
        "SELECT id, cloudinary_url FROM videos WHERE id = ?",
        (video_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise HTTPException(404, "Video not found")

    video_id, cloudinary_url = row
    if not cloudinary_url:
        raise HTTPException(400, "Video not uploaded to Cloudinary yet")

    try:
        publish_at_dt = local_to_utc(publish_at)
    except ValueError:
        raise HTTPException(400, "Invalid datetime format. Use YYYY-MM-DDTHH:MM.")

    # Compare datetime objects, not strings!
    if publish_at_dt <= utc_now():
        raise HTTPException(400, "Publish time must be in the future.")

    # Store as ISO string
    publish_at_utc = publish_at_dt.strftime("%Y-%m-%dT%H:%M:%S")

    DB.execute(
        """
        INSERT INTO scheduled_posts (video_id, platform, publish_at, status)
        VALUES (?, 'instagram', ?, 'pending')
        """,
        (video_id, publish_at_utc),
    )
    DB.commit()

    DB.execute("UPDATE videos SET description = ? WHERE id = ?", (description, video_id))
    DB.commit()

    return {
        "message": "Post scheduled successfully",
        "video_id": video_id,
        "publish_at_utc": publish_at_utc,
    }
# ============= GET SCHEDULED POSTS ENDPOINT =============
@app.get("/scheduled-posts")
async def get_scheduled_posts(status: str = None, limit: int = 20, offset: int = 0):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    base_query = """
        SELECT sp.*, v.title, v.cloudinary_url
        FROM scheduled_posts sp
        JOIN videos v ON sp.video_id = v.id
    """
    count_query = "SELECT COUNT(*) FROM scheduled_posts sp"
    params, count_params = [], []

    if status:
        base_query += " WHERE sp.status = ?"
        count_query += " WHERE sp.status = ?"
        params.append(status)
        count_params.append(status)

    base_query += " ORDER BY sp.publish_at ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = DB.execute(base_query, params).fetchall()
    total = DB.execute(count_query, count_params).fetchone()[0]

    scheduled_posts = [
        {
            "id": row[0],
            "video_id": row[1],
            "platform": row[2],
            "publish_at": row[3],
            "status": row[4],
            "created_at": row[5],
            "title": row[6],
            "cloudinary_url": row[7],
        }
        for row in rows
    ]

    return {
        "items": scheduled_posts,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(scheduled_posts) < total,
    }
# ============= GET VIDEOS ENDPOINT =============
@app.get("/videos")
async def get_videos():
    cursor = DB.execute(
        "SELECT id, title, description, published, cloudinary_url, created_at FROM videos ORDER BY created_at DESC"
    )
    rows = cursor.fetchall()

    videos = []
    for row in rows:
        videos.append({
            "id": row[0],
            "title": row[1],
            "description": row[2],
            "published": bool(row[3]),
            "cloudinary_url": row[4],
            "created_at": row[5]
        })

    return videos





@app.delete("/scheduled-posts/{schedule_id}")
async def delete_scheduled_post(schedule_id: int):
    cursor = DB.execute(
        "SELECT id, video_id, status FROM scheduled_posts WHERE id = ?",
        (schedule_id,),
    )
    row = cursor.fetchone()
    
    if row is None:
        raise HTTPException(404, "Scheduled post not found")
    
    # REMOVE THIS CHECK to allow deleting published posts
    # if row[2] == "published":
    #     raise HTTPException(400, "Cannot delete a published post")
    
    # Get the video_id before deleting
    video_id = row[1]
    
    # Delete the scheduled post
    DB.execute("DELETE FROM scheduled_posts WHERE id = ?", (schedule_id,))
    
    # Also delete the video if it exists
    cursor = DB.execute("SELECT video_path, cloudinary_public_id FROM videos WHERE id = ?", (video_id,))
    video_row = cursor.fetchone()
    
    if video_row:
        video_path, cloudinary_public_id = video_row
        
        # Delete from Cloudinary if exists
        if cloudinary_public_id:
            try:
                cloudinary.uploader.destroy(cloudinary_public_id, resource_type="video")
            except Exception as e:
                print(f"Error deleting from Cloudinary: {e}")
        
        # Delete local file if exists
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception as e:
                print(f"Error deleting local file: {e}")
        
        # Delete from database
        DB.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    
    DB.commit()
    
    return {"message": "Scheduled post and associated video deleted successfully", "id": schedule_id}

# ============= DELETE VIDEO ENDPOINT =============
@app.delete("/video/{video_id}")
async def delete_video(video_id: str):
    cursor = DB.execute(
        "SELECT video_path FROM videos WHERE id = ?",
        (video_id,),
    )
    row = cursor.fetchone()

    if row is None:
        raise HTTPException(404, "Video not found")

    video_path = row[0]

    DB.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    DB.execute("DELETE FROM scheduled_posts WHERE video_id = ?", (video_id,))
    DB.commit()

    if os.path.exists(video_path):
        os.remove(video_path)

    return {"message": "Video deleted successfully"}

# ============= CRON JOB: PUBLISH DUE SCHEDULED POSTS =============
def publish_scheduled_posts():
    now_iso = utc_now().strftime("%Y-%m-%dT%H:%M:%S")
    cursor = DB.execute(
        """
        SELECT sp.id, sp.video_id, v.video_path, v.cloudinary_url,
               v.cloudinary_public_id, v.description
        FROM scheduled_posts sp
        JOIN videos v ON sp.video_id = v.id
        WHERE sp.status = 'pending' AND sp.publish_at <= ? AND sp.platform = 'instagram'
        """,
        (now_iso,),
    )

    rows = cursor.fetchall()

    for row in rows:
        schedule_id, video_id, video_path, cloudinary_url, cloudinary_public_id, description = row

        try:
            if not cloudinary_url:
                raise RuntimeError("Missing cloudinary_url")

            _publish_to_instagram(cloudinary_url, description)

            DB.execute(
                "UPDATE scheduled_posts SET status = 'published' WHERE id = ?",
                (schedule_id,)
            )
            DB.execute(
                "UPDATE videos SET published = 1 WHERE id = ?",
                (video_id,)
            )
            DB.commit()
            print(f"✅ Published to Instagram: {video_id}")

            # cleanup
            if cloudinary_public_id:
                cloudinary.uploader.destroy(cloudinary_public_id, resource_type="video")
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
            DB.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            DB.commit()

        except Exception as e:
            print(f"❌ Error publishing scheduled post {schedule_id}: {str(e)}")
            DB.execute(
                "UPDATE scheduled_posts SET status = 'failed' WHERE id = ?",
                (schedule_id,)
            )
            DB.commit()
# ============= SCHEDULER SETUP =============
scheduler = BackgroundScheduler()
scheduler.add_job(publish_scheduled_posts, "interval", minutes=1, id="publish_scheduled_posts")

@app.on_event("startup")
def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        print("⏰ Scheduler started — checking for due posts every minute.")

@app.on_event("shutdown")
def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()

# ============= HEALTH CHECK ENDPOINT =============
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "ffmpeg": FFMPEG_PATH is not None,
        "cloudinary": True,
        "instagram_configured": bool(IG_ID and ACCESS_TOKEN),
        "scheduler_running": scheduler.running
    }

# ============= ROOT ENDPOINT =============
@app.get("/")
async def root():
    return {
        "message": "YouTube to Instagram API",
        "endpoints": {
            "POST /download": "Download video from YouTube",
            "GET /video/{video_id}": "Get video file",
            "POST /publish-test/{video_id}": "Immediately publish video to Instagram (optionally pass updated 'description' form field)",
            "POST /schedule": "Schedule a post for Instagram",
            "GET /scheduled-posts": "List scheduled posts",
            "GET /videos": "List all videos",
            "DELETE /video/{video_id}": "Delete a video",
            "GET /health": "Health check"
        }
    }