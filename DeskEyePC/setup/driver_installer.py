"""
setup/driver_installer.py
--------------------------
Detecta si hay un driver de cámara virtual disponible e instala el más
adecuado para el sistema operativo actual, sin intervención manual del usuario.

Drivers utilizados:
  Windows  →  Unity Capture (DLL DirectShow, ~600 KB, sin instalador)
               https://github.com/schellingb/UnityCapture
                             Crea el dispositivo "DeskEye" en el sistema.
               Requiere una única elevación UAC para registrar la DLL.

  Linux    →  v4l2loopback (módulo del kernel)
               Instalación:
                 sudo apt install v4l2loopback-dkms   (una sola vez)
                 sudo modprobe v4l2loopback            (cada arranque,
                                                        o configurar en /etc/modules)

  macOS    →  No hay driver autónomo libre bien mantenido que no necesite
               firma de Apple. Se ofrece al usuario el enlace al plugin
               standalone de OBS Virtual Camera (~2 MB).
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import subprocess
import sys
import time
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

WINDOWS_CAPTURE_NAME = "DeskEye"

def _unity_capture_filename() -> str:
    """Devuelve el nombre del DLL correcto para la arquitectura actual."""
    return "UnityCaptureFilter64.dll" if sys.maxsize > 2**32 else "UnityCaptureFilter32.dll"


def _unity_capture_url() -> str:
    """Devuelve la URL estable del DLL publicado en el repositorio."""
    return (
        "https://github.com/schellingb/UnityCapture/raw/refs/heads/master/Install/"
        f"{_unity_capture_filename()}"
    )


def _unity_capture_local() -> Path:
    """Ruta local donde guardaremos el DLL para que persista entre reinicios."""
    return Path(os.getenv("APPDATA", "")) / "DeskEye" / _unity_capture_filename()

ProgressCallback = Callable[[str, int], None]   # (mensaje, porcentaje 0-100)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def check() -> tuple[bool, str]:
    """
    Comprueba si hay algún backend de cámara virtual disponible.

    Devuelve (disponible: bool, mensaje: str).
    """
    try:
        import pyvirtualcam
        with pyvirtualcam.Camera(
            width=320, height=240, fps=15,
            fmt=pyvirtualcam.PixelFormat.BGR
        ):
            pass
        return True, "Driver detectado y operativo."
    except Exception as exc:
        return False, str(exc)


def install(progress: Optional[ProgressCallback] = None) -> tuple[bool, str]:
    """
    Instala el driver más adecuado para el SO actual.

    Devuelve (éxito: bool, mensaje: str).
    """
    system = platform.system()
    if system == "Windows":
        return _install_windows(progress)
    if system == "Linux":
        return _install_linux(progress)
    if system == "Darwin":
        return _install_macos()
    return False, f"SO no soportado: {system}"


def uninstall_windows() -> tuple[bool, str]:
    """Elimina el DLL de Unity Capture del sistema (Windows)."""
    unity_capture_local = _unity_capture_local()
    if not unity_capture_local.exists():
        return True, "No hay driver instalado."
    try:
        _run_as_admin("regsvr32", ["/u", "/s", str(unity_capture_local)])
        unity_capture_local.unlink(missing_ok=True)
        return True, "Driver eliminado correctamente."
    except Exception as exc:
        return False, f"Error al desinstalar: {exc}"


# ---------------------------------------------------------------------------
# Windows: Unity Capture
# ---------------------------------------------------------------------------

def _install_windows(progress: Optional[ProgressCallback]) -> tuple[bool, str]:
    _progress(progress, "Checking for existing driver…", 5)

    available, _ = check()
    if available:
        return True, "There is an existing driver installed."

    # 1. Descargar el DLL si no lo tenemos aún
    _progress(progress, "Downloading Unity Capture (~600 KB)…", 15)
    try:
        unity_capture_local = _unity_capture_local()
        unity_capture_local.parent.mkdir(parents=True, exist_ok=True)
        _download_with_progress(_unity_capture_url(), unity_capture_local, progress, 15, 65)
    except Exception as exc:
        return False, f"Error al descargar el driver: {exc}"

    # 2. Registrar la DLL con regsvr32 (requiere admin → UAC)
    _progress(progress, "Registering driver (admin permission will be requested)…", 70)
    try:
        _run_as_admin(
            "regsvr32",
            ["/s", f'/i:UnityCaptureName={WINDOWS_CAPTURE_NAME}', str(_unity_capture_local())],
        )
    except PermissionError:
        return False, "The user canceled the permission elevation."
    except subprocess.CalledProcessError as exc:
        return False, f"regsvr32 failed (code {exc.returncode}). Restart and try again."
    except Exception as exc:
        return False, f"Error al registrar el driver: {exc}"

    # 3. Verificar que pyvirtualcam lo ve ahora
    _progress(progress, "Verifying installation…", 90)
    available, msg = _check_with_retry(retries=6, delay_seconds=0.25)
    if available:
        _progress(progress, "Driver installed correctly.", 100)
        return True, f"Unity Capture installed. The device '{WINDOWS_CAPTURE_NAME}' is now available in other apps."
    else:
        return False, (
            "Driver registered, but pyvirtualcam is not detecting it yet. "
            "Try retrying the installation.\n\nDetails: " + msg
        )


def _check_with_retry(retries: int = 6, delay_seconds: float = 0.25) -> tuple[bool, str]:
    """Reintenta la detección del driver durante unos instantes.

    Esto cubre el caso en el que regsvr32 termina correctamente pero el backend
    tarda un momento en quedar visible para pyvirtualcam.
    """
    last_msg = ""
    for attempt in range(retries):
        available, msg = check()
        if available:
            return True, msg
        last_msg = msg
        if attempt + 1 < retries:
            time.sleep(delay_seconds)
    return False, last_msg


def _run_as_admin(exe: str, args: list[str]):
    """
    Ejecuta `exe args` con privilegios de administrador en Windows.
    o si tiene privilegios de administrador, lo ejecuta directamente.
    Si no, usa ShellExecute con 'runas' para provocar el prompt UAC.
    Lanza PermissionError si el usuario cancela.
    """
    if _is_admin():
        subprocess.run([exe] + args, check=True)
        return

    # ShellExecuteW devuelve el HINSTANCE (>32 = OK, ≤32 = error/cancelación)
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, " ".join(f'"{a}"' for a in args), None, 1
    )
    if ret <= 32:
        raise PermissionError(f"ShellExecuteW devolvió {ret} (usuario canceló o sin permisos)")


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Linux: v4l2loopback
# ---------------------------------------------------------------------------

def _install_linux(progress: Optional[ProgressCallback]) -> tuple[bool, str]:
    _progress(progress, "Comprobando v4l2loopback…", 10)

    # Comprobar si el módulo ya está cargado
    try:
        result = subprocess.run(
            ["lsmod"], capture_output=True, text=True, check=True
        )
        if "v4l2loopback" in result.stdout:
            _progress(progress, "v4l2loopback ya está cargado.", 100)
            return True, "v4l2loopback ya está activo."
    except FileNotFoundError:
        pass

    # Intentar cargarlo con modprobe (puede requerir sudo)
    _progress(progress, "Cargando módulo v4l2loopback…", 40)
    try:
        subprocess.run(
            ["sudo", "modprobe", "v4l2loopback", "devices=1", "video_nr=10"],
            check=True, timeout=15
        )
        _progress(progress, "Módulo cargado.", 80)
    except subprocess.CalledProcessError:
        return False, (
            "No se pudo cargar v4l2loopback automáticamente.\n\n"
            "Ejecuta manualmente:\n"
            "  sudo apt install v4l2loopback-dkms\n"
            "  sudo modprobe v4l2loopback"
        )
    except FileNotFoundError:
        return False, "sudo o modprobe no encontrados. Instala v4l2loopback manualmente."

    available, msg = check()
    if available:
        _progress(progress, "v4l2loopback activo y operativo.", 100)
        return True, "v4l2loopback instalado y listo."
    return False, "Módulo cargado pero pyvirtualcam no lo detecta: " + msg


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

def _install_macos() -> tuple[bool, str]:
    # En macOS, los System Extensions necesitan firma de Apple Developer.
    # No podemos instalar un driver firmado de forma autónoma.
    # Dirigimos al usuario al plugin standalone de OBS (2 MB, sin OBS Studio completo).
    return False, (
        "macOS requiere una extensión de sistema firmada por Apple para la cámara virtual.\n\n"
        "Instala el plugin standalone gratuito OBS Virtual Camera (~2 MB):\n"
        "https://github.com/obsproject/obs-studio/releases/latest\n\n"
        "Descarga el archivo .pkg de la sección 'Assets', instálalo y concede\n"
        "el permiso en Ajustes del Sistema → Privacidad → Extensiones de cámara."
    )


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _download_with_progress(
    url: str,
    dest: Path,
    progress: Optional[ProgressCallback],
    pct_start: int,
    pct_end: int,
):
    """Descarga url a dest, actualizando el progreso entre pct_start y pct_end."""
    def _reporthook(block_num, block_size, total_size):
        if total_size > 0 and progress:
            downloaded = block_num * block_size
            pct = pct_start + int((downloaded / total_size) * (pct_end - pct_start))
            pct = min(pct, pct_end)
            progress(f"Descargando… {downloaded // 1024} KB / {total_size // 1024} KB", pct)

    urllib.request.urlretrieve(url, dest, reporthook=_reporthook)


def _progress(cb: Optional[ProgressCallback], msg: str, pct: int):
    log.info("[%3d%%] %s", pct, msg)
    if cb:
        cb(msg, pct)
