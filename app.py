#!/usr/bin/env python3
"""
PhoneBridge — move files between an Android phone and a Mac.

Cable: plug in, tap "File transfer" on the phone (no Developer options needed).
Wi-Fi: scan a QR code with the phone camera; works with iPhone too.

Run:  python3 app.py   then open http://127.0.0.1:5590
"""
import hashlib
import io
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps

import mtp
import usb.core
from phones import Cancelled, PhoneManager, log, unique_name
from wifi import WifiShare

# --- resource location (works as a plain script AND inside a bundled .app) ----
if getattr(sys, "frozen", False):
    HERE = Path(sys._MEIPASS)
else:
    HERE = Path(__file__).resolve().parent

ADB = str(HERE / "tools" / "platform-tools" / "adb")
FFMPEG = str(HERE / "tools" / "ffmpeg")
if not os.path.exists(FFMPEG):
    FFMPEG = shutil.which("ffmpeg") or FFMPEG
for _b in (ADB, FFMPEG):
    try:
        if os.path.exists(_b):
            os.chmod(_b, 0o755)
    except OSError:
        pass

MAC_HOME = str(Path.home())
DATA_DIR = Path.home() / "Library" / "Application Support" / "PhoneBridge"
THUMB_DIR = DATA_DIR / "thumbcache"
OPEN_DIR = DATA_DIR / "opencache"
INBOX = Path.home() / "Downloads" / "PhoneBridge"           # where Wi-Fi uploads land
for _d in (THUMB_DIR, OPEN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

THUMB_PX = 240
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".3gp", ".m4v"}

PORT = int(os.environ.get("PHONEBRIDGE_PORT", "5590"))
TOKEN = secrets.token_urlsafe(16)                           # page <-> server handshake

app = Flask(__name__, template_folder=str(HERE / "templates"))
PM = PhoneManager(ADB)
PREFETCH_POOL = ThreadPoolExecutor(max_workers=3)


def _mac_names():
    model, name = "Mac", "this Mac"
    try:
        d = json.loads(subprocess.run(["system_profiler", "SPHardwareDataType", "-json"],
                                      capture_output=True, text=True, timeout=10).stdout)
        model = d["SPHardwareDataType"][0].get("machine_name") or model
    except Exception:
        pass
    try:
        name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True).stdout.strip() or name
    except Exception:
        pass
    return model, name


MAC_MODEL, MAC_NAME = _mac_names()


def _trash_word():
    """Finder says "Bin" in British/Indian English and "Trash" in US English."""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True).stdout
        first = out.replace("(", "").replace('"', "").split()[0].strip(",")
        return "Bin" if first.startswith("en-") and first not in ("en-US", "en-CA") else "Trash"
    except Exception:
        return "Trash"


TRASH = _trash_word()


# --- security: only our own page may drive the API ----------------------------
@app.before_request
def guard():
    host = (request.host or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):               # blocks DNS-rebinding tricks
        abort(403)
    if request.path.startswith("/api/") and request.method != "GET":
        if request.headers.get("X-PhoneBridge") != TOKEN:     # other websites can't set this header
            abort(403)


def body():
    return request.get_json(force=True, silent=True) or {}


def phone_join(folder, name):
    return folder.rstrip("/") + "/" + name


# =============================================================================
# Jobs (copies run in the background so the window never freezes)
# =============================================================================
class Job:
    def __init__(self, kind, label, direction=None):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.label, self.direction = kind, label, direction
        self.total = self.done = 0
        self.items_total = self.items_done = 0
        self.current = ""
        self.state = "queued"
        self.error = ""
        self.failed, self.renamed = [], []
        self.cancel_requested = False
        self.started = time.time()
        self.finished = None
        self._samples = []                                  # (time, done) for speed

    def add(self, n):
        self.done += n
        now = time.time()
        self._samples.append((now, self.done))
        while self._samples and now - self._samples[0][0] > 3:
            self._samples.pop(0)

    def cancelled(self):
        return self.cancel_requested

    def speed(self):
        if len(self._samples) < 2:
            return 0
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        return (d1 - d0) / (t1 - t0) if t1 > t0 else 0

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "direction": self.direction,
                "total": self.total, "done": self.done, "items_total": self.items_total,
                "items_done": self.items_done, "current": self.current, "state": self.state,
                "error": self.error, "failed": self.failed, "renamed": self.renamed,
                "speed": self.speed(), "finished": self.finished}


