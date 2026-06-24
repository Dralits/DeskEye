import threading
import time
import logging
from typing import Optional
import os
import sys
from urllib.request import urlopen
from urllib.error import URLError

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QSize
from PyQt6.QtGui import QImage, QPixmap, QIcon, QColor, QFont
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QGroupBox,
    QSizePolicy, QFrame, QStatusBar, QApplication
)

from core.mjpeg_reader import MjpegReader
from core.virtual_camera import VirtualCamera

log = logging.getLogger(__name__)

PALETTE = {
    "bg_dark":     "#0d0d0f",
    "bg_panel":    "#16161a",
    "bg_card":     "#1e1e24",
    "border":      "#2a2a35",
    "accent":      "#3b82f6",   
    "accent_dim":  "#1d4ed8",
    "text_primary":"#e8e8f0",
    "text_muted":  "#6b6b80",
    "led_green":   "#22c55e",
    "led_red":     "#ef4444",
    "led_yellow":  "#eab308",
    "led_off":     "#2a2a35",
}

def _load_stylesheet() -> str:
    """
    Carga styles.css desde la carpeta ui/
    """
    try:
        if hasattr(sys, '_MEIPASS'):
            here = os.path.join(sys._MEIPASS, "ui")
        else:
            here = os.path.dirname(__file__)
        path = os.path.join(here, "styles.css")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _load_logo() -> Optional[QIcon]:
    """
    Carga el logo desde assets/logo.png (o logo.ico)
    Devuelve un QIcon o None si no existe.
    """
    try:
        if hasattr(sys, '_MEIPASS'):
            project_root = sys._MEIPASS
        else:
            # Intenta cargar desde assets/ relativo al root del proyecto
            project_root = os.path.dirname(os.path.dirname(__file__))
            
        logo_paths = [
            os.path.join(project_root, "assets", "logo.png"),
            os.path.join(project_root, "assets", "logo.ico"),
        ]
        for logo_path in logo_paths:
            if os.path.exists(logo_path):
                return QIcon(logo_path)
    except Exception:
        pass
    return None


