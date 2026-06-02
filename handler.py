import runpod
import subprocess
import os
import requests
import tempfile
import uuid
import boto3
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
# Set these as RunPod Secrets / env vars:
#   R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET, R2_PUBLIC_URL
# Or replace with any S3-compatible storage (AWS S3, Backblaze B2, etc.)

R2_ENDPOINT   = os.environ.get("R2_ENDPOINT", "")          # e.g. https://<accountid>.r2.cloudflarestorage.com
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET", "caotinho")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")        # e.g. https://pub-xxx.r2.dev


def download_file(url: str, dest_path: str) -> str:
    """Download a remote URL to dest_path. Returns dest_path."""
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest_path


def upload_to_r2(local_path: str, key: str) -> str:
    """Upload file to Cloudflare R2 and return public URL."""
    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
    )
    s3.upload_file(local_path, R2_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    return f"{R2_PUBLIC_URL}/{key}"


def concatenate_videos(video_paths: list[str], output_path: str) -> str:
    """
    Concatenate video files using FFmpeg concat demuxer.
    All input clips must have the same codec / resolution for lossless concat.
    If not, use the filter_complex path (re-encode, slower).
    """
    # Write a concat list file
    list_file = output_path + ".txt"
    with open(list_file, "w") as f:
        for p in video_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c", "copy",        # lossless stream copy — fast
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(list_file)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed:\n{result.stderr}")
    return output_path


def add_music(video_path: str, music_path: str, output_path: str) -> str:
    """
    Replace the audio track of video_path with music_path.
    Music is looped / trimmed to match the video duration.
    Original video audio is discarded.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1",   # loop music if shorter than video
        "-i", music_path,
        "-map", "0:v:0",        # video stream from first input
        "-map", "1:a:0",        # audio stream from second input
        "-shortest",            # stop when the shorter stream ends (video)
        "-c:v", "copy",         # copy video — no re-encode
        "-c:a", "aac",          # encode audio to AAC
        "-b:a", "192k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg add_music failed:\n{result.stderr}")
    return output_path


def handler(job: dict) -> dict:
    """
    RunPod Serverless entrypoint.

    Expected input:
    {
        "input": {
            "video_urls": ["https://...", "https://...", ...],   // ordered list of video clips
            "music_url":  "https://...",                         // background music
            "output_key": "optional/custom/key.mp4"             // optional R2 object key
        }
    }

    Returns:
    {
        "output_url": "https://pub-xxx.r2.dev/caotinho/uuid.mp4"
    }
    """
    job_input = job.get("input", {})
    video_urls: list[str] = job_input.get("video_urls", [])
    music_url:  str       = job_input.get("music_url", "")
    output_key: str       = job_input.get("output_key", f"caotinho/{uuid.uuid4()}.mp4")

    if not video_urls:
        return {"error": "video_urls is required and must not be empty"}
    if not music_url:
        return {"error": "music_url is required"}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # 1. Download all video clips
        video_paths = []
        for idx, url in enumerate(video_urls):
            ext = Path(url.split("?")[0]).suffix or ".mp4"
            dest = str(tmp / f"clip_{idx:03d}{ext}")
            print(f"[handler] Downloading clip {idx+1}/{len(video_urls)}: {url}")
            download_file(url, dest)
            video_paths.append(dest)

        # 2. Download music
        music_ext  = Path(music_url.split("?")[0]).suffix or ".mp3"
        music_path = str(tmp / f"music{music_ext}")
        print(f"[handler] Downloading music: {music_url}")
        download_file(music_url, music_path)

        # 3. Concatenate clips
        concat_path = str(tmp / "concat.mp4")
        print(f"[handler] Concatenating {len(video_paths)} clips...")
        concatenate_videos(video_paths, concat_path)

        # 4. Mix in music
        final_path = str(tmp / "final.mp4")
        print("[handler] Adding music...")
        add_music(concat_path, music_path, final_path)

        # 5. Upload to R2
        print(f"[handler] Uploading to R2 as {output_key}...")
        public_url = upload_to_r2(final_path, output_key)

    print(f"[handler] Done → {public_url}")
    return {"output_url": public_url}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
