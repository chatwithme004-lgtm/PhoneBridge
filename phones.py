"""
Phone connections for PhoneBridge.

Two ways to reach the phone's files over a USB cable:
  * MtpPhone — the phone's normal "File transfer" mode. Works on any Android phone,
               nothing to enable. This is the default.
  * AdbPhone — USB debugging. Only used when the phone is plugged in with debugging
               on but not in File transfer mode (e.g. "charging only").

PhoneManager watches USB in the background and keeps one connection open.
All phone paths look like "/<storage name>/DCIM/Camera" (MTP) or "/sdcard/DCIM" (adb).
"""
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

import mtp

import logging
import logging.handlers
from pathlib import Path

LOG_PATH = Path.home() / "Library" / "Logs" / "PhoneBridge.log"
log = logging.getLogger("phonebridge")
if not log.handlers:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


def unique_name(name, taken):
    """'photo.jpg' -> 'photo (1).jpg' if the name is already used (case-insensitive)."""
    low = {t.lower() for t in taken}
    if name.lower() not in low:
        return name
    stem, ext = os.path.splitext(name)
    if not ext or stem == "":
        stem, ext = name, ""
    i = 1
    while f"{stem} ({i}){ext}".lower() in low:
        i += 1
    return f"{stem} ({i}){ext}"


def local_unique(dest_dir, name):
    try:
        taken = os.listdir(dest_dir)
    except OSError:
        taken = []
    return unique_name(name, taken)


class Cancelled(Exception):
    pass


