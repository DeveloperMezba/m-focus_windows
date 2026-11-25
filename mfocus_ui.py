#!/usr/bin/env python3
"""M Focus - focus timer + website/app blocker for Windows (PyQt6 / PyQt5).

The window is only the remote control: blocking is done by a small background service (Windows Task Scheduler, runs as SYSTEM)
(enforcer.py) so it keeps working even if this window is closed or crashes.
"""
import html
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import wave
from datetime import date, timedelta
from pathlib import Path

try:
    from PyQt6.QtCore import (QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, QTimer,
                              QVariantAnimation, pyqtSignal)
    from PyQt6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QFontDatabase, QFontMetricsF,
                             QIcon, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient)
    from PyQt6.QtWidgets import (QApplication, QCompleter, QDialog, QFrame, QGraphicsOpacityEffect,
                                 QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                                 QPushButton, QScrollArea, QStackedWidget, QSystemTrayIcon,
                                 QToolTip, QVBoxLayout, QWidget)
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
except ImportError:   # the Windows 8.1 build uses PyQt5
    from PyQt5.QtCore import (QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, QTimer,
                              QVariantAnimation, pyqtSignal)
    from PyQt5.QtGui import (QBrush, QColor, QConicalGradient, QFont, QFontDatabase, QFontMetricsF,
                             QIcon, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient)
    from PyQt5.QtWidgets import (QApplication, QCompleter, QDialog, QFrame, QGraphicsOpacityEffect,
                                 QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                                 QPushButton, QScrollArea, QStackedWidget, QSystemTrayIcon,
                                 QToolTip, QVBoxLayout, QWidget)
    from PyQt5.QtNetwork import QLocalServer, QLocalSocket

import mf_core

VERSION = "1.0"
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
STATE_DIR = mf_core.DATA
STATE, HIST = mf_core.STATE, mf_core.HIST
_APPDATA = Path(os.environ.get("APPDATA", str(Path.home())))
_LOCAL = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
CONF = Path(os.environ.get("MFOCUS_CONF", str(_APPDATA / "MFocus" / "config.json")))
CACHE = _LOCAL / "MFocus" / "cache"
MAXM = 180                      # longest session you can pick with the ring

CATEGORIES = {
    "Social": ["facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com",
               "snapchat.com", "reddit.com", "threads.net", "pinterest.com"],
    "Video": ["youtube.com", "twitch.tv", "netflix.com", "dailymotion.com", "vimeo.com"],
    "Chat": ["web.whatsapp.com", "web.telegram.org", "messenger.com", "discord.com", "t.me"],
    "News": ["cnn.com", "bbc.com", "nytimes.com", "theguardian.com", "prothomalo.com",
             "news.ycombinator.com"],
    "Shopping": ["amazon.com", "ebay.com", "aliexpress.com", "daraz.com.bd"],
    "Games": ["store.steampowered.com", "steamcommunity.com", "epicgames.com", "roblox.com"],
}
DEFAULT_CATS = ("Social", "Video", "Chat")
APP_SUGGESTIONS = "discord, steam, slack, spotify"

GROUP_ORDER = list(CATEGORIES) + ["My sites", "Apps"]


def _pairs(raw):
    out = []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, (list, tuple)) and len(it) == 2 and isinstance(it[0], str):
                out.append([it[0], bool(it[1])])
    return out


def default_group(name):
    return [[d, name in DEFAULT_CATS] for d in CATEGORIES.get(name, [])]


def load_groups(conf):
    """{group: [[name, on], ...]} - every site/app has its own switch.
    Also migrates the first config format (category switches + loose lists)."""
    groups = {n: default_group(n) for n in GROUP_ORDER}
    saved = conf.get("groups")
    if isinstance(saved, dict):
        for n in groups:
            if isinstance(saved.get(n), list):
                groups[n] = _pairs(saved[n])
    else:
        cats = conf.get("cats") if isinstance(conf.get("cats"), dict) else {}
        for n in CATEGORIES:
            groups[n] = [[d, bool(cats.get(n, n in DEFAULT_CATS))] for d in CATEGORIES[n]]
        groups["My sites"] = _pairs(conf.get("sites"))
        groups["Apps"] = _pairs(conf.get("apps"))
    return groups


DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
APP_RE = re.compile(r"^[A-Za-z0-9._+-]{1,15}$")


def norm_domain(raw):
    s = re.sub(r"^[a-z]+://", "", raw.strip().lower())
    s = s.split("/")[0].split("?")[0].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s if DOMAIN_RE.match(s) else ""


def norm_app(raw):
    s = raw.strip()
    return s if APP_RE.match(s) else ""


# ------------------------------------------------------------------ themes
# Every theme: surfaces (BG PANEL RAISED LINE), text (TEXT SUB), a two-colour accent
# gradient (A1 -> A2) and ON = text colour used on top of the accent.
THEMES = {
    "Aurora": dict(MODE="dark", BG="#0B0E14", PANEL="#121722", RAISED="#1A2130", LINE="#252E41",
                   TEXT="#EAF0FA", SUB="#8C98AE", A1="#35E0C0", A2="#7C6CFF", ON="#06151A"),
    "Ember": dict(MODE="dark", BG="#110D0B", PANEL="#1A1411", RAISED="#251D18", LINE="#362A22",
                  TEXT="#F6EDE6", SUB="#A8978A", A1="#FFB347", A2="#FF5E7E", ON="#1E0A06"),
    "Forest": dict(MODE="dark", BG="#0A110D", PANEL="#101B14", RAISED="#17261C", LINE="#233527",
                   TEXT="#E6F2E9", SUB="#86A08D", A1="#7CF29A", A2="#D4F25C", ON="#04150A"),
    "Ocean": dict(MODE="dark", BG="#07111A", PANEL="#0D1B29", RAISED="#142538", LINE="#1F3650",
                  TEXT="#E4F1FB", SUB="#7F9BB3", A1="#4DD2FF", A2="#3D7BFF", ON="#031521"),
    "Mono": dict(MODE="dark", BG="#000000", PANEL="#0C0C0C", RAISED="#171717", LINE="#2A2A2A",
                 TEXT="#F2F2F2", SUB="#8A8A8A", A1="#FFFFFF", A2="#A8A8A8", ON="#000000"),
    "Paper": dict(MODE="light", BG="#F7F4EE", PANEL="#FFFFFF", RAISED="#EFEBE3", LINE="#DDD6C8",
                  TEXT="#1D1B16", SUB="#7A7366", A1="#5B5BF0", A2="#A05CF6", ON="#FFFFFF"),
    "Mint": dict(MODE="light", BG="#F1F8F5", PANEL="#FFFFFF", RAISED="#E3F0EA", LINE="#CBE0D6",
                 TEXT="#12231C", SUB="#5E7A6E", A1="#20D3A0", A2="#12B886", ON="#03261A"),
    "Sky": dict(MODE="light", BG="#F2F7FC", PANEL="#FFFFFF", RAISED="#E4EEF8", LINE="#CFDDEC",
                TEXT="#0F1D2E", SUB="#5F7690", A1="#2D9CFF", A2="#6A5BFF", ON="#FFFFFF"),
}
DEFAULT_THEME = "Aurora"
THEME_NAME = DEFAULT_THEME


def apply_theme(name):
    """Copy the theme's colours into module globals (BG, PANEL, A1, ...)."""
    global THEME_NAME
    if name not in THEMES:
        name = DEFAULT_THEME
    THEME_NAME = name
    globals().update(THEMES[name])


apply_theme(DEFAULT_THEME)