JOBS = OrderedDict()
JOB_Q = queue.Queue()


def submit(job, fn):
    JOBS[job.id] = job
    while len(JOBS) > 30:
        JOBS.popitem(last=False)
    JOB_Q.put((job, fn))
    return job


def job_running():
    return any(j.state in ("queued", "running") for j in JOBS.values())


def _job_worker():
    while True:
        job, fn = JOB_Q.get()
        if job.cancel_requested:
            job.state, job.finished = "cancelled", time.time()
            continue
        job.state = "running"
        try:
            fn(job)
            job.state = "cancelled" if job.cancel_requested else "done"
        except Cancelled:
            job.state = "cancelled"
        except Exception as e:
            job.state, job.error = "error", str(e)
            log.exception("job %s (%s) failed", job.id, job.label)
            if isinstance(e, usb.core.USBError):
                PM.drop("usb error")                       # reconnect cleanly on the next poll
        finally:
            job.finished = time.time()
            PM.invalidate_details()


threading.Thread(target=_job_worker, daemon=True).start()


def local_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def run_copy(job, direction, src_dir, dst_dir, items):
    phone = PM.get()
    if direction == "to_mac":
        job.total = sum(phone.size_of(phone_join(src_dir, n)) for n in items)
        taken = os.listdir(dst_dir)
    else:
        job.total = sum(local_size(os.path.join(src_dir, n)) for n in items)
        taken = [e["name"] for e in phone.list(dst_dir)]
    job.items_total = len(items)
    for name in items:
        if job.cancel_requested:
            raise Cancelled()
        new = unique_name(name, taken)
        taken.append(new)
        if new != name:
            job.renamed.append({"from": name, "to": new})
        job.current = name
        try:
            if direction == "to_mac":
                phone.pull(phone_join(src_dir, name), dst_dir, new, job.add, job.cancelled)
            else:
                phone.push(os.path.join(src_dir, name), dst_dir, new, job.add, job.cancelled)
        except (Cancelled, usb.core.USBError):
            raise
        except Exception as e:
            job.failed.append({"name": name, "error": str(e)})
        job.items_done += 1


@app.route("/api/copy", methods=["POST"])
def copy():
    d = body()
    direction, items = d.get("direction"), d.get("items") or []
    if direction not in ("to_mac", "to_phone") or not items:
        return jsonify({"error": "Nothing to copy"}), 400
    try:
        phone_name = PM.get().name
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    label = f"{len(items)} item{'s' if len(items) != 1 else ''} to " + ("Mac" if direction == "to_mac" else phone_name)
    job = submit(Job("copy", label, direction),
                 lambda j: run_copy(j, direction, d["src_dir"], d["dst_dir"], items))
    return jsonify({"job": job.id})


@app.route("/api/jobs/<jid>/cancel", methods=["POST"])
def cancel_job(jid):
    job = JOBS.get(jid)
    if job:
        job.cancel_requested = True
    return jsonify({"ok": True})


# =============================================================================
# Status (polled by the window about once a second)
# =============================================================================
@app.route("/api/status")
def status():
    dev = {"status": PM.status, "name": PM.label, "error": PM.error}
    if PM.phone is not None and PM.status in ("ready", "locked"):
        dev["engine"] = PM.phone.kind
        dev["home"] = PM.phone.home()
        dev["name"] = PM.phone.name
    if PM.status == "ready":
        dev.update(PM.details())
    now = time.time()
    jobs = [j.to_dict() for j in JOBS.values()
            if j.state in ("queued", "running") or (j.finished and now - j.finished < 6)]
    return jsonify({"device": dev, "jobs": jobs, "wifi": WIFI.summary()})


