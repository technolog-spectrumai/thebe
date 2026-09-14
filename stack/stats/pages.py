"""Server-rendered pages: one shared layout filled by plain string substitution.

Templates contain {{ name }} placeholders. Every value is HTML-escaped unless it is a
Markup instance (fragments assembled here from escaped parts and constant SVG). An
unknown placeholder raises, and every page is rendered once at start-up, so a template
mistake stops the service instead of serving a broken page.

Adding a page: create templates/<key>.html and add a NavItem to NAV_ITEMS. app.py
registers a GET route for every entry and the navigation bar lists it, in this order,
followed by the external JupyterLab link.
"""

import html
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
APP_NAME = "JupyterLab over Tailscale"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


class Markup(str):
    """Trusted HTML that render() inserts without escaping."""


@dataclass(frozen=True)
class NavItem:
    key: str  # template name: templates/<key>.html
    label: str  # navigation label and page title
    path: str  # route
    icon: str  # key of ICON_PATHS
    scripts: tuple[str, ...] = ()  # files under /static, loaded at the end of <body>


NAV_ITEMS = (
    NavItem("statistics", "Statistics", "/", "chart", scripts=("stats.js",)),
    NavItem("dependencies", "Dependencies", "/dependencies", "package", scripts=("deps.js",)),
)

# Inline SVG icons (24x24 grid, stroked with currentColor so they follow the theme).
ICON_PATHS = {
    "logo": '<circle cx="10" cy="14" r="7"/><path d="M3.6 11.5h12.8M3.6 16.5h12.8"/><circle cx="19.5" cy="4.5" r="1.6"/>',
    "chart": '<path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 6-6"/>',
    "notebook": '<path d="M6 3h11a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6z"/><path d="M6 3v18M3 7.5h3M3 12h3M3 16.5h3M10 8h5"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "sun": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>'
    ),
    "moon": '<path d="M20.5 14.1A8.5 8.5 0 1 1 9.9 3.5a6.6 6.6 0 0 0 10.6 10.6z"/>',
    "cpu": (
        '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/>'
        '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>'
    ),
    "memory": '<rect x="2" y="6" width="20" height="11" rx="2"/><path d="M6 10v3M10 10v3M14 10v3M18 10v3M5 17v3M19 17v3"/>',
    "disk": (
        '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.66 3.58 3 8 3s8-1.34 8-3V5"/>'
        '<path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/>'
    ),
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "network": '<path d="M7 20V4M3 8l4-4 4 4"/><path d="M17 4v16M13 16l4 4 4-4"/>',
    "kernel": (
        '<path d="M8 3H7a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2 2 2 0 0 1 2 2v4a2 2 0 0 0 2 2h1"/>'
        '<path d="M16 3h1a2 2 0 0 1 2 2v4a2 2 0 0 0 2 2 2 2 0 0 0-2 2v4a2 2 0 0 1-2 2h-1"/>'
    ),
    "gpu": '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="15.5" cy="12" r="3"/><path d="M6 10h4M6 14h4M6 18v3M10 18v3"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    "package": '<path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "refresh": '<path d="M20.5 12a8.5 8.5 0 1 1-2.5-6"/><path d="M20.5 3.5V9H15"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    "trash": (
        '<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>'
        '<path d="m19 6-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
    ),
    "terminal": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m6 9 3 3-3 3M12 15h6"/>',
    "save": '<path d="M5 3h11l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M7 3v5h8V3M7 21v-7h10v7"/>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
}


def icon(name: str, extra_class: str = "") -> Markup:
    classes = f"icon {extra_class}".strip()
    return Markup(
        f'<svg class="{classes}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
        f"{ICON_PATHS[name]}</svg>"
    )


def render(source: str, values: dict, *, template: str = "<string>") -> str:
    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"templates/{template}: no value for placeholder {name!r}")
        value = values[name]
        return value if isinstance(value, Markup) else html.escape(str(value), quote=True)

    return _PLACEHOLDER.sub(substitute, source)


def safe_http_url(url: str) -> str:
    """Only absolute http(s) URLs become links; anything else (unset, javascript:) is dropped."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    return url.strip() if parts.scheme in ("http", "https") and parts.netloc else ""


class PageRenderer:
    """Renders every NAV_ITEMS page once; they depend only on start-up settings."""

    def __init__(self, *, host_name: str, jupyter_public_url: str, theme_color: dict[str, str]):
        self._jupyter_url = safe_http_url(jupyter_public_url)
        self._common = {
            "app_name": APP_NAME,
            "host_name": host_name,
            "jupyter_url": self._jupyter_url,
            # <meta name="theme-color"> per mode (the theme's app bar colour); theme.js switches it.
            "theme_color_light": theme_color["light"],
            "theme_color_dark": theme_color["dark"],
            **{f"icon_{name}": icon(name) for name in ICON_PATHS},
        }
        self._pages = {item.key: self._render_page(item) for item in NAV_ITEMS}

    def page(self, key: str) -> str:
        return self._pages[key]

    def _template(self, name: str) -> str:
        path = TEMPLATES_DIR / name
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read template {path}: {exc.strerror or exc}") from None

    def _render_page(self, active: NavItem) -> str:
        content = render(self._template(f"{active.key}.html"), self._common, template=f"{active.key}.html")
        scripts = "\n".join(
            f'<script src="/static/{html.escape(script, quote=True)}"></script>' for script in active.scripts
        )
        values = self._common | {
            "page_title": active.label,
            "nav": self._nav(active),
            "content": Markup(content),
            "scripts": Markup(scripts),
        }
        return render(self._template("base.html"), values, template="base.html")

    def _nav(self, active: NavItem) -> Markup:
        esc = html.escape
        links = []
        for item in NAV_ITEMS:
            current = ' aria-current="page"' if item == active else ""
            links.append(
                f'<a class="nav-link" href="{esc(item.path, quote=True)}"{current}>'
                f"{icon(item.icon)}<span>{esc(item.label)}</span></a>"
            )
        if self._jupyter_url:
            links.append(
                f'<a class="nav-link" href="{esc(self._jupyter_url, quote=True)}" target="_blank" rel="noopener noreferrer">'
                f'{icon("notebook")}<span>JupyterLab</span>{icon("external", "icon--xs")}'
                '<span class="visually-hidden"> (opens in a new tab)</span></a>'
            )
        return Markup("\n".join(links))
