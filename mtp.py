"""
Minimal MTP (Media Transfer Protocol) client over USB, in pure Python.

This is what lets PhoneBridge talk to any Android phone in its normal
"File transfer" USB mode — no Developer options, no USB debugging.

Uses pyusb + the libusb dylib shipped in the `libusb_package` wheel.
Only the operations PhoneBridge needs are implemented: list, download,
upload, make folder, rename, delete, thumbnails, storage and battery info.
"""
import io
import os
import struct
import subprocess
import threading
import time

import usb.core
import usb.util

try:
    import libusb_package
    _BACKEND = libusb_package.get_libusb1_backend()
except Exception:                                   # fall back to a system libusb
    _BACKEND = None

# --- protocol constants ------------------------------------------------------
CMD, DATA, RESP, EVENT = 1, 2, 3, 4

OP_GET_DEVICE_INFO = 0x1001
OP_OPEN_SESSION = 0x1002
OP_CLOSE_SESSION = 0x1003
OP_GET_STORAGE_IDS = 0x1004
OP_GET_STORAGE_INFO = 0x1005
OP_GET_OBJECT_HANDLES = 0x1007
OP_GET_OBJECT_INFO = 0x1008
OP_GET_OBJECT = 0x1009
OP_GET_THUMB = 0x100A
OP_DELETE_OBJECT = 0x100B
OP_SEND_OBJECT_INFO = 0x100C
OP_SEND_OBJECT = 0x100D
OP_GET_DEVICE_PROP_VALUE = 0x1015
OP_GET_PARTIAL_OBJECT = 0x101B
OP_GET_OBJECT_PROP_VALUE = 0x9803
OP_SET_OBJECT_PROP_VALUE = 0x9804
OP_GET_OBJECT_PROP_LIST = 0x9805
OP_GET_PARTIAL_OBJECT_64 = 0x95C1

RC_OK = 0x2001
RC_SESSION_ALREADY_OPEN = 0x201E
RC_DEVICE_BUSY = 0x2019

FMT_UNDEFINED = 0x3000
FMT_ASSOCIATION = 0x3001                            # = folder

PROP_STORAGE_ID = 0xDC01
PROP_FORMAT = 0xDC02
PROP_SIZE = 0xDC04
PROP_FILENAME = 0xDC07
PROP_DATE_MODIFIED = 0xDC09
PROP_PARENT = 0xDC0B

DEVPROP_BATTERY = 0x5001

ROOT = 0xFFFFFFFF                                    # "parent" of top-level objects

RESPONSE_NAMES = {
    0x2002: "General error", 0x2003: "Session not open", 0x2005: "Operation not supported",
    0x2008: "Invalid storage", 0x2009: "Invalid object", 0x200C: "Phone storage is full",
    0x200D: "Object is write-protected", 0x200F: "Access denied", 0x2013: "Storage not available",
    0x2019: "Phone is busy", 0x201D: "Invalid parameter", 0xA809: "Object too large",
}

# USB vendor IDs of Android phone makers (used to spot a phone that is only charging)
ANDROID_VENDORS = {
    0x18D1: "Google", 0x04E8: "Samsung", 0x22B8: "Motorola", 0x2717: "Xiaomi", 0x2A70: "OnePlus",
    0x22D9: "OPPO", 0x2D95: "vivo", 0x12D1: "Huawei", 0x1004: "LG", 0x0FCE: "Sony", 0x0BB4: "HTC",
    0x17EF: "Lenovo", 0x19D2: "ZTE", 0x0B05: "ASUS", 0x2E04: "Nokia", 0x0E8D: "MediaTek",
    0x05C6: "Qualcomm", 0x1EBF: "Coolpad", 0x2A45: "Meizu", 0x29A9: "Realme", 0x339B: "Honor",
    0x1D5B: "Tecno", 0x2B0E: "Nothing", 0x2970: "Wileyfox", 0x1BBB: "Alcatel", 0x0489: "Foxconn",
}


