#!/usr/bin/env python3
"""openHop control panel for the CardputerZero's built-in 320x170 screen.

Shows whether the openHop repeater is running, the radio region, recent
traffic and the address of the web dashboard, and lets you start/stop it,
pick a region and toggle start-at-boot. All real configuration lives in the
web dashboard; this is the on-device switch and status light.

Draws straight to the framebuffer and reads the keyboard through evdev, like
the other CardputerZero framebuffer apps. Set OPENHOP_UI_PNG=path to render
one frame to a PNG instead (used for store screenshots and development).
"""

import contextlib
import fcntl
import glob
import json
import mmap
import os
import select
import signal
import socket
import struct
import subprocess  # nosec B404 - fixed systemctl/journalctl argument lists only
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

UNIT = "openhop-cz.service"
ETC = os.environ.get("OPENHOP_CZ_ETC", "/etc/openhop-cz")
BIN = os.path.dirname(os.path.abspath(__file__))
FB_DEVICE = os.environ.get("APPLAUNCH_LINUX_FBDEV_DEVICE", "/dev/fb0")
PNG_OUT = os.environ.get("OPENHOP_UI_PNG")
FAKE = os.environ.get("OPENHOP_UI_FAKE")  # JSON state, for screenshots
W, H = 320, 170

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
BG, PANEL, TEXT, DIM = (13, 17, 23), (27, 34, 44), (232, 237, 242), (140, 152, 166)
ACCENT, GREEN, AMBER, RED = (64, 196, 255), (61, 214, 140), (255, 190, 70), (255, 107, 107)

# Linux input key codes
K_ESC, K_ENTER, K_KPENTER, K_UP, K_DOWN = 1, 28, 96, 103, 108
K_Q, K_R, K_Y, K_F, K_X, K_B, K_N, K_SEMI, K_DOT = 16, 19, 21, 33, 45, 48, 49, 39, 52
UP_KEYS, DOWN_KEYS = (K_UP, K_F, K_SEMI), (K_DOWN, K_X, K_DOT)
ENTER_KEYS = (K_ENTER, K_KPENTER)
EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)
EV_KEY = 1


def font(size, bold=False):
    try:
        return ImageFont.truetype(
            FONT_DIR + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size
        )
    except OSError:
        return ImageFont.load_default()


F_TITLE, F_BODY, F_BOLD, F_SMALL = font(14, True), font(12), font(12, True), font(10)


# --------------------------------------------------------------- display


class Screen:
    def __init__(self):
        self.mm = None
        if PNG_OUT:
            return
        fd = os.open(FB_DEVICE, os.O_RDWR)
        var = bytearray(160)
        fcntl.ioctl(fd, 0x4600, var)  # FBIOGET_VSCREENINFO
        self.xres, self.yres, _, _, _, _, bpp = struct.unpack_from("7I", var)
        fix = bytearray(struct.calcsize("16sL6IHHHI"))
        fcntl.ioctl(fd, 0x4602, fix)  # FBIOGET_FSCREENINFO
        fields = struct.unpack("16sL6IHHHI", fix)
        smem_len, self.line_length = fields[2], fields[8]
        if bpp != 16:
            raise RuntimeError(f"expected a 16 bpp framebuffer, got {bpp}")
        self.mm = mmap.mmap(fd, smem_len, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def show(self, img):
        if self.mm is None:
            img.save(PNG_OUT)
            return
        a = np.asarray(img, dtype=np.uint16)
        px = (((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)).astype("<u2")
        rows, cols = min(H, self.yres), min(W, self.xres)
        if self.line_length == cols * 2 and cols == W:
            self.mm.seek(0)
            self.mm.write(px[:rows].tobytes())
        else:
            for y in range(rows):
                self.mm.seek(y * self.line_length)
                self.mm.write(px[y, :cols].tobytes())


class Keys:
    def __init__(self):
        self.fds = []
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                self.fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def read(self, timeout):
        """Key-down codes that arrived within `timeout` seconds."""
        out = []
        if not self.fds:
            time.sleep(timeout)
            return out
        ready, _, _ = select.select(self.fds, [], [], timeout)
        for fd in ready:
            try:
                data = os.read(fd, EV_SIZE * 32)
            except OSError:
                continue
            for off in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
                _, _, etype, code, value = struct.unpack_from(EV_FMT, data, off)
                if etype == EV_KEY and value == 1:
                    out.append(code)
        return out


# ----------------------------------------------------------------- state


def run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)  # nosec B603
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def systemctl(*args):
    return run(["systemctl", "--no-ask-password", *args])


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def list_regions():
    code, out = run([sys.executable, os.path.join(BIN, "make_config.py"), "--list"])
    rows = []
    if code == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def read_state():
    if FAKE:
        return json.loads(FAKE)
    st = {"configured": os.path.exists(os.path.join(ETC, "config.yaml"))}
    st["active"] = (
        systemctl("is-active", UNIT)[1].splitlines()[-1] if st["configured"] else "inactive"
    )
    st["autostart"] = os.path.exists(os.path.join(ETC, "autostart"))
    try:
        with open(os.path.join(ETC, "summary.json")) as f:
            st.update(json.load(f))
    except (OSError, ValueError):
        pass
    st["ip"] = lan_ip()
    st["rx"] = st["tx"] = None
    if st["active"] == "active":
        code, out = run(
            ["journalctl", "-u", UNIT, "--since", "-60s", "-o", "cat", "--no-pager", "-q"],
            timeout=8,
        )
        if code == 0:
            st["rx"] = sum(
                1 for line in out.splitlines() if "Dispatcher" in line and " RX " in line
            )
            st["tx"] = sum(
                1 for line in out.splitlines() if "Dispatcher" in line and " TX " in line
            )
    return st


# --------------------------------------------------------------- drawing


def base(title, pill=None, pill_color=DIM):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 25], fill=PANEL)
    d.text((8, 5), title, font=F_TITLE, fill=TEXT)
    if pill:
        tw = d.textlength(pill, font=F_SMALL)
        d.rounded_rectangle([W - tw - 20, 5, W - 6, 20], radius=7, fill=pill_color)
        d.text((W - tw - 13, 7), pill, font=F_SMALL, fill=BG)
    return img, d


