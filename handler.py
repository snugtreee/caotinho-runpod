import runpod
import subprocess
import os
import requests
import tempfile
import uuid
import boto3
from pathlib import Path

R2_ENDPOINT   = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET", "caotinho")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def download_file(url: str, dest: str) -> str:
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return dest


def upload_to_r2(local_path: str, key: str, content_type="video/mp4") -> str:
    get_s3().upload_file(local_path, R2_BUCKET, key, ExtraArgs={"ContentType": content_type})
    return f"{R2_PUBLIC_URL}/{key}"


def presign_upload(key: str, content_type: str, expires: int = 3600) -> dict:
    """Generate a presigned PUT URL so the browser can upload directly to R2."""
    url = get_s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
        HttpMethod="PUT",
    )
    return {
        "upload_url": url,
        "public_url": f"{R2_PUBLIC_URL}/{key}",
    }


def concatenate_videos(paths: list, output: str) -> str:
    list_file = output + ".txt"
    with open(list_file, "w") as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output],
        capture_output=True, text=True
    )
    os.remove(list_file)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed:\n{r.stderr}")
    return output


def add_music(video: str, music: str, output: str) -> str:
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", video,
        "-stream_loop", "-1",
        "-i", music,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        output,
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg add_music failed:\n{r.stderr}")
    return output


def handler(job: dict) -> dict:
    inp = job.get("input", {})
    action = inp.get("action", "")

    # ── presign: gera URL para o browser fazer upload direto ──
    if action == "presign":
        key          = inp.get("key", f"caotinho/music/{uuid.uuid4()}.mp3")
        content_type = inp.get("content_type", "audio/mpeg")
        print(f"[handler] Generating presigned URL for {key}")
        return presign_upload(key, content_type)

    # ── concat + mix (fluxo principal) ──
    video_urls = inp.get("video_urls", [])
    music_url  = inp.get("music_url", "")
    output_key = inp.get("output_key", f"caotinho/{uuid.uuid4()}.mp4")

    if not video_urls:
        return {"error": "video_urls is required"}
    if not music_url:
        return {"error": "music_url is required"}
    if not music_url.startswith("http"):
        return {"error": f"music_url must be a valid URL, got: {music_url[:80]}"}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        video_paths = []
        for i, url in enumerate(video_urls):
            ext  = Path(url.split("?")[0]).suffix or ".mp4"
            dest = str(tmp / f"clip_{i:03d}{ext}")
            print(f"[handler] Downloading clip {i+1}/{len(video_urls)}")
            download_file(url, dest)
            video_paths.append(dest)

        music_ext  = Path(music_url.split("?")[0]).suffix or ".mp3"
        music_path = str(tmp / f"music{music_ext}")
        print("[handler] Downloading music")
        download_file(music_url, music_path)

        concat = str(tmp / "concat.mp4")
        print("[handler] Concatenating clips")
        concatenate_videos(video_paths, concat)

        final = str(tmp / "final.mp4")
        print("[handler] Adding music")
        add_music(concat, music_path, final)

        print(f"[handler] Uploading to R2: {output_key}")
        public_url = upload_to_r2(final, output_key)

    print(f"[handler] Done → {public_url}")
    return {"output_url": public_url}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
