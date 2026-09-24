"""zenobia's oya colour tokens (stack/theme/<THEME>.json): loading and the Qt stylesheet text.

Qt-free: the builder turns the tokens into a QPalette itself.
"""

from __future__ import annotations

import json
import re
import string
from pathlib import Path
from typing import Iterable, Mapping

# amazing.json's colours, used when the theme file is missing or incomplete.
BUILTIN_COLORS = {
    "primary-bg-light": "#f2f3f5", "header-bg-light": "#1d2333", "appbar-bg-light": "#252c3e",
    "appbar-text-light": "#e4e7ef", "bubble-bg-light": "#e8e9ec", "footer-bg-light": "#1d2333",
    "footer-text-light": "#e4e7ef", "text-main-light": "#0f1114", "accent-light": "#2f3d63",
    "warn-light": "#d94a4a", "success-light": "#4a8f7a", "sunken-light": "#e1e2e5",
    "link-light": "#2a4b8f", "primary-bg-dark": "#0a0c11", "header-bg-dark": "#0f1420",
    "appbar-bg-dark": "#151b29", "appbar-text-dark": "#cfd4df", "bubble-bg-dark": "#191f2d",
    "footer-bg-dark": "#0f1420", "footer-text-dark": "#cfd4df", "text-main-dark": "#d3d7e0",
    "accent-dark": "#4f5fa1", "warn-dark": "#ff4455", "caution-light": "#a07800",
    "caution-dark": "#f0a820", "success-dark": "#5fa38c", "sunken-dark": "#141821",
    "link-dark": "#5f7fc9", "accent-1": "#4f5fa1", "accent-2": "#2f3d63",
}

TOKEN_NAMES = (
    "window_bg", "card_bg", "card_border", "input_bg", "text", "muted", "accent", "on_accent",
    "header_bg", "header_text", "success", "caution", "warn", "log_bg", "link",
    # derived
    "accent_text", "accent_hover", "input_border", "divider", "button_hover", "disabled_text",
    "disabled_bg", "success_bg", "caution_bg", "warn_bg", "accent_bg", "header_muted",
    "header_ok", "header_warn", "header_caution", "scroll_handle",
)

