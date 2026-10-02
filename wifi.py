"""
Wi-Fi sharing: the phone scans a QR code and gets a simple page in its browser.

Runs as its own small web server on the local network, separate from the Mac app's
server, and it can only do two things: receive files from the phone (saved to
~/Downloads/PhoneBridge) and hand the phone files the Mac chose to send.
Every request must carry the one-time key that is inside the QR code.
"""
import io
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from werkzeug.serving import make_server

PORTS = range(5591, 5600)
SEEN_WINDOW = 20                     # seconds a phone counts as "connected" after its last check-in


def lan_ip():
    """This Mac's address on the local network (no packets are sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def clean_name(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip().replace(":", "-")
    return name.lstrip(".") or "file"


def phone_kind(ua):
    ua = ua or ""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        return "Android phone"
    return "Phone"


class WifiShare:
    def __init__(self, mac_name, inbox, thumb_fn):
        self.mac_name = mac_name
        self.inbox = Path(inbox)
        self.thumb_fn = thumb_fn
        self.server = None
        self.port = None
        self.key = None
        self.clients = {}            # addr -> {"name", "seen"}
        self.received = []           # newest last: {id, name, size, done, state, path}
        self.outbox = []             # {id, name, size, path, state, zip}
        self.session_client = None   # set once a phone has opened the page during this sharing session
        self.lock = threading.Lock()
        self.app = self._make_app()

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        if self.server:
            return
        last = None
        for port in PORTS:
            try:
                self.server = make_server("0.0.0.0", port, self.app, threaded=True)
                self.port = port
                break
            except OSError as e:
                last = e
        if not self.server:
            raise OSError(f"No free network port for Wi-Fi sharing ({last})")
        self.key = secrets.token_urlsafe(9)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        srv, self.server = self.server, None
        self.key = None
        if srv:
            threading.Thread(target=srv.shutdown, daemon=True).start()
        with self.lock:
            self.clients.clear()
            self.session_client = None
            self.outbox.clear()
            self.received = [r for r in self.received if r["state"] == "done"][-50:]

    @property
    def on(self):
        return self.server is not None

    def url(self):
        ip = lan_ip()
        if not (self.on and ip):
            return None
        return f"http://{ip}:{self.port}/?k={self.key}"

    def connected(self):
        now = time.time()
        return [c for c in self.clients.values() if now - c["seen"] < SEEN_WINDOW]

    def summary(self):
        live = self.connected()
        busy = any(r["state"] == "receiving" for r in self.received)
        return {"on": self.on, "connected": bool(live) or busy, "session": bool(self.session_client),
                "client": live[0]["name"] if live else (self.session_client or ("Phone" if busy else None)),
                "activity": len(self.received) + len(self.outbox)}

    def state(self, with_qr=False):
        url = self.url()
        out = {**self.summary(), "url": url, "ip": lan_ip(), "inbox": str(self.inbox),
               "received": list(self.received)[-40:], "outbox": list(self.outbox)}
        if with_qr and url:
            import segno
            buf = io.BytesIO()
            segno.make(url, error="m").save(buf, kind="svg", border=1, xmldecl=False, svgns=True, omitsize=True)
            out["qr"] = buf.getvalue().decode()
        return out

    # -- Mac -> phone ------------------------------------------------------------
    def offer(self, path):
        """Put a Mac file (or folder, zipped) on the phone's 'From your Mac' list."""
        if not os.path.exists(path):
            return
        item = {"id": uuid.uuid4().hex[:10], "name": os.path.basename(path), "path": path,
                "size": os.path.getsize(path) if os.path.isfile(path) else 0, "state": "ready", "zip": False}
        if os.path.isdir(path):
            item.update(name=item["name"] + ".zip", state="preparing", zip=True)
            threading.Thread(target=self._zip, args=(item,), daemon=True).start()
        with self.lock:
            self.outbox.append(item)

    def _zip(self, item):
        tmp = tempfile.mkdtemp(prefix="phonebridge-")
        try:
            out = shutil.make_archive(os.path.join(tmp, item["name"][:-4]), "zip",
                                      root_dir=os.path.dirname(item["path"]), base_dir=os.path.basename(item["path"]))
            item.update(path=out, size=os.path.getsize(out), state="ready")
        except Exception:
            item["state"] = "failed"

    # -- the phone's web server ----------------------------------------------------
    def _make_app(self):
        here = Path(__file__).resolve().parent
        import sys
        if getattr(sys, "frozen", False):
            here = Path(sys._MEIPASS)
        app = Flask("phonebridge-wifi", template_folder=str(here / "templates"))
        share = self

        @app.before_request
        def check_key():
            k = request.args.get("k") or request.headers.get("X-Key") or ""
            if not share.key or not secrets.compare_digest(k, share.key):
                if request.path == "/":
                    return render_template("phone_expired.html"), 403
                abort(403)
            addr = request.remote_addr
            with share.lock:
                c = share.clients.setdefault(addr, {"name": phone_kind(request.user_agent.string), "seen": 0})
                c["seen"] = time.time()
                share.session_client = c["name"]

        @app.route("/")
        def page():
            return render_template("phone.html", mac=share.mac_name, key=share.key)

        @app.route("/w/state")
        def w_state():
            with share.lock:
                items = [{k: o[k] for k in ("id", "name", "size", "state")} for o in share.outbox]
            return jsonify({"mac": share.mac_name, "outbox": items})

        @app.route("/w/upload", methods=["POST"])
        def w_upload():
            share.inbox.mkdir(parents=True, exist_ok=True)
            name = clean_name(request.args.get("name"))
            size = int(request.args.get("size") or request.content_length or 0)
            with share.lock:
                taken = set(os.listdir(share.inbox)) | {r["name"] for r in share.received if r["state"] == "receiving"}
                from phones import unique_name
                final = unique_name(name, taken)
                rec = {"id": uuid.uuid4().hex[:10], "name": final, "size": size, "done": 0,
                       "state": "receiving", "path": str(share.inbox / final), "t": time.time()}
                share.received.append(rec)
            part = share.inbox / (final + ".phonebridge-part")
            try:
                with open(part, "wb") as f:
                    while True:
                        chunk = request.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        rec["done"] += len(chunk)
                if size and rec["done"] < size:
                    raise OSError("Upload was interrupted")
                os.replace(part, rec["path"])
                rec["state"], rec["size"] = "done", rec["done"]
            except Exception as e:
                rec["state"] = "failed"
                if part.exists():
                    part.unlink()
                return jsonify({"error": str(e)}), 400
            return jsonify({"ok": True, "name": final})

        def find(item_id):
            with share.lock:
                for o in share.outbox:
                    if o["id"] == item_id and o["state"] in ("ready", "sent"):
                        return o
            abort(404)

        @app.route("/w/file/<item_id>")
        def w_file(item_id):
            o = find(item_id)
            size = os.path.getsize(o["path"])

            def stream():
                sent = 0
                with open(o["path"], "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        sent += len(chunk)
                        yield chunk
                if sent >= size:
                    o["state"] = "sent"

            from urllib.parse import quote
            headers = {"Content-Length": str(size),
                       "Content-Disposition": f"attachment; filename*=UTF-8''{quote(o['name'])}"}
            import mimetypes
            mime = mimetypes.guess_type(o["name"])[0] or "application/octet-stream"
            return Response(stream(), mimetype=mime, headers=headers, direct_passthrough=True)

        @app.route("/w/thumb/<item_id>")
        def w_thumb(item_id):
            o = find(item_id)
            if o["zip"]:
                abort(404)
            cache = share.thumb_fn(o["path"])
            if not cache:
                abort(404)
            return send_file(cache, mimetype="image/jpeg")

        return app