class MTPError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# --- dataset packing helpers -------------------------------------------------
def pack_str(s):
    if not s:
        return b"\x00"
    data = (s + "\x00").encode("utf-16-le")
    return struct.pack("<B", len(data) // 2) + data


class Reader:
    def __init__(self, data):
        self.d, self.p = data, 0

    def u8(self):
        v = self.d[self.p]; self.p += 1; return v

    def u16(self):
        v, = struct.unpack_from("<H", self.d, self.p); self.p += 2; return v

    def u32(self):
        v, = struct.unpack_from("<I", self.d, self.p); self.p += 4; return v

    def u64(self):
        v, = struct.unpack_from("<Q", self.d, self.p); self.p += 8; return v

    def s(self):
        n = self.u8()
        if n == 0:
            return ""
        raw = self.d[self.p:self.p + n * 2]; self.p += n * 2
        return raw.decode("utf-16-le", "replace").rstrip("\x00")

    def arr16(self):
        return [self.u16() for _ in range(self.u32())]

    def arr32(self):
        return [self.u32() for _ in range(self.u32())]

    def value(self, dtype):
        """Read one typed value (used by GetObjectPropList / GetDevicePropValue)."""
        if dtype == 0xFFFF:
            return self.s()
        simple = {1: ("<b", 1), 2: ("<B", 1), 3: ("<h", 2), 4: ("<H", 2), 5: ("<i", 4),
                  6: ("<I", 4), 7: ("<q", 8), 8: ("<Q", 8)}
        if dtype in simple:
            fmt, n = simple[dtype]
            v, = struct.unpack_from(fmt, self.d, self.p); self.p += n; return v
        if dtype in (9, 10):                         # 128-bit ints (persistent UIDs)
            v = self.d[self.p:self.p + 16]; self.p += 16; return v
        if dtype & 0x4000:                           # arrays
            count = self.u32()
            return [self.value(dtype & 0x3FFF) for _ in range(count)]
        raise MTPError(f"Unknown MTP data type 0x{dtype:04x}")


def parse_mtp_date(s):
    """'20260928T181012' (optionally with .0 / Z / +hhmm) -> unix time, or None."""
    if not s or len(s) < 15:
        return None
    try:
        return time.mktime(time.strptime(s[:15], "%Y%m%dT%H%M%S"))
    except ValueError:
        return None


# --- device discovery --------------------------------------------------------
def _interface_name(dev, intf):
    try:
        return usb.util.get_string(dev, intf.iInterface) if intf.iInterface else ""
    except Exception:
        return ""


def _find_mtp_interface(dev):
    """Return (config, interface) of the phone's MTP interface, or (None, None)."""
    try:
        if dev.idVendor == 0x05AC:                   # Apple: iPhones only expose a camera roll
            return None, None
        known = dev.idVendor in ANDROID_VENDORS
        for cfg in dev:
            for intf in cfg:
                cls = (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol)
                if intf.bNumEndpoints != 3:
                    continue
                if cls == (6, 1, 1) and known:       # PTP "still image" class (phone in PTP mode)
                    return cfg, intf
                if cls == (0xFF, 0xFF, 0x00) and (known or _interface_name(dev, intf) == "MTP"):
                    return cfg, intf                 # Android's MTP interface
    except usb.core.USBError:
        pass
    return None, None


def _has_adb_interface(dev):
    try:
        for cfg in dev:
            for intf in cfg:
                if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) == (0xFF, 0x42, 0x01):
                    return True
    except usb.core.USBError:
        pass
    return False


def scan_phones():
    """List Android-looking USB devices: [{vid, pid, maker, mtp, adb, bus, address}]."""
    found = []
    try:
        devs = list(usb.core.find(find_all=True, backend=_BACKEND))
    except Exception:
        return found
    for d in devs:
        try:
            vid, pid = d.idVendor, d.idProduct
        except Exception:
            continue
        cfg, intf = _find_mtp_interface(d)
        adb = _has_adb_interface(d)
        maker = ANDROID_VENDORS.get(vid)
        try:
            classes = [i.bInterfaceClass for c in d for i in c]
        except usb.core.USBError:
            classes = []
        # A phone that is only charging shows no interfaces (or just adb). Requiring vendor-specific
        # interfaces keeps Samsung SSDs, Lenovo keyboards, ASUS hubs etc. from looking like phones.
        phone_like = maker is not None and all(c in (0xFF, 6) for c in classes)
        if intf is not None or adb or phone_like:
            found.append({"vid": vid, "pid": pid, "maker": maker or "Android", "mtp": intf is not None,
                          "adb": adb, "bus": d.bus, "address": d.address, "dev": d})
    return found