STYLE = """
QWidget { background: @BG; color: @TEXT; font-size: 14px; }
QWidget#clear { background: transparent; }
QLabel { background: transparent; }
QLabel#h1 { font-size: 26px; font-weight: 700; }
QLabel#h2 { font-size: 16px; font-weight: 600; }
QLabel#brand { font-size: 20px; font-weight: 800; color: @A1; }
QLabel#sub { color: @SUB; }
QLabel#chip { background: @PANEL; color: @SUB; border: 1px solid @LINE; border-radius: 16px; padding: 7px 16px; }
QFrame#card { background: @PANEL; border: 1px solid @LINE; border-radius: 22px; }
QFrame#card[sel="true"] { border: 2px solid @A1; }
QFrame#row { background: @RAISED; border-radius: 12px; }
QFrame#nav { background: @PANEL; border: 1px solid @LINE; border-radius: 22px; }
QPushButton { background: @RAISED; color: @TEXT; border: 1px solid @LINE; border-radius: 14px; padding: 10px 18px; font-weight: 600; }
QPushButton:hover { border-color: @A1; }
QPushButton:disabled { color: @SUB; background: @PANEL; }
QPushButton#primary { background: @GRAD; color: @ON; border: none; border-radius: 18px; padding: 15px 30px; font-size: 16px; font-weight: 700; }
QPushButton#primary:hover { background: @A1; }
QPushButton#primary:disabled { background: @RAISED; color: @SUB; }
QPushButton#ghost { background: transparent; border: none; color: @SUB; padding: 8px 12px; }
QPushButton#ghost:hover { color: @TEXT; }
QPushButton#tab { background: transparent; color: @SUB; border: none; border-radius: 17px; padding: 9px 22px; font-weight: 600; }
QPushButton#tab:hover { color: @TEXT; }
QPushButton#tab:checked { background: @GRAD; color: @ON; }
QPushButton#pill { background: @RAISED; border: 1px solid @LINE; border-radius: 15px; padding: 7px 14px; color: @SUB; }
QPushButton#pill:hover { color: @TEXT; border-color: @A1; }
QLineEdit { background: @RAISED; border: 1px solid @LINE; border-radius: 14px; padding: 11px 14px; color: @TEXT; selection-background-color: @A1; selection-color: @ON; }
QLineEdit:focus { border-color: @A1; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget { background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @LINE; border-radius: 4px; min-height: 36px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: @RAISED; color: @TEXT; border: 1px solid @LINE; padding: 6px; }
QDialog, QMessageBox { background: @BG; }
QMenu { background: @PANEL; border: 1px solid @LINE; padding: 6px; }
QMenu::item { padding: 8px 22px; border-radius: 8px; }
QMenu::item:selected { background: @RAISED; }
QAbstractItemView { background: @PANEL; color: @TEXT; border: 1px solid @LINE; selection-background-color: @RAISED; }
"""


def qss():
    t = THEMES[THEME_NAME]
    s = STYLE.replace("@GRAD", "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                      "stop:0 %s, stop:1 %s)" % (t["A1"], t["A2"]))
    for k in sorted(t, key=len, reverse=True):
        s = s.replace("@" + k, str(t[k]))
    return s


def qc(hex_, alpha=255):
    c = QColor(hex_)
    c.setAlpha(alpha)
    return c


# ------------------------------------------------------------------ data helpers
def epos(e):
    """Mouse position (QPointF) on both PyQt6 and PyQt5."""
    return e.position() if hasattr(e, "position") else e.localPos()


def egpos(e):
    return e.globalPosition() if hasattr(e, "globalPosition") else e.globalPos()


def read_json(path, default):
    return mf_core.read_json(path, default)


def write_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def fmt_min(m, short=False):
    m = int(round(m))
    h, mm = divmod(m, 60)
    if short:
        if h and mm == 0:
            return f"{h}h"
        return f"{h}h{mm:02d}" if h else f"{mm}m"
    return f"{h} h {mm:02d} min" if h else f"{mm} min"


def fmt_clock(sec):
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def history_by_day():
    """{'YYYY-MM-DD': {minutes, sessions, hours[24], labels{}}} from history.json."""
    days = {}
    for e in read_json(HIST, []):
        try:
            d, m = e["date"], int(e["minutes"])
        except (KeyError, TypeError, ValueError):
            continue
        rec = days.setdefault(d, {"minutes": 0, "sessions": 0, "hours": [0.0] * 24, "labels": {}})
        rec["minutes"] += m
        rec["sessions"] += 1
        lab = str(e.get("label", "")).strip()
        if lab:
            rec["labels"][lab] = rec["labels"].get(lab, 0) + m
        st = e.get("start")
        if isinstance(st, (int, float)):
            for k in range(m):
                rec["hours"][time.localtime(st + k * 60).tm_hour] += 1
    return days


def minutes_on(days, d):
    return days.get(d.isoformat(), {}).get("minutes", 0)


def streak_of(days, today):
    d = today if minutes_on(days, today) > 0 else today - timedelta(days=1)
    n = 0
    while minutes_on(days, d) > 0:
        n += 1
        d -= timedelta(days=1)
    return n


class HistoryCache:
    """history.json parsed once; re-parsed only when the file or the date changes."""
    def __init__(self):
        self.days, self.version, self._sig = {}, 0, "x"

    def get(self):
        try:
            sig = (HIST.stat().st_mtime_ns, date.today())
        except OSError:
            sig = (None, date.today())
        if sig != self._sig:
            self._sig = sig
            self.days = history_by_day()
            self.version += 1
        return self.days


# ------------------------------------------------------------------ sound
class Sfx:
    """A wheel-style 'click' for every minute the ring moves, and a soft chime.
    Plays through Windows' built-in winsound (no extra libraries); silent if that fails."""
    GAIN = 0.6      # the Linux build plays these through QSoundEffect at volume 0.6

    def __init__(self):
        self.enabled = True
        self.last = {}
        g = self.GAIN
        rnd = __import__("random").Random(7)
        self.files = {
            "click": self._wav("click.wav", 0.028, lambda t: g * 0.6 * (
                0.55 * (rnd.random() * 2 - 1) * math.exp(-t * 520)
                + 0.75 * math.sin(2 * math.pi * 950 * t) * math.exp(-t * 170))),
            "chime": self._wav("chime.wav", 0.9, lambda t: g * 0.35 * math.exp(-t * 5) * (
                math.sin(2 * math.pi * 880 * t) + 0.6 * math.sin(2 * math.pi * 1320 * t))),
        }
        try:
            import winsound
            self._ws = winsound
        except ImportError:
            self._ws = None

    @staticmethod
    def _wav(name, dur, fn):
        path = CACHE / name
        if not path.exists():
            try:
                CACHE.mkdir(parents=True, exist_ok=True)
                rate = 44100
                data = bytearray()
                for i in range(int(rate * dur)):
                    data += struct.pack("<h", int(32767 * max(-1.0, min(1.0, fn(i / rate)))))
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(rate)
                    w.writeframes(bytes(data))
            except Exception:
                pass
        return path

    def play(self, name="click"):
        if not self.enabled or self._ws is None:
            return
        now = time.monotonic()
        if now - self.last.get(name, 0) < 0.03:
            return
        self.last[name] = now
        if self.files[name].exists():
            try:
                self._ws.PlaySound(str(self.files[name]),
                                   self._ws.SND_FILENAME | self._ws.SND_ASYNC | self._ws.SND_NODEFAULT)
            except Exception:
                pass


SFX = None


def sound(name="click"):
    if SFX is not None:
        SFX.play(name)


# ------------------------------------------------------------------ animation helpers
def animate_attr(widget, name, start, end, ms=450, curve=QEasingCurve.Type.OutCubic, cb=None):
    """Smoothly animate a float attribute of a widget (repaints every frame).
    Used for page transitions and charts - never for the time ring."""
    key = "_anim_" + name
    old = getattr(widget, key, None)
    if old is not None:
        old.stop()
        old.deleteLater()
    a = QVariantAnimation(widget)
    a.setDuration(ms)
    a.setEasingCurve(curve)
    a.setStartValue(float(start))
    a.setEndValue(float(end))

    def step(v):
        setattr(widget, name, float(v))
        if cb:
            cb(float(v))
        widget.update()
    a.valueChanged.connect(step)
    setattr(widget, key, a)
    a.start()


def fade_in(widget, ms=240):
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    a = QPropertyAnimation(eff, b"opacity", widget)
    a.setDuration(ms)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve.Type.OutCubic)
    a.finished.connect(lambda: widget.setGraphicsEffect(None))
    a.start()
    return a


# ------------------------------------------------------------------ small widgets
class Switch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, on=False):
        super().__init__()
        self.on = bool(on)
        self.setFixedSize(50, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_on(self, v):
        self.on = bool(v)
        self.update()

    def mousePressEvent(self, _e):
        if self.isEnabled():
            self.on = not self.on
            self.update()
            self.toggled.emit(self.on)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        r = QRectF(1, 1, 48, 26)
        if self.on:
            g = QLinearGradient(r.left(), 0, r.right(), 0)
            g.setColorAt(0, QColor(A1))
            g.setColorAt(1, QColor(A2))
            p.setBrush(QBrush(g))
            p.setPen(Qt.PenStyle.NoPen)
        else:
            p.setBrush(QColor(RAISED))
            p.setPen(QPen(QColor(LINE), 1.5))
        p.drawRoundedRect(r, 13, 13)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ON if self.on else SUB))
        p.drawEllipse(QRectF(26 if self.on else 5, 5, 18, 18))