_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}")
_THEME_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def mix(a: str, b: str, amount: float) -> str:
    """`a` moved `amount` (0..1) of the way towards `b`."""
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * amount) for x, y in zip(_rgb(a), _rgb(b)))


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio."""
    def luminance(color: str) -> float:
        channels = [c / 255 for c in _rgb(color)]
        r, g, b_ = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# The colours a theme file must define: the same list as stack/stats/theme.py (caution is optional).
_THEME_MODE_KEYS = ("primary-bg", "bubble-bg", "sunken", "header-bg", "appbar-bg", "appbar-text",
                    "footer-bg", "footer-text", "text-main", "accent", "link", "warn", "success")
REQUIRED_COLORS = tuple(f"{key}-{mode}" for mode in ("light", "dark") for key in _THEME_MODE_KEYS) + ("accent-1", "accent-2")
_FONT_NAME = re.compile(r"[A-Za-z0-9 _-]{1,64}")
MAX_THEME_BYTES = 256 * 1024


def load_theme(theme_dir: Path, name: str) -> tuple[dict[str, str], str]:
    """(colours, heading font) of <theme_dir>/<name>.json, or Amazing Moon's when it is not usable.

    The dashboard's rules (stack/stats/theme.py): the whole file or nothing. A partly valid file
    is not merged with the built-in colours, so the GUI and the dashboard never disagree. Like
    the installer, a symlinked theme is refused.
    """
    fallback = (dict(BUILTIN_COLORS), "Orbitron")
    if not _THEME_NAME.fullmatch(name or ""):
        return fallback
    path = theme_dir / f"{name}.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_THEME_BYTES:
            return fallback
        data = json.loads(path.read_bytes().decode("utf-8"))    # strict UTF-8: a BOM is refused
    except (OSError, ValueError, RecursionError):
        return fallback
    colors = data.get("colors") if isinstance(data, dict) else None
    if (not isinstance(colors, dict)
            or not all(isinstance(value, str) and _HEX.fullmatch(value) for value in colors.values())
            or any(key not in colors for key in REQUIRED_COLORS)):
        return fallback
    font = data.get("font", "")
    font = font.strip() if isinstance(font, str) else ""
    return dict(colors), font if _FONT_NAME.fullmatch(font) else ""


def load_theme_colors(theme_dir: Path, name: str) -> dict[str, str]:
    return load_theme(theme_dir, name)[0]


def theme_tokens(colors: Mapping[str, str], mode: str) -> dict[str, str]:
    """Qt colour tokens for 'light' or 'dark' from an oya colour table."""
    c = {**BUILTIN_COLORS, **colors}
    m = "dark" if mode == "dark" else "light"
    window_bg, card_bg = c[f"primary-bg-{m}"], c[f"bubble-bg-{m}"]
    text, accent, link = c[f"text-main-{m}"], c[f"accent-{m}"], c[f"link-{m}"]
    header_bg, header_text = c[f"appbar-bg-{m}"], c[f"appbar-text-{m}"]
    success, warn = c[f"success-{m}"], c[f"warn-{m}"]
    caution = colors.get(f"caution-{m}") or ("#f0a820" if m == "dark" else "#a07800")
    # oya borders cards with accent-2 in light mode and accent-1 in dark mode.
    card_border = c["accent-1"] if m == "dark" else c["accent-2"]

    def best(options: Iterable[str], background: str) -> str:
        return max(options, key=lambda color: contrast(color, background))

    return {
        "window_bg": window_bg, "card_bg": card_bg, "card_border": card_border,
        "input_bg": window_bg, "text": text, "muted": mix(text, window_bg, 0.40),
        # Text on accent buttons: in light mode the same choice as the dashboard's --on-accent
        # (a light accent such as market's orange needs the dark text colour); dark mode may use white.
        "accent": accent,
        "on_accent": best((window_bg, text), accent) if m == "light" else best((window_bg, "#ffffff"), accent),
        "header_bg": header_bg, "header_text": header_text,
        "success": success, "caution": caution, "warn": warn,
        "log_bg": c[f"sunken-{m}"], "link": link,
        # Small accent text: oya's dark accent is only ~2.7:1 on cards, so the
        # (brighter) link colour takes over where it reads better.
        "accent_text": best((accent, link), card_bg),
        "accent_hover": mix(accent, "#ffffff", 0.12),
        "input_border": mix(card_border, card_bg, 0.45),
        "divider": mix(card_border, card_bg, 0.65),
        "button_hover": mix(card_bg, text, 0.08),
        "disabled_text": mix(text, card_bg, 0.60),
        "disabled_bg": mix(card_bg, window_bg, 0.50),
        "success_bg": mix(card_bg, success, 0.15),
        "caution_bg": mix(card_bg, caution, 0.15),
        "warn_bg": mix(card_bg, warn, 0.15),
        "accent_bg": mix(card_bg, accent, 0.12),
        "header_muted": mix(header_text, header_bg, 0.32),
        "header_ok": best((c["success-light"], c["success-dark"]), header_bg),
        "header_warn": best((c["warn-light"], c["warn-dark"]), header_bg),
        "header_caution": best((c["caution-light"], c["caution-dark"]), header_bg),
        "scroll_handle": mix(text, card_bg, 0.70),
    }


QSS_TEMPLATE = """
QMainWindow, QWidget#root, QScrollArea#page { background: $window_bg; border: none; }
QWidget { color: $text; font-size: 14px; }
QLabel, QCheckBox { background: transparent; }
QToolTip { background: $card_bg; color: $text; border: 1px solid $card_border; padding: 4px 8px; }

QFrame#header { background: $header_bg; border: none; }
QLabel#title { color: $header_text; font-size: 22px; font-weight: 700; $display_font }
QLabel#headerEyebrow { color: $header_muted; font-size: 10px; font-weight: 700; letter-spacing: 2px; $display_font }
QLabel#headerMeta { color: $header_muted; font-size: 13px; }
QLabel#headerMeta[state="ok"] { color: $header_text; }
QLabel#headerMeta[state="problem"] { color: $header_warn; }
QLabel#headerDot { border-radius: 4px; background: $header_muted; }
QLabel#headerDot[state="ok"] { background: $header_ok; }
QLabel#headerDot[state="problem"] { background: $header_warn; }
QLabel#headerDot[state="pending"] { background: $header_caution; }

QFrame#card { background: $card_bg; border: 1px solid $card_border; border-radius: 16px; }
QFrame#divider { background: $divider; border: none; min-height: 1px; max-height: 1px; }
QLabel#eyebrow { color: $accent_text; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; $display_font }
QLabel#fieldLabel { color: $muted; font-size: 13px; font-weight: 600; }
QLabel#hint { color: $muted; font-size: 12px; }
QLabel#errorText { color: $warn; font-size: 13px; font-weight: 600; }
QLabel#serviceName { font-size: 15px; font-weight: 700; }
QLabel#url { color: $link; font-size: 13px; }
QLabel#url[kind="muted"] { color: $muted; }

