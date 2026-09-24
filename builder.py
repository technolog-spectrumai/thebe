#!/usr/bin/env python3
"""JupyterLab on Tailscale - a small PyQt6 builder.

A thin GUI over setup-jupyterlab-tailscale.sh and its Docker Compose stack.
It edits config.yaml (shared with the headless ./run.sh), writes the
installer's settings .env from it, runs the installer's install / start /
restart / stop commands through QProcess, shows container state from
`docker compose ps`, and opens the deployed pages in a browser. All real work
stays in the installer and the Qt-free thebe package, so the CLI workflow and
run.py behave exactly the same.

Start it with ./run-builder.sh, which keeps PyQt6 inside ./.venv.
"""

from __future__ import annotations

import codecs
import dataclasses
import os
import shlex
import shutil
import signal
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, Mapping

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QPalette
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QBoxLayout, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QVBoxLayout, QWidget,
)

# The Qt-free core, shared with run.py. Names the GUI does not use itself are imported too, so
# builder.<name> keeps working for tests and older callers.
from thebe.ai import ai_file, describe, save_ai_file
from thebe.config import (  # noqa: F401
    Config, ConfigError, config_from_settings, effective_workspace, load_config, make_private, save_config,
    workspace_dir,
)
from thebe.imports import (  # noqa: F401
    IMPORT_DIR, MAX_IMPORT_DIRS, ImportDir, default_workspace, plan_imports,
)
from thebe.packages import CopyRefused, check_requirements, copy_requirements, requirements_file
from thebe.settings import (  # noqa: F401
    APP_NAME, DEFAULTS, HTTPS_MODES, PAGES, PROJECT, REPO, SERVICES, TAILNET, BindProbe, Page, Paths,
    SettingsError, absolute_path, check_port, is_public_host, is_valid_hostname, load_settings_file,
    normalize_bool, occupied_port_errors, parse_settings, password_problems, probe_bind, read_text,
    render_settings, save_settings, settings_line_problems, validate_settings, write_private_file,
)
from thebe.stack import (  # noqa: F401
    CHILD_ENV_EXTRA, MASK, TailscaleStatus, child_environment, clean_line, magicdns_name, mask_secrets,
    page_url, parse_compose_ps, parse_tailscale_status, root_step_from_line, service_state, service_url,
)
from thebe.theme import (  # noqa: F401
    BUILTIN_COLORS, QSS_TEMPLATE, TOKEN_NAMES, available_themes, build_stylesheet, contrast,
    load_theme, load_theme_colors, mix, theme_tokens,
)

KILL_GRACE_MS = 10_000
STATUS_INTERVAL_MS = 4_000
TAILSCALE_INTERVAL_MS = 30_000
PROBE_TIMEOUT_MS = 15_000      # a wedged dockerd/tailscaled must not hang the GUI
LOG_MAX_BLOCKS = 4000
LOG_FLUSH_MS = 80              # output reaches the log in batches, not per chunk
MAX_LINE_CHARS = 4096          # longer output lines are cut
ACTIVE_STATES = ("running", "restarting", "paused")    # containers Stop still has to stop


# --------------------------------------------------------------------------
# Theme: the oya tokens (thebe.theme) as a Qt palette and fonts
# --------------------------------------------------------------------------

def build_palette(tokens: Mapping[str, str]) -> QPalette:
    """Fusion paints some parts (placeholders, selections, dialogs) from the palette."""
    role = QPalette.ColorRole
    palette = QPalette()
    for r, key in ((role.Window, "window_bg"), (role.WindowText, "text"), (role.Base, "input_bg"),
                   (role.AlternateBase, "card_bg"), (role.Text, "text"), (role.Button, "card_bg"),
                   (role.ButtonText, "text"), (role.PlaceholderText, "muted"), (role.Highlight, "accent"),
                   (role.HighlightedText, "on_accent"), (role.ToolTipBase, "card_bg"),
                   (role.ToolTipText, "text"), (role.Link, "link"), (role.BrightText, "warn")):
        palette.setColor(r, QColor(tokens[key]))
    for r in (role.Text, role.WindowText, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, r, QColor(tokens["disabled_text"]))
    return palette


_display_families: dict[str, str] = {}


def load_display_family(font_file: Path) -> str:
    """Register Orbitron once; '' when the file is missing or Qt cannot read it."""
    key = str(font_file)
    if key not in _display_families:
        family = ""
        if font_file.is_file():
            font_id = QFontDatabase.addApplicationFont(key)
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            family = families[0] if families else ""
        _display_families[key] = family
    return _display_families[key]


# --------------------------------------------------------------------------
# Running commands
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    outcome: str               # ok | exit (non-zero) | crashed | failed (never started) | cancelled | timeout
    code: int
    error: str = ""
    lines: tuple[str, ...] = ()  # only filled when the Runner captures output