class Seg(QFrame):
    """Pill-shaped segmented control; also used as the top navigation."""
    changed = pyqtSignal(str)

    def __init__(self, items, key=None):
        super().__init__()
        self.setObjectName("nav")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 5)
        lay.setSpacing(4)
        self.btns, self.key = {}, None
        for k, label in items:
            b = QPushButton(label)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c, kk=k: self._click(kk))
            lay.addWidget(b)
            self.btns[k] = b
        self.set(key or items[0][0])

    def set(self, key):
        self.key = key
        for k, b in self.btns.items():
            b.setChecked(k == key)

    def _click(self, key):
        self.set(key)
        self.changed.emit(key)


class Stepper(QWidget):
    changed = pyqtSignal(int)

    def __init__(self, value, lo, hi, step=1, fmt=str):
        super().__init__()
        self.setObjectName("clear")
        self.lo, self.hi, self.step, self.fmt, self.value = lo, hi, step, fmt, int(value)
        self.value = max(lo, min(hi, self.value))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        minus, plus = QPushButton("\u2212"), QPushButton("+")
        for b, d in ((minus, -step), (plus, step)):
            b.setFixedWidth(42)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c, dd=d: self.set(self.value + dd))
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl.setMinimumWidth(96)
        lay.addWidget(minus)
        lay.addWidget(self.lbl)
        lay.addWidget(plus)
        self._show()

    def _show(self):
        self.lbl.setText(self.fmt(self.value))

    def set(self, v):
        v = max(self.lo, min(self.hi, int(v)))
        if v != self.value:
            self.value = v
            self._show()
            self.changed.emit(v)


class RowList(QWidget):
    """Editable list of names, each with its own on/off switch and a remove button.
    The 'add' box sits at the bottom (add_bottom=True)."""
    changed = pyqtSignal()

    def __init__(self, items, normalize, placeholder, hint, add_bottom=False):
        super().__init__()
        self.setObjectName("clear")
        self.items = [[str(n), bool(o)] for n, o in items]
        self.normalize, self.hint = normalize, hint
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.returnPressed.connect(self.add)
        btn = QPushButton("Add")
        btn.clicked.connect(self.add)
        row = QHBoxLayout()
        row.addWidget(self.edit, 1)
        row.addWidget(btn)
        self.msg = QLabel(hint)
        self.msg.setObjectName("sub")
        self.msg.setWordWrap(True)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.inner = QWidget()
        self.inner.setObjectName("clear")
        self.col = QVBoxLayout(self.inner)
        self.col.setContentsMargins(0, 0, 6, 0)
        self.col.setSpacing(8)
        self.scroll.setWidget(self.inner)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        if add_bottom:
            v.addWidget(self.scroll, 1)
            v.addWidget(self.msg)
            v.addLayout(row)
        else:
            v.addLayout(row)
            v.addWidget(self.msg)
            v.addWidget(self.scroll, 1)
        self.rebuild()

    def rebuild(self):
        while self.col.count():
            w = self.col.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not self.items:
            empty = QLabel("Nothing here yet. Add one below.")
            empty.setObjectName("sub")
            self.col.addWidget(empty)
        for idx, (name, on) in enumerate(self.items):
            fr = QFrame()
            fr.setObjectName("row")
            h = QHBoxLayout(fr)
            h.setContentsMargins(14, 8, 8, 8)
            sw = Switch(on)
            sw.toggled.connect(lambda val, i=idx: self._toggle(i, val))
            rm = QPushButton("\u2715")
            rm.setObjectName("ghost")
            rm.setFixedWidth(38)
            rm.clicked.connect(lambda _c, i=idx: self._remove(i))
            h.addWidget(QLabel(name), 1)
            h.addWidget(sw)
            h.addWidget(rm)
            self.col.addWidget(fr)
        self.col.addStretch(1)

    def add(self):
        n = self.normalize(self.edit.text())
        if not n:
            self.msg.setText("That doesn't look right. " + self.hint)
            return
        if n in [x[0] for x in self.items]:
            self.msg.setText(f"{n} is already in the list.")
            return
        self.items.append([n, True])
        self.edit.clear()
        self.msg.setText(self.hint)
        self.rebuild()
        self.changed.emit()

    def _toggle(self, i, val):
        self.items[i][1] = val
        self.changed.emit()

    def _remove(self, i):
        del self.items[i]
        self.rebuild()
        self.changed.emit()

    def set_items(self, items):
        self.items = [[str(n), bool(o)] for n, o in items]
        self.rebuild()
        self.changed.emit()

    def set_all(self, on):
        for it in self.items:
            it[1] = bool(on)
        self.rebuild()
        self.changed.emit()

    def checked(self):
        return [n for n, o in self.items if o]

    def counts(self):
        return len(self.checked()), len(self.items)


class Tile(QFrame):
    """One group in the blocklist. Click it to see its sites; the switch turns the whole group on/off."""
    toggled = pyqtSignal(str, bool)
    picked = pyqtSignal(str)

    def __init__(self, name, verb="blocked"):
        super().__init__()
        self.setObjectName("card")
        self.name, self.verb = name, verb
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(20, 14, 16, 14)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(name)
        t.setObjectName("h2")
        self.sub = QLabel("")
        self.sub.setObjectName("sub")
        col.addWidget(t)
        col.addWidget(self.sub)
        arrow = QLabel("\u203a")
        arrow.setObjectName("sub")
        arrow.setStyleSheet("font-size: 24px;")
        self.sw = Switch(False)
        self.sw.toggled.connect(lambda v: self.toggled.emit(name, v))
        h.addLayout(col, 1)
        h.addWidget(arrow)
        h.addSpacing(8)
        h.addWidget(self.sw)

    def set_counts(self, on, total):
        self.sub.setText("empty" if total == 0 else f"{on} of {total} {self.verb}")
        self.sw.set_on(on > 0)
        self.sw.setEnabled(total > 0)

    def set_selected(self, sel):
        self.setProperty("sel", "true" if sel else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, _e):
        self.picked.emit(self.name)


class ThemeCard(QWidget):
    clicked = pyqtSignal(str)

    def __init__(self, name):
        super().__init__()
        self.name, self.sel = name, False
        self.setFixedSize(150, 98)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, _e):
        self.clicked.emit(self.name)

    def paintEvent(self, _e):
        t = THEMES[self.name]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(2, 2, self.width() - 4, self.height() - 4)
        p.setBrush(QColor(t["BG"]))
        p.setPen(QPen(QColor(t["A1"] if self.sel else t["LINE"]), 3 if self.sel else 1.5))
        p.drawRoundedRect(r, 16, 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t["PANEL"]))
        p.drawRoundedRect(QRectF(12, 12, self.width() - 24, 36), 10, 10)
        g = QLinearGradient(12, 0, self.width() - 12, 0)
        g.setColorAt(0, QColor(t["A1"]))
        g.setColorAt(1, QColor(t["A2"]))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(20, 24, self.width() - 40, 12), 6, 6)
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(t["TEXT"]))
        p.drawText(QRectF(14, 56, self.width() - 28, 30),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.name)