# =============================================================================
# Mac side
# =============================================================================
def mac_crumbs(path):
    p = Path(path)
    home = Path(MAC_HOME)
    if p == home or home in p.parents:
        parts = p.relative_to(home).parts
        out = [{"name": home.name, "path": str(home), "home": True}]
        for i in range(len(parts)):
            out.append({"name": parts[i], "path": str(home.joinpath(*parts[:i + 1]))})
        return out
    out = [{"name": "Macintosh HD", "path": "/"}]
    for i, part in enumerate(p.parts[1:]):
        out.append({"name": part, "path": "/" + "/".join(p.parts[1:i + 2])})
    return out


@app.route("/api/mac/list")
def mac_list():
    path = os.path.abspath(os.path.expanduser(request.args.get("path") or MAC_HOME))
    hidden = request.args.get("hidden") == "1"
    if not os.path.isdir(path):
        return jsonify({"error": f"“{os.path.basename(path) or path}” isn’t a folder"}), 400
    entries = []
    try:
        for e in os.scandir(path):
            if not hidden and e.name.startswith("."):
                continue
            try:
                st = e.stat()
                is_dir = e.is_dir()
            except OSError:
                continue
            entries.append({"name": e.name, "is_dir": is_dir, "size": None if is_dir else st.st_size,
                            "mtime": st.st_mtime})
    except PermissionError:
        return jsonify({"error": "PhoneBridge isn’t allowed to open this folder. You can allow it in "
                                 "System Settings › Privacy & Security › Files and Folders."}), 403
    parent = str(Path(path).parent)
    return jsonify({"path": path, "parent": parent if parent != path else None,
                    "crumbs": mac_crumbs(path), "entries": entries})


@app.route("/api/mac/info")
def mac_info():
    u = shutil.disk_usage(MAC_HOME)
    return jsonify({"model": MAC_MODEL, "name": MAC_NAME, "free": u.free, "total": u.total, "home": MAC_HOME,
                    "trash": TRASH})


def move_to_trash(path):
    try:
        from Foundation import NSFileManager, NSURL
        ok, _, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(path), None, None)
        if ok:
            return
        raise OSError(str(err))
    except ImportError:
        script = f'tell application "Finder" to delete POSIX file {json.dumps(path)}'
        if subprocess.run(["osascript", "-e", script], capture_output=True).returncode != 0:
            raise OSError(f"Could not move to the {TRASH}")


# =============================================================================
# Phone side
# =============================================================================
@app.route("/api/phone/list")
def phone_list():
    try:
        phone = PM.get()
        path = request.args.get("path") or phone.home()
        entries = phone.list(path)
    except FileNotFoundError:
        return jsonify({"error": "That folder isn’t on the phone any more"}), 404
    except usb.core.USBError as e:
        log.exception("phone list failed (usb)")
        PM.drop("usb error")
        return jsonify({"error": f"Lost the phone ({e})"}), 400
    except Exception as e:
        log.exception("phone list failed")
        return jsonify({"error": str(e)}), 400
    if request.args.get("hidden") != "1":
        entries = [e for e in entries if not e["name"].startswith(".")]
    parent = path.rstrip("/").rsplit("/", 1)[0] or "/"
    return jsonify({"path": path, "parent": parent if path.strip("/") else None,
                    "crumbs": phone.crumbs(path), "entries": entries})


# =============================================================================
# Shared file actions
# =============================================================================
@app.route("/api/mkdir", methods=["POST"])
def mkdir():
    d = body()
    side, path, name = d.get("side"), d.get("path"), (d.get("name") or "").strip()
    if not name or "/" in name:
        return jsonify({"error": "Please choose a different name"}), 400
    try:
        if side == "mac":
            target = os.path.join(path, name)
            if os.path.exists(target):
                return jsonify({"error": f"An item named “{name}” already exists"}), 400
            os.makedirs(target)
        else:
            phone = PM.get()
            if any(e["name"].lower() == name.lower() for e in phone.list(path)):
                return jsonify({"error": f"An item named “{name}” already exists"}), 400
            phone.mkdir(path, name)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/delete", methods=["POST"])
