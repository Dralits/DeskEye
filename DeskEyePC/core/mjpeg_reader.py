"""
core/mjpeg_reader.py
--------------------
Lee el stream multipart/x-mixed-replace que sirve la app Android y entrega
frames BGR (numpy arrays) listos para mostrar en Qt o enviar a pyvirtualcam.

Diseño:
  - Un hilo (threading.Thread) abre la conexión HTTP con streaming=True y
    parsea los chunks multipart, detectando el boundary y extrayendo cada
    imagen JPEG.
  - Los frames se publican a través de un callback proporcionado por el
    exterior, ejecutado en el hilo lector (el receptor debe hacer .copy()
    si necesita guardar el array).
  - Si la conexión se cae se reintenta automáticamente (con backoff
    exponencial) mientras el reader esté en marcha.
"""

import threading
import time
import logging
from typing import Callable, Optional

import cv2
import numpy as np
import requests

log = logging.getLogger(__name__)


class MjpegReader:
    """
    Conecta a un endpoint MJPEG y entrega frames en tiempo real.

    Uso:
        def on_frame(frame: np.ndarray, fps: float):
            ...

        reader = MjpegReader("http://192.168.1.42:8080/stream")
        reader.on_frame = on_frame
        reader.start()
        ...
        reader.stop()
    """

    BOUNDARY_MARKER = b"--"
    CONTENT_TYPE_JPEG = b"image/jpeg"

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

        # Callback invocado con (frame_bgr: np.ndarray, fps: float)
        self.on_frame: Optional[Callable[[np.ndarray, float], None]] = None
        # Callback invocado cuando cambia el estado de conexión
        self.on_status: Optional[Callable[[str], None]] = None
        # Callback invocado cuando ocurre un error
        self.on_error: Optional[Callable[[str], None]] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Métricas internas de FPS
        self._fps_buffer: list[float] = []
        self._last_frame_time: float = 0.0

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def start(self):
        """Arranca el hilo lector. Idempotente si ya está en marcha."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="MjpegReader")
        self._thread.start()

    def stop(self):
        """Señaliza el hilo para que pare y espera a que termine (máx. 3 s)."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    # ------------------------------------------------------------------
    # Hilo principal
    # ------------------------------------------------------------------

    def _run(self):
        retry_delay = 1.0
        max_delay = 8.0

        while not self._stop_event.is_set():
            try:
                self._notify_status("Conectando…")
                self._stream_loop()
                break
            except requests.exceptions.ConnectionError:
                msg = f"It's not reachable {self.url}. Retrying in {retry_delay:.0f}s…"
                log.warning(msg)
                self._notify_error(msg)
                self._notify_status("Without connection")
            except requests.exceptions.Timeout:
                msg = f"Timeout. Retrying in {retry_delay:.0f}s…"
                log.warning(msg)
                self._notify_error(msg)
                self._notify_status("Timeout")
            except Exception as exc:
                msg = f"Unexpected error: {exc}"
                log.exception(msg)
                self._notify_error(msg)
                self._notify_status("Error")

            # Backoff exponencial
            self._stop_event.wait(timeout=retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)

        self._notify_status("Disconnected")

    def _stream_loop(self):
        """Mantiene la conexión abierta y parsea el stream multipart."""
        with requests.get(self.url, stream=True, timeout=self.timeout) as resp:
            resp.raise_for_status()
            self._notify_status("Connected")

            # Detectar el boundary del Content-Type
            # Ejemplo: multipart/x-mixed-replace; boundary=frameboundary
            content_type = resp.headers.get("Content-Type", "")
            boundary = self._parse_boundary(content_type)
            boundary_bytes = f"--{boundary}".encode() if boundary else b"--frameboundary"

            buf = b""
            in_jpeg = False
            jpeg_start = 0

            for chunk in resp.iter_content(chunk_size=4096):
                if self._stop_event.is_set():
                    return

                buf += chunk

                while True:
                    if not in_jpeg:
                        # Buscar el start-of-JPEG (SOI: 0xFF 0xD8)
                        soi = buf.find(b"\xff\xd8")
                        if soi == -1:
                            # Podría haber media marca al final, conservar
                            buf = buf[-2:] if len(buf) >= 2 else buf
                            break
                        in_jpeg = True
                        buf = buf[soi:]    # Descartamos cabeceras MIME
                        jpeg_start = 0
                    else:
                        # Buscar el end-of-JPEG (EOI: 0xFF 0xD9)
                        eoi = buf.find(b"\xff\xd9", jpeg_start)
                        if eoi == -1:
                            # EOI no ha llegado aún, mantener todo el buffer
                            jpeg_start = max(0, len(buf) - 1)
                            break

                        jpeg_bytes = buf[:eoi + 2]
                        buf = buf[eoi + 2:]
                        in_jpeg = False
                        jpeg_start = 0

                        self._decode_and_emit(jpeg_bytes)

    @staticmethod
    def _parse_boundary(content_type: str) -> Optional[str]:
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                return part[len("boundary="):]
        return None

    def _decode_and_emit(self, jpeg_bytes: bytes):
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return

        now = time.monotonic()
        fps = self._compute_fps(now)
        self._last_frame_time = now

        if self.on_frame:
            try:
                self.on_frame(frame, fps)
            except Exception:
                log.exception("Excepción en callback on_frame")

    def _compute_fps(self, now: float) -> float:
        if self._last_frame_time > 0:
            self._fps_buffer.append(now - self._last_frame_time)
            if len(self._fps_buffer) > 30:
                self._fps_buffer.pop(0)
        if not self._fps_buffer:
            return 0.0
        avg_interval = sum(self._fps_buffer) / len(self._fps_buffer)
        return 1.0 / avg_interval if avg_interval > 0 else 0.0

    def _notify_status(self, msg: str):
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _notify_error(self, msg: str):
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass
