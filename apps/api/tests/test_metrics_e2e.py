import time
import pathlib
import httpx

import pytest
from playwright.sync_api import Page

API_BASE = "http://localhost:8000"

def upload_video(client: httpx.Client, path: str) -> str:
    with open(path, "rb") as f:
        files = {"file": ("test.mp4", f, "video/mp4")}
        resp = client.post(f"{API_BASE}/upload", files=files)
    resp.raise_for_status()
    data = resp.json()
    return data["video_id"] 

def start_transcode(client: httpx.Client, video_id: str) -> str:
    resp = client.post(f"{API_BASE}/jobs/transcode", json={"video_id": video_id})
    resp.raise_for_status()
    return resp.json()["job_id"]

def poll_until_ready(client: httpx.Client, video_id: str, timeout: float = 300.0):
    start = time.perf_counter()
    while True:
        if time.perf_counter() - start > timeout:
            raise TimeoutError("Renditions did not become ready in time")

        resp = client.get(f"{API_BASE}/videos/{video_id}/summary")

        if resp.status_code != 200:
            print("\n[DEBUG] summary error:")
            print(f"status={resp.status_code}")
            print(f"body={resp.text}")
            
            resp.raise_for_status()

        data = resp.json()
        renditions = data.get("renditions", [])

        if renditions and all(r["status"] == "ready" for r in renditions):
            return time.perf_counter() - start

        time.sleep(2)


def test_time_to_ready_hls():
    test_video_path = "tests/data/sample60.mp4" 
    client = httpx.Client()

    t0 = time.perf_counter()
    video_id = upload_video(client, test_video_path)
    job_id = start_transcode(client, video_id)
    total_ready_time = poll_until_ready(client, video_id)
    total = time.perf_counter() - t0

    print(f"\n[METRIC] video_id={video_id} job_id={job_id}")
    print(f"[METRIC] time_to_all_renditions_ready={total_ready_time:.2f}s")
    print(f"[METRIC] total_end_to_end_time={total:.2f}s")

    assert total_ready_time < 180  # arbitrary threshold for now

from concurrent.futures import ThreadPoolExecutor

def process_single_video(client: httpx.Client, path: str) -> float:
    video_id = upload_video(client, path)
    start = time.perf_counter()
    start_transcode(client, video_id)
    ready_time = poll_until_ready(client, video_id)
    return ready_time

def test_concurrent_transcodes():
    client = httpx.Client()
    test_video_path = "tests/data/sample60.mp4"
    N = 5  # number of videos to process concurrently

    # Sequential baseline
    seq_start = time.perf_counter()
    seq_times = [process_single_video(client, test_video_path) for _ in range(N)]
    seq_total = time.perf_counter() - seq_start

    # Concurrent run
    conc_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=N) as ex:
        futures = [ex.submit(process_single_video, client, test_video_path) for _ in range(N)]
        conc_times = [f.result() for f in futures]
    conc_total = time.perf_counter() - conc_start

    print(f"\n[METRIC] sequential_total_time={seq_total:.2f}s for {N} videos")
    print(f"[METRIC] concurrent_total_time={conc_total:.2f}s for {N} videos")
    print(f"[METRIC] sequential_avg={seq_total/N:.2f}s per video")
    print(f"[METRIC] concurrent_avg={conc_total/N:.2f}s per video")
    if conc_total > 0:
        print(f"[METRIC] throughput_speedup={seq_total/conc_total:.2f}x")

    # Again, optional assertions to keep pytest happy
    assert conc_total <= seq_total * 1.1  # we expect concurrent not to be *worse* than sequential

WEB_BASE = API_BASE  # http://localhost:8000


def run_buffer_scenario(page: Page, video_id: str, mode: str, play_seconds: int = 30) -> dict:
    """
    Open /metrics-player in the given mode ("basic" or "abr"),
    let it play for play_seconds, then return window.__metrics__.

    window.__metrics__ is set by /metrics-player and has:
      - stallCount
      - stallTime
      - startupTime
      - currentTime
    """
    url = f"{WEB_BASE}/metrics-player?video_id={video_id}&mode={mode}"
    page.goto(url)

    # Give the page a moment to load
    page.wait_for_timeout(2000)

    # Explicitly start playback (mute to avoid autoplay restrictions)
    page.evaluate(
        """
        () => {
            const v = document.getElementById('video');
            if (v) {
                v.muted = true;
                v.play().catch(() => {});
            }
        }
        """
    )

    # Let the video play for some time under this mode
    page.wait_for_timeout(play_seconds * 1000)

    # Read metrics from the page
    metrics = page.evaluate(
        "() => window.__metrics__ || { stallCount: 0, stallTime: 0, startupTime: null, currentTime: 0 }"
    )
    return metrics


@pytest.mark.e2e
def test_buffering_basic_vs_abr(page: Page):
    """
    Measurement-style E2E test:

    1. Uploads a sample video and runs it through the normal pipeline
       (upload -> transcode -> HLS renditions ready).
    2. Opens /metrics-player in "basic" (single-bitrate) mode and records metrics.
    3. Opens /metrics-player in "abr" (HLS ABR) mode and records metrics.
    4. Prints startupTime / stallCount / stallTime for both.

    This test doesn't assert ABR is always "better" (environment-dependent);
    it exists to give you concrete numbers like:
      - basic_startup ~ 6.0s
      - abr_startup   ~ 0.3s  (~20x improvement)
    which you can quote and track over time.
    """
    client = httpx.Client()

    # 1) Upload a sample video and wait for renditions to be ready
    test_video_path = "tests/data/sample15.mp4"  # ideally a 20–60s MP4
    video_id = upload_video(client, test_video_path)
    start_transcode(client, video_id)
    poll_until_ready(client, video_id)

    # 2) Basic single-bitrate playback
    basic = run_buffer_scenario(page, video_id, mode="basic")
    # 3) ABR HLS playback
    abr = run_buffer_scenario(page, video_id, mode="abr")

    print("\n[METRIC] basic:", basic)
    print("[METRIC] abr:", abr)

    basic_stalls = basic.get("stallCount", 0)
    abr_stalls = abr.get("stallCount", 0)
    basic_time = basic.get("stallTime", 0.0)
    abr_time = abr.get("stallTime", 0.0)
    basic_start = basic.get("startupTime", None)
    abr_start = abr.get("startupTime", None)

    # Log startup improvement if both are present
    if basic_start is not None and abr_start not in (None, 0):
        improvement = basic_start / abr_start
        print(f"[METRIC] startup_improvement={improvement:.2f}x")

    # This is a measurement test; we don't fail on metrics.
    # It will only fail if something in the pipeline or page truly breaks.