class Runner(QObject):
    """One command at a time through QProcess: argv only, never a shell."""

    output = pyqtSignal(list)    # cleaned lines, in batches as they arrive
    done = pyqtSignal(object)    # RunResult

    def __init__(self, parent: QObject | None = None, *, echo: bool = True, capture: bool = False,
                 kill_grace_ms: int = KILL_GRACE_MS) -> None:
        super().__init__(parent)
        self.echo, self.capture, self.kill_grace_ms = echo, capture, kill_grace_ms
        self._proc: QProcess | None = None
        self._argv: tuple[str, ...] = ()
        self._lines: list[str] = []
        self._pending = ""           # the unfinished last line, at most MAX_LINE_CHARS
        self._truncated = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._cancelled = self._timed_out = False
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill)
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)

    def is_running(self) -> bool:
        return self._proc is not None

    def start(self, argv: Iterable[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
              timeout_ms: int = 0) -> None:
        """Run argv; with timeout_ms, cancel it after that long (outcome 'timeout')."""
        if self._proc is not None:
            raise RuntimeError("the runner is already running a command")
        argv = [str(arg) for arg in argv]
        env = child_environment(os.environ) if env is None else dict(env)
        self._argv, self._lines, self._pending, self._truncated = tuple(argv), [], "", False
        self._cancelled = self._timed_out = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        # Resolve the program with the child's PATH. Left to itself QProcess
        # would search the builder's own PATH, which may differ.
        program = argv[0] if "/" in argv[0] else shutil.which(argv[0], path=env.get("PATH", os.defpath))
        if program is None:
            if self.echo:
                self.output.emit(["$ " + shlex.join(argv)])
            self.done.emit(RunResult(self._argv, "failed", -1, f"{argv[0]}: command not found"))
            return
        proc = QProcess(self)    # parented, so it is never collected mid-run
        proc.setProgram(program)
        proc.setArguments(argv[1:])
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        # No stdin: a prompt (sudo, docker login) fails instead of hanging a GUI.
        proc.setStandardInputFile(QProcess.nullDevice())
        if hasattr(QProcess, "UnixProcessFlag") and hasattr(QProcess.UnixProcessFlag, "CreateNewSession"):
            # Own session and process group: cancel can signal the whole tree,
            # and without a controlling terminal sudo cannot prompt either.
            proc.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
        environment = QProcessEnvironment()
        for key, value in env.items():
            environment.insert(key, value)
        proc.setProcessEnvironment(environment)
        if cwd:
            proc.setWorkingDirectory(cwd)
        proc.readyReadStandardOutput.connect(self._drain)
        proc.errorOccurred.connect(self._on_error)
        proc.finished.connect(self._on_finished)
        self._proc = proc
        if self.echo:
            self.output.emit(["$ " + shlex.join(argv)])
        if timeout_ms > 0:
            self._timeout_timer.start(timeout_ms)
        proc.start()

    def cancel(self) -> None:
        """SIGTERM the process group now, SIGKILL it after the grace period."""
        proc = self._proc
        if proc is None or self._cancelled:
            return
        self._cancelled = True
        if proc.state() != QProcess.ProcessState.NotRunning:
            self._signal(proc, signal.SIGTERM)
            self._kill_timer.start(self.kill_grace_ms)

    def terminate_and_wait(self) -> bool:
        """Cancel and block until the process is gone (used when closing)."""
        proc = self._proc
        if proc is None:
            return True
        self.cancel()
        # The QTimer cannot fire while we block, so escalate by hand.
        if proc.waitForFinished(self.kill_grace_ms):
            return True
        if self._proc is proc:
            self._signal(proc, signal.SIGKILL)
        return proc.waitForFinished(3000)

    def _on_timeout(self) -> None:
        if self._proc is not None and not self._cancelled:
            self._timed_out = True
            self.cancel()

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            self._signal(proc, signal.SIGKILL)

    @staticmethod
    def _signal(proc: QProcess, sig: int) -> None:
        pid = int(proc.processId())
        if pid > 0:     # never killpg(0): that would be our own group
            try:
                os.killpg(pid, sig)   # CreateNewSession: the group id is the pid
                return
            except (ProcessLookupError, PermissionError):
                pass
        (proc.kill if sig == signal.SIGKILL else proc.terminate)()

    def _drain(self, final: bool = False) -> None:
        proc = self._proc
        if proc is None:
            return
        data = self._decoder.decode(bytes(proc.readAllStandardOutput()), final)
        # Only the new data is split, and the unfinished line is capped, so a
        # flood of output (or one endless line) costs linear time.
        lines = []
        for index, piece in enumerate(data.split("\n")):
            if index:
                lines.append(self._take_pending())
            self._add_pending(piece)
        if final and self._pending:
            lines.append(self._take_pending())
        if lines:
            if self.capture:
                self._lines.extend(lines)
            self.output.emit(lines)

    def _add_pending(self, piece: str) -> None:
        text = self._pending + piece
        if "\r" in piece:
            # A carriage return redraws the line (progress output): keep only
            # the latest state. A trailing '\r' may be half of a CRLF, so it stays.
            cut = text.rstrip("\r").rfind("\r")
            if cut >= 0:
                text, self._truncated = text[cut + 1:], False
        if len(text) > MAX_LINE_CHARS:
            text, self._truncated = text[:MAX_LINE_CHARS], True
        self._pending = text

    def _take_pending(self) -> str:
        line = clean_line(self._pending) + (" […line cut]" if self._truncated else "")
        self._pending, self._truncated = "", False
        return line

    def _on_error(self, error: QProcess.ProcessError) -> None:
        # finished() does not follow a failed start; every other error does.
        if error == QProcess.ProcessError.FailedToStart and self._proc is not None:
            self._complete("failed", -1, self._proc.errorString())

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._proc is None:
            return
        self._drain(final=True)
        if self._timed_out:
            outcome = "timeout"
        elif self._cancelled:
            outcome = "cancelled"
        elif status == QProcess.ExitStatus.CrashExit:
            outcome = "crashed"
        else:
            outcome = "ok" if code == 0 else "exit"
        self._complete(outcome, code)

    def _complete(self, outcome: str, code: int, error: str = "") -> None:
        proc, self._proc = self._proc, None   # cleared first: a done() slot may start the next command
        self._kill_timer.stop()
        self._timeout_timer.stop()
        if proc is not None:
            proc.deleteLater()
        self.done.emit(RunResult(self._argv, outcome, int(code), error, tuple(self._lines)))


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class PortSpinBox(QSpinBox):
    """A port field that the mouse wheel changes only once it has focus.

    Otherwise scrolling the page over the field would edit a port.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)     # the wheel does not grab focus
        # The whole port range, not 1024+: a narrower range makes Qt silently put
        # back the previous value when someone types 80, and Deploy would then
        # ship a port nobody asked for. validate_settings rejects it with a message.
        self.setRange(1, 65535)
        self.setGroupSeparatorShown(False)    # 8888, not 8,888
        self.setKeyboardTracking(False)       # no clamping or validation mid-typing
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setMinimumWidth(120)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()                    # let the scroll area have it


@dataclass
class ImportRow:
    widget: QWidget
    path: QLineEdit
    browse: QPushButton
    name: QLineEdit


@dataclass
class ServiceRow:
    dot: QLabel
    badge: QLabel
    url: QLabel
    open_button: QPushButton


def _repolish(widget: QWidget, **properties: str) -> None:
    """Set dynamic properties; Qt only re-reads the stylesheet after unpolish/polish."""
    for name, value in properties.items():
        widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


BUSY_TEXT = {
    "import": "Deploying: copying the imported directories into the workspace…",
    "install": "Deploying: building images and starting containers…",
    "start": "Starting containers…",
    "restart": "Restarting containers…",
    "stop": "Stopping containers…",
}
JOB_TITLES = {"import": "Deploy", "install": "Deploy", "start": "Start", "restart": "Restart", "stop": "Stop"}
BUSY_TEXT_HTTPS = "Switching to HTTPS: deploying again with the new certificate…"
# Part of the installer's host-setup error when sysctl/firewall were applied and only the
# certificate could not be issued.
CERT_ONLY_FAILURE = "but no certificate for"


def host_step_label(args: Iterable[str]) -> str:
    """What a host-setup step changes, for messages."""
    return "sysctl/firewall/HTTPS certificate" if "--cert" in args else "sysctl/firewall"


class MainWindow(QMainWindow):
    def __init__(self, paths: Paths | None = None, *, environ: Mapping[str, str] | None = None,
                 bind: BindProbe = probe_bind, open_url: Callable[[QUrl], bool] = QDesktopServices.openUrl,
                 kill_grace_ms: int = KILL_GRACE_MS, poll: bool = True) -> None:
        super().__init__()
        self._environ = dict(os.environ if environ is None else environ)
        self.paths = paths or Paths.from_environment(self._environ)
        self._bind = bind
        self._open_url = open_url
        self._bash = shutil.which("bash", path=self._environ.get("PATH", os.defpath)) or "/bin/bash"
        self.themes = available_themes(self.paths.theme_dir)
        self.ts = TailscaleStatus()                 # neither ip nor problem: not checked yet
        self.docker_problem: str | None = None      # None: not checked yet, '': reachable
        self.services: dict[str, dict] = {}
        self.runtime = self._read_runtime()
        self.tokens: dict[str, str] = {}
        self.mode = "light"
        self._busy = False
        self._closing = False
        self._job = ""               # install | start | restart | stop | pkexec
        self._command = ""           # the installer command a pkexec step belongs to
        self._root_step: list[str] | None = None
        self._after_certificate = False    # this install follows a host step that issued a certificate
        self._success_note = ""
        self._nvidia_auto = True     # NVIDIA='auto' until the checkbox is clicked (_fill_form)
        self._tail: list[str] = []   # last output lines, for failure messages
        self._after_tailscale: Callable[[], None] | None = None
        self._after_status: Callable[[], None] | None = None
        self._status_again = False
        self._log_queue: list[str] = []
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(LOG_FLUSH_MS)
        self._log_timer.timeout.connect(self._flush_log)

        self.settings_values, load_notes = self._load_settings()
        self._secrets = {self.settings_values["JUPYTER_PASSWORD"]}

        self.job_runner = Runner(self, kill_grace_ms=kill_grace_ms)
        self.job_runner.output.connect(self._on_job_output)
        self.job_runner.done.connect(self._on_job_done)
        self.status_runner = Runner(self, echo=False, capture=True, kill_grace_ms=2000)
        self.status_runner.done.connect(self._on_status_done)
        self.tailscale_runner = Runner(self, echo=False, capture=True, kill_grace_ms=2000)
        self.tailscale_runner.done.connect(self._on_tailscale_done)

        self._display_family = load_display_family(self.paths.display_font)
        self._build_ui()
        self._fill_form(self.settings_values)
        self._fill_imports(self.config.imports)
        self._refresh_packages()
        self.apply_theme()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_color_scheme_changed)

        self.status_timer = QTimer(self)
        self.status_timer.setInterval(STATUS_INTERVAL_MS)
        self.status_timer.timeout.connect(self._poll_status)
        self.tailscale_timer = QTimer(self)
        self.tailscale_timer.setInterval(TAILSCALE_INTERVAL_MS)
        self.tailscale_timer.timeout.connect(self._poll_tailscale)

        self._refresh_view()
        if load_notes:
            self._banner("error", " ".join(load_notes))
        else:
            self._banner("info", "Ready. Deploy saves the settings, builds the images and starts the containers.")
        if poll:
            self.status_timer.start()
            self.tailscale_timer.start()
            self.refresh_status()
            self.refresh_tailscale()

    # -- construction -------------------------------------------------------

    def _load_settings(self) -> tuple[dict[str, str], list[str]]:
        """The form's values from config.yaml, or from the installer's settings file until it exists.

        Sets self.config (what Deploy saves, with the form's values) and self.config_problem (why
        config.yaml is left alone: it exists but cannot be read as a whole).
        """
        path = self.paths.config_file
        self.config_problem = ""
        try:
            self.config, problems = load_config(path)
        except FileNotFoundError:
            self.config, notes = config_from_settings(self.paths.settings)
            return dict(self.config.settings), notes
        except ConfigError as exc:
            self.config = Config()
            self.config_problem = (f"{exc}, so the form shows the defaults and Deploy will not rewrite it. "
                                   "Fix it (or delete it) and restart the builder.")
            return dict(self.config.settings), [self.config_problem]
        # A setting with a problem keeps its default; say so instead of silently replacing it.
        return dict(self.config.settings), [f"{path.name}: {problem} The form shows the default." for problem in problems]

    @staticmethod
    def _label(text: str = "", name: str = "", *, wrap: bool = False, selectable: bool = False) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(wrap)
        if selectable:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _button(text: str, name: str = "", slot: Callable | None = None, tooltip: str = "") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        if slot is not None:
            button.clicked.connect(slot)
        return button

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(self._label(title.upper(), "eyebrow"))
        return card, layout

    def _build_ui(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(640, 480)
        self.resize(900, 820)
        page = QWidget()
        page.setObjectName("root")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        body = QVBoxLayout()
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(14)
        outer.addLayout(body, 1)
        # Side by side on a wide window, stacked below ~860 px (see resizeEvent).
        self.cards_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.cards_layout.setSpacing(14)
        self.cards_layout.addWidget(self._build_settings_card(), 1)
        self.cards_layout.addWidget(self._build_services_card(), 1)
        body.addLayout(self.cards_layout)
        body.addWidget(self._build_imports_card())
        body.addWidget(self._build_packages_card())
        body.addLayout(self._build_actions())
        body.addWidget(self._build_output_card(), 1)
        # A short screen scrolls the page instead of squeezing the inputs.
        scroll = QScrollArea()
        scroll.setObjectName("page")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header")
        layout = QVBoxLayout(header)
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(2)
        layout.addWidget(self._label("DOCKER · TAILSCALE · BUILDER", "headerEyebrow"))
        layout.addWidget(self._label(APP_NAME, "title"))
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 6, 0, 0)
        meta.setSpacing(8)
        self.tailscale_dot, self.docker_dot = self._label(name="headerDot"), self._label(name="headerDot")
        self.tailscale_label = self._label(name="headerMeta", selectable=True)
        self.docker_label = self._label(name="headerMeta", selectable=True)
        for dot, label, stretch in ((self.tailscale_dot, self.tailscale_label, 0),
                                    (self.docker_dot, self.docker_label, 1)):
            dot.setFixedSize(8, 8)
            # A long problem text is clipped rather than widening the window;
            # the tooltip carries the full text.
            label.setMinimumWidth(40)
            meta.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            meta.addWidget(label, stretch)
            meta.addSpacing(12)
        layout.addLayout(meta)
        return header

    def _build_settings_card(self) -> QFrame:
        card, layout = self._card("Settings")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("8 to 128 characters")
        self.password_toggle = self._button("Show", "smallButton", tooltip="Show or hide the password")
        self.password_toggle.setCheckable(True)
        self.password_toggle.toggled.connect(self._toggle_password)
        self.jupyter_port = PortSpinBox()
        self.stats_check = QCheckBox("Enable FastAPI statistics")
        self.stats_port = PortSpinBox()
        self.stats_user_label = self._label(name="hint")
        self.stats_user_label.setToolTip("The dashboard asks for this username and the password above (HTTP Basic).")
        self.nvidia_check = QCheckBox("Expect an NVIDIA GPU")
        self.nvidia_check.setToolTip("Ticked: the notebooks get the NVIDIA GPU, and Deploy and Start stop with the reason "
                                     "when Docker cannot hand it over. Unticked: never use a GPU (machines without "
                                     "NVIDIA, or a broken driver or container toolkit).")
        self.nvidia_check.clicked.connect(self._on_nvidia_clicked)    # the user's clicks only
        self.nvidia_hint = self._label(name="hint", wrap=True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        rows = (("Password", (self.password_edit, 1), self.password_toggle),
                ("JupyterLab port", self.jupyter_port, 1),
                ("Statistics", self.stats_check, 1),
                ("Statistics port", self.stats_port, self.stats_user_label, 1),
                ("NVIDIA GPU", self.nvidia_check, (self.nvidia_hint, 1)))
        for index, (caption, *items) in enumerate(rows):
            grid.addWidget(self._label(caption, "fieldLabel"), index, 0)
            row = QHBoxLayout()
            row.setSpacing(10)
            for item in items:     # widget, (widget, stretch) or a trailing stretch
                if isinstance(item, int):
                    row.addStretch(item)
                else:
                    row.addWidget(*(item if isinstance(item, tuple) else (item,)))
            grid.addLayout(row, index, 1)
        layout.addLayout(grid)

        self.error_label = self._label(name="errorText", wrap=True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        for changed in (self.password_edit.textChanged, self.jupyter_port.valueChanged,
                        self.stats_port.valueChanged, self.stats_check.toggled):
            changed.connect(self._on_form_changed)
        return card

    def _build_services_card(self) -> QFrame:
        card, layout = self._card("Services")
        self.rows: dict[str, ServiceRow] = {}
        self.page_buttons: dict[Page, QPushButton] = {}
        for index, (service, name) in enumerate(SERVICES):
            if index:
                divider = QFrame()
                divider.setObjectName("divider")
                layout.addWidget(divider)
            row = ServiceRow(self._label(name="dot"), self._label(name="badge"),
                             self._label(name="url", selectable=True),
                             self._button("Open", "smallButton", partial(self.open_page, service),
                                          f"Open {name} in the browser"))
            row.dot.setFixedSize(10, 10)
            # An https://<machine>.<tailnet>.ts.net URL is long: clip it (tooltip: the full URL)
            # rather than widening the card beyond the window.
            row.url.setMinimumWidth(40)
            row.url.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(self._label(name, "serviceName"))
            text.addWidget(row.url)
            line = QHBoxLayout()
            line.setSpacing(12)
            line.addWidget(row.dot, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addLayout(text, 1)
            line.addWidget(row.badge, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(row.open_button, 0, Qt.AlignmentFlag.AlignVCenter)
            layout.addLayout(line)
            self.rows[service] = row
            # The service's other pages, as links under its row (indented past the dot).
            extra = [page for page in PAGES if page.service == service][1:]
            if extra:
                links = QHBoxLayout()
                links.setContentsMargins(16, 0, 0, 0)
                links.setSpacing(0)
                for page in extra:
                    button = self._button(page.label, "linkButton", partial(self.open_link, page),
                                          f"Open {name} · {page.label} in the browser")
                    links.addWidget(button)
                    self.page_buttons[page] = button
                links.addStretch(1)
                layout.addLayout(links)
        layout.addStretch(1)
        self.services_hint = self._label(
            "State refreshes every 4 seconds. Open uses the deployed address: the Tailscale name over HTTPS "
            "when a certificate is in use, otherwise HTTP.", "hint", wrap=True)
        layout.addWidget(self.services_hint)
        return card

    def _build_imports_card(self) -> QFrame:
        """Up to MAX_IMPORT_DIRS host directories copied into <workspace>/imported/ on Deploy.

        Compact: the filled rows plus one empty row are shown; an empty row is an unused slot.
        """
        card, layout = self._card("Imported directories")
        layout.setSpacing(8)
        self.import_rows: list[ImportRow] = []
        for index in range(MAX_IMPORT_DIRS):
            widget = QWidget()
            line = QHBoxLayout(widget)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(8)
            path = QLineEdit()
            path.setPlaceholderText("Host directory to copy (scripts, tools)…")
            browse = self._button("Browse…", "smallButton", partial(self._browse_import, index),
                                  "Choose a directory to copy into the workspace")
            name = QLineEdit()
            name.setFixedWidth(150)
            name.setToolTip(f"Name under {IMPORT_DIR}/ (default: the directory's own name). Needed when two "
                            "directories have the same name.")
            line.addWidget(path, 1)
            line.addWidget(browse)
            line.addWidget(self._label("as", "hint"))
            line.addWidget(name)
            for edit in (path, name):
                edit.textChanged.connect(self._on_imports_changed)
            layout.addWidget(widget)
            self.import_rows.append(ImportRow(widget, path, browse, name))
        self.imports_plan = self._label(name="hint", wrap=True, selectable=True)
        self.imports_error = self._label(name="errorText", wrap=True)
        self.imports_error.hide()
        layout.addWidget(self.imports_plan)
        layout.addWidget(self.imports_error)
        return card

    def _build_packages_card(self) -> QFrame:
        """The project's requirements.txt: packages preinstalled on Deploy (thebe.packages)."""
        card, layout = self._card("Python packages")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.packages_label = self._label(name="hint", wrap=True, selectable=True)
        self.packages_choose = self._button(
            "Choose requirements.txt…", "smallButton", self.choose_requirements,
            "Copy a pip requirements file into the project; the next Deploy installs its packages")
        row.addWidget(self.packages_label, 1)
        row.addWidget(self.packages_choose, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(row)
        self.packages_error = self._label(name="errorText", wrap=True)
        self.packages_error.hide()
        layout.addWidget(self.packages_error)
        return card

    def _build_actions(self) -> QHBoxLayout:
        self.banner = self._label(name="banner", wrap=True, selectable=True)
        self.banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.stop_button = self._button("Stop", "", self.stop, "docker compose stop: containers and data are kept")
        self.start_button = self._button("Start", "", self.start_or_restart,
                                         "Start the containers, or restart them when they run")
        self.deploy_button = self._button("Deploy", "primaryButton", self.deploy,
                                          "Save the settings, build the images and (re)create the containers")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.banner, 1)
        for button in (self.stop_button, self.start_button, self.deploy_button):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_output_card(self) -> QFrame:
        card, layout = self._card("Output")
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setPlaceholderText("Output of Deploy, Start/Restart and Stop appears here.")
        self.log_view.setMinimumHeight(80)
        layout.addWidget(self.log_view, 1)
        return card

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        wide = self.width() >= 860
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        if self.cards_layout.direction() != direction:
            self.cards_layout.setDirection(direction)
        self.services_hint.setVisible(wide)     # stacked cards need the height
        super().resizeEvent(event)

    # -- theme ----------------------------------------------------------------

    def apply_theme(self, mode: str | None = None) -> None:
        """Apply the oya theme; the mode follows the OS unless given."""
        if mode is None:
            dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
            mode = "dark" if dark else "light"
        self.mode = mode
        colors, font = load_theme(self.paths.theme_dir, self.settings_values.get("THEME", "amazing"))
        self.tokens = theme_tokens(colors, mode)
        palette = build_palette(self.tokens)
        app = QApplication.instance()
        if app is not None:
            app.setPalette(palette)
        self.setPalette(palette)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        # Headings like the dashboard: the bundled Orbitron for Orbitron themes, any other theme
        # font only when it is installed (Qt falls back otherwise), the default font for none.
        display = self._display_family if font == "Orbitron" else font
        self.setStyleSheet(build_stylesheet(self.tokens, display, mono))

    def _on_color_scheme_changed(self, _scheme: object) -> None:
        if not self._closing:
            self.apply_theme()

    # -- form -----------------------------------------------------------------

    def _fill_form(self, values: Mapping[str, str]) -> None:
        # auto stays auto until the checkbox is clicked; it shows whether a driver is installed.
        nvidia = values.get("NVIDIA", "auto")
        self._nvidia_auto = nvidia == "auto"
        detected = shutil.which("nvidia-smi", path=self._environ.get("PATH", os.defpath)) is not None
        self.nvidia_check.setChecked(nvidia == "1" or (self._nvidia_auto and detected))
        self._refresh_nvidia_hint()
        self.password_edit.setText(values["JUPYTER_PASSWORD"])
        self.password_edit.setCursorPosition(0)     # show the start, not a scrolled-off first dot
        self.jupyter_port.setValue(int(values["JUPYTER_PORT"]))
        self.stats_port.setValue(int(values["STATS_PORT"]))
        self.stats_check.setChecked(normalize_bool(values.get("STATS_ENABLED", "1")) == "1")
        user = values.get("STATS_USER") or "jupyter"
        self.stats_user_label.setText(f"Username: {user}")

    def _refresh_nvidia_hint(self) -> None:
        if self._nvidia_auto:
            text = "Auto: used when Docker can hand it to the containers."
        elif self.nvidia_check.isChecked():
            text = "Expected: Deploy and Start stop when it is not usable."
        else:
            text = "Off: the containers run without a GPU."
        self.nvidia_hint.setText(text)

    def _on_nvidia_clicked(self, _checked: bool) -> None:
        self._nvidia_auto = False
        self._refresh_nvidia_hint()
        self._on_form_changed()

    def form_values(self, *, commit: bool = False) -> dict[str, str]:
        """The settings the form shows. Only Deploy commits digits typed without Enter.

        Committing on a status poll would snap a half-typed port ("12" on the
        way to 12000) back to the old value under the user's fingers.
        """
        if commit:
            for spin in (self.jupyter_port, self.stats_port):
                spin.interpretText()
        values = dict(self.settings_values)    # keeps STATS_USER and THEME
        values.update(
            JUPYTER_PASSWORD=self.password_edit.text(),
            JUPYTER_PORT=str(self.jupyter_port.value()),
            STATS_ENABLED="1" if self.stats_check.isChecked() else "0",
            STATS_PORT=str(self.stats_port.value()),
            NVIDIA="auto" if self._nvidia_auto else "1" if self.nvidia_check.isChecked() else "0",
        )
        return values

    def _toggle_password(self, shown: bool) -> None:
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.password_toggle.setText("Hide" if shown else "Show")

    def _on_form_changed(self, *_args: object) -> None:
        self._validate_live()
        self._refresh_view()

    def published_ports(self) -> dict[str, set[int]]:
        """Ports each of this project's services publishes, from the last `compose ps`."""
        return {service: set(entry["ports"]) for service, entry in self.services.items()}

    def current_errors(self) -> list[str]:
        """Static validation plus port occupancy once Tailscale and Docker have answered."""
        values = self.form_values()
        errors = validate_settings(values, self.themes or None)
        # Before the first `compose ps` the project's own containers would look
        # like strangers holding the ports.
        ready = self.ts.ip and self.docker_problem is not None
        if ready and not any(check_port(values[k], k)[1] for k in ("JUPYTER_PORT", "STATS_PORT")):
            wanted = [("jupyterlab", int(values["JUPYTER_PORT"]))]
            if values["STATS_ENABLED"] == "1" and values["STATS_PORT"] != values["JUPYTER_PORT"]:
                wanted.append(("stats", int(values["STATS_PORT"])))
            errors += occupied_port_errors(self.ts.ip, wanted, self.published_ports(), bind=self._bind)
        return errors

    def _validate_live(self) -> list[str]:
        errors = self.current_errors()
        imports = self.import_problems()
        self.error_label.setText("\n".join(errors))
        self.error_label.setVisible(bool(errors))
        self.imports_error.setText("\n".join(imports))
        self.imports_error.setVisible(bool(imports))
        return errors + imports

    # -- imported directories -------------------------------------------------

    def _fill_imports(self, dirs: Iterable[ImportDir]) -> None:
        dirs = list(dirs)
        for index, row in enumerate(self.import_rows):
            item = dirs[index] if index < len(dirs) else ImportDir("")
            row.path.setText(item.path)
            row.name.setText(item.name)
        self._refresh_imports()

    def import_values(self) -> list[ImportDir]:
        """The filled import rows, in order; empty rows are unused slots."""
        return [ImportDir(row.path.text().strip(), row.name.text().strip())
                for row in self.import_rows if row.path.text().strip()]

    def import_workspace(self) -> Path:
        """The workspace the imports go to: the one the installer will mount."""
        return effective_workspace(self.config, self.paths.config_file, self._environ) or default_workspace()

    def ai_problems(self) -> list[str]:
        """The ai: section of config.yaml has no form: its problems are fixed in the file."""
        return [f"{self.paths.config_file.name}: {p} Fix it in the file and restart the builder."
                for p in self.config.ai_problems]

    def import_problems(self) -> list[str]:
        return plan_imports(self.import_values(), self.paths.config_file.parent, self.import_workspace())[1]

    def _browse_import(self, index: int, *_args: object) -> None:
        row = self.import_rows[index]
        start = row.path.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Directory to copy into the workspace", start,
                                                  QFileDialog.Option.ShowDirsOnly)
        if chosen:
            row.path.setText(chosen)

    def _on_imports_changed(self, *_args: object) -> None:
        self._refresh_imports()
        self._validate_live()

    def _refresh_imports(self) -> None:
        """Show the filled rows and the first empty one; say what will be copied where."""
        shown_empty = False
        for row in self.import_rows:
            filled = bool(row.path.text().strip() or row.name.text().strip())
            row.widget.setVisible(filled or not shown_empty)
            shown_empty = shown_empty or not filled
            default = Path(row.path.text().strip()).expanduser().name if row.path.text().strip() else "name"
            row.name.setPlaceholderText(default)
        dirs = self.import_values()
        target = self.import_workspace() / IMPORT_DIR
        if dirs:
            lines = [f"Copied on Deploy, before JupyterLab starts, into {target}/ — a plain copy: "
                     "symbolic links are skipped, nothing is deleted:"]
            lines += [f"  {item.path}  →  {IMPORT_DIR}/{item.name or Path(item.path).expanduser().name}/"
                      for item in dirs]
        else:
            lines = [f"Up to {MAX_IMPORT_DIRS} host directories can be copied into {target}/ on Deploy. "
                     "Empty rows are unused."]
        self.imports_plan.setText("\n".join(lines))

    # -- python packages (the project's requirements.txt) -------------------------

    def requirements_path(self) -> Path:
        return requirements_file(self._environ)

    def _refresh_packages(self) -> None:
        check = check_requirements(self.requirements_path())
        if not check.exists:
            text = (f"No {check.path}: Deploy installs nothing extra. Choose a pip requirements file to have "
                    "its packages (e.g. opencv-python, torch) installed into the custom packages environment "
                    "on every Deploy.")
        elif check.problems:
            text = f"{check.path} is refused, so Deploy is too:"
        elif not check.packages:
            text = f"{check.path} lists no packages: Deploy installs nothing extra."
        else:
            text = (f"{check.path}: {check.packages} package line(s), installed on every Deploy into the custom "
                    "packages environment (the Dependencies page shows them). Unchanged, they are skipped.")
        self.packages_label.setText(text)
        self.packages_error.setText("\n".join(check.problems))
        self.packages_error.setVisible(bool(check.problems))

    def choose_requirements(self, *_args: object) -> None:
        """Pick a requirements file and copy it to the project's requirements.txt."""
        if self._busy:
            return
        dest = self.requirements_path()
        start = str(dest.parent if dest.parent.is_dir() else Path.home())
        chosen, _filter = QFileDialog.getOpenFileName(self, "Requirements file to install on Deploy", start,
                                                      "Requirements (*.txt);;All files (*)")
        if not chosen:
            return
        source = Path(chosen)
        try:
            differs = dest.exists() and dest.read_bytes() != source.read_bytes()
        except OSError:
            differs = True
        if differs:
            answer = QMessageBox.question(
                self, APP_NAME,
                f"Replace {dest} with a copy of {source}?\n\nThe next Deploy installs the new list. Packages only "
                "the old list had stay installed until Reset & reinstall on the Dependencies page.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            check = copy_requirements(source, dest)
        except CopyRefused as exc:
            self.log(f"# {exc}", *(f"#   {problem}" for problem in exc.problems))
            self._banner("error", str(exc))
            QMessageBox.critical(self, APP_NAME, "\n".join([str(exc), "", *(f"• {p}" for p in exc.problems)]).strip())
        else:
            message = (f"Copied {source} to {dest} ({check.packages} package line(s)). "
                       "Deploy installs them into the custom packages environment.")
            self.log(f"# {message}")
            self._banner("success", message)
        self._refresh_packages()

    # -- view -----------------------------------------------------------------

    def _refresh_view(self) -> None:
        self._refresh_header()
        self._refresh_services()
        self._refresh_controls()

    def _refresh_header(self) -> None:
        if self.ts.ip:
            ts = ("ok", f"Tailscale {self.ts.ip}" + (f" · {self.ts.name}" if self.ts.name else ""))
        else:
            ts = ("problem", self.ts.problem) if self.ts.problem else ("pending", "Checking Tailscale…")
        if self.docker_problem is None:
            docker = ("pending", "Checking Docker…")
        else:
            docker = ("problem", self.docker_problem) if self.docker_problem else ("ok", "Docker ready")
        for dot, label, (state, text) in ((self.tailscale_dot, self.tailscale_label, ts),
                                          (self.docker_dot, self.docker_label, docker)):
            label.setText(text)
            label.setToolTip(text)
            if label.property("state") != state:
                _repolish(dot, state=state)
                _repolish(label, state=state)

    def _refresh_services(self) -> None:
        for service, row in self.rows.items():
            entry = self.services.get(service)
            if self.docker_problem:
                label, kind = "Unknown", "muted"
            else:
                label, kind = service_state(entry, disabled=service == "stats" and not self.stats_check.isChecked())
            url = service_url(self.runtime, service)
            if entry is not None and url:
                url_text, url_kind = url, "link"
            else:
                url_text = {"Disabled": "Statistics are off", "Not deployed": "Not deployed yet"}.get(label, "—")
                url_kind = "muted"
            row.badge.setText(label)
            row.url.setText(url_text)
            row.url.setToolTip(url if url_kind == "link" else "")
            for widget, value in ((row.dot, kind), (row.badge, kind), (row.url, url_kind)):
                if widget.property("kind") != value:
                    _repolish(widget, kind=value)
            running = entry is not None and entry["state"] == "running"
            row.open_button.setEnabled(running and bool(url) and not self._busy)
            for page, button in self.page_buttons.items():
                if page.service == service:
                    link = page_url(self.runtime, page)
                    button.setEnabled(running and bool(link) and not self._busy)
                    button.setToolTip(link or f"{page.label} is not deployed")

    def _any_active(self) -> bool:
        """A container runs, restarts (a crash loop) or is paused: Stop and Restart apply."""
        return any(entry["state"] in ACTIVE_STATES for entry in self.services.values())

    def _refresh_controls(self) -> None:
        idle = not self._busy
        for widget in (self.password_edit, self.password_toggle, self.jupyter_port, self.stats_check,
                       self.nvidia_check, self.deploy_button):
            widget.setEnabled(idle)
        self.stats_port.setEnabled(idle and self.stats_check.isChecked())
        for row in self.import_rows:
            for widget in (row.path, row.browse, row.name):
                widget.setEnabled(idle)
        self.packages_choose.setEnabled(idle)
        active = self._any_active()
        known = self.docker_problem == ""
        self.start_button.setText("Restart" if active else "Start")
        self.deploy_button.setText("Update" if self.services else "Deploy")
        # While the state is unknown the installer gets the final word. Start
        # also works for an installed app whose containers were removed.
        self.stop_button.setEnabled(idle and (active or not known))
        self.start_button.setEnabled(idle and (bool(self.services) or bool(self.runtime) or not known))

    def _banner(self, kind: str, text: str) -> None:
        self.banner.setText(self._mask(text))
        if self.banner.property("kind") != kind:
            _repolish(self.banner, kind=kind)

    def _mask(self, text: str) -> str:
        return mask_secrets(text, {self.password_edit.text(), *self._secrets})

    def log(self, *lines: str) -> None:
        """Append lines now, after any queued command output."""
        self._log_queue.extend(lines)
        self._flush_log()

    def _queue_log(self, lines: list[str]) -> None:
        # One append per LOG_FLUSH_MS: a flood of output cannot starve the event loop.
        self._log_queue.extend(lines)
        if not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_log(self) -> None:
        self._log_timer.stop()
        # Older lines would fall out of the block limit straight away anyway.
        lines, self._log_queue = self._log_queue[-LOG_MAX_BLOCKS:], []
        if not lines:
            return
        bar = self.log_view.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2     # do not yank a reader scrolled up
        self.log_view.appendPlainText(self._mask("\n".join(lines)))
        if follow:
            bar.setValue(bar.maximum())

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        if busy:
            self._banner("busy", text)
        else:
            self._job = ""
        self._refresh_view()

    def _read_runtime(self) -> dict[str, str]:
        try:
            return parse_settings(read_text(self.paths.runtime_env))
        except (OSError, ValueError):
            return {}

    def _child_env(self) -> dict[str, str]:
        overrides = {}
        defaults = Paths.from_environment({"HOME": self._environ.get("HOME", str(Path.home()))})
        # Tell the installer about non-default locations, so it edits what we edit.
        if "JLT_SETTINGS_FILE" in self._environ or self.paths.settings != defaults.settings:
            overrides["JLT_SETTINGS_FILE"] = str(self.paths.settings)
        if "JLT_APP_DIR" in self._environ or self.paths.runtime_env != defaults.runtime_env:
            overrides["JLT_APP_DIR"] = str(self.paths.runtime_env.parent)
        if self._environ.get("JLT_WORKSPACE_DIR"):     # relative to where the builder was started
            overrides["JLT_WORKSPACE_DIR"] = str(absolute_path(self._environ["JLT_WORKSPACE_DIR"]))
        elif workspace_dir(self.config, self.paths.config_file) is not None:
            overrides["JLT_WORKSPACE_DIR"] = str(workspace_dir(self.config, self.paths.config_file))
        env = child_environment(self._environ, {self.password_edit.text(), *self._secrets}, overrides)
        env.pop("JLT_GPU", None)      # the NVIDIA checkbox decides, not a stray export
        return env

    # -- status polling -------------------------------------------------------

    def _poll_status(self) -> None:
        if not self.status_runner.is_running():    # never overlapping
            self.refresh_status()

    def refresh_status(self) -> None:
        if self._closing:
            return
        if self.status_runner.is_running():
            self._status_again = True
            return
        self.status_runner.start(["docker", "compose", "-p", PROJECT, "ps", "-a", "--format", "json"],
                                 env=self._child_env(), timeout_ms=PROBE_TIMEOUT_MS)

    @staticmethod
    def _docker_problem(result: RunResult) -> str:
        if result.outcome == "failed":
            return "Docker is not installed (no docker command on PATH)"
        if result.outcome == "timeout":
            return f"docker compose ps did not answer within {PROBE_TIMEOUT_MS // 1000} s (is the Docker daemon stuck?)"
        text = "\n".join(result.lines).lower()
        if "permission denied" in text:
            return "Docker refused access: add your user to the docker group and log in again"
        if "is not a docker command" in text or "unknown command" in text:
            return "Docker Compose v2 is missing (the docker compose plugin)"
        if "cannot connect" in text or "daemon running" in text:
            return "The Docker daemon is not running"
        return f"docker compose ps failed (exit {result.code})"

    def _on_status_done(self, result: RunResult) -> None:
        if self._closing:
            return
        if result.outcome == "cancelled":
            if self._after_status is not None:
                self._after_status = None
                self._set_busy(False)
                self._banner("error", "Deploy cancelled.")
            return
        if result.outcome == "ok":
            self.docker_problem = ""
            self.services = parse_compose_ps("\n".join(result.lines))
        else:
            self.docker_problem = self._docker_problem(result)
            self.services = {}
        self.runtime = self._read_runtime()
        if not self._busy:
            self._validate_live()      # the own-port whitelist may have changed
        self._refresh_view()
        if self._status_again:
            # Someone asked while this ps ran; a waiting callback wants that newer answer.
            self._status_again = False
            self.refresh_status()
            return
        callback, self._after_status = self._after_status, None
        if callback is not None:
            callback()

    def _poll_tailscale(self) -> None:
        if not self._busy:
            self.refresh_tailscale()

    def refresh_tailscale(self) -> None:
        if not self._closing and not self.tailscale_runner.is_running():
            self.tailscale_runner.start(["tailscale", "status", "--json"], env=self._child_env(),
                                        timeout_ms=PROBE_TIMEOUT_MS)

    def _on_tailscale_done(self, result: RunResult) -> None:
        callback, self._after_tailscale = self._after_tailscale, None
        if self._closing:
            return
        if result.outcome == "cancelled":
            if callback is not None:
                self._set_busy(False)
                self._banner("error", "Deploy cancelled.")
            return
        if result.outcome == "failed":
            self.ts = TailscaleStatus(problem="Tailscale is not installed (no tailscale command on PATH)")
        elif result.outcome == "timeout":
            self.ts = TailscaleStatus(
                problem=f"tailscale status did not answer within {PROBE_TIMEOUT_MS // 1000} s (is tailscaled stuck?)")
        else:
            status = parse_tailscale_status("\n".join(result.lines))
            if result.outcome != "ok" and not status.ip and "could not be read" in status.problem:
                status = TailscaleStatus(problem="tailscaled is not reachable (sudo systemctl start tailscaled)")
            self.ts = status
        if not self._busy:
            self._validate_live()
        self._refresh_view()
        if callback is not None:
            callback()

    # -- actions --------------------------------------------------------------

    def deploy(self, *_args: object) -> None:
        """Validate, re-detect Tailscale, refresh `compose ps`, check ports, save .env, run `install`."""
        if self._busy:
            return
        self._refresh_packages()
        errors = (validate_settings(self.form_values(commit=True), self.themes or None) + self.import_problems()
                  + check_requirements(self.requirements_path()).problems + self.ai_problems())
        if errors:
            self._refuse(errors)
            return
        if not self._installer_present():
            return
        self._set_busy(True, "Checking Tailscale…")
        self._after_tailscale = self._deploy_tailscale_checked
        self.refresh_tailscale()     # if a probe is already running, its result is used

    def _deploy_tailscale_checked(self) -> None:
        if not self.ts.ip:
            self._set_busy(False)
            self._refuse([f"No usable Tailscale IPv4 address: {self.ts.problem or 'unknown problem'}."])
            return
        # A fresh `compose ps`: the port check must know which ports our own
        # containers hold right now.
        self._banner("busy", "Checking Docker and the ports…")
        self._after_status = self._deploy_checked
        self.refresh_status()

    def _deploy_checked(self) -> None:
        if self.docker_problem:      # the installer needs a working `docker compose` as well
            self._set_busy(False)
            self._refuse([f"Docker is not usable: {self.docker_problem}."])
            return
        errors = self.current_errors() + self.import_problems() + self.ai_problems()
        if errors:
            self._set_busy(False)
            self._refuse(errors)
            return
        if self.config_problem:
            self._set_busy(False)
            self._fail(self.config_problem)
            return
        values = self.form_values()
        config = dataclasses.replace(self.config, settings={k: values[k] for k in DEFAULTS},
                                     imports=self.import_values())
        # The installer's settings first: they carry its own checks (lines it would refuse).
        try:
            save_settings(self.paths.settings, config.settings)
        except SettingsError as exc:
            self._set_busy(False)
            if exc.problems:
                self._refuse(exc.problems)
            else:
                self._fail(str(exc))
            return
        try:
            save_config(self.paths.config_file, config)
        except OSError as exc:
            self._set_busy(False)
            self._fail(f"Could not save {self.paths.config_file}: {exc.strerror or exc}")
            return
        try:
            ai_on = save_ai_file(ai_file(self.paths.settings), config.ai)
        except OSError as exc:
            self._set_busy(False)
            self._fail(f"Could not save {ai_file(self.paths.settings)}: {exc.strerror or exc}")
            return
        self.config = config
        self.settings_values = values
        self._secrets.add(values["JUPYTER_PASSWORD"])
        self._secrets.update(p.api_key for p in (config.ai.providers if config.ai else ()))
        self.log(f"# Configuration saved to {self.paths.config_file} (mode 0600)",
                 f"# Settings saved to {self.paths.settings} (mode 0600)",
                 f"# AI: {describe(config.ai)}"
                 + (f"; settings saved to {ai_file(self.paths.settings)} (mode 0600)" if ai_on else ""))
        if config.imports:
            self._run_import()      # then install (_on_job_done)
        else:
            self._run_installer("install")

    def _run_import(self) -> None:
        """`run.py import`: the one copy implementation, in its own process so the window stays live."""
        self._job = self._command = "import"
        self._root_step = None
        self._after_certificate = False
        self._success_note = ""
        self._tail = []
        self._set_busy(True, BUSY_TEXT["import"])
        self.job_runner.start([sys.executable, str(REPO / "run.py"), "--config", str(self.paths.config_file),
                               "import"], cwd=str(REPO), env=self._child_env())

    def stop(self, *_args: object) -> None:
        if not self._busy and self._installer_present():
            self._run_installer("stop")

    def start_or_restart(self, *_args: object) -> None:
        if not self._busy and self._installer_present():
            self._run_installer("restart" if self._any_active() else "start")

    def open_page(self, service: str, *_args: object) -> None:
        self._open(service_url(self.runtime, service))

    def open_link(self, page: Page, *_args: object) -> None:
        self._open(page_url(self.runtime, page))

    def _open(self, url: str) -> None:
        if not url:
            return
        self.log(f"# Opening {url}")
        if not self._open_url(QUrl(url)):
            QMessageBox.critical(self, APP_NAME, f"Could not open a browser. Open this address yourself:\n\n{url}")

    def _refuse(self, errors: list[str]) -> None:
        self.error_label.setText("\n".join(errors))
        self.error_label.show()
        self._banner("error", "Deploy refused: fix the problems listed under the settings.")
        QMessageBox.critical(self, APP_NAME, "Deploy refused:\n\n" + "\n".join(f"• {e}" for e in errors))

    def _fail(self, detail: str) -> None:
        self.log(f"# FAILED: {detail}")
        self._banner("error", detail)
        QMessageBox.critical(self, APP_NAME, detail)

    def _installer_present(self) -> bool:
        if self.paths.installer.is_file():
            return True
        self._fail(f"The installer was not found at {self.paths.installer}.")
        return False

    def _run_installer(self, command: str, *, after_certificate: bool = False, note: str = "") -> None:
        self._job = self._command = command
        self._root_step = None
        self._after_certificate = after_certificate
        self._success_note = note
        self._tail = []
        self._set_busy(True, BUSY_TEXT_HTTPS if after_certificate else BUSY_TEXT[command])
        self.job_runner.start([self._bash, str(self.paths.installer), command],
                              cwd=str(self.paths.installer.parent), env=self._child_env())

    def _on_job_output(self, lines: list[str]) -> None:
        for line in lines:
            if self._job != "pkexec" and line.startswith("ROOT_STEP_REQUIRED"):
                step = root_step_from_line(line)
                if step is not None:
                    self._root_step = step
        self._tail.extend(line for line in lines[-20:] if line.strip() and not line.startswith("$ "))
        del self._tail[:-20]
        self._queue_log(lines)

    def _on_job_done(self, result: RunResult) -> None:
        if self._closing:
            return
        if self._job == "pkexec":
            self._on_root_step_done(result)
            return
        command = self._job
        if command == "import" and result.outcome == "ok":
            self._run_installer("install")
        elif result.outcome != "ok":
            self._job_failed(command, result)
        elif self._root_step is not None and self._after_certificate:
            # Asking again right after the step was applied could go round in circles.
            self._root_step_repeated(command, self._root_step)
        elif self._root_step is not None:
            # sudo could not prompt without a terminal; polkit asks graphically.
            self.log(f"# The host step needs root: {shlex.join(self._root_step)} (asking through polkit)")
            self._job = "pkexec"
            self._tail = []     # only the host step's own lines count for its result message
            self._banner("busy", f"Waiting for authorisation of the host step ({host_step_label(self._root_step)})…")
            self.job_runner.start(["pkexec", "/bin/bash", str(self.paths.installer), *self._root_step],
                                  cwd=str(self.paths.installer.parent), env=self._child_env())
        else:
            self._job_succeeded(command, self._success_note)

    def _on_root_step_done(self, result: RunResult) -> None:
        command, args = self._command, self._root_step or []
        label = host_step_label(args)
        if result.outcome == "ok":
            note = f"The host step ({label}) was applied."
            if "--cert" in args and not self._after_certificate:
                # The install that asked for the step ran without the certificate; only a new
                # deploy switches the containers to HTTPS. Once: that run never asks for pkexec.
                self.log("# The host step issued the HTTPS certificate. Running install once more: "
                         "the new certificate switches the services to HTTPS.")
                self._run_installer("install", after_certificate=True, note=note)
                return
            self._job_succeeded(command, note)
            return
        if result.outcome == "failed":
            reason = "pkexec is not available"
        elif result.outcome == "cancelled":
            reason = "cancelled"
        elif result.code == 126:
            reason = "authorisation dismissed"
        elif result.code == 127:
            reason = "not authorised"
        else:
            reason = f"it failed with exit code {result.code}"
        terminal = shlex.join(["sudo", str(self.paths.installer), *args])
        if ("--cert" in args and result.outcome == "exit"
                and any(CERT_ONLY_FAILURE in line for line in self._tail)):
            # host-setup still applies sysctl/firewall when only 'tailscale cert' fails.
            message = (f"{JOB_TITLES[command]} finished and the services run. The host step applied the "
                       "sysctl/firewall settings, but the HTTPS certificate could not be issued, so the services "
                       "keep using HTTP. The Output panel shows why; HTTPS certificates must be enabled in the "
                       "Tailscale admin console (DNS page).")
        else:
            message = (f"{JOB_TITLES[command]} finished and the services run, but the host step "
                       f"({label}) was not applied: {reason}.")
            if "--cert" in args:
                message += (" The services keep using HTTP until the certificate is issued. If issuing it failed, "
                            "the Output panel shows why; HTTPS certificates must be enabled in the Tailscale admin "
                            "console (DNS page).")
        self.log(f"# {message}", f"# Run it in a terminal: {terminal}")
        self._set_busy(False)
        self._banner("error", f"{message} Run in a terminal: {terminal}")
        QMessageBox.critical(self, APP_NAME, f"{message}\n\nRun it in a terminal:\n\n    {terminal}")
        self.refresh_status()

    def _root_step_repeated(self, command: str, args: list[str]) -> None:
        terminal = shlex.join(["sudo", str(self.paths.installer), *args])
        message = (f"{JOB_TITLES[command]} finished and the services run, but the installer still reports the host "
                   f"step ({host_step_label(args)}) as needed right after it was applied, so the builder does not "
                   "ask again.")
        self.log(f"# {message}", f"# Run it in a terminal to see why: {terminal}")
        self._set_busy(False)
        self._banner("error", f"{message} Run in a terminal: {terminal}")
        QMessageBox.warning(self, APP_NAME, f"{message}\n\nRun it in a terminal to see why:\n\n    {terminal}")
        self.refresh_status()

    def _job_succeeded(self, command: str, note: str = "") -> None:
        self._set_busy(False)
        self.runtime = self._read_runtime()
        if command == "stop":
            text = "Stopped. Containers and data are kept; Start brings them back."
        else:
            verb = {"install": "Deployed", "start": "Started", "restart": "Restarted"}[command]
            stats_on = "stats" in self.runtime.get("COMPOSE_PROFILES", "").split(",")
            urls = [f"{name} {service_url(self.runtime, service)}" for service, name in SERVICES
                    if service_url(self.runtime, service) and (service != "stats" or stats_on)]
            text = f"{verb}. " + "  ·  ".join(urls)
        text = " ".join(part for part in (text.strip(), note) if part)
        self.log(f"# {text}")
        self._banner("success", text.replace("  ·  ", "\n"))    # one URL per line
        self.refresh_status()

    def _job_failed(self, command: str, result: RunResult) -> None:
        self._set_busy(False)
        title = JOB_TITLES.get(command, command)
        if result.outcome == "failed":
            detail = f"Could not start {result.argv[0] if result.argv else 'the command'}: {result.error}"
        elif result.outcome == "cancelled":
            detail = "Cancelled. Containers that already started keep running."
        elif result.outcome == "crashed":
            detail = "The installer was killed before it finished."
        elif command == "import":
            detail = (f"Copying the imported directories failed (exit code {result.code}); nothing was "
                      "deployed. The Output panel names the directory and the reason.")
        else:
            detail = f"The installer exited with code {result.code}."
        self.log(f"# FAILED: {title}: {detail}")
        self._banner("error", f"{title} failed. {detail}")
        if result.outcome != "cancelled":
            last = self._mask(self._tail[-1]) if self._tail else ""
            QMessageBox.critical(self, APP_NAME, f"{title} failed.\n\n{detail}"
                                 + (f"\n\nLast output line:\n{last}" if last else "")
                                 + "\n\nThe Output panel shows the full log.")
        self.refresh_status()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if self.job_runner.is_running():
            if self._job == "pkexec":
                # Once authorised, the child runs as root and ignores our signals.
                text = ("The host step (sysctl/firewall) is waiting for authorisation or running as root.\n\n"
                        "Close anyway? A step that already runs as root cannot be interrupted, so closing "
                        "waits for it to finish.")
            else:
                text = "A command is still running.\n\nCancel it and close? Containers that already started keep running."
            answer = QMessageBox.question(
                self, APP_NAME, text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        self.status_timer.stop()
        self.tailscale_timer.stop()
        # Blocks up to the kill grace period: the child must not outlive the window.
        for runner in (self.job_runner, self.status_runner, self.tailscale_runner):
            runner.terminate_and_wait()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    # Ctrl+C in the terminal closes the window the normal way; the idle timer
    # gives the Python interpreter a chance to run the signal handler.
    signal.signal(signal.SIGINT, lambda *_: window.close())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