def describe_usb():
    """One line per USB device (vendor:product, maker, interface classes) for the log."""
    out = []
    try:
        for d in usb.core.find(find_all=True, backend=_BACKEND):
            try:
                intfs = ",".join(f"{i.bInterfaceClass:02x}/{i.bInterfaceSubClass:02x}/{i.bInterfaceProtocol:02x}x{i.bNumEndpoints}"
                                 for c in d for i in c)
                out.append(f"{d.idVendor:04x}:{d.idProduct:04x} {ANDROID_VENDORS.get(d.idVendor, '')} [{intfs}]")
            except Exception as e:
                out.append(f"? ({e.__class__.__name__})")
    except Exception as e:
        out.append(f"scan failed: {e}")
    return sorted(out)


def release_from_macos():
    """macOS's photo-import daemon grabs MTP devices on plug-in; stop it so we can connect.
    It runs as the current user (a LaunchAgent) and restarts on its own when needed."""
    for name in ("ptpcamerad", "Android File Transfer Agent"):
        subprocess.run(["pkill", "-9", "-x", name], capture_output=True)   # it ignores a polite SIGTERM


# --- the MTP session ---------------------------------------------------------
class MTPDevice:
    CHUNK = 1024 * 1024                              # USB transfer size (multiple of packet size)

    def __init__(self, usb_dev):
        self.dev = usb_dev
        self.lock = threading.RLock()
        self.tid = 0
        self.info = None
        self.ops = set()
        self._claimed = False
        cfg, intf = _find_mtp_interface(usb_dev)
        if intf is None:
            raise MTPError("Phone is not in File transfer mode")
        self.intf = intf
        self.ep_in = self.ep_out = self.ep_int = None
        for ep in intf:
            t = usb.util.endpoint_type(ep.bmAttributes)
            d = usb.util.endpoint_direction(ep.bEndpointAddress)
            if t == usb.util.ENDPOINT_TYPE_BULK and d == usb.util.ENDPOINT_IN:
                self.ep_in = ep
            elif t == usb.util.ENDPOINT_TYPE_BULK and d == usb.util.ENDPOINT_OUT:
                self.ep_out = ep
            elif t == usb.util.ENDPOINT_TYPE_INTR:
                self.ep_int = ep
        self.psize = self.ep_out.wMaxPacketSize or 512

    # -- connection ----------------------------------------------------------
    def open(self, attempts=12):
        last = None
        if self.intf.bInterfaceClass == 6:          # camera-class phones (Xiaomi…): macOS grabs these at once
            release_from_macos()
            time.sleep(0.3)
        for i in range(attempts):
            try:
                usb.util.claim_interface(self.dev, self.intf.bInterfaceNumber)
                self._claimed = True
                break
            except usb.core.USBError as e:           # someone else (ptpcamerad) holds it
                last = e
                release_from_macos()
                time.sleep(0.25 + 0.1 * i)
        if not self._claimed:
            raise MTPError(f"Could not open the phone ({last})")
        if self.intf.bInterfaceClass == 6:
            release_from_macos()                    # in case it restarted while we were claiming
        for ep in (self.ep_in, self.ep_out):
            try:
                self.dev.clear_halt(ep)
            except usb.core.USBError:
                pass
        self._drain()
        try:
            self.info = self.device_info()
        except usb.core.USBError:
            try:                                    # PTP class "Device Reset", then try once more
                self.dev.ctrl_transfer(0x21, 0x66, 0, self.intf.bInterfaceNumber, None, timeout=3000)
                time.sleep(0.3)
            except usb.core.USBError:
                pass
            self.info = self.device_info()
        self.ops = set(self.info["operations"])
        try:
            self.transaction(OP_OPEN_SESSION, [1])
        except MTPError as e:
            if e.code != RC_SESSION_ALREADY_OPEN:
                raise
        return self

    def close(self):
        with self.lock:
            try:
                self.transaction(OP_CLOSE_SESSION, timeout=2000)
            except Exception:
                pass
            try:
                if self._claimed:
                    usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self._claimed = False

    def _drain(self):
        """Throw away anything left in the pipe by a previous owner."""
        for _ in range(4):
            try:
                self.ep_in.read(self.CHUNK, timeout=60)
            except usb.core.USBError:
                break

    # -- low level -----------------------------------------------------------
    def _send_command(self, code, params):
        self.tid += 1
        hdr = struct.pack("<IHHI", 12 + 4 * len(params), CMD, code, self.tid)
        self.ep_out.write(hdr + b"".join(struct.pack("<I", p) for p in params), timeout=10000)

    def _send_data(self, code, src, size, progress=None, timeout=60000):
        """Send a data phase. src: bytes or a file object; size: payload length."""
        total = 12 + size
        hdr = struct.pack("<IHHI", total if total <= 0xFFFFFFFF else 0xFFFFFFFF, DATA, code, self.tid)
        if isinstance(src, (bytes, bytearray)):
            src = io.BytesIO(src)
        first = hdr + src.read(self.CHUNK - 12)
        self.ep_out.write(first, timeout=timeout)
        sent = len(first) - 12
        if progress:
            progress(sent)
        while sent < size:
            buf = src.read(self.CHUNK)
            if not buf:
                raise MTPError("File ended early while sending")
            self.ep_out.write(buf, timeout=timeout)
            sent += len(buf)
            if progress:
                progress(len(buf))
        if total % self.psize == 0:                 # tell the phone the transfer is complete
            self.ep_out.write(b"", timeout=timeout)

    def _read(self, timeout):
        return bytes(self.ep_in.read(self.CHUNK, timeout=timeout))

    def _recv_data_or_response(self, code, sink=None, progress=None, timeout=60000):
        """Read the data phase (if any) and the response. Returns (data_bytes, response_params)."""
        buf = self._read(timeout)
        while len(buf) == 0:                         # zero-length packet left over
            buf = self._read(timeout)
        length, ctype, rcode, tid = struct.unpack_from("<IHHI", buf, 0)
        data = None
        if ctype == DATA:
            got = buf[12:]
            if sink is not None:
                sink.write(got)
            else:
                parts = [got]
            received = len(got)
            if progress:
                progress(len(got))
            unknown = length == 0xFFFFFFFF
            expected = length - 12
            last_len = len(buf)
            while (unknown and last_len == self.CHUNK) or (not unknown and received < expected):
                chunk = self._read(timeout)
                last_len = len(chunk)
                if not chunk:
                    if unknown:
                        break
                    continue
                received += len(chunk)
                if sink is not None:
                    sink.write(chunk)
                else:
                    parts.append(chunk)
                if progress:
                    progress(len(chunk))
                if unknown and len(chunk) % self.psize:
                    break
            if sink is None:
                data = b"".join(parts)
            buf = self._read(timeout)
            while len(buf) == 0:
                buf = self._read(timeout)
            length, ctype, rcode, tid = struct.unpack_from("<IHHI", buf, 0)
        if ctype != RESP:
            raise MTPError(f"Unexpected reply from phone (type {ctype})")
        params = list(struct.unpack_from("<%dI" % ((length - 12) // 4), buf, 12)) if length > 12 else []
        if rcode != RC_OK:
            raise MTPError(RESPONSE_NAMES.get(rcode, f"Phone said 0x{rcode:04x}"), rcode)
        return data, params

    def transaction(self, code, params=(), data_out=None, out_size=None, sink=None,
                    progress=None, timeout=60000):
        with self.lock:
            for attempt in range(3):
                self._send_command(code, list(params))
                if data_out is not None:
                    size = out_size if out_size is not None else len(data_out)
                    self._send_data(code, data_out, size, progress, timeout)
                try:
                    return self._recv_data_or_response(code, sink, progress, timeout)
                except MTPError as e:
                    if e.code == RC_DEVICE_BUSY and attempt < 2 and data_out is None and sink is None:
                        time.sleep(0.5)
                        continue
                    raise

    # -- datasets ------------------------------------------------------------
    def device_info(self):
        data, _ = self.transaction(OP_GET_DEVICE_INFO)
        r = Reader(data)
        r.u16(); r.u32(); r.u16(); r.s(); r.u16()
        ops = r.arr16(); r.arr16(); props = r.arr16(); r.arr16(); r.arr16()
        return {"operations": ops, "device_props": props, "manufacturer": r.s(),
                "model": r.s(), "version": r.s(), "serial": r.s()}

    def storage_ids(self):
        data, _ = self.transaction(OP_GET_STORAGE_IDS)
        # skip "logical" storages without media (id low word 0)
        return [s for s in Reader(data).arr32() if s & 0xFFFF]

    def storage_info(self, sid):
        data, _ = self.transaction(OP_GET_STORAGE_INFO, [sid])
        r = Reader(data)
        r.u16(); r.u16(); access = r.u16()
        total, free = r.u64(), r.u64()
        r.u32()
        desc, label = r.s(), r.s()
        return {"id": sid, "name": desc or label or "Storage", "total": total, "free": free,
                "read_only": access == 1}

    def battery(self):
        if DEVPROP_BATTERY not in self.info.get("device_props", []):
            return None
        try:
            data, _ = self.transaction(OP_GET_DEVICE_PROP_VALUE, [DEVPROP_BATTERY], timeout=5000)
            return data[0] if data else None
        except Exception:
            return None

    def object_info(self, handle):
        data, _ = self.transaction(OP_GET_OBJECT_INFO, [handle])
        r = Reader(data)
        sid = r.u32(); fmt = r.u16(); r.u16(); size = r.u32()
        thumb_fmt = r.u16(); thumb_size = r.u32()
        r.u32(); r.u32(); r.u32(); r.u32(); r.u32()
        parent = r.u32(); r.u16(); r.u32(); r.u32()
        name = r.s(); r.s(); modified = r.s()
        if size == 0xFFFFFFFF:                       # bigger than 4 GB — ask for the 64-bit size
            size = self.prop_value(handle, PROP_SIZE) or size
        return {"handle": handle, "storage": sid, "name": name, "is_dir": fmt == FMT_ASSOCIATION,
                "size": 0 if fmt == FMT_ASSOCIATION else size, "mtime": parse_mtp_date(modified),
                "parent": parent, "thumb": thumb_size if thumb_fmt else 0}

    def prop_value(self, handle, prop):
        dtypes = {PROP_SIZE: 8, PROP_FILENAME: 0xFFFF, PROP_FORMAT: 4}
        try:
            data, _ = self.transaction(OP_GET_OBJECT_PROP_VALUE, [handle, prop])
            return Reader(data).value(dtypes.get(prop, 6))
        except MTPError:
            return None

    def children(self, sid, parent):
        """List a folder. parent=ROOT for the top of a storage."""
        data, _ = self.transaction(OP_GET_OBJECT_HANDLES, [sid, 0, parent], timeout=120000)
        handles = Reader(data).arr32()
        if not handles:
            return []
        fast = self._children_proplist(sid, parent, handles)
        if fast is not None:
            return fast
        return [self.object_info(h) for h in handles]

    def _children_proplist(self, sid, parent, handles):
        """Fetch a whole folder's names/types/sizes/dates with one GetObjectPropList (depth 1) per
        property, instead of one GetObjectInfo per file. None if the phone can't do it."""
        if OP_GET_OBJECT_PROP_LIST not in self.ops or parent == ROOT:
            return None
        objs = {}
        try:
            for prop in (PROP_FILENAME, PROP_FORMAT, PROP_SIZE, PROP_DATE_MODIFIED):
                data, _ = self.transaction(OP_GET_OBJECT_PROP_LIST, [parent, 0, prop, 0, 1], timeout=120000)
                r = Reader(data)
                for _ in range(r.u32()):
                    h = r.u32(); code = r.u16(); dtype = r.u16()
                    objs.setdefault(h, {})[code] = r.value(dtype)
        except Exception:
            return None
        objs.pop(parent, None)                       # Android includes the folder itself
        if set(objs) != set(handles):                # some phones get this wrong — fall back
            return None
        out = []
        for h in handles:
            p = objs[h]
            if PROP_FILENAME not in p or PROP_FORMAT not in p:
                return None
            is_dir = p[PROP_FORMAT] == FMT_ASSOCIATION
            out.append({"handle": h, "storage": p.get(PROP_STORAGE_ID, sid), "name": p[PROP_FILENAME],
                        "is_dir": is_dir, "size": 0 if is_dir else p.get(PROP_SIZE, 0),
                        "mtime": parse_mtp_date(p.get(PROP_DATE_MODIFIED)), "parent": parent, "thumb": None})
        return out

    # -- file operations -----------------------------------------------------
    def download(self, handle, fileobj, size=None, progress=None, cancel=None):
        """Copy an object into an open file. Uses 16 MB partial reads when the phone supports it,
        so big files can be cancelled and other requests can slip in between chunks."""
        if size is None:
            size = self.object_info(handle)["size"]
        step = 16 * 1024 * 1024
        if OP_GET_PARTIAL_OBJECT_64 in self.ops and size > step:
            off = 0
            while off < size:
                if cancel and cancel():
                    raise MTPError("Cancelled")
                n = min(step, size - off)
                self.transaction(OP_GET_PARTIAL_OBJECT_64, [handle, off & 0xFFFFFFFF, off >> 32, n],
                                 sink=fileobj, progress=progress)
                off += n
            return
        self.transaction(OP_GET_OBJECT, [handle], sink=fileobj, progress=progress, timeout=120000)

    def read_range(self, handle, offset, length):
        buf = io.BytesIO()
        if OP_GET_PARTIAL_OBJECT_64 in self.ops:
            self.transaction(OP_GET_PARTIAL_OBJECT_64, [handle, offset & 0xFFFFFFFF, offset >> 32, length], sink=buf)
        elif OP_GET_PARTIAL_OBJECT in self.ops and offset + length < 0xFFFFFFFF:
            self.transaction(OP_GET_PARTIAL_OBJECT, [handle, offset, length], sink=buf)
        else:
            raise MTPError("Partial reads not supported")
        return buf.getvalue()

    def thumbnail(self, handle):
        try:
            data, _ = self.transaction(OP_GET_THUMB, [handle], timeout=15000)
            return data or None
        except MTPError:
            return None

    def _object_info_dataset(self, sid, parent, name, size, fmt, mtime=None):
        mod = time.strftime("%Y%m%dT%H%M%S", time.localtime(mtime)) if mtime else ""
        return (struct.pack("<IHHI", sid, fmt, 0, size if size < 0xFFFFFFFF else 0xFFFFFFFF)
                + struct.pack("<HIIIIIII", 0, 0, 0, 0, 0, 0, 0, parent)
                + struct.pack("<HII", 1 if fmt == FMT_ASSOCIATION else 0, 0, 0)
                + pack_str(name) + pack_str("") + pack_str(mod) + pack_str(""))

    def make_folder(self, sid, parent, name):
        ds = self._object_info_dataset(sid, parent, name, 0, FMT_ASSOCIATION)
        _, params = self.transaction(OP_SEND_OBJECT_INFO, [sid, parent], data_out=ds)
        return params[2]

    def upload(self, sid, parent, name, path, progress=None):
        size = os.path.getsize(path)
        ds = self._object_info_dataset(sid, parent, name, size, FMT_UNDEFINED, os.path.getmtime(path))
        with self.lock:                              # info + data must be back to back
            _, params = self.transaction(OP_SEND_OBJECT_INFO, [sid, parent], data_out=ds)
            with open(path, "rb") as f:
                self.transaction(OP_SEND_OBJECT, data_out=f, out_size=size, progress=progress, timeout=120000)
        return params[2]

    def delete(self, handle):
        self.transaction(OP_DELETE_OBJECT, [handle, 0], timeout=120000)

    def rename(self, handle, new_name):
        self.transaction(OP_SET_OBJECT_PROP_VALUE, [handle, PROP_FILENAME], data_out=pack_str(new_name))