# ------------------------------------------------------------------ the time ring
class TimeRing(QWidget):
    """Drag the knob (or scroll / use the arrow keys) to pick 1-180 minutes, one minute
    per step with a click. While a session runs it shows the time left in the phase.
    Deliberately NOT animated: it must follow the pointer exactly."""
    valueChanged = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.value = 50
        self.running = False
        self.frac, self.big, self.small = 0.0, "", ""
        self._drag = False
        self.setMinimumSize(300, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_value(self, v):
        v = max(1, min(MAXM, int(v)))
        if v != self.value:
            self.value = v
            self.update()
        self.valueChanged.emit(self.value)

    def set_running(self, running, frac=0.0, big="", small=""):
        self.running, self.frac, self.big, self.small = running, frac, big, small
        self.setCursor(Qt.CursorShape.ArrowCursor if running else Qt.CursorShape.PointingHandCursor)
        self.update()

    def _geo(self):
        return QPointF(self.width() / 2, self.height() / 2), min(self.width(), self.height()) / 2 - 34

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, r = self._geo()
        for i in range(60):                         # ticks: one per 3 min, major every 15
            a = math.radians(i * 6 - 90)
            major = i % 5 == 0
            r1, r2 = r + 22, r + 22 + (10 if major else 5)
            p.setPen(QPen(QColor(SUB if major else LINE), 2 if major else 1.4, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(c.x() + r1 * math.cos(a), c.y() + r1 * math.sin(a)),
                       QPointF(c.x() + r2 * math.cos(a), c.y() + r2 * math.sin(a)))
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        p.setPen(QPen(QColor(RAISED), 20, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawEllipse(rect)
        span = (self.frac if self.running else self.value / MAXM) * 360
        if span > 0.4:
            g = QConicalGradient(c, 90)
            g.setColorAt(0.0, QColor(A2))
            g.setColorAt(1.0, QColor(A1))
            p.setPen(QPen(QBrush(g), 20, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, 90 * 16, int(-span * 16))
            a = math.radians(span - 90)
            kx, ky = c.x() + r * math.cos(a), c.y() + r * math.sin(a)
            glow = QRadialGradient(QPointF(kx, ky), 30)
            glow.setColorAt(0, qc(A1, 140))
            glow.setColorAt(1, qc(A1, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(kx, ky), 30, 30)
            p.setBrush(QColor(TEXT))
            p.setPen(QPen(QColor(A1), 4))
            p.drawEllipse(QPointF(kx, ky), 11 if not self.running else 8, 11 if not self.running else 8)
        big = self.big if self.running else str(self.value)
        small = self.small if self.running else ("minutes" if self.value < 60 else fmt_min(self.value))
        f = QFont(self.font())
        f.setWeight(QFont.Weight.DemiBold)
        px = max(20, int(r * 0.62))
        f.setPixelSize(px)
        w = QFontMetricsF(f).horizontalAdvance(big)
        if w > r * 1.5:
            f.setPixelSize(max(16, int(px * r * 1.5 / w)))
        p.setFont(f)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(c.x() - r, c.y() - r * 0.55, 2 * r, r * 1.0), Qt.AlignmentFlag.AlignCenter, big)
        f2 = QFont(self.font())
        f2.setPixelSize(max(12, int(r * 0.13)))
        p.setFont(f2)
        p.setPen(QColor(SUB))
        p.drawText(QRectF(c.x() - r, c.y() + r * 0.36, 2 * r, r * 0.3), Qt.AlignmentFlag.AlignCenter, small)

    # -- interaction (picking) --
    def _set_from(self, pos):
        c, _ = self._geo()
        ang = math.degrees(math.atan2(pos.x() - c.x(), -(pos.y() - c.y()))) % 360
        m = int(round(ang / 360 * MAXM))
        if m <= 0:
            m = MAXM if self.value > MAXM / 2 else 1
        if m != self.value:
            self.value = m
            self.valueChanged.emit(m)
            sound("click")
            self.update()

    def _bump(self, d):
        v = max(1, min(MAXM, self.value + d))
        if v != self.value:
            self.value = v
            self.valueChanged.emit(v)
            sound("click")
            self.update()

    def mousePressEvent(self, e):
        if not self.running:
            self._drag = True
            self.setFocus()
            self._set_from(epos(e))

    def mouseMoveEvent(self, e):
        if self._drag and not self.running:
            self._set_from(epos(e))

    def mouseReleaseEvent(self, _e):
        self._drag = False

    def wheelEvent(self, e):
        if not self.running:
            step = 5 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            self._bump(step if e.angleDelta().y() > 0 else -step)

    def keyPressEvent(self, e):
        k = e.key()
        d = {Qt.Key.Key_Up: 1, Qt.Key.Key_Right: 1, Qt.Key.Key_Down: -1, Qt.Key.Key_Left: -1,
             Qt.Key.Key_PageUp: 5, Qt.Key.Key_PageDown: -5}.get(k)
        if d is not None and not self.running:
            self._bump(d)
        else:
            super().keyPressEvent(e)


# ------------------------------------------------------------------ insight charts
def _grad_v(top, bottom):
    g = QLinearGradient(0, top, 0, bottom)
    g.setColorAt(0, QColor(A1))
    g.setColorAt(1, QColor(A2))
    return QBrush(g)


class WeekBars(QWidget):
    """Last 7 days against the daily goal."""
    def __init__(self):
        super().__init__()
        self.data, self.goal, self._k = [], 120, 1.0
        self.setMinimumHeight(200)

    def set_data(self, data, goal):
        self.data, self.goal = data, goal
        self.update()

    def replay(self):
        animate_attr(self, "_k", 0.0, 1.0, 650)

    def paintEvent(self, _e):
        if not self.data:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right, top, bottom = 4, 4, 26, 30
        w, h = self.width() - left - right, self.height() - top - bottom
        base, slot = top + h, w / len(self.data)
        bw = min(36.0, slot * 0.5)
        vmax = max(self.goal * 1.15, max(m for _, m, _ in self.data) * 1.1, 1)
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        gy = base - self.goal / vmax * h
        p.setPen(QPen(QColor(LINE), 1.5, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(left, gy), QPointF(left + w, gy))
        for i, (lab, m, today) in enumerate(self.data):
            cx = left + slot * (i + 0.5)
            bh = m / vmax * h * self._k
            if m > 0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_grad_v(base - bh, base))
                p.drawRoundedRect(QRectF(cx - bw / 2, base - max(bh, 6), bw, max(bh, 6)), 9, 9)
                p.setPen(QColor(SUB))
                p.drawText(QRectF(cx - slot / 2, base - bh - 20, slot, 16), Qt.AlignmentFlag.AlignCenter,
                           fmt_min(m, True))
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(LINE))
                p.drawEllipse(QPointF(cx, base - 4), 4, 4)
            p.setPen(QColor(A1 if today else SUB))
            p.drawText(QRectF(cx - slot / 2, base + 8, slot, 18), Qt.AlignmentFlag.AlignCenter, lab)


class Heatmap(QWidget):
    """22 weeks of daily focus, one square per day (darker = closer to your goal)."""
    WEEKS = 22

    def __init__(self):
        super().__init__()
        self.days, self.goal, self._k = {}, 120, 1.0
        self.setMouseTracking(True)
        self.setMinimumHeight(200)

    def set_data(self, days, goal):
        self.days, self.goal = days, goal
        self.update()

    def replay(self):
        animate_attr(self, "_k", 0.0, 1.0, 700)

    def _geo(self):
        left, top = 34, 24
        cs = min((self.width() - left) / self.WEEKS, (self.height() - top - 4) / 7)
        today = date.today()
        start = today - timedelta(days=today.weekday()) - timedelta(weeks=self.WEEKS - 1)
        return left, top, cs, start, today

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top, cs, start, today = self._geo()
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        p.setOpacity(self._k)
        gap = max(2.0, cs * 0.16)
        for wk in range(self.WEEKS):
            d0 = start + timedelta(weeks=wk)
            if d0.day <= 7:
                p.setPen(QColor(SUB))
                p.drawText(QRectF(left + wk * cs, 2, cs * 3, 18),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, d0.strftime("%b"))
            for dy in range(7):
                d = d0 + timedelta(days=dy)
                if d > today:
                    continue
                m = minutes_on(self.days, d)
                col = QColor(RAISED) if m <= 0 else qc(A1, int(70 + 185 * min(1.0, m / max(1, self.goal))))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(left + wk * cs, top + dy * cs, cs - gap, cs - gap), 5, 5)
        p.setPen(QColor(SUB))
        for dy, lab in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
            p.drawText(QRectF(0, top + dy * cs, left - 4, cs - gap),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, lab)

    def mouseMoveEvent(self, e):
        left, top, cs, start, today = self._geo()
        pt = epos(e)
        wk, dy = int((pt.x() - left) // cs), int((pt.y() - top) // cs)
        if 0 <= wk < self.WEEKS and 0 <= dy < 7:
            d = start + timedelta(weeks=wk, days=dy)
            if d <= today:
                QToolTip.showText(egpos(e).toPoint(),
                                  f"{d.strftime('%a %d %b')}: {fmt_min(minutes_on(self.days, d))}", self)
                return
        QToolTip.hideText()


class HourBars(QWidget):
    """Which hours of the day you focus in."""
    def __init__(self):
        super().__init__()
        self.hours, self._k = [0.0] * 24, 1.0
        self.setMinimumHeight(180)

    def set_hours(self, hours):
        self.hours = list(hours)
        self.update()

    def replay(self):
        animate_attr(self, "_k", 0.0, 1.0, 650)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right, top, bottom = 4, 4, 8, 28
        w, h = self.width() - left - right, self.height() - top - bottom
        base, slot = top + h, w / 24
        bw = max(5.0, slot * 0.55)
        vmax = max(30.0, max(self.hours))
        for hr in range(24):
            cx = left + slot * (hr + 0.5)
            v = self.hours[hr]
            bh = max(bw, v / vmax * h * self._k) if v > 0 else bw * 0.6
            p.setPen(Qt.PenStyle.NoPen)
            if v > 0:
                p.setBrush(_grad_v(base - bh, base))
            else:
                p.setBrush(QColor(RAISED))
            p.drawRoundedRect(QRectF(cx - bw / 2, base - bh, bw, bh), bw / 2, bw / 2)
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(SUB))
        for hr, lab in ((0, "12 am"), (6, "6 am"), (12, "12 pm"), (18, "6 pm")):
            cx = left + slot * (hr + 0.5)
            p.drawText(QRectF(cx - 30, base + 8, 60, 18), Qt.AlignmentFlag.AlignCenter, lab)


class LabelBars(QWidget):
    """Top labels by time."""
    def __init__(self):
        super().__init__()
        self.rows, self._k = [], 1.0
        self.setMinimumHeight(180)

    def set_data(self, rows):
        self.rows = rows
        self.update()

    def replay(self):
        animate_attr(self, "_k", 0.0, 1.0, 650)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.rows:
            p.setPen(QColor(SUB))
            p.drawText(QRectF(0, 0, self.width(), self.height()), Qt.AlignmentFlag.AlignCenter,
                       "No labelled sessions yet.\nType what you're working on before you start.")
            return
        rh = min(44.0, self.height() / max(1, len(self.rows)))
        vmax = max(m for _, m in self.rows)
        for i, (lab, m) in enumerate(self.rows):
            y = i * rh
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(0, y, self.width() * 0.6, 20), Qt.AlignmentFlag.AlignVCenter |
                       Qt.AlignmentFlag.AlignLeft, lab if len(lab) < 28 else lab[:27] + "\u2026")
            p.setPen(QColor(SUB))
            p.drawText(QRectF(0, y, self.width(), 20), Qt.AlignmentFlag.AlignVCenter |
                       Qt.AlignmentFlag.AlignRight, fmt_min(m))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(RAISED))
            p.drawRoundedRect(QRectF(0, y + 24, self.width(), 9), 4.5, 4.5)
            g = QLinearGradient(0, 0, self.width(), 0)
            g.setColorAt(0, QColor(A1))
            g.setColorAt(1, QColor(A2))
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(0, y + 24, max(9.0, self.width() * m / vmax * self._k), 9), 4.5, 4.5)


