"""
core/virtual_camera.py
----------------------
Abstrae pyvirtualcam para exponer una API sencilla de start/stop/push_frame.

pyvirtualcam soporta distintos backends según el SO y el driver instalado;
este módulo los prueba en el orden de preferencia correcto para cada SO:

  Windows  →  'unitycapture' (Unity Capture, instalado automáticamente por el
                               wizard de configuración, sin necesidad de OBS)
               'obs'          (fallback si el usuario ya tenía OBS)

  Linux    →  'v4l2'          (v4l2loopback, instalado automáticamente)

  macOS    →  'obs'           (requiere instalación manual, ver wizard)

La resolución y el FPS de la cámara virtual pueden diferir del stream real;
el módulo reescala los frames automáticamente si es necesario.
"""

import logging
import platform
import threading
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)
_SYSTEM = platform.system()

# Importación diferida para dar un mensaje de error útil si no está instalado
try:
    import pyvirtualcam
    PYVIRTUALCAM_AVAILABLE = True
except ImportError:
    PYVIRTUALCAM_AVAILABLE = False


class VirtualCamera:
    """
    Encapsula la cámara virtual.

    Uso:
        cam = VirtualCamera(width=1280, height=720, fps=30)
        if cam.start():
            cam.push_frame(bgr_frame)
            cam.stop()
    """

    # Backends en orden de preferencia según el SO.
    # Windows: primero unitycapture (instalado por el wizard sin necesitar OBS),
    #          luego obs como fallback para quien ya lo tenga.
    # Linux:   v4l2loopback instalado automáticamente.
    # macOS:   solo obs (requiere instalación manual).
    _BACKENDS_BY_OS: dict = {
        "Windows": ["unitycapture", "obs"],
        "Linux":   ["v4l2"],
        "Darwin":  ["obs"],
    }

    @property
    def _backends(self) -> list:
        return self._BACKENDS_BY_OS.get(_SYSTEM, ["obs", "v4l2", "unitycapture"])

    def __init__(self, width: int = 720, height: int = 720, fps: float = 30.0):
        self.width = width
        self.height = height
        self.fps = fps

        self._cam: Optional["pyvirtualcam.Camera"] = None
        self._lock = threading.Lock()  # push_frame puede llamarse desde el hilo lector
        self._backend_used: Optional[str] = None

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._cam is not None

    @property
    def backend(self) -> Optional[str]:
        return self._backend_used

    def start(self) -> bool:
        """
        Intenta abrir la cámara virtual.
        Devuelve True si tuvo éxito, False en caso contrario.
        """
        if not PYVIRTUALCAM_AVAILABLE:
            log.error(
                "pyvirtualcam no está instalado. "
                "Ejecuta: pip install pyvirtualcam"
            )
            return False

        for backend in self._backends:
            try:
                cam = pyvirtualcam.Camera(
                    width=self.width,
                    height=self.height,
                    fps=self.fps,
                    backend=backend,
                    fmt=pyvirtualcam.PixelFormat.BGR,
                )
                self._cam = cam
                self._backend_used = backend
                log.info(
                    "Cámara virtual abierta [%s]: %dx%d @ %.0f fps  →  %s",
                    backend, self.width, self.height, self.fps, cam.device
                )
                return True
            except Exception as exc:
                log.debug("Backend '%s' no disponible: %s", backend, exc)

        log.error(
            "No se encontró ningún driver de cámara virtual. "
            "Instala OBS Studio (Windows/macOS) o v4l2loopback (Linux)."
        )
        return False

    def push_frame(self, frame_bgr: np.ndarray):
        """
        Envía un frame BGR a la cámara virtual.
        Reescala automáticamente si el tamaño no coincide.
        Seguro para llamar desde cualquier hilo.
        """
        if self._cam is None:
            return

        h, w = frame_bgr.shape[:2]
        if w != self.width or h != self.height:
            frame_bgr = cv2.resize(
                frame_bgr, (self.width, self.height), interpolation=cv2.INTER_LINEAR
            )

        with self._lock:
            if self._cam is not None:
                try:
                    self._cam.send(frame_bgr)
                    # sleep_until_next_frame() sincroniza con el reloj de la cámara virtual;
                    # lo omitimos aquí porque el reloj real lo marca el stream entrante.
                except Exception as exc:
                    log.warning("Error al enviar frame a la cámara virtual: %s", exc)

    def stop(self):
        """Cierra la cámara virtual."""
        with self._lock:
            if self._cam is not None:
                try:
                    self._cam.close()
                except Exception:
                    pass
                self._cam = None
                self._backend_used = None

    @staticmethod
    def is_available() -> bool:
        return PYVIRTUALCAM_AVAILABLE

    @staticmethod
    def available_resolutions() -> list[tuple[int, int, str]]:
        """Resoluciones predefinidas ordenadas de mayor a menor calidad."""
        return [
            (1920, 1920, "1080p — Square HD"),
            (1920, 1080, "1080p — Full HD"),
            (1280, 720,  "720p  — HD"),
            (854,  480,  "480p  — SD"),
            (640,  360,  "360p  — Baja"),
        ]
