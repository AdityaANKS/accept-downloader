"""
Integration and regression test suite for CDM Bridge, Universal Downloader & Web UI.
"""
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

def test_cdm_bridge_and_queues():
    print("\n--- Testing CDM Bridge & Priority Queues ---")
    from cdm_bridge import CDMQueueManager, CDMBridge, CDMCategoryManager
    
    # Test Category Manager
    cat_video = CDMCategoryManager.categorize("https://example.com/test.mp4")
    assert cat_video == "Videos", f"Expected Videos, got {cat_video}"
    cat_audio = CDMCategoryManager.categorize("https://example.com/track.flac")
    assert cat_audio == "Musics", f"Expected Musics, got {cat_audio}"
    cat_zip = CDMCategoryManager.categorize("https://example.com/archive.tar.gz")
    assert cat_zip == "Compressed", f"Expected Compressed, got {cat_zip}"
    print("CDM Category Manager tests PASSED!")
    
    # Test Queue Manager
    qm = CDMQueueManager.get_instance()
    task = qm.add_url("https://example.com/sample.pdf", description="Sample Doc", category="Documents")
    assert task.category == "Documents"
    print(f"Created task: {task.id} in {task.category}")
    
    tasks = qm.list_tasks()
    found = any(t.id == task.id for t in tasks)
    assert found, "Task not found in queue!"
    
    qm.pause_task(task.id)
    assert task.status == "paused", f"Expected paused, got {task.status}"
    
    qm.resume_task(task.id)
    assert task.status in ("pending", "downloading"), f"Expected pending/downloading, got {task.status}"
    
    qm.remove_task(task.id)
    print("CDM Queue Manager tests PASSED!")

def test_web_app_endpoints():
    print("\n--- Testing Flask Web App Endpoints ---")
    from web.app import app
    client = app.test_client()
    
    # 1. Main index
    r = client.get("/")
    assert r.status_code == 200
    assert b"Crunchyroll Anime" not in r.data, "Crunchyroll Anime should not be in web HTML!"
    assert b"CrossPlatformDownloadManager" not in r.data, "CDM should not be in web HTML!"
    assert b"Unknown Media Sniffer" in r.data
    print("GET / passed (web UI index verified).")
    
    # 2. Universal Downloader Endpoints
    r_uni = client.get("/api/universal/status")
    assert r_uni.status_code == 200
    data_uni = r_uni.get_json()
    assert data_uni.get("available") is True
    print("GET /api/universal/status passed.")

    r_sniff = client.post("/api/universal/sniff", json={
        "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
    })
    assert r_sniff.status_code == 200
    data_sniff = r_sniff.get_json()
    assert data_sniff.get("success") is True
    assert len(data_sniff.get("candidates", [])) > 0
    print(f"POST /api/universal/sniff passed ({len(data_sniff['candidates'])} candidate stream(s)).")
    print("All Flask Web App tests PASSED!\n")

def test_downloader_cli_menu():
    print("\n--- Testing Downloader CLI Options ---")
    from downloader import DownloaderCLI
    cli = DownloaderCLI()
    import io
    from contextlib import redirect_stdout
    f = io.StringIO()
    with redirect_stdout(f):
        cli.print_menu()
    out = f.getvalue()
    # Verify active features remain present
    assert "SpotiFLAC Lossless Music" in out, "SpotiFLAC missing from CLI menu!"
    assert "Spotify Search & Explorer" in out, "Spotify Search missing from CLI menu!"
    assert "Unknown Media Sniffer" in out, "Unknown Media Sniffer missing from CLI menu!"
    # Verify removed options are strictly absent
    assert "CDM Queues" not in out, "CDM Queues should be removed from CLI menu!"
    assert "Crunchyroll Anime" not in out, "Crunchyroll Anime should be removed from CLI menu!"
    assert "Download File (Direct URL)" not in out, "Option 22 should be removed from CLI menu!"
    assert "Record Stream (M3U8/HLS)" not in out, "Option 23 should be removed from CLI menu!"
    assert "Download GitHub Release" not in out, "Option 24 should be removed from CLI menu!"
    assert "Manage Active Tasks" not in out, "Option 25 should be removed from CLI menu!"
    print("CLI Menu options verified (CDM, Crunchyroll, and 22-25 removed, active options preserved)!\n")

def test_universal_engine():
    print("\n--- Testing Universal Media Engine & Sniffer ---")
    from universal_downloader import UniversalMediaDownloader, WebpageStreamSniffer, StreamCandidate, parse_dash_manifest_tracks
    dl = UniversalMediaDownloader()
    candidates = dl.analyze_url("https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8")
    assert len(candidates) > 0, "No stream candidates found!"
    c = candidates[0]
    assert c.type == "hls", f"Expected HLS, got {c.type}"
    assert len(c.unique_resolutions) >= 3, f"Expected multiple resolutions, got {c.unique_resolutions}"
    print(f"Stream analysis verified: '{c.title}' with resolutions {c.unique_resolutions}")

    # Test DASH MPD XML parsing
    sample_mpd = """<?xml version="1.0" encoding="UTF-8"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" minBufferTime="PT1.5S" type="static">
      <Period duration="PT60S">
        <AdaptationSet contentType="video" mimeType="video/mp4">
          <Representation id="v1080" bandwidth="4500000" width="1920" height="1080" codecs="avc1.640028" frameRate="30"/>
          <Representation id="v720" bandwidth="2500000" width="1280" height="720" codecs="avc1.4d401f" frameRate="30"/>
        </AdaptationSet>
        <AdaptationSet contentType="audio" mimeType="audio/mp4" lang="en">
          <Representation id="a1" bandwidth="128000" codecs="mp4a.40.2" audioSamplingRate="48000"/>
        </AdaptationSet>
      </Period>
    </MPD>"""
    dash_v, dash_a, dash_s = parse_dash_manifest_tracks(sample_mpd)
    assert len(dash_v) == 2, f"Expected 2 DASH video tracks, got {len(dash_v)}"
    assert dash_v[0].height == 1080 and dash_v[1].height == 720, f"Unexpected DASH heights: {dash_v}"
    assert len(dash_a) == 1, f"Expected 1 DASH audio track, got {len(dash_a)}"
    print(f"DASH MPD parser verified: resolutions={[v.height for v in dash_v]}, audio={[a.language for a in dash_a]}")
    print("Universal Media Engine tests PASSED!")

if __name__ == "__main__":
    try:
        test_universal_engine()
        test_web_app_endpoints()
        test_downloader_cli_menu()
        print("\n=========================================")
        print("ALL TESTS PASSED SUCCESSFULLY! (0 FAILS)")
        print("=========================================\n")
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
