"""
YouTube MP3 Downloader - Flask Backend
Downloads audio from YouTube videos and converts to MP3.
"""

import os
import uuid
import re
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template

import yt_dlp

app = Flask(__name__)

# Directory to store downloaded files temporarily
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Track download progress per task
tasks: dict[str, dict] = {}

# Auto-cleanup: delete files older than 10 minutes
CLEANUP_INTERVAL = 60  # seconds
MAX_FILE_AGE = 600  # 10 minutes


def cleanup_old_files():
    """Remove downloaded files older than MAX_FILE_AGE."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file() and (now - f.stat().st_mtime) > MAX_FILE_AGE:
                try:
                    f.unlink()
                except OSError:
                    pass
        # Also clean up completed/failed tasks older than 10 min
        expired = [
            tid for tid, t in tasks.items()
            if t.get("completed_at") and (now - t["completed_at"]) > MAX_FILE_AGE
        ]
        for tid in expired:
            tasks.pop(tid, None)


cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()


def is_valid_youtube_url(url: str) -> bool:
    """Validate that the URL is a YouTube link."""
    patterns = [
        r'(https?://)?(www\.)?youtube\.com/watch\?v=[\w-]+',
        r'(https?://)?(www\.)?youtu\.be/[\w-]+',
        r'(https?://)?(www\.)?youtube\.com/shorts/[\w-]+',
        r'(https?://)?music\.youtube\.com/watch\?v=[\w-]+',
    ]
    return any(re.match(p, url.strip()) for p in patterns)


def progress_hook(task_id):
    """Create a progress hook for yt-dlp."""
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                tasks[task_id]['progress'] = round((downloaded / total) * 100, 1)
            tasks[task_id]['status'] = 'downloading'
        elif d['status'] == 'finished':
            tasks[task_id]['status'] = 'converting'
            tasks[task_id]['progress'] = 100
    return hook


def download_audio(task_id: str, url: str):
    """Download audio from YouTube in a background thread."""
    file_id = str(uuid.uuid4())
    output_path = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'progress_hooks': [progress_hook(task_id)],
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['mediaconnect'],
            },
        },
        'extractor_retries': 5,
        'retries': 5,
        'geo_bypass': True,
        'socket_timeout': 30,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'audio')
            duration = info.get('duration', 0)
            thumbnail = info.get('thumbnail', '')
            channel = info.get('channel', info.get('uploader', ''))

        # Find the downloaded mp3 file
        mp3_path = DOWNLOAD_DIR / f"{file_id}.mp3"
        if not mp3_path.exists():
            # Sometimes yt-dlp uses a different extension before converting
            for f in DOWNLOAD_DIR.glob(f"{file_id}.*"):
                if f.suffix == '.mp3':
                    mp3_path = f
                    break

        if mp3_path.exists():
            file_size = mp3_path.stat().st_size
            tasks[task_id].update({
                'status': 'completed',
                'progress': 100,
                'title': title,
                'duration': duration,
                'thumbnail': thumbnail,
                'channel': channel,
                'file_path': str(mp3_path),
                'file_name': f"{title}.mp3",
                'file_size': file_size,
                'completed_at': time.time(),
            })
        else:
            tasks[task_id].update({
                'status': 'error',
                'error': 'فشل في تحويل الملف إلى MP3. تأكد من تثبيت FFmpeg.',
                'completed_at': time.time(),
            })

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if 'Sign in' in error_msg or 'bot' in error_msg.lower():
            friendly = 'يوتيوب يطلب تسجيل دخول. جرّب رابط فيديو ثاني.'
        elif 'Private' in error_msg:
            friendly = 'الفيديو خاص ولا يمكن تحميله.'
        elif 'unavailable' in error_msg.lower():
            friendly = 'الفيديو غير متاح أو محذوف.'
        else:
            friendly = f'فشل في تحميل الفيديو: {error_msg[:200]}'
        tasks[task_id].update({
            'status': 'error',
            'error': friendly,
            'completed_at': time.time(),
        })
    except Exception as e:
        tasks[task_id].update({
            'status': 'error',
            'error': f'حدث خطأ غير متوقع: {str(e)}',
            'completed_at': time.time(),
        })


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/download', methods=['POST'])
def start_download():
    """Start an audio download task."""
    data = request.get_json()
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'error': 'الرجاء إدخال رابط يوتيوب'}), 400

    if not is_valid_youtube_url(url):
        return jsonify({'error': 'الرابط غير صالح. الرجاء إدخال رابط يوتيوب صحيح'}), 400

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        'status': 'starting',
        'progress': 0,
        'url': url,
    }

    thread = threading.Thread(target=download_audio, args=(task_id, url))
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id})


@app.route('/api/status/<task_id>')
def check_status(task_id):
    """Check the status of a download task."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'المهمة غير موجودة'}), 404

    # Don't expose file_path to the client
    safe_task = {k: v for k, v in task.items() if k != 'file_path'}
    return jsonify(safe_task)


@app.route('/api/file/<task_id>')
def download_file(task_id):
    """Download the converted MP3 file."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'المهمة غير موجودة'}), 404

    if task['status'] != 'completed':
        return jsonify({'error': 'الملف غير جاهز بعد'}), 400

    file_path = task.get('file_path')
    if not file_path or not Path(file_path).exists():
        return jsonify({'error': 'الملف غير موجود'}), 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=task.get('file_name', 'audio.mp3'),
        mimetype='audio/mpeg',
    )


if __name__ == '__main__':
    print("=" * 50)
    print("  YouTube MP3 Downloader")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(debug=True, host='0.0.0.0', port=5000)
