#!/usr/bin/env python3
"""
PhoneBridge desktop launcher.

Starts the Flask server in a background thread, then shows the UI in a native
macOS window (pywebview) whose toolbar sits in the title bar, Finder-style, with
the real traffic-light buttons. Falls back to the default web browser if
pywebview is unavailable. This is the entry point bundled into PhoneBridge.app.
"""
import atexit
import socket
import threading
import time
import urllib.request

import app as phonebridge


def free_port(start=5590, tries=10):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("PhoneBridge: no free local port")


def wait_for_server(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def unify_titlebar(window):
    """Let the web toolbar sit in the title bar next to the real traffic lights."""
    import AppKit
    from PyObjCTools import AppHelper

    def apply():
        w = window.native
        w.setStyleMask_(w.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
        w.setTitlebarAppearsTransparent_(True)
        w.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        try:   # pywebview paints the title bar opaque; make it see-through again
            w.contentView().superview().subviews().lastObject().setBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:
            pass

    AppHelper.callAfter(apply)


def dark_mode():
    import subprocess
    out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True).stdout
    return "Dark" in out


def main():
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    phonebridge.start_background()
    threading.Thread(target=lambda: phonebridge.app.run(host="127.0.0.1", port=port, threaded=True,
                                                       use_reloader=False, debug=False), daemon=True).start()
    wait_for_server(url)
    atexit.register(lambda: phonebridge.PM.drop("quit"))       # close the phone session cleanly
    try:
        import webview
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
        window = webview.create_window("PhoneBridge", url + "/?native=1", width=1280, height=820,
                                       min_size=(960, 620), background_color="#1E1E20" if dark_mode() else "#FFFFFF")
        window.events.loaded += lambda: unify_titlebar(window)
        webview.start()                                       # blocks until the window closes
    except Exception:
        import webbrowser
        webbrowser.open(url)
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