# =============================================================================
class MtpPhone:
    kind = "mtp"

    def __init__(self, usb_info):
        self.usb_key = (usb_info["bus"], usb_info["address"])
        self.dev = mtp.MTPDevice(usb_info["dev"]).open()
        self.name = self.dev.info["model"] or f'{usb_info["maker"]} phone'
        self.storages = {}                      # name -> storage id
        self.cache = {}                         # folder path -> {name: entry}
        self.refresh_storages()

    # -- connection ----------------------------------------------------------
    def refresh_storages(self):
        out = {}
        for sid in self.dev.storage_ids():
            info = self.dev.storage_info(sid)
            name = info["name"]
            while name in out:
                name += " 2"
            out[name] = sid
        self.storages = out
        return out

    @property
    def locked(self):
        return not self.storages

    def close(self):
        self.dev.close()

    def home(self):
        return "/" + next(iter(self.storages)) if self.storages else "/"

    def battery(self):
        return self.dev.battery()

    def storage(self):
        if not self.storages:
            return None, None
        info = self.dev.storage_info(next(iter(self.storages.values())))
        return info["free"], info["total"]

    # -- paths ---------------------------------------------------------------
    @staticmethod
    def _split(path):
        return [p for p in path.strip("/").split("/") if p]

    def crumbs(self, path):
        parts, out = self._split(path), []
        for i, p in enumerate(parts):
            out.append({"name": p, "path": "/" + "/".join(parts[:i + 1])})
        return out

    def _listing(self, path, fresh=False):
        if fresh or path not in self.cache:
            sid, handle = self._resolve(path)
            kids = self.dev.children(sid, handle)
            self.cache[path] = {k["name"]: k for k in kids}
        return self.cache[path]

    def _resolve(self, path):
        """path -> (storage id, object handle). Handle is mtp.ROOT for a storage's top level."""
        parts = self._split(path)
        if not parts or parts[0] not in self.storages:
            raise FileNotFoundError(path)
        sid, handle, cur = self.storages[parts[0]], mtp.ROOT, "/" + parts[0]
        for p in parts[1:]:
            ent = self._listing(cur).get(p)
            if ent is None:                      # stale cache — look again once
                ent = self._listing(cur, fresh=True).get(p)
            if ent is None:
                raise FileNotFoundError(path)
            handle, cur = ent["handle"], cur + "/" + p
        return sid, handle

    def _entry(self, path):
        parent, name = path.rsplit("/", 1)
        ent = self._listing(parent or "/").get(name)
        if ent is None:
            ent = self._listing(parent or "/", fresh=True).get(name)
        if ent is None:
            raise FileNotFoundError(path)
        return ent

    def _forget(self, path):
        for k in [k for k in self.cache if k == path or k.startswith(path.rstrip("/") + "/")]:
            self.cache.pop(k, None)

    # -- browsing ------------------------------------------------------------
    def list(self, path):
        if self.locked:
            self.refresh_storages()
        parts = self._split(path)
        if not parts:                            # top level = the phone's storages
            return [{"name": n, "is_dir": True, "size": None, "mtime": None} for n in self.storages]
        kids = self._listing(path, fresh=True)
        return [{"name": k["name"], "is_dir": k["is_dir"], "size": None if k["is_dir"] else k["size"],
                 "mtime": k["mtime"]} for k in kids.values()]

    def size_of(self, path):
        """Total bytes of a file or folder (walks folders)."""
        ent = self._entry(path)
        if not ent["is_dir"]:
            return ent["size"] or 0
        total = 0
        for k in self._listing(path, fresh=True).values():
            total += self.size_of(path + "/" + k["name"])
        return total

    # -- transfers -----------------------------------------------------------
    def pull(self, path, dest_dir, name, progress, cancel):
        """Copy phone file/folder `path` into Mac folder `dest_dir` as `name`."""
        if cancel():
            raise Cancelled()
        ent = self._entry(path)
        target = os.path.join(dest_dir, name)
        if ent["is_dir"]:
            os.makedirs(target, exist_ok=True)
            for k in list(self._listing(path, fresh=True).values()):
                self.pull(path + "/" + k["name"], target, k["name"], progress, cancel)
            return target
        part = target + ".phonebridge-part"
        try:
            with open(part, "wb") as f:
                self.dev.download(ent["handle"], f, ent["size"], progress, cancel)
            os.replace(part, target)
            if ent.get("mtime"):
                os.utime(target, (ent["mtime"], ent["mtime"]))
        except BaseException:
            if os.path.exists(part):
                os.remove(part)
            raise
        return target

    def push(self, local, dest_dir, name, progress, cancel):
        """Copy Mac file/folder `local` into phone folder `dest_dir` as `name`."""
        if cancel():
            raise Cancelled()
        sid, parent = self._resolve(dest_dir)
        if os.path.isdir(local):
            handle = self.dev.make_folder(sid, parent, name)
            self._forget(dest_dir)
            sub = dest_dir.rstrip("/") + "/" + name
            self.cache[sub] = {}
            for child in sorted(os.listdir(local)):
                if child.startswith("."):
                    continue
                self.push(os.path.join(local, child), sub, child, progress, cancel)
            return
        self.dev.upload(sid, parent, name, local, progress)
        self._forget(dest_dir)

    def mkdir(self, dest_dir, name):
        sid, parent = self._resolve(dest_dir)
        self.dev.make_folder(sid, parent, name)
        self._forget(dest_dir)

    def delete(self, path):
        ent = self._entry(path)
        try:
            self.dev.delete(ent["handle"])
        except mtp.MTPError:
            if not ent["is_dir"]:
                raise
            for k in list(self._listing(path, fresh=True).values()):   # empty it first
                self.delete(path + "/" + k["name"])
            self.dev.delete(ent["handle"])
        self._forget(path)
        self._forget(path.rsplit("/", 1)[0] or "/")

    def rename(self, path, new_name):
        ent = self._entry(path)
        self.dev.rename(ent["handle"], new_name)
        self._forget(path)
        self._forget(path.rsplit("/", 1)[0] or "/")

    # -- previews ------------------------------------------------------------
    def image_bytes(self, path):
        """Small JPEG for an image (the phone's own thumbnail when it has one), plus EXIF orientation."""
        ent = self._entry(path)
        thumb = self.dev.thumbnail(ent["handle"])
        if thumb:
            orientation = 1
            try:
                from PIL import Image
                import io
                head = self.dev.read_range(ent["handle"], 0, min(ent["size"], 65536))
                orientation = Image.open(io.BytesIO(head)).getexif().get(0x0112, 1)
            except Exception:
                pass
            return thumb, orientation
        if ent["size"] > 40 * 1024 * 1024:
            return None, 1
        import io
        buf = io.BytesIO()
        self.dev.download(ent["handle"], buf, ent["size"])
        return buf.getvalue(), None              # None = use the file's own EXIF

    def video_sample(self, path):
        """A local stand-in for a phone video good enough for grabbing the first frame:
        a sparse file holding only the start and end (where MP4 keeps its index)."""
        ent = self._entry(path)
        size = ent["size"]
        ext = os.path.splitext(path)[1]
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        if size <= 16 * 1024 * 1024:
            self.dev.download(ent["handle"], tmp, size)
        else:
            head = self.dev.read_range(ent["handle"], 0, 8 * 1024 * 1024)
            tail_len = 4 * 1024 * 1024
            tail = self.dev.read_range(ent["handle"], size - tail_len, tail_len)
            tmp.truncate(size)
            tmp.seek(0); tmp.write(head)
            tmp.seek(size - tail_len); tmp.write(tail)
        tmp.close()
        return tmp.name


