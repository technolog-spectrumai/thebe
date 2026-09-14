"""Theme tokens for the dashboard, generated from zenobia's theme files.

theme/<name>.json (stack/theme in the repository) are zenobia's theme files, copied verbatim:
same format, same colour keys. The THEME setting picks one. app.py loads it once at start-up
and serves theme_css() as /theme.css, which base.html links before /static/oya.css; oya.css
only consumes the variables, so a theme switch re-colours every page.

A theme that cannot be used (unknown name, unreadable file, invalid JSON, a colour that is
not #rgb/#rrggbb, a missing key) never stops the dashboard: load_theme() logs one warning
naming the problem and returns the built-in copy of Amazing Moon.

Mapping, the same one builder.py's theme_tokens() uses for the Qt GUI, so both show one
palette (<mode> is light or dark):

  zenobia colour key             CSS custom property         builder.py token
  primary-bg-<mode>              --primary-bg                window_bg, input_bg
  bubble-bg-<mode>               --bubble-bg                 card_bg
  sunken-<mode>                  --sunken                    log_bg
  appbar-bg-<mode>               --appbar-bg                 header_bg
  appbar-text-<mode>             --appbar-text               header_text
  header-bg-<mode>               --header-bg                 -
  footer-bg-<mode>               --footer-bg                 -
  footer-text-<mode>             --footer-text               -
  text-main-<mode>               --text-main                 text
  accent-<mode>                  --accent                    accent
  link-<mode>                    --link                      link
  success-<mode>, warn-<mode>    --success, --warn           success, warn
  caution-<mode>                 --caution                   caution (#a07800 light / #f0a820 dark when absent)
  accent-1, accent-2             --accent-1, --accent-2      -
  accent-2 light, accent-1 dark  --border                    card_border
  font                           --font-display              - (the GUI always uses the bundled Orbitron)

Two light-mode properties are choices CSS cannot make, so they are generated here too:

  accent or link, whichever contrasts more with bubble-bg        --accent-text   accent_text
  primary-bg or text-main, whichever contrasts more with accent  --on-accent     - (on_accent: window_bg or white)

A light accent (market's orange) is unreadable as small text or under button text otherwise.
Dark mode keeps oya.css's own rules for both. Everything else (status text colours, dividers,
meters) is derived in oya.css with var() and color-mix() from these properties.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("stats.theme")

DEFAULT_THEME = "amazing"
MAX_THEME_BYTES = 256 * 1024

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}")
# A font name is written into CSS inside double quotes: nothing that could end the string.
_FONT = re.compile(r"[A-Za-z0-9 _-]{1,64}")

MODES = ("light", "dark")
# Colours with a light and a dark value ('<token>-light', '<token>-dark'), in CSS order.
MODE_TOKENS = (
    "primary-bg",
    "bubble-bg",
    "sunken",
    "header-bg",
    "appbar-bg",
    "appbar-text",
    "footer-bg",
    "footer-text",
    "text-main",
    "accent",
    "link",
    "warn",
    "success",
    "caution",
)
SHARED_TOKENS = ("accent-1", "accent-2")
# Older zenobia theme files have no caution colours; these are Amazing Moon's.
CAUTION_FALLBACK = {"light": "#a07800", "dark": "#f0a820"}
REQUIRED_COLORS = (
    tuple(f"{token}-{mode}" for mode in MODES for token in MODE_TOKENS if token != "caution") + SHARED_TOKENS
)

# amazing.json's colours: the theme used when the configured one cannot be loaded.
BUILTIN_COLORS = {
    "primary-bg-light": "#f2f3f5",
    "header-bg-light": "#1d2333",
    "appbar-bg-light": "#252c3e",
    "appbar-text-light": "#e4e7ef",
    "bubble-bg-light": "#e8e9ec",
    "footer-bg-light": "#1d2333",
    "footer-text-light": "#e4e7ef",
    "text-main-light": "#0f1114",
    "accent-light": "#2f3d63",
    "warn-light": "#d94a4a",
    "success-light": "#4a8f7a",
    "sunken-light": "#e1e2e5",
    "link-light": "#2a4b8f",
    "primary-bg-dark": "#0a0c11",
    "header-bg-dark": "#0f1420",
    "appbar-bg-dark": "#151b29",
    "appbar-text-dark": "#cfd4df",
    "bubble-bg-dark": "#191f2d",
    "footer-bg-dark": "#0f1420",
    "footer-text-dark": "#cfd4df",
    "text-main-dark": "#d3d7e0",
    "accent-dark": "#4f5fa1",
    "warn-dark": "#ff4455",
    "caution-light": "#a07800",
    "caution-dark": "#f0a820",
    "success-dark": "#5fa38c",
    "sunken-dark": "#141821",
    "link-dark": "#5f7fc9",
    "accent-1": "#4f5fa1",
    "accent-2": "#2f3d63",
}

# "Orbitron" resolves to the @font-face in oya.css (static/orbitron-latin.woff2); any other
# name is used only when that font is installed on the viewing device (nothing is downloaded).
SYSTEM_SANS = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'


class ThemeError(Exception):
    """The theme cannot be used; the message names the problem."""


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 contrast ratio of two hex colours (the same formula as builder.contrast)."""

    def luminance(color: str) -> float:
        channels = [c / 255 for c in _rgb(color)]
        r, g, b_ = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _best(options: tuple[str, ...], background: str) -> str:
    """The option that reads best on background; the first one on a tie (like builder.py)."""
    return max(options, key=lambda color: contrast(color, background))


