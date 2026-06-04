import runpod
import subprocess
import os
import requests
import tempfile
import uuid
import json
import boto3
from pathlib import Path

R2_ENDPOINT   = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET     = os.environ.get("R2_BUCKET", "caotinho")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")


def get_s3():
    return boto3.client("s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto")


def download_file(url, dest):
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return dest


def upload_to_r2(local_path, key, content_type="video/mp4"):
    get_s3().upload_file(local_path, R2_BUCKET, key, ExtraArgs={"ContentType": content_type})
    return f"{R2_PUBLIC_URL}/{key}"


def presign_upload(key, content_type, expires=3600):
    url = get_s3().generate_presigned_url("put_object",
        Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=expires, HttpMethod="PUT")
    return {"upload_url": url, "public_url": f"{R2_PUBLIC_URL}/{key}"}


def get_duration(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except:
        return 5.0


def scale_to_1080p(input_path, output_path):
    """Scale to 1920x1080, pad if needed. Clips come in as 832x480 (16:9) from Wan2.2."""
    r = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
               "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", "24", "-pix_fmt", "yuv420p", "-an", output_path
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"scale failed:\n{r.stderr[-400:]}")
    return output_path


def concatenate_loop(paths, output, target_dur):
    clip_dur = sum(get_duration(p) for p in paths) or len(paths) * 5.0
    loops = int(target_dur / clip_dur) + 2
    print(f"[handler] clips={clip_dur:.1f}s target={target_dur:.1f}s loops={loops}")

    lst = output + ".txt"
    with open(lst, "w") as f:
        for _ in range(loops):
            for p in paths:
                f.write(f"file '{p}'\n")

    looped = output + "_loop.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", looped],
        check=True, capture_output=True)
    os.remove(lst)

    subprocess.run(["ffmpeg", "-y", "-i", looped, "-t", str(target_dur), "-c", "copy", "-an", output],
        check=True, capture_output=True)
    os.remove(looped)
    return output


def concatenate(paths, output):
    lst = output + ".txt"
    with open(lst, "w") as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", output],
        check=True, capture_output=True)
    os.remove(lst)
    return output


def add_music(video, music, output):
    subprocess.run([
        "ffmpeg", "-y", "-i", video,
        "-stream_loop", "-1", "-i", music,
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", output
    ], check=True, capture_output=True)
    return output


def handler(job):
    inp = job.get("input", {})

    if inp.get("action") == "presign":
        key = inp.get("key", f"caotinho/music/{uuid.uuid4()}.mp3")
        return presign_upload(key, inp.get("content_type", "audio/mpeg"))

    video_urls = inp.get("video_urls", [])
    music_url  = inp.get("music_url", "")
    output_key = inp.get("output_key", f"caotinho/{uuid.uuid4()}.mp4")
    loop_clips = inp.get("loop_clips", False)

    if not video_urls: return {"error": "video_urls is required"}
    if not music_url:  return {"error": "music_url is required"}
    if not music_url.startswith("http"):
        return {"error": f"music_url must be URL, got: {music_url[:60]}"}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # Download + scale clips to 1920x1080
        scaled = []
        for i, url in enumerate(video_urls):
            ext  = Path(url.split("?")[0]).suffix or ".mp4"
            raw  = str(tmp / f"raw_{i:03d}{ext}")
            norm = str(tmp / f"scaled_{i:03d}.mp4")
            print(f"[handler] Clip {i+1}/{len(video_urls)}: downloading...")
            download_file(url, raw)
            print(f"[handler] Clip {i+1}: scaling to 1920x1080...")
            scale_to_1080p(raw, norm)
            scaled.append(norm)

        # Download music
        m_ext = Path(music_url.split("?")[0]).suffix or ".mp3"
        music = str(tmp / f"music{m_ext}")
        print("[handler] Downloading music...")
        download_file(music_url, music)
        dur = get_duration(music)
        print(f"[handler] Music: {dur:.1f}s")

        # Assemble video
        video = str(tmp / "video.mp4")
        if loop_clips and dur > 10:
            concatenate_loop(scaled, video, dur)
        else:
            concatenate(scaled, video)

        # Mix audio
        final = str(tmp / "final.mp4")
        add_music(video, music, final)

        # Upload
        url = upload_to_r2(final, output_key)
        print(f"[handler] Done → {url}")
        return {"output_url": url}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