QLineEdit, QSpinBox {
    background: $input_bg; color: $text; border: 1px solid $input_border; border-radius: 10px;
    padding: 7px 10px; selection-background-color: $accent; selection-color: $on_accent;
}
QLineEdit { lineedit-password-character: 8226; }
QLineEdit:focus, QSpinBox:focus { border: 1px solid $accent_text; }
QLineEdit:disabled, QSpinBox:disabled { color: $disabled_text; background: $disabled_bg; border: 1px solid $divider; }

QCheckBox { spacing: 10px; padding: 2px 0; }
QCheckBox:disabled { color: $disabled_text; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px; border: 1px solid $input_border; background: $input_bg; }
QCheckBox::indicator:hover { border: 1px solid $accent_text; }
QCheckBox::indicator:checked { background: $accent; border: 1px solid $accent; }
QCheckBox::indicator:disabled { background: $disabled_bg; border: 1px solid $divider; }
QCheckBox::indicator:checked:disabled { background: $disabled_text; border: 1px solid $disabled_text; }

QPushButton {
    background: transparent; color: $text; border: 1px solid $input_border; border-radius: 10px;
    padding: 8px 18px; font-weight: 600;
}
QPushButton:hover { background: $button_hover; border: 1px solid $card_border; }
QPushButton:pressed { background: $accent_bg; }
QPushButton:disabled { color: $disabled_text; background: transparent; border: 1px solid $divider; }
QPushButton#primaryButton { background: $accent; color: $on_accent; border: 1px solid $accent; padding: 8px 26px; }
QPushButton#primaryButton:hover { background: $accent_hover; border: 1px solid $accent_hover; }
QPushButton#primaryButton:disabled { background: $disabled_bg; color: $disabled_text; border: 1px solid $divider; }
QPushButton#smallButton { padding: 5px 14px; font-size: 13px; }
QPushButton#linkButton { background: transparent; border: none; color: $accent_text; padding: 6px 8px; }
QPushButton#linkButton:hover { color: $text; }
QPushButton#linkButton:disabled { color: $disabled_text; }

QLabel#dot { border-radius: 5px; background: $disabled_text; }
QLabel#dot[kind="success"] { background: $success; }
QLabel#dot[kind="caution"] { background: $caution; }
QLabel#dot[kind="warn"] { background: $warn; }
QLabel#badge {
    border-radius: 9px; padding: 3px 10px; font-size: 12px; font-weight: 700;
    color: $muted; background: $log_bg; border: 1px solid $divider;
}
QLabel#badge[kind="success"] { color: $success; background: $success_bg; border: 1px solid $success; }
QLabel#badge[kind="caution"] { color: $caution; background: $caution_bg; border: 1px solid $caution; }
QLabel#badge[kind="warn"] { color: $warn; background: $warn_bg; border: 1px solid $warn; }

QLabel#banner {
    border-radius: 10px; padding: 8px 14px; font-size: 13px; font-weight: 600;
    color: $text; background: $accent_bg; border: 1px solid $divider;
}
QLabel#banner[kind="success"] { color: $success; background: $success_bg; border: 1px solid $success; }
QLabel#banner[kind="error"] { color: $warn; background: $warn_bg; border: 1px solid $warn; }
QLabel#banner[kind="busy"] { color: $caution; background: $caution_bg; border: 1px solid $caution; }

QPlainTextEdit#log {
    background: $log_bg; color: $text; border: 1px solid $divider; border-radius: 12px; padding: 8px;
    font-family: "$mono_family"; font-size: 12px;
    selection-background-color: $accent; selection-color: $on_accent;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px 4px 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 4px 2px 4px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:horizontal { background: $scroll_handle; border-radius: 4px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


def build_stylesheet(tokens: Mapping[str, str], display_family: str = "", mono_family: str = "monospace") -> str:
    # substitute() raises KeyError on a missing token, which the tests rely on.
    display = f'font-family: "{display_family}";' if display_family else ""
    return string.Template(QSS_TEMPLATE).substitute(tokens, display_font=display, mono_family=mono_family)


def available_themes(theme_dir: Path) -> list[str]:
    """Theme names the installer accepts: regular files (not symlinks) with a valid name."""
    try:
        return sorted(p.stem for p in theme_dir.glob("*.json")
                      if _THEME_NAME.fullmatch(p.stem) and p.is_file() and not p.is_symlink())
    except OSError:
        return []
