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

W, H, FPS = 1920, 1080, 60


def get_s3():
    return boto3.client("s3", endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY, region_name="auto")


def download(url, dest):
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192): f.write(chunk)
    return dest


def upload(path, key, ct="video/mp4"):
    get_s3().upload_file(path, R2_BUCKET, key, ExtraArgs={"ContentType": ct})
    return f"{R2_PUBLIC_URL}/{key}"


def get_duration(path):
    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",path],
        capture_output=True,text=True)
    try: return float(json.loads(r.stdout)["format"]["duration"])
    except: return 5.0


def image_to_video_ken_burns(img_path, duration, out_path, index):
    """Convert image to video with Ken Burns effect (zoom + pan)."""
    # Alternate between zoom in and zoom out for variety
    effects = [
        f"zoompan=z='min(zoom+0.0008,1.3)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={duration*FPS}:s={W}x{H}:fps={FPS}",
        f"zoompan=z='if(lte(zoom,1.0),1.3,max(1.0,zoom-0.0008))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={duration*FPS}:s={W}x{H}:fps={FPS}",
        f"zoompan=z='min(zoom+0.0006,1.2)':x='if(lte(on,1),0,x+1)':y='ih/2-(ih/zoom/2)':d={duration*FPS}:s={W}x{H}:fps={FPS}",
        f"zoompan=z='min(zoom+0.0006,1.2)':x='iw-(iw/zoom)':y='ih/2-(ih/zoom/2)':d={duration*FPS}:s={W}x{H}:fps={FPS}",
    ]
    vf = effects[index % len(effects)]

    r = subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", img_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-t", str(duration), "-pix_fmt", "yuv420p", "-an", out_path
    ], capture_output=True, text=True, timeout=120)

    if r.returncode != 0:
        # Fallback: simple scale without Ken Burns
        subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-i", img_path,
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-t", str(duration), "-pix_fmt", "yuv420p", "-r", str(FPS), "-an", out_path
        ], check=True, capture_output=True)
    return out_path


def concat_videos(paths, output):
    lst = output + ".txt"
    with open(lst, "w") as f:
        for p in paths: f.write(f"file '{p}'\n")
    subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",lst,"-c","copy",output],
        check=True, capture_output=True)
    os.remove(lst)
    return output


def add_crossfade(paths, output, duration=0.5):
    """Add crossfade transitions between clips."""
    if len(paths) == 1:
        import shutil
        shutil.copy(paths[0], output)
        return output

    # Build complex filter for crossfade
    inputs = []
    for p in paths:
        inputs += ["-i", p]

    # Simple concat with crossfade using xfade filter
    n = len(paths)
    filter_parts = []
    prev = "[0:v]"
    for i in range(1, n):
        curr = f"[{i}:v]"
        out = f"[v{i}]" if i < n-1 else "[vout]"
        d = get_duration(paths[i-1]) - duration
        filter_parts.append(f"{prev}{curr}xfade=transition=fade:duration={duration}:offset={max(0,d)}{out}")
        prev = f"[v{i}]"

    filter_str = ";".join(filter_parts)

    r = subprocess.run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_str,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", output
    ], capture_output=True, text=True)

    if r.returncode != 0:
        # Fallback to simple concat
        concat_videos(paths, output)
    return output


def mix_narration(video, narration, output):
    """Mix narration audio with video, trim to video duration."""
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video,
        "-i", narration,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", output
    ], check=True, capture_output=True)
    return output


def handler(job):
    inp = job.get("input", {})
    mode = inp.get("mode", "documentary")

    # Existing caotinho mode (loop clips)
    if mode != "documentary":
        return {"error": "Use the caotinho handler for non-documentary mode"}

    scenes_data = inp.get("scenes", {})
    images = scenes_data.get("images", [])
    narration_url = scenes_data.get("narration_url", "")
    durations = scenes_data.get("durations", [])
    output_key = f"futebol/{uuid.uuid4()}.mp4"

    if not images: return {"error": "images is required"}
    if not narration_url: return {"error": "narration_url is required"}
    if not durations: durations = [8] * len(images)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1. Download narration
        print(f"[handler] Downloading narration...")
        narr_ext = Path(narration_url.split("?")[0]).suffix or ".mp3"
        narr_path = str(tmp / f"narration{narr_ext}")
        download(narration_url, narr_path)
        total_dur = get_duration(narr_path)
        print(f"[handler] Narration duration: {total_dur:.1f}s")

        # Adjust durations to match narration
        if sum(durations) != total_dur:
            ratio = total_dur / sum(durations)
            durations = [d * ratio for d in durations]

        # 2. Download images and create Ken Burns clips
        clips = []
        for i, (img_url, dur) in enumerate(zip(images, durations)):
            print(f"[handler] Processing image {i+1}/{len(images)} ({dur:.1f}s)...")
            img_path = str(tmp / f"img_{i:03d}.jpg")
            download(img_url, img_path)
            clip_path = str(tmp / f"clip_{i:03d}.mp4")
            image_to_video_ken_burns(img_path, dur, clip_path, i)
            clips.append(clip_path)

        # 3. Concatenate with crossfade transitions
        print("[handler] Adding crossfade transitions...")
        concat_path = str(tmp / "concat.mp4")
        add_crossfade(clips, concat_path, duration=0.4)

        # 4. Mix narration
        print("[handler] Mixing narration...")
        final_path = str(tmp / "final.mp4")
        mix_narration(concat_path, narr_path, final_path)

        # 5. Upload
        print(f"[handler] Uploading: {output_key}")
        url = upload(final_path, output_key)

    print(f"[handler] Done → {url}")
    return {"output_url": url}


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
