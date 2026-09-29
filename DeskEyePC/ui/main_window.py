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
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QSettings, QSize
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
    "text_muted":  "#e9e0cf",
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


# ----------------------------------
#  Mandar frames a la camara virtual                        
# ----------------------------------

class FrameBridgeThread(QThread):
    """
    Desacopla el hilo lector (red) del envío a la cámara virtual y de la UI.

    Antes, TODO el trabajo por frame se hacía dentro del callback del lector:
    reescalados a 1080p, conversión de color, copia a QImage y una señal Qt por
    frame hacia la GUI. Si algo iba lento se dejaba de leer el socket y, peor,
    las señales se acumulaban sin límite en la cola de la GUI (memoria + UI
    congelada). Ahora:

      * Hilo lector (MjpegReader): decodifica y solo deja el ÚLTIMO frame en
        `_latest`. Nunca espera a nadie.
      * Este hilo (QThread): toma el último frame, lo manda a la cámara virtual
        y, como mucho ~20 veces/s, genera una miniatura de preview.
        Si va lento, se saltan frames (sin acumular cola ni latencia).
      * Hilo de la GUI: un QTimer pide la miniatura con take_preview()
        (modelo "pull": nunca hay más de una imagen pendiente).
    """
    fps_update  = pyqtSignal(float)
    frame_count = pyqtSignal(int)

    PREVIEW_MAX_SIDE = 640          # lado mayor de la miniatura del preview (px)
    PREVIEW_INTERVAL = 1.0 / 20.0   # como máximo 20 miniaturas por segundo
    STATS_INTERVAL   = 0.5          # cada cuánto se emite el FPS a la UI (s)

    def __init__(self, reader: MjpegReader, vcam: VirtualCamera, parent=None):
        super().__init__(parent)
        self._reader = reader
        self._vcam = vcam
        self._sent = 0

        self._lock = threading.Lock()
        self._latest: Optional[tuple] = None      # (frame_bgr, fps)
        self._preview: Optional[QImage] = None
        self._new_frame = threading.Event()
        self._stop_evt = threading.Event()

    # -- hilo lector -------------------------------------------------------

    def on_frame(self, frame_bgr: np.ndarray, fps: float):
        """Llamado desde el hilo del MjpegReader: solo guarda el último frame."""
        with self._lock:
            self._latest = (frame_bgr, fps)
        self._new_frame.set()

    # -- hilo de la GUI ----------------------------------------------------

    def take_preview(self) -> Optional[QImage]:
        """Devuelve la última miniatura aún no mostrada (o None). Llamar desde la GUI."""
        with self._lock:
            img, self._preview = self._preview, None
        return img

    def stop(self):
        """Pide parar al hilo y al lector, sin bloquear. Después, hacer wait()."""
        self._stop_evt.set()
        self._new_frame.set()
        self._reader.stop(wait=False)

    # -- este hilo ---------------------------------------------------------

    def _preview_size(self) -> tuple:
        # Mismo aspecto que la cámara virtual (es lo que verá Discord/Teams).
        w, h = self._vcam.width, self._vcam.height
        scale = min(1.0, self.PREVIEW_MAX_SIDE / max(w, h))
        return max(1, int(w * scale)), max(1, int(h * scale))

    def _store_preview(self, frame_bgr: np.ndarray, pw: int, ph: int):
        small = cv2.resize(frame_bgr, (pw, ph), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
        with self._lock:
            self._preview = qimg   # sobrescribe: solo importa la más reciente

    def run(self):
        self._reader.on_frame = self.on_frame
        self._reader.start()

        pw, ph = self._preview_size()
        last_preview = 0.0
        last_stats = 0.0
        try:
            while not self._stop_evt.is_set():
                if not self._new_frame.wait(timeout=0.2):
                    continue
                self._new_frame.clear()

                with self._lock:
                    item = self._latest
                if item is None:
                    continue
                frame, fps = item

                # 1. Cámara virtual (incluye el reescalado a la resolución elegida)
                self._vcam.push_frame(frame)
                self._sent += 1

                now = time.monotonic()

                # 2. Miniatura para el preview (limitada en frecuencia y tamaño)
                if now - last_preview >= self.PREVIEW_INTERVAL:
                    last_preview = now
                    self._store_preview(frame, pw, ph)

                # 3. Estadísticas (poco frecuentes)
                if now - last_stats >= self.STATS_INTERVAL:
                    last_stats = now
                    self.fps_update.emit(fps)
                if self._sent % 30 == 0:
                    self.frame_count.emit(self._sent)
        finally:
            self._reader.on_frame = None
            # Sin join: si el móvil se ha quedado mudo, el lector sigue bloqueado en
            # el socket hasta su timeout (5 s). Es un hilo daemon y muere solo.
            self._reader.stop(wait=False)


# --------------------------------------------------------------------------- #
#  Ventana principal                                                           #
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):

    # El MjpegReader notifica desde su propio hilo. Los widgets de Qt solo se
    # pueden tocar desde el hilo de la GUI, así que el reader emite estas señales
    # (thread-safe, entrega encolada) en vez de llamar directamente a los widgets.
    stream_status_changed = pyqtSignal(str)
    stream_error = pyqtSignal(str)

    def __init__(self, driver_ready: bool = True):
        super().__init__()
        self.stream_status_changed.connect(self._on_stream_status)
        self.stream_error.connect(lambda msg: self.statusBar().showMessage(f"⚠  {msg}"))
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

        # Pide al hilo puente la miniatura más reciente (modelo "pull", ver FrameBridgeThread)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(50)          # ~20 fps de preview
        self._preview_timer.timeout.connect(self._poll_preview)

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
        root.setObjectName("centralRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 12)
        root_layout.setSpacing(12)
        self.setCentralWidget(root)

        # ── Cabecera ──────────────────────────────────────────────────
        header = QHBoxLayout()
        

        title = QLabel("DeskEye")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        subtitle = QLabel("Turn your phone into a virtual camera")
        subtitle.setStyleSheet(f"color: {PALETTE['text_muted']}; font-size: 12px;")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(subtitle)
        root_layout.addLayout(header)

        # ── Preview ───────────────────────────────────────────────────
        preview_container = QWidget()
        preview_layout = QGridLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)

        self.lbl_preview = QLabel()
        self.lbl_preview.setObjectName("preview")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.lbl_preview.setMinimumHeight(300)
        preview_layout.addWidget(self.lbl_preview, 0, 0)

        self.btn_toggle = QPushButton("⟲")
        self.btn_toggle.setObjectName("btnToggle")
        self.btn_toggle.clicked.connect(self._on_toggle_camera)
        self.btn_toggle.setEnabled(False)
        self.btn_toggle.setMaximumWidth(96)
        self.btn_toggle.setToolTip("Switch camera (front/back)")

        self.btn_lturn = QPushButton("⤷")
        self.btn_lturn.setObjectName("btnlturn")
        self.btn_lturn.clicked.connect(self._on_lturn_camera)
        self.btn_lturn.setEnabled(False)
        self.btn_lturn.setMaximumWidth(96)
        self.btn_lturn.setToolTip("Rotate camera 90° left")

        self.btn_rturn = QPushButton("⤶")
        self.btn_rturn.setObjectName("btnrturn")
        self.btn_rturn.clicked.connect(self._on_rturn_camera)
        self.btn_rturn.setEnabled(False)
        self.btn_rturn.setMaximumWidth(96)
        self.btn_rturn.setToolTip("Rotate camera 90° right")

        camcontrol = QHBoxLayout()
        camcontrol.addWidget(self.btn_lturn)
        camcontrol.addWidget(self.btn_rturn)
        camcontrol.addWidget(self.btn_toggle)
        preview_layout.addLayout(
            camcontrol,
            0, 0,
            alignment=Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight
        )

        self._show_placeholder()
        root_layout.addWidget(preview_container, stretch=1)

        # ── Panel de control ──────────────────────────────────────────
        control_box = QGroupBox("Connection")
        control_box.setObjectName("controlBox")
        ctrl_layout = QGridLayout(control_box)
        ctrl_layout.setContentsMargins(0, 4, 0, 0)
        ctrl_layout.setSpacing(8)
      

        lbl_ip = QLabel("Phone IP")
        lbl_ip.setFixedWidth(72)
        ctrl_layout.addWidget(lbl_ip, 0, 0)
        self.input_ip = QLineEdit()
        self.input_ip.setObjectName("inputIp")
        self.input_ip.setPlaceholderText("192.168.1.000")
        ctrl_layout.addWidget(self.input_ip, 0, 1)

        lbl_port = QLabel("Phone Port")
        lbl_port.setFixedWidth(72)
        ctrl_layout.addWidget(lbl_port, 0, 2)
        self.input_port = QLineEdit("0000")
        self.input_port.setFixedWidth(70)
        ctrl_layout.addWidget(self.input_port, 0, 3)

        lbl_res = QLabel("Resolution")
        lbl_res.setFixedWidth(72)
        ctrl_layout.addWidget(lbl_res, 1, 0)
        self.combo_res = QComboBox()
        for w, h, label in VirtualCamera.available_resolutions():
            self.combo_res.addItem(label, (w, h))
        self.combo_res.setCurrentIndex(1)   # 720p por defecto
        ctrl_layout.addWidget(self.combo_res, 1, 1)


        # Botones Conectar / Desconectar en columna aparte
        btn_layout = QVBoxLayout()
        self.btn_connect = QPushButton("▶  Start")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_connect.setMaximumWidth(110)
        self.btn_stop = QPushButton("■  Stop")
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.clicked.connect(self._on_disconnect)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setMaximumWidth(110)
        btn_layout.addWidget(self.btn_connect)
        btn_layout.addWidget(self.btn_stop)
        ctrl_layout.addLayout(btn_layout, 0, 4, 2, 1)
        ctrl_layout.setColumnStretch(1, 0)
        ctrl_layout.setColumnStretch(4, 1)

        root_layout.addWidget(control_box)

        # ── Panel de estado ───────────────────────────────────────────
        status_box = QGroupBox("Status")
        status_box.setObjectName("statusBox")
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
        sb.showMessage("Ready  ·  Introduce the IP and port shown in the Phone app and press Connect")

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
            "No active stream<br>"
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

        # 1. Cámara virtual
        self._vcam = VirtualCamera(width=w, height=h, fps=30)
        if not self._vcam.start():
            self.statusBar().showMessage(
                "✗  Failed to open virtual camera — "
                "Is The required driver installed? (see README)"
            )
            self._vcam = None
            return

        self.led_vcam.set_color(PALETTE["led_green"])
        self.lbl_vcam.setText(f"Open  ·  {w}×{h}")
        self.lbl_backend.setText(self._vcam.backend or "—")

        # 2. Lector MJPEG
        self._reader = MjpegReader(url)
        self._reader.on_status = self.stream_status_changed.emit
        self._reader.on_error  = self.stream_error.emit

        # 3. Hilo puente
        self._bridge = FrameBridgeThread(self._reader, self._vcam, parent=self)
        self._bridge.fps_update.connect(
            lambda fps: self.lbl_fps.setText(f"{fps:.1f} fps")
        )
        self._bridge.frame_count.connect(
            lambda n: self.lbl_frames.setText(str(n))
        )
        self._bridge.start()
        self._preview_timer.start()

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


    def _on_rturn_camera(self):
        """Envía GET /rturn al movil para rotar la imagen de la cámara 90º."""
        ip   = self.input_ip.text().strip()
        port = self.input_port.text().strip()
        url  = f"http://{ip}:{port}/rturn"

        def _request():
            try:
                with urlopen(url, timeout=3) as resp:
                    log.debug("rturn camera → %s  status=%s", url, resp.status)
                self.statusBar().showMessage("Camera rotated 90° right")
            except URLError as exc:
                log.warning("rturn camera request failed: %s", exc)
                self.statusBar().showMessage(f"Rotate failed: {exc.reason if hasattr(exc, 'reason') else exc}")
            except Exception as exc:
                log.warning("rturn camera request failed: %s", exc)
                self.statusBar().showMessage(f"Rotate error: {exc}")

        threading.Thread(target=_request, daemon=True).start()

    def _on_lturn_camera(self):
        """Envía GET /lturn al movil para rotar la imagen de la cámara -90º."""
        ip   = self.input_ip.text().strip()
        port = self.input_port.text().strip()
        url  = f"http://{ip}:{port}/lturn"

        def _request():
            try:
                with urlopen(url, timeout=3) as resp:
                    log.debug("lturn camera → %s  status=%s", url, resp.status)
                self.statusBar().showMessage("Camera rotated 90° left")
            except URLError as exc:
                log.warning("lturn camera request failed: %s", exc)
                self.statusBar().showMessage(f"Rotate failed: {exc.reason if hasattr(exc, 'reason') else exc}")
            except Exception as exc:
                log.warning("lturn camera request failed: %s", exc)
                self.statusBar().showMessage(f"Rotate error: {exc}")

        threading.Thread(target=_request, daemon=True).start()


    def _stop_all(self):
        self._preview_timer.stop()

        if self._reader:
            # Evita que el "Disconnected" final del reader llegue tarde y pise la UI ya reseteada.
            self._reader.on_status = None
            self._reader.on_error = None

        if self._bridge:
            self._bridge.stop()
            self._bridge.wait(2000)   # el bucle del puente sale en <0,2 s
            self._bridge = None

        if self._reader:
            self._reader.stop(wait=False)   # no bloquear la GUI esperando a la red
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
        self.btn_rturn.setEnabled(running)
        self.btn_lturn.setEnabled(running)
        self.input_ip.setEnabled(not running)
        self.input_port.setEnabled(not running)
        self.combo_res.setEnabled(not running)

    # ------------------------------------------------------------------
    # Slots Qt
    # ------------------------------------------------------------------

    def _poll_preview(self):
        if self._bridge is None:
            return
        qimg = self._bridge.take_preview()
        if qimg is not None:
            self._update_preview(qimg)

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
        # OJO: "disconnected" contiene "connected"; comprobarlo primero.
        if any(token in status_lower for token in ("disconnected", "desconectado")):
            self.led_stream.set_color(PALETTE["led_red"])
        elif any(token in status_lower for token in ("connected", "conectado")):
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

    def _restore_settings(self):
        self.input_ip.setText(self._settings.value("ip",   "192.168.1.000"))
        self.input_port.setText(self._settings.value("port", "0000"))
        self.combo_res.setCurrentIndex(int(self._settings.value("res", 1)))

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._stop_all()
        super().closeEvent(event)