def footer(d, text):
    d.rectangle([0, H - 18, W, H], fill=PANEL)
    d.text((8, H - 15), text, font=F_SMALL, fill=DIM)


def row(d, y, label, value, color=TEXT):
    d.text((8, y), label, font=F_BODY, fill=DIM)
    d.text((76, y), value, font=F_BOLD, fill=color)


def fit(d, text, fnt, width):
    while text and d.textlength(text, font=fnt) > width:
        text = text[:-2].rstrip() + "…" if len(text) > 2 else ""
    return text


PILLS = {
    "active": ("RUNNING", GREEN),
    "activating": ("STARTING", AMBER),
    "deactivating": ("STOPPING", AMBER),
    "failed": ("FAILED", RED),
}


def draw_main(st, note=None):
    pill, color = PILLS.get(st["active"], ("STOPPED", DIM))
    img, d = base("openHop Repeater", pill, color)
    running = st["active"] == "active"
    row(d, 32, "Node", fit(d, st.get("node_name", "-"), F_BOLD, 236))
    row(d, 50, "Region", fit(d, st.get("region", "-"), F_BOLD, 236))
    row(d, 68, "Radio", st.get("radio", "-"))
    if running and st["rx"] is not None:
        row(d, 86, "Last min", f"{st['rx']} heard, {st['tx']} repeated")
    else:
        row(d, 86, "Last min", "-", DIM)
    if st.get("ip"):
        row(d, 104, "Web", f"http://{st['ip']}:8000", ACCENT if running else DIM)
    else:
        row(d, 104, "Web", "no network", DIM)
    row(
        d,
        122,
        "At boot",
        "starts automatically" if st["autostart"] else "stays off",
        TEXT if st["autostart"] else DIM,
    )
    if note:
        d.rectangle([0, H - 36, W, H - 18], fill=BG)
        d.text((8, H - 34), fit(d, note, F_SMALL, W - 16), font=F_SMALL, fill=AMBER)
    footer(d, f"Enter {'stop' if running else 'start'}   B boot   R region   Esc exit")
    return img


def draw_regions(rows, sel, first_run):
    img, d = base("Choose your region")
    top = max(0, min(sel - 2, len(rows) - 6))
    for i, (_, title, desc) in enumerate(rows[top : top + 6]):
        y = 30 + i * 20
        chosen = top + i == sel
        if chosen:
            d.rounded_rectangle([4, y - 2, W - 4, y + 16], radius=4, fill=PANEL, outline=ACCENT)
        d.text(
            (10, y),
            fit(d, title, F_BOLD, 215),
            font=F_BOLD if chosen else F_BODY,
            fill=TEXT if chosen else DIM,
        )
        freq = desc.split(" / ")[0] if desc else ""
        d.text(
            (W - 10 - d.textlength(freq, font=F_SMALL), y + 2),
            freq,
            font=F_SMALL,
            fill=ACCENT if chosen else DIM,
        )
    footer(d, "Up/Down choose   Enter select" + ("" if first_run else "   Esc back"))
    return img