@dataclass(frozen=True)
class Theme:
    key: str  # file name without .json
    name: str  # display name from the file, e.g. "Amazing Moon"
    font: str  # validated font family, "" for the system sans-serif stack
    colors: dict[str, str]
    source: str  # where the colours came from, for the generated CSS comment

    def tokens(self, mode: str) -> dict[str, str]:
        """CSS custom properties (names without the leading --) for 'light' or 'dark'."""
        values = {token: self.colors.get(f"{token}-{mode}") for token in MODE_TOKENS}
        if values["caution"] is None:
            values["caution"] = CAUTION_FALLBACK[mode]
        values.update((token, self.colors[token]) for token in SHARED_TOKENS)
        # oya borders cards with accent-2 in light mode and accent-1 in dark mode.
        values["border"] = "var(--accent-1)" if mode == "dark" else "var(--accent-2)"
        if mode == "light":
            values["accent-text"] = _best((values["accent"], values["link"]), values["bubble-bg"])
            values["on-accent"] = _best((values["primary-bg"], values["text-main"]), values["accent"])
        return values


FALLBACK = Theme(DEFAULT_THEME, "Amazing Moon", "Orbitron", dict(BUILTIN_COLORS), "built-in copy of amazing.json")


def _shown(value: object) -> str:
    return repr(value[:64] if isinstance(value, str) else value)


def _read_theme(name: object, theme_dir: Path) -> Theme:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ThemeError("the name must be 1-64 letters, digits, '_' or '-'")
    try:
        root = theme_dir.resolve()
        path = (root / f"{name}.json").resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop before Python 3.13
        raise ThemeError(f"cannot resolve {theme_dir / name}.json: {exc}") from None
    # The name cannot contain a path separator; this catches a symlink pointing elsewhere.
    if path.parent != root:
        raise ThemeError(f"{root / name}.json points outside {root}")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_THEME_BYTES + 1)
    except FileNotFoundError:
        raise ThemeError(f"{path} does not exist") from None
    except OSError as exc:
        raise ThemeError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(raw) > MAX_THEME_BYTES:
        raise ThemeError(f"{path} is larger than {MAX_THEME_BYTES // 1024} KB")
    try:
        # Strict UTF-8 without a BOM, like builder.py's read_text(): both apps accept the same files.
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError is a ValueError
        raise ThemeError(f"{path} is not valid JSON ({exc})") from None

    if not isinstance(data, dict):
        raise ThemeError(f"{path} does not contain a JSON object")
    colors = data.get("colors")
    if not isinstance(colors, dict):
        raise ThemeError(f'{path} has no "colors" object')
    invalid = [key for key, value in colors.items() if not (isinstance(value, str) and _HEX.fullmatch(value))]
    if invalid:
        listed = ", ".join(_shown(key) for key in invalid[:3]) + (f" and {len(invalid) - 3} more" if len(invalid) > 3 else "")
        raise ThemeError(f"{path}: colours must be #rgb or #rrggbb, not the values of {listed}")
    missing = [key for key in REQUIRED_COLORS if key not in colors]
    if missing:
        raise ThemeError(f"{path} lacks the colours {', '.join(missing)}")

    title = data.get("name")
    font = data.get("font", "")
    if isinstance(font, str):
        font = font.strip()
    if font != "" and not (isinstance(font, str) and _FONT.fullmatch(font)):
        # Only the heading font is affected, so this is no reason to drop the colours.
        log.warning(
            "Theme %s: font %s ignored (only letters, digits, spaces, '_' and '-'); headings use the system sans-serif font.",
            name,
            _shown(font),
        )
        font = ""
    return Theme(name, title.strip() if isinstance(title, str) else name, font, dict(colors), f"theme/{name}.json")


def load_theme(name: object, theme_dir: Path | str) -> Theme:
    """The theme <theme_dir>/<name>.json, or the built-in Amazing Moon after one warning."""
    try:
        return _read_theme(name, Path(theme_dir))
    except ThemeError as exc:
        log.warning("Theme %s cannot be used: %s. Using the built-in Amazing Moon colours.", _shown(name), exc)
        return FALLBACK


def font_display(font: str) -> str:
    """Value of --font-display: the theme font first, then the system sans-serif stack."""
    return f'"{font}", {SYSTEM_SANS}' if font and _FONT.fullmatch(font) else SYSTEM_SANS


def theme_css(theme: Theme) -> str:
    """The /theme.css stylesheet: light tokens on :root, dark tokens for data-theme="dark"."""
    title = re.sub(r"[^A-Za-z0-9 _.-]", "", theme.name)[:64]
    lines = [f"/* {title}: generated by the stats app from the {theme.source} (theme.py). */"]
    for mode in MODES:
        lines.append(":root {" if mode == "light" else ':root[data-theme="dark"] {')
        lines.append(f"  color-scheme: {mode};")
        lines.extend(f"  --{token}: {value};" for token, value in theme.tokens(mode).items())
        if mode == "light":
            lines.append(f"  --font-display: {font_display(theme.font)};")
        lines.append("}")
    return "\n".join(lines) + "\n"
