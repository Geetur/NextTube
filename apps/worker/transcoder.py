"""FFmpeg transcoding: source MP4 to per-height fragmented-MP4 HLS variants."""

import pathlib
import subprocess
import time
from collections.abc import Callable

from config import settings

# resolution height → (video_kbps, audio_kbps)
PROFILE_PRESETS: dict[int, tuple[int, int]] = {
    240: (400, 96),
    480: (800, 96),
    720: (1500, 128),
}


def _run_ffmpeg(cmd: list[str], heartbeat: Callable[[], bool] | None = None) -> int:
    print("[ffmpeg]>", " ".join(cmd))
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    started_at = time.monotonic()

    while True:
        try:
            output, _ = process.communicate(timeout=5)
            print(output.decode(errors="ignore")[-4000:])
            return process.returncode
        except subprocess.TimeoutExpired:
            if heartbeat is not None and not heartbeat():
                process.terminate()
                output, _ = process.communicate()
                print(output.decode(errors="ignore")[-4000:])
                raise RuntimeError("worker shutdown requested or job lease was lost")
            if time.monotonic() - started_at > settings.ffmpeg_timeout_seconds:
                process.kill()
                output, _ = process.communicate()
                print(output.decode(errors="ignore")[-4000:])
                raise TimeoutError(
                    f"FFmpeg exceeded {settings.ffmpeg_timeout_seconds} seconds"
                )


def transcode(
    src: pathlib.Path,
    out_dir: pathlib.Path,
    profiles: list[int],
    heartbeat: Callable[[], bool] | None = None,
) -> dict[int, bool]:
    """Transcode *src* into HLS variants for each height in *profiles*.

    Returns a mapping of ``height -> success`` for each profile.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, bool] = {}

    for height in profiles:
        vkbps, akbps = PROFILE_PRESETS.get(height, (800, 96))
        maxrate = int(vkbps * 1.1)
        bufsize = int(vkbps * 2.0)
        out_m3u8 = out_dir / f"{height}.m3u8"
        seg_tmpl = str(out_dir / f"{height}_%03d.m4s")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", f"scale=-2:{height}",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{vkbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            "-c:a", "aac", "-profile:a", "aac_low", "-ar", "48000", "-ac", "2", "-b:a", f"{akbps}k",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", f"{height}_init.mp4",
            "-hls_playlist_type", "vod",
            "-hls_list_size", "0",
            "-hls_segment_filename", seg_tmpl,
            str(out_m3u8),
        ]

        rc = _run_ffmpeg(cmd, heartbeat)
        results[height] = rc == 0 and out_m3u8.exists()

    return results


def build_master_playlist(profiles: list[int]) -> str:
    """Return the text of an HLS master playlist referencing each variant."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for height in profiles:
        vkbps, akbps = PROFILE_PRESETS.get(height, (800, 96))
        bandwidth = (vkbps + akbps) * 1000
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}")
        lines.append(f"{height}.m3u8")
    return "\n".join(lines) + "\n"