# =============================================================================
class AdbPhone:
    kind = "adb"

    def __init__(self, adb_path, serial, model):
        self.adb_path, self.serial = adb_path, serial
        self.name = model.replace("_", " ") if model else "Android phone"
        rc, out, _ = self.run("shell", "getprop", "ro.product.model", timeout=10)
        if rc == 0 and out.strip():
            self.name = out.strip()
        self.locked = False

    def run(self, *args, timeout=120):
        p = subprocess.run([self.adb_path, "-s", self.serial, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def close(self):
        pass

    def home(self):
        return "/sdcard"

    def crumbs(self, path):
        if path.rstrip("/") == "/sdcard" or path.startswith("/sdcard/"):
            rest = [p for p in path[len("/sdcard"):].split("/") if p]
            out = [{"name": "Internal storage", "path": "/sdcard"}]
            for i, p in enumerate(rest):
                out.append({"name": p, "path": "/sdcard/" + "/".join(rest[:i + 1])})
            return out
        parts = [p for p in path.split("/") if p]
        return [{"name": p, "path": "/" + "/".join(parts[:i + 1])} for i, p in enumerate(parts)]

    def battery(self):
        rc, out, _ = self.run("shell", "dumpsys", "battery", timeout=10)
        for line in out.splitlines():
            if line.strip().startswith("level:"):
                try:
                    return int(line.split(":")[1])
                except ValueError:
                    return None
        return None

    def storage(self):
        rc, out, _ = self.run("shell", "df", "/sdcard", timeout=10)
        try:
            line = out.strip().splitlines()[-1].split()
            return int(line[3]) * 1024, int(line[1]) * 1024
        except Exception:
            return None, None

    def list(self, path):
        rc, out, err = self.run("shell", "ls", "-1Ap", shlex.quote(path))
        if rc != 0:
            raise OSError((err or out).strip() or "Cannot open folder")
        entries = []
        for name in out.splitlines():
            if not name:
                continue
            is_dir = name.endswith("/")
            entries.append({"name": name.rstrip("/"), "is_dir": is_dir, "size": None, "mtime": None})
        return entries

    def size_of(self, path):
        rc, out, _ = self.run("shell", "du", "-sk", shlex.quote(path))
        try:
            return int(out.split()[0]) * 1024
        except Exception:
            return 0

    def _watch(self, proc, measure, progress, cancel):
        last = 0
        while proc.poll() is None:
            if cancel():
                proc.kill()
                raise Cancelled()
            time.sleep(0.5)
            now = measure()
            if now > last:
                progress(now - last)
                last = now
        return last

    def pull(self, path, dest_dir, name, progress, cancel):
        target = os.path.join(dest_dir, name)
        proc = subprocess.Popen([self.adb_path, "-s", self.serial, "pull", path, target],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def measure():
            if os.path.isfile(target):
                return os.path.getsize(target)
            total = 0
            for root, _, files in os.walk(target):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            return total
        try:
            done = self._watch(proc, measure, progress, cancel)
        except Cancelled:
            shutil.rmtree(target, ignore_errors=True) if os.path.isdir(target) else (os.path.exists(target) and os.remove(target))
            raise
        out, err = proc.communicate()
        if proc.returncode != 0:
            raise OSError((err or out).strip().splitlines()[-1] if (err or out).strip() else "Copy failed")
        final = measure()
        if final > done:
            progress(final - done)
        return target

    def push(self, local, dest_dir, name, progress, cancel):
        target = dest_dir.rstrip("/") + "/" + name
        proc = subprocess.Popen([self.adb_path, "-s", self.serial, "push", local, target],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        done = self._watch(proc, lambda: self.size_of(target), progress, cancel)
        out, err = proc.communicate()
        if proc.returncode != 0:
            raise OSError((err or out).strip().splitlines()[-1] if (err or out).strip() else "Copy failed")
        total = os.path.getsize(local) if os.path.isfile(local) else sum(
            os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(local) for f in fs)
        if total > done:
            progress(total - done)

    def mkdir(self, dest_dir, name):
        rc, out, err = self.run("shell", "mkdir", "-p", shlex.quote(dest_dir.rstrip("/") + "/" + name))
        if rc != 0:
            raise OSError((err or out).strip())

    def delete(self, path):
        rc, out, err = self.run("shell", "rm", "-rf", shlex.quote(path))
        if rc != 0:
            raise OSError((err or out).strip())

    def rename(self, path, new_name):
        new = path.rsplit("/", 1)[0] + "/" + new_name
        rc, out, err = self.run("shell", "mv", shlex.quote(path), shlex.quote(new))
        if rc != 0:
            raise OSError((err or out).strip())

    def image_bytes(self, path):
        p = subprocess.run([self.adb_path, "-s", self.serial, "exec-out", "cat", shlex.quote(path)],
                           capture_output=True, timeout=120)
        return (p.stdout, None) if p.returncode == 0 else (None, None)

    def video_sample(self, path):
        tmp = tempfile.NamedTemporaryFile(suffix=os.path.splitext(path)[1], delete=False)
        tmp.close()
        rc, _, _ = self.run("pull", path, tmp.name, timeout=600)
        return tmp.name if rc == 0 else None


# =============================================================================
class PhoneManager:
    """Background watcher: finds the phone, opens the best connection, tracks its state."""

    def __init__(self, adb_path):
        self.adb_path = adb_path if adb_path and os.path.exists(adb_path) else None
        self.phone = None
        self.status = "none"                     # none | charging | connecting | locked | ready
        self.label = ""
        self.error = ""
        self.lock = threading.RLock()
        self.info_cache = {"battery": None, "free": None, "total": None, "t": 0}
        self._stop = False
        if self.adb_path:
            subprocess.run([self.adb_path, "start-server"], capture_output=True)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _adb_devices(self):
        if not self.adb_path:
            return []
        try:
            out = subprocess.run([self.adb_path, "devices", "-l"], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return []
        devs = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and not parts[0].startswith("emulator-"):
                model = next((p.split(":", 1)[1] for p in parts if p.startswith("model:")), "")
                devs.append({"serial": parts[0], "state": parts[1], "model": model})
        return devs

    def _loop(self):
        while not self._stop:
            try:
                self.poll()
            except Exception as e:               # never let the watcher die
                self.error = str(e)
                log.exception("watcher error")
            time.sleep(1.0)

    def drop(self, reason=""):
        with self.lock:
            if self.phone:
                log.info("closing %s connection to %s (%s)", self.phone.kind, self.phone.name, reason)
                try:
                    self.phone.close()
                except Exception:
                    pass
            self.phone = None
            self.status = "none"
            self.error = reason

    def poll(self):
        usb_phones = mtp.scan_phones()
        mtp_ready = [p for p in usb_phones if p["mtp"]]
        # Watch for a connection that keeps dropping and coming back (loose cable, weak hub port)
        keys = tuple(sorted((p["bus"], p["address"]) for p in usb_phones))
        now = time.time()
        if keys != getattr(self, "_last_keys", None):
            self._last_keys = keys
            self._flaps = [t for t in getattr(self, "_flaps", []) if now - t < 12] + [now]
        flapping = len([t for t in getattr(self, "_flaps", []) if now - t < 12]) >= 5
        seen = mtp.describe_usb()
        if seen != getattr(self, "_last_usb", None):  # log every change on the USB bus
            self._last_usb = seen
            log.info("USB devices now: %s", " || ".join(seen) or "none")
            log.info("phones recognised: %s", [{k: v for k, v in p.items() if k != "dev"} for p in usb_phones])
        before = self.status
        try:
            self._poll(usb_phones, mtp_ready)
            if flapping and self.status != "ready":
                self.status, self.label = "unstable", (usb_phones[0]["maker"] + " phone") if usb_phones else self.label
        finally:
            if self.status != before:
                log.info("status %s -> %s %s", before, self.status, self.error if self.status == "charging" else "")

    def _poll(self, usb_phones, mtp_ready):
        with self.lock:
            # 1) is the current connection still there?
            if self.phone is not None:
                if self.phone.kind == "mtp":
                    if not any((p["bus"], p["address"]) == self.phone.usb_key for p in mtp_ready):
                        self.drop("unplugged")
                    elif self.phone.locked:
                        try:
                            self.phone.refresh_storages()
                            self.status = "locked" if self.phone.locked else "ready"
                        except Exception:
                            self.drop("lost")
                    return
                if self.phone.kind == "adb":
                    if mtp_ready:                # File transfer just got switched on — prefer it
                        self.drop("switching")
                    elif not any(d["serial"] == self.phone.serial and d["state"] == "device"
                                 for d in self._adb_devices()):
                        self.drop("unplugged")
                        return
                    else:
                        return
            # 2) nothing open: pick the best way in
            if mtp_ready:
                p = mtp_ready[0]
                self.status, self.label = "connecting", p["maker"]
                try:
                    self.phone = MtpPhone(p)
                    self.status = "locked" if self.phone.locked else "ready"
                    self.label = self.phone.name
                    self.info_cache["t"] = 0
                    self.error = ""
                    log.info("opened %s over MTP, storages=%s", self.phone.name, list(self.phone.storages))
                except Exception as e:
                    self.phone = None
                    self.status, self.error = "charging", f"Could not open phone: {e}"
                    log.exception("could not open MTP phone %04x:%04x", p["vid"], p["pid"])
                return
            adb = self._adb_devices()
            ready = [d for d in adb if d["state"] == "device"]
            if ready:
                self.phone = AdbPhone(self.adb_path, ready[0]["serial"], ready[0]["model"])
                self.status, self.label, self.error = "ready", self.phone.name, ""
                self.info_cache["t"] = 0
                return
            if usb_phones or adb:
                maker = usb_phones[0]["maker"] if usb_phones else "Android"
                self.status, self.label = "charging", f"{maker} phone"
            else:
                self.status, self.label = "none", ""

    def details(self):
        """Battery + storage, refreshed at most every 20 s (they need a round trip)."""
        with self.lock:
            ph = self.phone
            if not ph or self.status != "ready":
                return {}
            if time.time() - self.info_cache["t"] > 20:
                try:
                    free, total = ph.storage()
                    self.info_cache.update(battery=ph.battery(), free=free, total=total, t=time.time())
                except Exception:
                    pass
            c = self.info_cache
            return {"battery": c["battery"], "free": c["free"], "total": c["total"]}

    def invalidate_details(self):
        self.info_cache["t"] = 0

    def get(self):
        """The open phone, or raise if there isn't a usable one."""
        with self.lock:
            if self.phone is None or self.status != "ready":
                raise OSError("Phone not connected")
            return self.phone