def make_card(title=None, margins=(24, 20, 24, 20)):
    fr = QFrame()
    fr.setObjectName("card")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(*margins)
    lay.setSpacing(12)
    if title:
        t = QLabel(title)
        t.setObjectName("h2")
        lay.addWidget(t)
    return fr, lay


class InsightsPage(QWidget):
    def __init__(self, goal):
        super().__init__()
        self.setObjectName("clear")
        self.goal, self.days = goal, {}
        self.week, self.heat = WeekBars(), Heatmap()
        self.hours, self.labels = HourBars(), LabelBars()

        def kpi(title):
            fr, lay = make_card(None, (24, 18, 24, 18))
            lay.setSpacing(4)
            a = QLabel(title)
            a.setObjectName("sub")
            v = QLabel("")
            v.setStyleSheet("font-size: 30px; font-weight: 700;")
            s = QLabel("")
            s.setObjectName("sub")
            for w in (a, v, s):
                lay.addWidget(w)
            return fr, v, s

        k1, self.k1v, self.k1s = kpi("Today")
        k2, self.k2v, self.k2s = kpi("Last 7 days")
        k3, self.k3v, self.k3s = kpi("Current streak")
        row1 = QHBoxLayout()
        row1.setSpacing(18)
        for k in (k1, k2, k3):
            row1.addWidget(k, 1)
        c_heat, hl = make_card("Consistency")
        hl.addWidget(self.heat, 1)
        c_week, wl = make_card("Last 7 days")
        wl.addWidget(self.week, 1)
        row2 = QHBoxLayout()
        row2.setSpacing(18)
        row2.addWidget(c_heat, 3)
        row2.addWidget(c_week, 2)
        c_hr, rl = make_card("When you focus  \u00b7  last 30 days")
        rl.addWidget(self.hours, 1)
        c_lb, ll = make_card("Top labels  \u00b7  last 30 days")
        ll.addWidget(self.labels, 1)
        row3 = QHBoxLayout()
        row3.setSpacing(18)
        row3.addWidget(c_hr, 1)
        row3.addWidget(c_lb, 1)

        inner = QWidget()
        inner.setObjectName("clear")
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(18)
        title = QLabel("Insights")
        title.setObjectName("h1")
        col.addWidget(title)
        col.addLayout(row1)
        col.addLayout(row2)
        col.addLayout(row3)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

    def load(self, days):
        self.days = days
        self.render()

    def set_goal(self, goal):
        self.goal = goal
        self.render()

    def render(self):
        today, days, goal = date.today(), self.days, self.goal
        week = [today - timedelta(days=6 - i) for i in range(7)]
        vals = [minutes_on(days, d) for d in week]
        total, t = sum(vals), minutes_on(days, today)
        self.k1v.setText(fmt_min(t))
        self.k1s.setText(f"{min(100, round(100 * t / max(1, goal)))}% of your {fmt_min(goal)} goal")
        self.k2v.setText(fmt_min(total))
        self.k2s.setText(f"average {fmt_min(total / 7)} a day")
        n = streak_of(days, today)
        self.k3v.setText(f"{n} day" + ("" if n == 1 else "s"))
        self.k3s.setText("any focus counts" if n else "focus today to start one")
        self.week.set_data([(d.strftime("%a"), m, d == today) for d, m in zip(week, vals)], goal)
        self.heat.set_data(days, goal)
        hours, labels = [0.0] * 24, {}
        for k in range(30):
            rec = days.get((today - timedelta(days=k)).isoformat())
            if rec:
                hours = [a + b for a, b in zip(hours, rec["hours"])]
                for lab, m in rec["labels"].items():
                    labels[lab] = labels.get(lab, 0) + m
        self.hours.set_hours(hours)
        self.labels.set_data(sorted(labels.items(), key=lambda kv: -kv[1])[:5])

    def play_intro(self):
        for w in (self.week, self.heat, self.hours, self.labels):
            w.replay()


