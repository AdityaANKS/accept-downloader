"""
Unit and integration tests for Network Resilience, Auto-Pause, and Resumption.
"""

import sys
import os
import socket
import threading
import time
from urllib.error import URLError

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from utils import (
    is_internet_available,
    is_network_error,
    wait_for_internet_connection,
)


def test_is_internet_available():
    print("\n--- Testing is_internet_available ---")
    online = is_internet_available()
    assert isinstance(online, bool)
    print(f"Current internet connectivity status: {online}")


def test_is_network_error():
    print("\n--- Testing is_network_error ---")
    # True positives
    assert is_network_error(socket.timeout("Socket timed out")) is True
    assert is_network_error(ConnectionResetError("Connection reset by peer")) is True
    assert is_network_error(ConnectionRefusedError("Connection refused")) is True
    assert is_network_error(URLError("nodename nor servname provided, or not known")) is True
    assert is_network_error(Exception("WinError 10054 An existing connection was forcibly closed")) is True
    assert is_network_error(Exception("curl: (28) Operation timed out after 30000ms")) is True
    assert is_network_error(Exception("Failed to establish a new connection: [Errno 11001] getaddrinfo failed")) is True

    # False positives
    assert is_network_error(ValueError("Invalid format")) is False
    assert is_network_error(FileNotFoundError("File not found")) is False
    assert is_network_error(KeyError("missing_key")) is False
    print("is_network_error classification tests PASSED!")


def test_wait_for_internet_connection_immediate():
    print("\n--- Testing wait_for_internet_connection immediate return when online ---")
    if is_internet_available():
        called = False
        def cb(msg):
            nonlocal called
            called = True
        res = wait_for_internet_connection(status_callback=cb)
        assert res is True
        # Since it was already online, it should return immediately without offline notice
        assert called is False
        print("wait_for_internet_connection immediate return PASSED!")


def test_wait_for_internet_connection_stop_event():
    print("\n--- Testing wait_for_internet_connection stop_event cancellation ---")
    stop_event = threading.Event()
    stop_event.set()  # Pre-cancelled

    # Mock is_internet_available to simulate offline
    import utils
    orig_check = utils.is_internet_available
    try:
        utils.is_internet_available = lambda *args, **kwargs: False
        res = wait_for_internet_connection(stop_event=stop_event, check_interval=0.1)
        assert res is False, "Expected cancellation to return False"
        print("Cancellation via stop_event PASSED!")
    finally:
        utils.is_internet_available = orig_check


def test_continuedl_in_configs():
    print("\n--- Testing 'continuedl': True presence across all engines ---")
    from downloader import MediaDownloader
    from platforms import Platform
    md = MediaDownloader()
    default_opts = md._build_ydl_opts(output_dir=".", platform=Platform.YOUTUBE)
    assert default_opts.get('continuedl') is True, "continuedl missing from MediaDownloader._build_ydl_opts"
    assert default_opts.get('fragment_retries', 0) >= 20, "fragment_retries should be >= 20"

    from spotiflac import SpotiFLACDownloader
    # Verify spotiflac file contains continuedl
    with open("spotiflac.py", "r", encoding="utf-8") as f:
        assert "'continuedl': True" in f.read(), "continuedl missing from spotiflac.py"

    with open("universal_downloader.py", "r", encoding="utf-8") as f:
        assert '"continuedl": True' in f.read(), "continuedl missing from universal_downloader.py"

    with open("web/app.py", "r", encoding="utf-8") as f:
        assert "'continuedl': True" in f.read(), "continuedl missing from web/app.py"

    print("All download engines configured with 'continuedl': True and high fragment retries PASSED!")


if __name__ == "__main__":
    test_is_internet_available()
    test_is_network_error()
    test_wait_for_internet_connection_immediate()
    test_wait_for_internet_connection_stop_event()
    test_continuedl_in_configs()
    print("\n=========================================")
    print("ALL NETWORK RESILIENCE TESTS PASSED! (0 FAILS)")
    print("=========================================\n")
