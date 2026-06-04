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

TARGET_W = 1920
TARGET_H = 1080


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
    url = get_s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
        HttpMethod="PUT",
    )
    return {"upload_url": url, "public_url": f"{R2_PUBLIC_URL}/{key}"}


def get_duration(path: str) -> float:
    r = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path
    ], capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except:
        return 5.0


def get_video_info(path: str) -> dict:
    r = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", path
    ], capture_output=True, text=True)
    try:
        data = json.loads(r.stdout)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return {
                    "width": int(s.get("width", 0)),
                    "height": int(s.get("height", 0)),
                }
    except:
        pass
    return {"width": 0, "height": 0}


def normalize_to_landscape(input_path: str, output_path: str) -> str:
    """
    Convert ANY video to 1920x1080 Full HD landscape, NO black bars.
    
    Strategy:
    - Portrait (h > w): crop width-based 16:9 from center, then scale up
    - Landscape (w >= h): scale to fill 1920x1080, crop center to remove any excess
    - Both result in full 1920x1080 with NO black bars
    """
    info = get_video_info(input_path)
    w, h = info["width"], info["height"]
    print(f"[handler] Input size: {w}x{h}")

    if w <= 0 or h <= 0:
        # fallback - just scale and crop
        vf = f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,crop={TARGET_W}:{TARGET_H}"
    elif h > w:
        # PORTRAIT video (e.g. 480x720, 540x960)
        # Step 1: scale so width = TARGET_W
        # Step 2: crop height to TARGET_H from center
        # This fills the frame completely - no black bars
        vf = (
            f"scale={TARGET_W}:-2:flags=lanczos,"
            f"crop={TARGET_W}:{TARGET_H}:0:(ih-{TARGET_H})/2"
        )
    else:
        # LANDSCAPE or SQUARE video
        # Step 1: scale so height = TARGET_H  
        # Step 2: crop width to TARGET_W from center
        vf = (
            f"scale=-2:{TARGET_H}:flags=lanczos,"
            f"crop={TARGET_W}:{TARGET_H}:(iw-{TARGET_W})/2:0"
        )

    r = subprocess.run([
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", "24", "-pix_fmt", "yuv420p",
        "-an", output_path
    ], capture_output=True, text=True)

    if r.returncode != 0:
        print(f"[handler] normalize stderr: {r.stderr[-300:]}")
        raise RuntimeError(f"normalize failed:\n{r.stderr[-500:]}")

    out_info = get_video_info(output_path)
    print(f"[handler] Output size: {out_info['width']}x{out_info['height']} ✓")
    return output_path


def concatenate_videos_loop(paths: list, output: str, target_duration: float) -> str:
    clip_duration = sum(get_duration(p) for p in paths)
    if clip_duration <= 0:
        clip_duration = len(paths) * 5.0

    loops_needed = int(target_duration / clip_duration) + 2
    print(f"[handler] clip_dur={clip_duration:.1f}s target={target_duration:.1f}s loops={loops_needed}")

    list_file = output + ".txt"
    with open(list_file, "w") as f:
        for _ in range(loops_needed):
            for p in paths:
                f.write(f"file '{p}'\n")

    looped = output + "_looped.mp4"
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", looped
    ], capture_output=True, text=True)
    os.remove(list_file)
    if r.returncode != 0:
        raise RuntimeError(f"loop concat failed:\n{r.stderr[-500:]}")

    r2 = subprocess.run([
        "ffmpeg", "-y", "-i", looped,
        "-t", str(target_duration),
        "-c", "copy", "-an", output
    ], capture_output=True, text=True)
    os.remove(looped)
    if r2.returncode != 0:
        raise RuntimeError(f"trim failed:\n{r2.stderr[-500:]}")
    return output


def concatenate_videos(paths: list, output: str) -> str:
    list_file = output + ".txt"
    with open(list_file, "w") as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", output
    ], capture_output=True, text=True)
    os.remove(list_file)
    if r.returncode != 0:
        raise RuntimeError(f"concat failed:\n{r.stderr[-500:]}")
    return output


def add_music(video: str, music: str, output: str) -> str:
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", video,
        "-stream_loop", "-1", "-i", music,
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        output,
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"add_music failed:\n{r.stderr[-500:]}")
    return output


def handler(job: dict) -> dict:
    inp = job.get("input", {})
    action = inp.get("action", "")

    if action == "presign":
        key = inp.get("key", f"caotinho/music/{uuid.uuid4()}.mp3")
        content_type = inp.get("content_type", "audio/mpeg")
        print(f"[handler] Presigning {key}")
        return presign_upload(key, content_type)

    video_urls = inp.get("video_urls", [])
    music_url  = inp.get("music_url", "")
    output_key = inp.get("output_key", f"caotinho/{uuid.uuid4()}.mp4")
    loop_clips = inp.get("loop_clips", False)

    if not video_urls:
        return {"error": "video_urls is required"}
    if not music_url:
        return {"error": "music_url is required"}
    if not music_url.startswith("http"):
        return {"error": f"music_url must be a valid URL, got: {music_url[:80]}"}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1. Download clips
        raw_paths = []
        for i, url in enumerate(video_urls):
            ext = Path(url.split("?")[0]).suffix or ".mp4"
            dest = str(tmp / f"raw_{i:03d}{ext}")
            print(f"[handler] Downloading clip {i+1}/{len(video_urls)}")
            download_file(url, dest)
            raw_paths.append(dest)

        # 2. Normalize ALL clips to 1920x1080 Full HD — NO BLACK BARS
        print("[handler] Normalizing clips to 1920x1080 Full HD...")
        norm_paths = []
        for i, rp in enumerate(raw_paths):
            np_ = str(tmp / f"norm_{i:03d}.mp4")
            normalize_to_landscape(rp, np_)
            norm_paths.append(np_)

        # 3. Download music
        music_ext = Path(music_url.split("?")[0]).suffix or ".mp3"
        music_path = str(tmp / f"music{music_ext}")
        print("[handler] Downloading music")
        download_file(music_url, music_path)
        music_duration = get_duration(music_path)
        print(f"[handler] Music duration: {music_duration:.1f}s")

        # 4. Concat with loop
        video_path = str(tmp / "video.mp4")
        if loop_clips and music_duration > 10:
            print("[handler] Looping clips to fill music duration...")
            concatenate_videos_loop(norm_paths, video_path, music_duration)
        else:
            concatenate_videos(norm_paths, video_path)

        # 5. Mix music
        final = str(tmp / "final.mp4")
        print("[handler] Adding music...")
        add_music(video_path, music_path, final)

        # 6. Upload
        print(f"[handler] Uploading: {output_key}")
        public_url = upload_to_r2(final, output_key)

    print(f"[handler] Done → {public_url}")
    return {"output_url": public_url}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