# ------------------------------------------------------------------ pages
class FocusPage(QWidget):
    plan_changed = pyqtSignal()
    start_clicked = pyqtSignal()
    end_clicked = pyqtSignal()

    def __init__(self, conf):
        super().__init__()
        self.setObjectName("clear")
        self._running = False
        self.ring = TimeRing()
        self.ring.valueChanged.connect(lambda _v: self._changed())
        self.label = QLineEdit()
        self.label.setPlaceholderText("What are you focusing on?  (optional)")
        self.label.setMaxLength(60)
        self.label.setText(str(conf.get("label", "")))
        self.label.editingFinished.connect(self.plan_changed.emit)
        self.mode = Seg([("single", "Single"), ("pomo", "Pomodoro")], conf.get("mode", "single"))
        self.mode.changed.connect(lambda _k: self._changed())
        self.presets = []
        for m in (15, 25, 45, 60, 90):
            b = QPushButton(str(m))
            b.setObjectName("pill")
            b.setToolTip(f"{m} minutes")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c, mm=m: self.ring.set_value(mm))
            self.presets.append(b)
        self.brk = Stepper(conf.get("brk", 5), 1, 60, 1, lambda v: f"{v} min")
        self.rounds = Stepper(conf.get("rounds", 4), 2, 12, 1, lambda v: f"{v} rounds")
        self.brk.changed.connect(lambda _v: self._changed())
        self.rounds.changed.connect(lambda _v: self._changed())
        self.pomo_box = QWidget()
        self.pomo_box.setObjectName("clear")
        ph = QHBoxLayout(self.pomo_box)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.setSpacing(12)
        for text, w in (("Break", self.brk), ("Rounds", self.rounds)):
            lab = QLabel(text)
            lab.setObjectName("sub")
            ph.addWidget(lab)
            ph.addWidget(w)
            ph.addSpacing(18)
        ph.addStretch(1)
        self.summary = QLabel("")
        self.summary.setObjectName("sub")
        self.summary.setWordWrap(True)
        self.start_btn = QPushButton("Start focusing")
        self.start_btn.setObjectName("primary")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self.start_clicked.emit)
        self.end_btn = QPushButton("End early\u2026")
        self.end_btn.setObjectName("ghost")
        self.end_btn.setVisible(False)
        self.end_btn.clicked.connect(self.end_clicked.emit)
        self._lockables = [self.label, self.mode, self.pomo_box] + self.presets

        dock, dl = make_card(None, (26, 22, 26, 22))
        dl.addWidget(self.label)
        r1 = QHBoxLayout()
        r1.addWidget(self.mode)
        r1.addStretch(1)
        for b in self.presets:
            r1.addWidget(b)
        dl.addLayout(r1)
        dl.addWidget(self.pomo_box)
        r2 = QHBoxLayout()
        r2.setSpacing(12)
        r2.addWidget(self.summary, 1)
        r2.addWidget(self.end_btn)
        r2.addWidget(self.start_btn)
        dl.addLayout(r2)
        dock.setMaximumWidth(880)
        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addWidget(dock, 8)
        wrap.addStretch(1)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        root.addWidget(self.ring, 1)
        root.addLayout(wrap)
        self.ring.set_value(int(conf.get("minutes", 50)))
        self.set_recent(conf.get("recent", []))

    def plan(self):
        f = self.ring.value
        if self.mode.key != "pomo":
            return [("focus", f)]
        out = []
        for i in range(self.rounds.value):
            out.append(("focus", f))
            if i < self.rounds.value - 1:
                out.append(("break", self.brk.value))
        return out

    def summary_text(self):
        plan = self.plan()
        if len(plan) == 1:
            return f"One {fmt_min(plan[0][1])} focus session"
        total = sum(m for _, m in plan)
        return (f"{self.rounds.value} \u00d7 {self.ring.value} min focus with {self.brk.value} min breaks"
                f"  \u00b7  {fmt_min(total)} in all")

    def _changed(self):
        self.pomo_box.setVisible(self.mode.key == "pomo")
        if not self._running:
            self.summary.setText(self.summary_text())
        self.plan_changed.emit()

    def set_recent(self, labels):
        c = QCompleter([str(x) for x in labels], self)
        c.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.label.setCompleter(c)

    def show_running(self, frac, big, small, hint, until, can_end):
        self._running = True
        self.ring.set_running(True, frac, big, small)
        for w in self._lockables:
            w.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.start_btn.setText(f"Locked until {until}")
        self.summary.setText(hint)
        self.end_btn.setVisible(can_end)

    def show_idle(self):
        if not self._running:
            return
        self._running = False
        self.ring.set_running(False)
        for w in self._lockables:
            w.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Start focusing")
        self.end_btn.setVisible(False)
        self.summary.setText(self.summary_text())


class BlocklistPage(QWidget):
    """Groups on the left; click one and every site in it appears on the right, each with its
    own switch, so you can keep (say) WhatsApp or YouTube available while you focus."""
    changed = pyqtSignal()

    def __init__(self, conf):
        super().__init__()
        self.setObjectName("clear")
        self.groups = load_groups(conf)
        self.lists, self.tiles, self.current = {}, {}, None
        self.stack = QStackedWidget()
        grid = QGridLayout()
        grid.setSpacing(14)
        for i, (name, items) in enumerate(self.groups.items()):
            is_app = name == "Apps"
            if is_app:
                rl = RowList(items, norm_app, "Add an app, e.g. discord",
                             "Use the process name (max 15 characters). Try: " + APP_SUGGESTIONS, True)
            else:
                rl = RowList(items, norm_domain, f"Add a site to {name}, e.g. example.com",
                             "Just the domain, like news.example.com", True)
            rl.changed.connect(self._edited)
            self.lists[name] = rl
            self.stack.addWidget(rl)
            t = Tile(name, "closed" if is_app else "blocked")
            t.picked.connect(self.select)
            t.toggled.connect(self._group_switch)
            self.tiles[name] = t
            grid.addWidget(t, i // 2, i % 2)

        self.count = QLabel("")
        self.count.setObjectName("chip")
        title = QLabel("Blocklist")
        title.setObjectName("h1")
        sub = QLabel("Pick a group, then switch off the sites you still need while you focus. "
                     "Breaks open everything again.")
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.count)
        left = QVBoxLayout()
        left.addLayout(grid)
        left.addStretch(1)

        right, rl_ = make_card(None)
        hh = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        self.d_title = QLabel("")
        self.d_title.setObjectName("h2")
        self.d_sub = QLabel("")
        self.d_sub.setObjectName("sub")
        col.addWidget(self.d_title)
        col.addWidget(self.d_sub)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setObjectName("ghost")
        self.reset_btn.setToolTip("Restore this group's default sites")
        self.reset_btn.clicked.connect(self.reset)
        hh.addLayout(col, 1)
        hh.addWidget(self.reset_btn)
        rl_.addLayout(hh)
        rl_.addWidget(self.stack, 1)

        body = QHBoxLayout()
        body.setSpacing(22)
        body.addLayout(left, 5)
        body.addWidget(right, 4)
        self.body_w = QWidget()
        self.body_w.setObjectName("clear")
        self.body_w.setLayout(body)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addLayout(head)
        root.addWidget(sub)
        root.addWidget(self.body_w, 1)
        self._locked = False
        self._edited(emit=False)
        self.select(next(iter(self.groups)))

    def select(self, name):
        self.current = name
        for n, t in self.tiles.items():
            t.set_selected(n == name)
        self.stack.setCurrentWidget(self.lists[name])
        self.d_title.setText(name)
        self.d_sub.setText({"Apps": "Closed automatically while you focus.",
                            "My sites": "Sites you added yourself."}.get(
                                name, "Switch a site off to keep it available while you focus."))
        self.reset_btn.setVisible(name in CATEGORIES)

    def reset(self):
        if self.current in CATEGORIES:
            self.lists[self.current].set_items(default_group(self.current))

    def _group_switch(self, name, on):
        self.lists[name].set_all(on)

    def domains(self):
        out = set()
        for n, rl in self.lists.items():
            if n != "Apps":
                out.update(rl.checked())
        return sorted(out)

    def app_names(self):
        return self.lists["Apps"].checked()

    def groups_data(self):
        return {n: rl.items for n, rl in self.lists.items()}

    def _edited(self, emit=True):
        for n, t in self.tiles.items():
            t.set_counts(*self.lists[n].counts())
        self._update_count()
        if emit:
            self.changed.emit()

    def _update_count(self):
        text = f"{len(self.domains())} sites  \u00b7  {len(self.app_names())} apps"
        self.count.setText(text + ("  \u00b7  locked" if self._locked else ""))

    def set_locked(self, locked):
        if locked != self._locked:
            self._locked = locked
            self.body_w.setEnabled(not locked)
            self._update_count()


def setting_row(title, sub, widget):
    row = QHBoxLayout()
    col = QVBoxLayout()
    col.setSpacing(2)
    t = QLabel(title)
    s = QLabel(sub)
    s.setObjectName("sub")
    s.setWordWrap(True)
    col.addWidget(t)
    col.addWidget(s)
    row.addLayout(col, 1)
    row.addSpacing(16)
    row.addWidget(widget)
    return row