def draw_confirm():
    img, d = base("Before transmitting")
    d.text((8, 36), "Is the LoRa antenna attached?", font=F_BOLD, fill=TEXT)
    d.text((8, 60), "Transmitting without an antenna can", font=F_BODY, fill=DIM)
    d.text((8, 76), "damage the radio. Use only a region", font=F_BODY, fill=DIM)
    d.text((8, 92), "that is legal where you are.", font=F_BODY, fill=DIM)
    d.text((8, 118), "The M5 LoRa and GPS apps cannot use", font=F_SMALL, fill=DIM)
    d.text((8, 131), "the radio while the repeater runs.", font=F_SMALL, fill=DIM)
    footer(d, "Enter start repeater   Esc cancel")
    return img


def draw_message(title, lines, color=TEXT):
    img, d = base(title)
    for i, line in enumerate(lines):
        d.text((8, 40 + i * 18), fit(d, line, F_BODY, W - 16), font=F_BODY, fill=color)
    return img


# ------------------------------------------------------------------ main


class App:
    def __init__(self):
        self.screen = Screen()
        self.keys = Keys()
        self.running = True
        self.note = None
        signal.signal(signal.SIGINT, self.quit)
        signal.signal(signal.SIGTERM, self.quit)

    def quit(self, *_):
        self.running = False

    def act(self, title, args):
        self.screen.show(draw_message(title, ["Please wait…"]))
        code, out = systemctl(*args)
        self.note = None if code == 0 else (out.splitlines()[-1] if out else "command failed")
        return code == 0

    def pick_region(self, first_run):
        rows = list_regions()
        if not rows:
            self.note = "could not load the region list"
            return
        sel = next((i for i, r in enumerate(rows) if "USA" in r[1]), 0)
        while self.running:
            self.screen.show(draw_regions(rows, sel, first_run))
            for k in self.keys.read(0.5):
                if k in UP_KEYS:
                    sel = (sel - 1) % len(rows)
                elif k in DOWN_KEYS:
                    sel = (sel + 1) % len(rows)
                elif k in ENTER_KEYS:
                    was_running = (
                        systemctl("is-active", UNIT)[1].endswith("active") and not first_run
                    )
                    if (
                        self.act(
                            "Saving region", ["start", f"openhop-cz-setup@{rows[sel][0]}.service"]
                        )
                        and was_running
                    ):
                        self.act("Restarting repeater", ["restart", UNIT])
                    return
                elif k in (K_ESC, K_Q):
                    if first_run:
                        self.running = False
                    return

    def confirm_start(self):
        while self.running:
            self.screen.show(draw_confirm())
            for k in self.keys.read(0.5):
                if k in ENTER_KEYS or k == K_Y:
                    return True
                if k in (K_ESC, K_Q, K_N):
                    return False
        return False

    def loop(self):
        st = read_state()
        if PNG_OUT:
            view = os.environ.get("OPENHOP_UI_VIEW", "main")
            if view == "regions":
                rows = list_regions()
                self.screen.show(
                    draw_regions(
                        rows, next((i for i, r in enumerate(rows) if "USA" in r[1]), 0), True
                    )
                )
            elif view == "confirm":
                self.screen.show(draw_confirm())
            else:
                self.screen.show(draw_main(st))
            return
        if not st["configured"]:
            self.pick_region(first_run=True)
        last = 0.0
        while self.running:
            if time.monotonic() - last > 3:
                st = read_state()
                last = time.monotonic()
                if not st["configured"]:
                    self.pick_region(first_run=True)
                    continue
                self.screen.show(draw_main(st, self.note))
            for k in self.keys.read(0.25):
                if k in (K_ESC, K_Q):
                    self.running = False
                elif k in ENTER_KEYS:
                    if st["active"] in ("active", "activating"):
                        self.act("Stopping repeater", ["stop", UNIT])
                    elif self.confirm_start():
                        self.act("Starting repeater", ["start", UNIT])
                    last = 0.0
                elif k == K_B:
                    self.act(
                        "Saving",
                        [
                            "start",
                            f"openhop-cz-autostart@{'off' if st['autostart'] else 'on'}.service",
                        ],
                    )
                    last = 0.0
                elif k == K_R:
                    self.pick_region(first_run=False)
                    last = 0.0


def main():
    try:
        App().loop()
    except Exception as e:  # show the failure on screen rather than a silent exit
        with contextlib.suppress(Exception):  # the screen itself may be what failed
            Screen().show(draw_message("openHop", [str(e)[:44], str(e)[44:88]], RED))
            time.sleep(4)
        print(f"openhop_ui: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
