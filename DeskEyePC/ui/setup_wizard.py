from __future__ import annotations

import os
import platform
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QSizePolicy, QFrame
)

from setup.driver_installer import install, check

# Paleta 
_ACCENT  = "#3b82f6"
_BG      = "#16161a"
_CARD    = "#1e1e24"
_BORDER  = "#2a2a35"
_TEXT    = "#e8e8f0"
_MUTED   = "#6b6b80"
_GREEN   = "#22c55e"
_RED     = "#ef4444"

_SYSTEM = platform.system()

_DRIVER_DESCRIPTION = {
    "Windows": (
        "<b>Unity Capture</b> — free and open-source DirectShow driver (~600 KB).<br>"
        "Creates the <b>'DeskEye'</b> or <b>'Unity Video Capture'</b> device on your system.<br>"
        "A <b>one-time administrator permission</b> (UAC) will be requested to register the driver."
    ),
    "Linux": (
        "<b>v4l2loopback</b> — Linux kernel module that creates a<br>"
        "virtual video device at <code>/dev/video10</code>.<br>"
        "The <b>sudo</b> password will be requested if the module is not already loaded."
    ),
    "Darwin": (
        "macOS requires Apple-signed system extensions.<br>"
        "Standalone installation is not possible on this OS.<br>"
        "Refer to the manual instructions below."
    ),
}

def _load_stylesheet() -> str:
    try:
        import sys
        if hasattr(sys, '_MEIPASS'):
            here = os.path.join(sys._MEIPASS, "ui")
        else:
            here = os.path.dirname(__file__)
        path = os.path.join(here, "styles.css")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


class _InstallerWorker(QThread):
    progress = pyqtSignal(str, int)       
    finished = pyqtSignal(bool, str)      

    def run(self):
        def _progress_cb(msg: str, pct: int):
            self.progress.emit(msg, pct)

        success, message = install(_progress_cb)
        self.finished.emit(success, message)



class SetupWizardDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Initial Setup · DeskEye")
        self.setMinimumWidth(520)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint
        )
        self.setStyleSheet(_load_stylesheet())

        self._worker: Optional[_InstallerWorker] = None
        self._success = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 24)
        root.setSpacing(18)

        # Cabecera
        self._lbl_title = QLabel("Initial Setup")
        self._lbl_title.setObjectName("title")

        self._lbl_subtitle = QLabel(
            "No virtual camera driver detected on this system."
        )
        self._lbl_subtitle.setObjectName("subtitle")
        self._lbl_subtitle.setWordWrap(True)

        root.addWidget(self._lbl_title)
        root.addWidget(self._lbl_subtitle)

        # Tarjeta de información del driver
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(8)

        lbl_what = QLabel("What will be installed?")
        lbl_what.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))

        desc = _DRIVER_DESCRIPTION.get(_SYSTEM, "Virtual camera driver.")
        self._lbl_desc = QLabel(desc)
        self._lbl_desc.setWordWrap(True)
        self._lbl_desc.setTextFormat(Qt.TextFormat.RichText)
        self._lbl_desc.setOpenExternalLinks(True)

        card_layout.addWidget(lbl_what)
        card_layout.addWidget(self._lbl_desc)
        root.addWidget(card)

        # Barra de progreso 
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.hide()
        root.addWidget(self._progress_bar)

        # Etiqueta de estado
        self._lbl_status = QLabel("")
        self._lbl_status.setObjectName("subtitle")
        self._lbl_status.setWordWrap(True)
        self._lbl_status.hide()
        root.addWidget(self._lbl_status)

        # Log de detalle (oculto inicialmente)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(90)
        self._log.hide()
        root.addWidget(self._log)

        root.addStretch()

        # Botones
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self._btn_skip = QPushButton("Skip installation")
        self._btn_skip.setObjectName("btn_skip")
        self._btn_skip.clicked.connect(self.reject)

        self._btn_install = QPushButton("Install driver")
        self._btn_install.clicked.connect(self._start_install)

        # En macOS no podemos instalar; mostramos solo "Cerrar"
        if _SYSTEM == "Darwin":
            self._btn_install.setText("View instructions")
            self._btn_install.clicked.disconnect()
            self._btn_install.clicked.connect(self._show_macos_instructions)

        btn_row.addWidget(self._btn_skip)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_install)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------

    def _start_install(self):
        self._btn_install.setEnabled(False)
        self._btn_skip.setEnabled(False)
        self._progress_bar.show()
        self._lbl_status.show()
        self._log.show()

        self._worker = _InstallerWorker(parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, msg: str, pct: int):
        self._progress_bar.setValue(pct)
        self._lbl_status.setText(msg)
        self._log.append(f"[{pct:3d}%] {msg}")

    def _on_finished(self, success: bool, message: str):
        self._success = success
        self._progress_bar.setValue(100 if success else self._progress_bar.value())
        self._log.append(message)

        if success:
            self._lbl_title.setText("✓  Driver installed successfully")
            self._lbl_subtitle.setText(message)
            self._lbl_status.hide()
            self._btn_install.setText("Start using DeskEye")
            self._btn_install.setEnabled(True)
            self._btn_install.clicked.disconnect()
            self._btn_install.clicked.connect(self.accept)
            self._btn_skip.hide()
        else:
            self._lbl_title.setText("Failed to install the driver")
            self._lbl_subtitle.setText(
                "You can continue without the virtual camera, "
                "install the driver manually and restart the app."
            )
            self._lbl_status.setText(
                f"<span style='color:{_RED}'>{message}</span>"
            )
            self._btn_install.setText("Retry installation")
            self._btn_install.setEnabled(True)
            self._btn_install.clicked.disconnect()
            self._btn_install.clicked.connect(self._start_install)
            self._btn_skip.setText("Continue without driver")
            self._btn_skip.setEnabled(True)

    def _show_macos_instructions(self):
        self._log.show()
        self._log.setPlainText(
            "Steps for macOS:\n"
            "1. Download the OBS Virtual Camera installer:\n"
            "   https://github.com/obsproject/obs-studio/releases/latest\n\n"
            "2. Find the .pkg file in the 'Assets' section and run it.\n\n"
            "3. Go to System Settings → Privacy and Security\n"
            "   → Camera Extensions → enable OBS Virtual Camera.\n\n"
            "4. Restart this application."
        )
        self._btn_install.setText("Close")
        self._btn_install.clicked.disconnect()
        self._btn_install.clicked.connect(self.reject)

    @property
    def driver_installed(self) -> bool:
        return self._success


def run_if_needed(parent=None) -> bool:

    available, _ = check()
    if available:
        return True

    dlg = SetupWizardDialog(parent)
    dlg.exec()
    return dlg.driver_installed