class SettingsPage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.setObjectName("clear")
        self.main, self.cards = main, {}
        title = QLabel("Settings")
        title.setObjectName("h1")

        theme, tv = make_card(None, (26, 22, 26, 22))
        th = QHBoxLayout()
        t = QLabel("Theme")
        t.setObjectName("h2")
        self.mode = Seg([("dark", "Dark"), ("light", "Light")], THEMES[THEME_NAME]["MODE"])
        self.mode.changed.connect(self.pick_mode)
        th.addWidget(t)
        th.addStretch(1)
        th.addWidget(self.mode)
        self.grid = QGridLayout()
        self.grid.setSpacing(14)
        tv.addLayout(th)
        tv.addLayout(self.grid)
        tv.addStretch(1)

        goal, gl = make_card("Daily goal", (26, 22, 26, 22))
        self.goal = Stepper(main.goal, 15, 720, 15, fmt_min)
        self.goal.changed.connect(main.set_goal)
        gl.addLayout(setting_row("Focus time you aim for each day",
                                 "Drives the bars and progress in Insights.", self.goal))

        beh, bl = make_card("Behaviour", (26, 22, 26, 22))
        self.sw_sound, self.sw_notify, self.sw_early = (Switch(main.sound_on), Switch(main.notify_on),
                                                        Switch(main.early_end))
        self.sw_sound.toggled.connect(main.set_sound)
        self.sw_notify.toggled.connect(main.set_notify)
        self.sw_early.toggled.connect(main.set_early_end)
        bl.addLayout(setting_row("Click sound on the ring", "One click per minute while you set the time.",
                                 self.sw_sound))
        bl.addLayout(setting_row("Notifications and chime", "Tells you when a focus phase or break ends.",
                                 self.sw_notify))
        bl.addLayout(setting_row("Allow ending a session early",
                                 "Needs Windows admin permission (UAC) and a 30 second wait. Off = no way out.",
                                 self.sw_early))

        about, al = make_card("About", (26, 22, 26, 22))
        info = QLabel(f"M Focus {VERSION}\nBlocking runs in a small background service, so it keeps working "
                      "if this window is closed. Close the window during a session and M Focus stays in "
                      "the tray.")
        info.setObjectName("sub")
        info.setWordWrap(True)
        al.addWidget(info)

        right = QVBoxLayout()
        right.setSpacing(18)
        for c in (goal, beh, about):
            right.addWidget(c)
        right.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(18)
        row.addWidget(theme, 5)
        row.addLayout(right, 4)

        inner = QWidget()
        inner.setObjectName("clear")
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 8, 0)
        col.setSpacing(18)
        col.addWidget(title)
        col.addLayout(row)
        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)
        self.sync()

    def _build_gallery(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.cards = {}
        mode = THEMES[THEME_NAME]["MODE"]
        for i, name in enumerate(n for n, t in THEMES.items() if t["MODE"] == mode):
            c = ThemeCard(name)
            c.clicked.connect(self.main.set_theme)
            self.cards[name] = c
            self.grid.addWidget(c, i // 3, i % 3)

    def sync(self):
        mode = THEMES[THEME_NAME]["MODE"]
        self.mode.set(mode)
        if not self.cards or any(THEMES[n]["MODE"] != mode for n in self.cards):
            self._build_gallery()
        for n, c in self.cards.items():
            c.sel = n == THEME_NAME
            c.update()

    def pick_mode(self, mode):
        if THEMES[THEME_NAME]["MODE"] != mode:
            first = next(n for n, t in THEMES.items() if t["MODE"] == mode)
            self.main.set_theme(self.main.last_theme.get(mode, first))


class EndEarlyDialog(QDialog):
    WAIT = 30

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("End session early?")
        self.setMinimumWidth(440)
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 26, 28, 24)
        v.setSpacing(14)
        t = QLabel("End this session early?")
        t.setObjectName("h2")
        s = QLabel("The focus time you already did is saved. Windows will ask for permission (UAC).")
        s.setObjectName("sub")
        s.setWordWrap(True)
        keep = QPushButton("Keep going")
        keep.setObjectName("primary")
        keep.clicked.connect(self.reject)
        self.end = QPushButton()
        self.end.setEnabled(False)
        self.end.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(self.end)
        row.addStretch(1)
        row.addWidget(keep)
        v.addWidget(t)
        v.addWidget(s)
        v.addLayout(row)
        self.left = self.WAIT
        self._show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

    def _show(self):
        self.end.setText("End session" if self.left <= 0 else f"End session ({self.left})")

    def _tick(self):
        self.left -= 1
        self._show()
        if self.left <= 0:
            self.timer.stop()
            self.end.setEnabled(True)


def app_icon():
    for p in (APP_DIR / "mfocus.png",):
        if p.exists():
            return QIcon(str(p))
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 64, 64)
    g.setColorAt(0, QColor(A1))
    g.setColorAt(1, QColor(A2))
    pt.setBrush(QBrush(g))
    pt.setPen(Qt.PenStyle.NoPen)
    pt.drawEllipse(4, 4, 56, 56)
    pt.end()
    return QIcon(pm)


