"""
La aplicación es autónoma: si no detecta ningún driver de cámara virtual,
muestra un asistente de configuración que instala automáticamente.
"""

import sys
import logging

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from ui.main_window import MainWindow
from ui.setup_wizard import run_if_needed


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    configure_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("DeskEye")
    app.setOrganizationName("Dralit")

    # ── Primer arranque: comprobar/instalar driver de cámara virtual 
    driver_ready = run_if_needed()

    # La ventana principal sigue funcionando aunque no haya driver
    # Se ve la preview pero sin cámara virtual activa.

    window = MainWindow(driver_ready=driver_ready)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