class LedIndicator(QLabel):
    """Pequeño círculo de color estilo LED para los indicadores de estado."""

    def __init__(self, color: str = PALETTE["led_off"], parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.set_color(color)

    def set_color(self, color: str):
        self.setStyleSheet(
            f"background-color: {color}; border-radius: 5px;"
            f"border: 1px solid rgba(255,255,255,0.1);"
        )


# ---------------------------------------------------
#  Hilo de publicación de frames a la cámara virtual                          
# ---------------------------------------------------

class FrameBridgeThread(QThread):
    """
    Recibe frames del MjpegReader (hilo lector) y los envía a la VirtualCamera,
    además de emitir señales Qt para actualizar la UI sin cruzar hilos.
    """
    frame_ready   = pyqtSignal(QImage)          
    status_update = pyqtSignal(str, str)         
    fps_update    = pyqtSignal(float)
    frame_count   = pyqtSignal(int)
    error_signal  = pyqtSignal(str)

    def __init__(self, reader: MjpegReader, vcam: VirtualCamera, parent=None):
        super().__init__(parent)
        self._reader = reader
        self._vcam = vcam
        self._sent = 0

    def on_frame(self, frame_bgr: np.ndarray, fps: float):
        """Llamado desde el hilo del MjpegReader."""
        # 1. Enviar a cámara virtual
        self._vcam.push_frame(frame_bgr)
        self._sent += 1

        #TODO: poder cambiar el tamaño de la cámara virtual en caliente.
        # 2. Convertir a QImage para el preview (escalado a 640×640 para la UI)
        preview = cv2.resize(frame_bgr, (640, 640), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()

        self.frame_ready.emit(qimg)
        self.fps_update.emit(fps)
        if self._sent % 30 == 0:
            self.frame_count.emit(self._sent)

    def run(self):
        self._reader.on_frame = self.on_frame
        self._reader.start()
        self.exec()   
        self._reader.stop()


# --------------------------------------------------------------------------- #
#  Ventana principal                                                           #
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):

    def __init__(self, driver_ready: bool = True):
        super().__init__()
        self.setWindowTitle("DeskEye")
        self.setMinimumSize(760, 680)

        # Cargar y establecer el logo como icono de ventana
        logo = _load_logo()
        if logo:
            self.setWindowIcon(logo)

        self._driver_ready = driver_ready
        self._reader: Optional[MjpegReader] = None
        self._vcam: Optional[VirtualCamera] = None
        self._bridge: Optional[FrameBridgeThread] = None

        self._settings = QSettings("Dralit", "DeskEye")
        self._setup_ui()
        self._restore_settings()

        if not driver_ready:
            self._show_no_driver_banner()

    # ------------------------------------------------------------------
    # Construcción de la UI
    # ------------------------------------------------------------------

    def _setup_ui(self):
        self.setStyleSheet(_load_stylesheet())

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 12)
        root_layout.setSpacing(12)
        self.setCentralWidget(root)

        # ── Cabecera ──────────────────────────────────────────────────
        header = QHBoxLayout()
        

        title = QLabel("DeskEye")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        subtitle = QLabel("Turn your phone into a virtual camera for other apps.")
        subtitle.setStyleSheet(f"color: {PALETTE['text_muted']}; font-size: 12px;")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(subtitle)
        root_layout.addLayout(header)

        # ── Preview ───────────────────────────────────────────────────
        self.lbl_preview = QLabel()
        self.lbl_preview.setObjectName("preview")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.lbl_preview.setMinimumHeight(300)
        self._show_placeholder()
        root_layout.addWidget(self.lbl_preview, stretch=1)

        # ── Panel de control ──────────────────────────────────────────
        control_box = QGroupBox("Connection")
        ctrl_layout = QGridLayout(control_box)
        ctrl_layout.setSpacing(8)

        ctrl_layout.addWidget(QLabel("Mobile IP"), 0, 0)
        self.input_ip = QLineEdit()
        self.input_ip.setPlaceholderText("192.168.1.000")
        ctrl_layout.addWidget(self.input_ip, 0, 1)

        ctrl_layout.addWidget(QLabel("Port"), 0, 2)
        self.input_port = QLineEdit("0000")
        self.input_port.setFixedWidth(70)
        ctrl_layout.addWidget(self.input_port, 0, 3)

        ctrl_layout.addWidget(QLabel("Virtual resolution"), 1, 0)
        self.combo_res = QComboBox()
        for w, h, label in VirtualCamera.available_resolutions():
            self.combo_res.addItem(label, (w, h))
        self.combo_res.setCurrentIndex(1)   # 720p por defecto
        ctrl_layout.addWidget(self.combo_res, 1, 1)

        ctrl_layout.addWidget(QLabel("FPS virtual"), 1, 2)
        self.combo_fps = QComboBox()
        for fps in [60, 30, 24, 15]:
            self.combo_fps.addItem(f"{fps} fps", fps)
        self.combo_fps.setCurrentIndex(1)   # 30 por defecto
        ctrl_layout.addWidget(self.combo_fps, 1, 3)

        # Botones Conectar / Desconectar en columna aparte
        btn_layout = QVBoxLayout()
        self.btn_connect = QPushButton("▶  Connect")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.clicked.connect(self._on_disconnect)
        self.btn_stop.setEnabled(False)
        self.btn_toggle = QPushButton("⟲  Toggle")
        self.btn_toggle.setObjectName("btnToggle")
        self.btn_toggle.clicked.connect(self._on_toggle_camera)
        self.btn_toggle.setEnabled(False)
        btn_layout.addWidget(self.btn_connect)
        btn_layout.addWidget(self.btn_stop)
        btn_layout.addWidget(self.btn_toggle)
        ctrl_layout.addLayout(btn_layout, 0, 4, 2, 1)
        ctrl_layout.setColumnStretch(1, 1)

        root_layout.addWidget(control_box)

        # ── Panel de estado ───────────────────────────────────────────
        status_box = QGroupBox("Status")
        status_grid = QGridLayout(status_box)
        status_grid.setSpacing(8)

        # Fila 0: Stream
        status_grid.addWidget(self._muted("Stream"), 0, 0)
        row0 = QHBoxLayout()
        self.led_stream = LedIndicator()
        self.lbl_stream = QLabel("Disconnected")
        self.lbl_stream.setProperty("class", "stat_value")
        row0.addWidget(self.led_stream)
        row0.addWidget(self.lbl_stream)
        row0.addStretch()
        status_grid.addLayout(row0, 0, 1)

        status_grid.addWidget(self._muted("FPS received"), 0, 2)
        self.lbl_fps = QLabel("—")
        self.lbl_fps.setProperty("class", "stat_value")
        status_grid.addWidget(self.lbl_fps, 0, 3)

        # Fila 1: Cámara virtual
        status_grid.addWidget(self._muted("Virtual camera"), 1, 0)
        row1 = QHBoxLayout()
        self.led_vcam = LedIndicator()
        self.lbl_vcam = QLabel("Closed")
        self.lbl_vcam.setProperty("class", "stat_value")
        row1.addWidget(self.led_vcam)
        row1.addWidget(self.lbl_vcam)
        row1.addStretch()
        status_grid.addLayout(row1, 1, 1)

        status_grid.addWidget(self._muted("Driver"), 1, 2)
        self.lbl_backend = QLabel("—")
        self.lbl_backend.setProperty("class", "stat_value")
        status_grid.addWidget(self.lbl_backend, 1, 3)

        # Fila 2: Frames
        status_grid.addWidget(self._muted("Frames sent"), 2, 0)
        self.lbl_frames = QLabel("0")
        self.lbl_frames.setProperty("class", "stat_value")
        status_grid.addWidget(self.lbl_frames, 2, 1)

        status_grid.addWidget(self._muted("Stream URL"), 2, 2)
        self.lbl_url = QLabel("—")
        self.lbl_url.setProperty("class", "stat_value")
        status_grid.addWidget(self.lbl_url, 2, 3)

        status_grid.setColumnStretch(1, 1)
        status_grid.setColumnStretch(3, 1)

        root_layout.addWidget(status_box)

        # ── Barra de estado inferior ───────────────────────────────────
        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.showMessage("Ready  ·  Introduce the IP and port of the Android app and press Connect")

    @staticmethod
    def _muted(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {PALETTE['text_muted']}; font-size: 12px;")
        return lbl

    def _show_no_driver_banner(self):
        """Muestra un aviso amarillo cuando no hay driver de cámara virtual."""
        from PyQt6.QtWidgets import QMessageBox
        from setup.driver_installer import install as driver_install
        from setup.driver_installer import check as driver_check

        self.statusBar().showMessage(
            "⚠  Without virtual camera driver — install it to use the camera in other apps."
        )

        msg = QMessageBox(self)
        msg.setWindowTitle("Virtual camera driver not found")
        msg.setText(
            "No virtual camera driver was detected.\n\n"
            "You can continue and install the driver later, "
            "but you won't be able to use the camera in other apps.\n\n"
            "Do you want to try installing it now?"
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Yes)

        if msg.exec() == QMessageBox.StandardButton.Yes:
            from ui.setup_wizard import SetupWizardDialog
            wizard = SetupWizardDialog(self)
            if wizard.exec() and wizard.driver_installed:
                self._driver_ready = True
                self.statusBar().showMessage(
                    "✓  Driver installed — you can connect now."
                )

    def _show_placeholder(self):
        """Mensaje centrado cuando no hay stream activo."""
        self.lbl_preview.setText(
            "<div style='color:#3a3a48; font-size:14px; text-align:center;'>"
            "📱  No active stream<br>"
            "<span style='font-size:11px'>Enter the phone's IP and click Connect</span>"
            "</div>"
        )
        self.lbl_preview.setPixmap(QPixmap())

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------

    def _on_connect(self):
        ip   = self.input_ip.text().strip()
        port = self.input_port.text().strip()

        if not ip:
            self.statusBar().showMessage("⚠  Enter the phone's IP")
            return
        if not port.isdigit():
            self.statusBar().showMessage("⚠  Invalid port")
            return

        url = f"http://{ip}:{port}/stream"
        w, h = self.combo_res.currentData()
        fps  = self.combo_fps.currentData()

        # 1. Cámara virtual
        self._vcam = VirtualCamera(width=w, height=h, fps=float(fps))
        if not self._vcam.start():
            self.statusBar().showMessage(
                "✗  Failed to open virtual camera — "
                "Is The required driver installed? (see README)"
            )
            self._vcam = None
            return

        self.led_vcam.set_color(PALETTE["led_green"])
        self.lbl_vcam.setText(f"Open  ·  {w}×{h} @ {fps} fps")
        self.lbl_backend.setText(self._vcam.backend or "—")

        # 2. Lector MJPEG
        self._reader = MjpegReader(url)
        self._reader.on_status = self._on_stream_status
        self._reader.on_error  = lambda msg: self.statusBar().showMessage(f"⚠  {msg}")

        # 3. Hilo puente
        self._bridge = FrameBridgeThread(self._reader, self._vcam, parent=self)
        self._bridge.frame_ready.connect(self._update_preview)
        self._bridge.fps_update.connect(
            lambda fps: self.lbl_fps.setText(f"{fps:.1f} fps")
        )
        self._bridge.frame_count.connect(
            lambda n: self.lbl_frames.setText(str(n))
        )
        self._bridge.start()

        self.lbl_url.setText(url)
        self._save_settings()
        self._set_running_mode(True)

    def _on_disconnect(self):
        self._stop_all()
        self._show_placeholder()
        self.statusBar().showMessage("Disconnected")

    def _on_toggle_camera(self):
        """Envía GET /toggle al movil para cambiar cámara frontal y trasera."""
        ip   = self.input_ip.text().strip()
        port = self.input_port.text().strip()
        url  = f"http://{ip}:{port}/toggle"

        def _request():
            try:
                with urlopen(url, timeout=3) as resp:
                    log.debug("toggle camera → %s  status=%s", url, resp.status)
                self.statusBar().showMessage(f"Camera toggled")
            except URLError as exc:
                log.warning("toggle camera request failed: %s", exc)
                self.statusBar().showMessage(f"Toggle failed: {exc.reason if hasattr(exc, 'reason') else exc}")
            except Exception as exc:
                log.warning("toggle camera request failed: %s", exc)
                self.statusBar().showMessage(f"Toggle error: {exc}")

        threading.Thread(target=_request, daemon=True).start()


    def _stop_all(self):
        if self._bridge:
            self._reader.on_frame = None  
            self._bridge.quit()
            self._bridge.wait(3000)
            self._bridge = None

        if self._reader:
            self._reader.stop()
            self._reader = None

        if self._vcam:
            self._vcam.stop()
            self._vcam = None

        self.led_stream.set_color(PALETTE["led_off"])
        self.lbl_stream.setText("Disconnected")
        self.led_vcam.set_color(PALETTE["led_off"])
        self.lbl_vcam.setText("Closed")
        self.lbl_backend.setText("—")
        self.lbl_fps.setText("—")
        self.lbl_frames.setText("0")
        self.lbl_url.setText("—")
        self._set_running_mode(False)

    def _set_running_mode(self, running: bool):
        self.btn_connect.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_toggle.setEnabled(running)
        self.input_ip.setEnabled(not running)
        self.input_port.setEnabled(not running)
        self.combo_res.setEnabled(not running)
        self.combo_fps.setEnabled(not running)

    # ------------------------------------------------------------------
    # Slots Qt
    # ------------------------------------------------------------------

    def _update_preview(self, qimg: QImage):
        pix = QPixmap.fromImage(qimg)
        scaled = pix.scaled(
            self.lbl_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.lbl_preview.setPixmap(scaled)

    def _on_stream_status(self, status: str):
        self.lbl_stream.setText(status)
        status_lower = status.lower()
        if any(token in status_lower for token in ("connected", "conectado")):
            self.led_stream.set_color(PALETTE["led_green"])
            self.statusBar().showMessage(f"✓  Connected to stream  ·  {self.lbl_url.text()}")
        elif any(token in status_lower for token in ("connecting", "conectando", "reconnecting", "reconectando")):
            self.led_stream.set_color(PALETTE["led_yellow"])
        else:
            if any(token in status_lower for token in ("error", "timeout", "without connection", "sin conexión", "sin conexion", "disconnected", "desconectado")):
                self.led_stream.set_color(PALETTE["led_red"])
            else:
                self.led_stream.set_color(PALETTE["led_off"])

    # ------------------------------------------------------------------
    # Persistencia de ajustes
    # ------------------------------------------------------------------

    def _save_settings(self):
        self._settings.setValue("ip",   self.input_ip.text().strip())
        self._settings.setValue("port", self.input_port.text().strip())
        self._settings.setValue("res",  self.combo_res.currentIndex())
        self._settings.setValue("fps",  self.combo_fps.currentIndex())

    def _restore_settings(self):
        self.input_ip.setText(self._settings.value("ip",   "192.168.1.000"))
        self.input_port.setText(self._settings.value("port", "0000"))
        self.combo_res.setCurrentIndex(int(self._settings.value("res", 1)))
        self.combo_fps.setCurrentIndex(int(self._settings.value("fps", 1)))

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._stop_all()
        super().closeEvent(event)