# ------------------------------------------------------------------ main window
class Main(QWidget):
    PAGES = [("focus", "Focus"), ("block", "Blocklist"), ("insights", "Insights"), ("settings", "Settings")]

    def __init__(self, conf):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("M Focus")
        # keep the window inside the screen: on small / scaled laptop screens a fixed 980x660
        # minimum is taller than the usable area and pushes the title bar and taskbar away
        scr = QApplication.primaryScreen()
        av = scr.availableGeometry() if scr else None
        mw, mh = (min(980, av.width() - 24), min(660, av.height() - 48)) if av else (980, 660)
        self.setMinimumSize(max(mw, 640), max(mh, 480))
        self.resize(max(self.minimumWidth(), min(1120, av.width() - 24) if av else 1120),
                    max(self.minimumHeight(), min(740, av.height() - 48) if av else 740))
        if av:
            self.move(av.x() + max(0, (av.width() - self.width()) // 2),
                      av.y() + max(0, (av.height() - self.height()) // 2))
        self.icon = app_icon()
        self.setWindowIcon(self.icon)
        self.conf = conf
        self.last_theme = {m: n for m, n in (conf.get("last", {}) if isinstance(conf.get("last"), dict)
                                            else {}).items() if n in THEMES and THEMES[n]["MODE"] == m}
        self.last_theme[THEMES[THEME_NAME]["MODE"]] = THEME_NAME
        try:
            self.goal = max(15, min(720, int(conf.get("goal", 120))))
        except (TypeError, ValueError):
            self.goal = 120
        self.sound_on = bool(conf.get("sound", True))
        self.notify_on = bool(conf.get("notify", True))
        self.early_end = bool(conf.get("early_end", False))
        self.page, self._key, self._quitting, self._hinted = "focus", None, False, False
        self.hist, self._seen = HistoryCache(), -1

        self.focus = FocusPage(conf)
        self.block = BlocklistPage(conf)
        self.insights = InsightsPage(self.goal)
        self.settings = SettingsPage(self)
        self.stack = QStackedWidget()
        for w in (self.focus, self.block, self.insights, self.settings):
            self.stack.addWidget(w)

        brand = QLabel("M Focus")
        brand.setObjectName("brand")
        self.nav = Seg(self.PAGES, "focus")
        self.nav.changed.connect(self.set_page)
        self.chip = QLabel("")
        self.chip.setObjectName("chip")
        top = QGridLayout()
        top.setColumnStretch(0, 1)
        top.setColumnStretch(2, 1)
        top.addWidget(brand, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.nav, 0, 1)
        top.addWidget(self.chip, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 20, 30, 22)
        root.setSpacing(18)
        root.addLayout(top)
        root.addWidget(self.stack, 1)

        self.focus.plan_changed.connect(self.schedule_save)
        self.block.changed.connect(self.schedule_save)
        self.focus.start_clicked.connect(self.start_session)
        self.focus.end_clicked.connect(self.end_early)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.save_conf)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.icon, self)
            self.tray_menu = QMenu()
            self.tray_menu.addAction("Show M Focus").triggered.connect(self.show_normal)
            self.tray_menu.addAction("Quit").triggered.connect(self.quit_app)
            self.tray.setContextMenu(self.tray_menu)
            self.tray.activated.connect(self._tray_click)
            self.tray.show()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # -- settings / persistence --
    def schedule_save(self):
        self._save_timer.start()

    def save_conf(self):
        f = self.focus
        label = f.label.text().strip()
        recent = [r for r in self.conf.get("recent", []) if r != label]
        if label:
            recent.insert(0, label)
        self.conf = {
            "theme": THEME_NAME, "last": self.last_theme, "goal": self.goal, "sound": self.sound_on,
            "notify": self.notify_on, "early_end": self.early_end, "minutes": f.ring.value,
            "mode": f.mode.key, "brk": f.brk.value, "rounds": f.rounds.value, "label": label,
            "recent": recent[:8], "groups": self.block.groups_data(),
        }
        try:
            write_json(CONF, self.conf)
        except OSError as e:
            print("could not save settings:", e, file=sys.stderr)

    def set_goal(self, g):
        self.goal = max(15, min(720, int(g)))
        self.insights.set_goal(self.goal)
        self.save_conf()

    def set_sound(self, on):
        self.sound_on = bool(on)
        if SFX:
            SFX.enabled = self.sound_on
        self.save_conf()

    def set_notify(self, on):
        self.notify_on = bool(on)
        self.save_conf()

    def set_early_end(self, on):
        self.early_end = bool(on)
        self.save_conf()

    def set_theme(self, name):
        if name not in THEMES:
            return
        old = QLabel(self)                       # cross-fade: freeze the old look, switch, fade it out
        old.setPixmap(self.grab())
        old.setGeometry(self.rect())
        old.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        old.show()
        old.raise_()
        eff = QGraphicsOpacityEffect(old)
        old.setGraphicsEffect(eff)
        fade = QPropertyAnimation(eff, b"opacity", old)
        fade.setDuration(320)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        fade.finished.connect(old.deleteLater)
        self._theme_fade = fade
        apply_theme(name)
        self.last_theme[THEMES[THEME_NAME]["MODE"]] = THEME_NAME
        QApplication.instance().setStyleSheet(qss())
        self.insights.render()
        self.settings.sync()
        self.update()
        fade.start()
        self.save_conf()

    def set_page(self, key):
        if key == self.page:
            return
        self.page = key
        self.nav.set(key)
        self.stack.setCurrentIndex([k for k, _ in self.PAGES].index(key))
        self._page_anim = fade_in(self.stack.currentWidget(), 260)
        if key == "insights":
            self.insights.play_intro()

    # -- the session --
    def start_session(self):
        domains, apps = self.block.domains(), self.block.app_names()
        if not domains and not apps:
            QMessageBox.information(self, "Nothing to block",
                                    "Turn on a category (or add a site or app) in the Blocklist first.")
            return
        phases = self.focus.plan()
        label = self.focus.label.text().strip()[:60]
        end_note = ("You can end it early only with Windows admin permission after a 30 second wait."
                    if self.early_end else "It cannot be ended early.")
        ans = QMessageBox.question(
            self, "Start focus session?",
            f"{self.focus.summary_text()}\n\nBlocking {len(domains)} sites and {len(apps)} apps.\n{end_note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        req = {"label": label, "phases": [[k, m] for k, m in phases], "domains": domains, "apps": apps}
        QMessageBox.information(self, "Windows permission",
                                "Windows will now ask for permission (UAC). Click Yes to start.")
        try:
            code = mf_core.run_elevated_and_wait(["start", mf_core.encode_request(req)],
                                                 pump=QApplication.processEvents)
        except RuntimeError as e:
            QMessageBox.warning(self, "Could not start", str(e))
            return
        if code is None:
            QMessageBox.warning(self, "Could not start", "The request was cancelled.")
        elif code != 0:
            QMessageBox.warning(self, "Could not start",
                                mf_core.read_ctl_error() or "Failed (code %s)." % code)
        self.save_conf()
        self.focus.set_recent(self.conf.get("recent", []))
        self.refresh()

    def end_early(self):
        if EndEarlyDialog(self).exec() != QDialog.DialogCode.Accepted:
            return
        code = mf_core.run_elevated_and_wait(["stop"], pump=QApplication.processEvents)
        if code is None:
            QMessageBox.warning(self, "Not ended", "Cancelled.")
        elif code != 0:
            QMessageBox.warning(self, "Not ended", mf_core.read_ctl_error() or "Failed (code %s)." % code)
        self.refresh()

    def notify(self, title, body):
        if not self.notify_on:
            return
        sound("chime")
        if self.tray:
            self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 6000)

    def refresh(self):
        now = time.time()
        st = read_json(STATE, None)
        active = isinstance(st, dict) and st.get("end", 0) > now and bool(st.get("phases"))
        key, tip = None, "M Focus"
        if active:
            phases = st["phases"]
            idx = next((i for i, p in enumerate(phases) if p["start"] <= now < p["end"]), len(phases) - 1)
            ph = phases[idx]
            foc = [i for i, p in enumerate(phases) if p["kind"] == "focus"]
            rem, length = max(0, ph["end"] - now), max(1, ph["end"] - ph["start"])
            until = time.strftime("%H:%M", time.localtime(st["end"]))
            if ph["kind"] == "focus":
                small = f"focus {foc.index(idx) + 1} of {len(foc)}" if len(foc) > 1 else "remaining"
                hint = (f"{len(st.get('domains', []))} sites and {len(st.get('apps', []))} apps are blocked."
                        f"  {st.get('label') or 'Stay with it.'}")
            else:
                small = "break"
                hint = "Break time: everything is open until " + time.strftime("%H:%M", time.localtime(ph["end"]))
            self.focus.show_running(rem / length, fmt_clock(rem), small, hint, until, self.early_end)
            key = (st.get("start"), idx)
            tip = f"M Focus \u2014 {fmt_clock(rem)} ({small})"
        else:
            self.focus.show_idle()
        self.block.set_locked(active)
        if self._key is not None and key != self._key:
            if key is None:
                self.notify("Session complete", "Nice work. Your focus time has been saved.")
            else:
                kind = st["phases"][key[1]]["kind"]
                if kind == "break":
                    self.notify("Break time", "Everything is open until your next round.")
                else:
                    self.notify("Back to focus", "Distractions are blocked again.")
        self._key = key
        if self.tray:
            self.tray.setToolTip(tip)
        days = self.hist.get()
        if self.hist.version != self._seen:
            self._seen = self.hist.version
            self.insights.load(days)
        today = date.today()
        self.chip.setText(f"Today {fmt_min(minutes_on(days, today))} / {fmt_min(self.goal)}"
                          f"   \u00b7   Streak {streak_of(days, today)}")

    # -- window / tray --
    def show_normal(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_click(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_normal()

    def quit_app(self):
        self._quitting = True
        self.save_conf()
        QApplication.quit()

    def closeEvent(self, ev):
        if self.tray and self._key is not None and not self._quitting:
            ev.ignore()                           # session running: keep living in the tray
            self.hide()
            if not self._hinted:
                self._hinted = True
                self.tray.showMessage("M Focus is still running",
                                      "Your session continues. Click the tray icon to come back.",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        self.save_conf()
        ev.accept()
        QApplication.quit()


def load_fonts():
    """Ship Inter with the app so Windows looks exactly like the Linux version."""
    fdir = APP_DIR / "fonts"
    if fdir.is_dir():
        for f in sorted(fdir.glob("Inter-*.otf")):
            QFontDatabase.addApplicationFont(str(f))


def main(selftest_out=None):
    global SFX
    try:   # tell Windows this is its own app (taskbar icon / grouping)
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MFocus.App")
    except Exception:
        pass
    try:   # sharp rendering on scaled screens (needed on Qt5; Qt6 does it by itself)
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    except AttributeError:
        pass
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except AttributeError:
        pass
    conf = read_json(CONF, {})
    if not isinstance(conf, dict):
        conf = {}
    app = QApplication(sys.argv)
    app.setApplicationName("M Focus")
    app.setQuitOnLastWindowClosed(False)
    server = None
    if selftest_out is None:                       # a second launch just raises the first window
        sock = QLocalSocket()
        sock.connectToServer("mfocus-ipc")
        if sock.waitForConnected(250):
            sock.write(b"show")
            sock.waitForBytesWritten(300)
            return 0
        QLocalServer.removeServer("mfocus-ipc")
        server = QLocalServer()
        server.listen("mfocus-ipc")
    load_fonts()
    font = app.font()
    font.setFamilies(["Inter", "Noto Sans", "Ubuntu", "DejaVu Sans", "Segoe UI"])
    font.setPointSize(10)
    app.setFont(font)
    apply_theme(conf.get("theme", DEFAULT_THEME))
    app.setStyleSheet(qss())
    SFX = Sfx()
    SFX.enabled = bool(conf.get("sound", True))
    w = Main(conf)
    if server is not None:
        server.newConnection.connect(lambda: (server.nextPendingConnection(), w.show_normal()))
    w.show()
    if selftest_out is not None:
        QTimer.singleShot(1200, app.quit)
    code = app.exec() if hasattr(app, "exec") else app.exec_()
    if selftest_out is not None:
        try:
            fams = QFontDatabase.families()
        except TypeError:          # PyQt5
            fams = QFontDatabase().families()
        Path(selftest_out).write_text("ui ok\ninter loaded: %s" % ("Inter" in fams), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
