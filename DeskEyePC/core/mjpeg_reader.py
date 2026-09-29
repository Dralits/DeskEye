

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

    CHUNK_SIZE = 16 * 1024
    MAX_HEADER_BYTES = 8 * 1024
    MAX_FRAME_BYTES = 16 * 1024 * 1024   # tope de cordura para Content-Length

    # Grados horarios (los que manda Android) -> código de cv2.rotate
    _ROTATIONS = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }

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
        self._retry_delay = 1.0

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

    def stop(self, wait: bool = True):
        """
        Señaliza el hilo para que pare. Con wait=True (por defecto) espera a que
        termine (máx. 3 s); con wait=False solo lo señaliza y vuelve al instante
        (una llamada posterior a stop() hace el join).
        """
        self._stop_event.set()
        if wait and self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    # ------------------------------------------------------------------
    # Hilo principal
    # ------------------------------------------------------------------

    def _run(self):
        max_delay = 8.0
        self._retry_delay = 1.0

        while not self._stop_event.is_set():
            try:
                self._notify_status("Conectando…")
                self._stream_loop()
                if self._stop_event.is_set():
                    break
                # El servidor cerró el stream limpiamente (sin excepción):
                # antes esto terminaba el hilo para siempre. Ahora se reconecta.
                msg = f"Stream closed by the phone. Reconnecting in {self._retry_delay:.0f}s…"
                log.warning(msg)
                self._notify_error(msg)
                self._notify_status("Reconnecting")
            except requests.exceptions.ConnectionError:
                msg = f"It's not reachable {self.url}. Retrying in {self._retry_delay:.0f}s…"
                log.warning(msg)
                self._notify_error(msg)
                self._notify_status("Without connection")
            except requests.exceptions.Timeout:
                msg = f"Timeout. Retrying in {self._retry_delay:.0f}s…"
                log.warning(msg)
                self._notify_error(msg)
                self._notify_status("Timeout")
            except Exception as exc:
                msg = f"Unexpected error: {exc}"
                log.exception(msg)
                self._notify_error(msg)
                self._notify_status("Error")

            # Backoff exponencial (se reinicia a 1 s cada vez que se conecta)
            self._stop_event.wait(timeout=self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, max_delay)

        self._notify_status("Disconnected")

    def _stream_loop(self):
        """Mantiene la conexión abierta y parsea el stream multipart."""
        with requests.get(self.url, stream=True, timeout=self.timeout) as resp:
            resp.raise_for_status()
            self._retry_delay = 1.0   # conexión establecida: reiniciar backoff
            self._notify_status("Connected")

            # Detectar el boundary del Content-Type
            # Ejemplo: multipart/x-mixed-replace; boundary=frameboundary
            content_type = resp.headers.get("Content-Type", "")
            boundary = self._parse_boundary(content_type)
            boundary_bytes = f"--{boundary}".encode() if boundary else b"--frameboundary"

            # bytearray: añadir y recortar no copia todo el buffer en cada chunk
            buf = bytearray()

            for chunk in resp.iter_content(chunk_size=self.CHUNK_SIZE):
                if self._stop_event.is_set():
                    return
                if not chunk:
                    continue

                buf += chunk
                self._drain(buf, boundary_bytes)

    def _drain(self, buf: bytearray, boundary: bytes):
        """
        Extrae de `buf` todas las partes completas (cabeceras + JPEG) y emite
        cada frame. Deja en `buf` solo la parte que aún está incompleta.
        """
        while True:
            b = buf.find(boundary)
            if b == -1:
                # Sin boundary: conservar solo el final, por si llega partido.
                keep = len(boundary) - 1
                if len(buf) > keep:
                    del buf[:-keep]
                return

            hdr_start = b + len(boundary)
            hdr_end = buf.find(b"\r\n\r\n", hdr_start)
            if hdr_end == -1:
                if len(buf) - hdr_start > self.MAX_HEADER_BYTES:
                    del buf[:hdr_start]      # cabeceras absurdas: resincronizar
                elif b > 0:
                    del buf[:b]              # descartar basura anterior al boundary
                return

            length, rotation = self._parse_part_headers(bytes(buf[hdr_start:hdr_end]))
            body_start = hdr_end + 4

            if length is not None:
                if length <= 0 or length > self.MAX_FRAME_BYTES:
                    del buf[:hdr_start]      # cabecera corrupta: resincronizar
                    continue
                body_end = body_start + length
                if len(buf) < body_end:
                    if b > 0:
                        del buf[:b]
                    return                   # el JPEG aún no ha llegado entero
            else:
                # Servidor sin Content-Length: buscar el fin de JPEG (EOI: FF D9)
                eoi = buf.find(b"\xff\xd9", body_start)
                if eoi == -1:
                    if b > 0:
                        del buf[:b]
                    return
                body_end = eoi + 2

            jpeg_bytes = bytes(buf[body_start:body_end])
            del buf[:body_end]
            self._decode_and_emit(jpeg_bytes, rotation)

    @staticmethod
    def _parse_part_headers(raw: bytes):
        """Devuelve (content_length | None, rotation_degrees) de las cabeceras de una parte."""
        length: Optional[int] = None
        rotation = 0
        for line in raw.split(b"\r\n"):
            key, sep, value = line.partition(b":")
            if not sep:
                continue
            key = key.strip().lower()
            value = value.strip()
            try:
                if key == b"content-length":
                    length = int(value)
                elif key == b"x-rotation":
                    rotation = int(value) % 360
            except ValueError:
                pass
        return length, rotation

    @staticmethod
    def _parse_boundary(content_type: str) -> Optional[str]:
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                return part[len("boundary="):]
        return None

    def _decode_and_emit(self, jpeg_bytes: bytes, rotation: int = 0):
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return

        # El móvil manda el JPEG sin rotar; se orienta aquí (devuelve un array
        # nuevo). Cada frame es un array recién creado, así que el receptor
        # puede guardarlo sin hacer .copy().
        rot_code = self._ROTATIONS.get(rotation)
        if rot_code is not None:
            frame = cv2.rotate(frame, rot_code)

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