def delete():
    d = body()
    side, folder, items = d.get("side"), d.get("dir"), d.get("items") or []
    failed = []
    for name in items:
        try:
            if side == "mac":
                move_to_trash(os.path.join(folder, name))
            else:
                PM.get().delete(phone_join(folder, name))
        except Exception as e:
            failed.append({"name": name, "error": str(e)})
    PM.invalidate_details()
    return jsonify({"ok": not failed, "failed": failed})


@app.route("/api/rename", methods=["POST"])
def rename():
    d = body()
    side, folder, old, new = d.get("side"), d.get("dir"), d.get("old"), (d.get("new") or "").strip()
    if not new or "/" in new or new.startswith("."):
        return jsonify({"error": "Please choose a different name"}), 400
    try:
        if side == "mac":
            dst = os.path.join(folder, new)
            if os.path.exists(dst) and new.lower() != old.lower():
                return jsonify({"error": f"An item named “{new}” already exists"}), 400
            os.rename(os.path.join(folder, old), dst)
        else:
            phone = PM.get()
            if new.lower() != old.lower() and any(e["name"].lower() == new.lower() for e in phone.list(folder)):
                return jsonify({"error": f"An item named “{new}” already exists"}), 400
            phone.rename(phone_join(folder, old), new)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/open", methods=["POST"])
def open_file():
    d = body()
    side, path = d.get("side"), d.get("path")
    if side == "mac":
        subprocess.run(["open", path], check=False)
        return jsonify({"ok": True})
    name = path.rsplit("/", 1)[-1]

    def run(job):
        phone = PM.get()
        job.total = phone.size_of(path)
        job.items_total = 1
        job.current = name
        folder = OPEN_DIR / job.id
        folder.mkdir(parents=True, exist_ok=True)
        target = phone.pull(path, str(folder), name, job.add, job.cancelled)
        job.items_done = 1
        subprocess.run(["open", target], check=False)

    job = submit(Job("open", f"Opening {name}"), run)
    return jsonify({"job": job.id})


@app.route("/api/reveal", methods=["POST"])
def reveal():
    subprocess.run(["open", "-R", body().get("path", "")], check=False)
    return jsonify({"ok": True})


# =============================================================================
# Thumbnails
# =============================================================================
_inflight = {}
_inflight_lock = threading.Lock()
_ROTATE = {2: Image.FLIP_LEFT_RIGHT, 3: Image.ROTATE_180, 4: Image.FLIP_TOP_BOTTOM, 5: Image.TRANSPOSE,
           6: Image.ROTATE_270, 7: Image.TRANSVERSE, 8: Image.ROTATE_90}


def make_thumb(raw, cache_path, orientation=None):
    img = Image.open(io.BytesIO(raw))
    img.draft("RGB", (THUMB_PX, THUMB_PX))
    if orientation is None:
        img = ImageOps.exif_transpose(img)
    elif orientation in _ROTATE:
        img = img.transpose(_ROTATE[orientation])
    img.thumbnail((THUMB_PX, THUMB_PX))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(cache_path, "JPEG", quality=82)


def make_video_thumb(video_path, cache_path, seeks=("00:00:01", "00:00:00")):
    vf = f"scale={THUMB_PX}:{THUMB_PX}:force_original_aspect_ratio=decrease"
    for seek in seeks:
        proc = subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", seek, "-i", video_path,
                               "-frames:v", "1", "-vf", vf, "-q:v", "4", str(cache_path)],
                              capture_output=True, timeout=120)
        if proc.returncode == 0 and cache_path.exists() and cache_path.stat().st_size > 0:
            return True
    return False


def thumb_path(side, path, version):
    return THUMB_DIR / (hashlib.sha1(f"{side}:{path}:{version}".encode()).hexdigest() + ".jpg")


def ensure_thumb(side, path, version=""):
    ext = os.path.splitext(path)[1].lower()
    is_image, is_video = ext in IMAGE_EXTS, ext in VIDEO_EXTS
    if not (is_image or is_video):
        return None
    cache = thumb_path(side, path, version)
    if cache.exists():
        return cache
    with _inflight_lock:
        ev = _inflight.get(cache)
        mine = ev is None
        if mine:
            ev = _inflight[cache] = threading.Event()
    if not mine:                                    # someone else is making it — wait for them
        ev.wait(30)
        return cache if cache.exists() else None
    try:
        if side == "mac":
            if is_image and ext in (".heic", ".heif", ".tif", ".tiff"):
                subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(THUMB_PX), path, "--out", str(cache)],
                               capture_output=True, timeout=60)
            elif is_image:
                with open(path, "rb") as f:
                    make_thumb(f.read(), cache)
            else:
                make_video_thumb(path, cache)
        else:
            phone = PM.get()
            if is_image:
                raw, orientation = phone.image_bytes(path)
                if raw:
                    make_thumb(raw, cache, orientation)
            else:
                sample = phone.video_sample(path)
                if sample:
                    try:
                        make_video_thumb(sample, cache, seeks=("00:00:00",))
                    finally:
                        os.remove(sample)
        return cache if cache.exists() else None
    except Exception:
        return None
    finally:
        with _inflight_lock:
            _inflight.pop(cache, None)
        ev.set()


@app.route("/api/thumb")
def thumb():
    cache = ensure_thumb(request.args.get("side"), request.args.get("path", ""), request.args.get("v", ""))
    if not cache:
        return ("", 204)
    resp = send_file(cache, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "max-age=86400"
    return resp


@app.route("/api/prefetch", methods=["POST"])
def prefetch():
    """Warm thumbnails for the images in a folder, in the background."""
    d = body()
    side, folder = d.get("side"), d.get("path")
    for item in d.get("items", [])[:2000]:
        name, version = item.get("name", ""), item.get("v", "")
        if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
            continue
        full = phone_join(folder, name) if side == "phone" else os.path.join(folder, name)
        if not thumb_path(side, full, version).exists():
            PREFETCH_POOL.submit(_prefetch_one, side, full, version)
    return jsonify({"ok": True})


def _prefetch_one(side, full, version):
    if side == "phone" and job_running():            # copies come first
        return
    ensure_thumb(side, full, version)


# =============================================================================
# Wi-Fi sharing
# =============================================================================
WIFI = WifiShare(MAC_NAME, INBOX, lambda path: ensure_thumb("mac", path, str(os.path.getmtime(path))))


@app.route("/api/wifi")
def wifi_state():
    return jsonify(WIFI.state(with_qr=True))


@app.route("/api/wifi/start", methods=["POST"])
def wifi_start():
    try:
        WIFI.start()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(WIFI.state(with_qr=True))


@app.route("/api/wifi/stop", methods=["POST"])
def wifi_stop():
    WIFI.stop()
    return jsonify({"ok": True})


@app.route("/api/wifi/send", methods=["POST"])
def wifi_send():
    d = body()
    for name in d.get("items") or []:
        WIFI.offer(os.path.join(d.get("dir", ""), name))
    return jsonify({"ok": True})


@app.route("/api/wifi/reveal", methods=["POST"])
def wifi_reveal():
    INBOX.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(INBOX)], check=False)
    return jsonify({"ok": True})


# =============================================================================
@app.route("/")
def index():
    return render_template("index.html", token=TOKEN, mac_home=MAC_HOME,
                           native=request.args.get("native") == "1")


def start_background():
    PM.start()


if __name__ == "__main__":
    print(f"PhoneBridge running at  http://127.0.0.1:{PORT}")
    start_background()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
