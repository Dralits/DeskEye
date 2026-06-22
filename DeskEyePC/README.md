# DeskEye (Desktop Client)

This directory contains the source code for the DeskEye desktop client. The application connects to the MJPEG HTTP stream exposed by the mobile device, decodes it, and feeds it into a virtual webcam driver so it can be selected as a camera input in applications like Discord, Zoom, Teams, Google Meet, OBS, etc.

## Installation & Usage

To install and run the desktop client:
1. Go to the project's **[Releases](../../releases)** section.
2. Download the pre-compiled executable matching your operating system (e.g., `.exe` for Windows).
3. Run the application.
4. Enter your mobile device's IP and port, and click **Connect**.
5. Select the **"Unity Video Capture"** camera (Windows) or **"Dummy video device"** / `/dev/video10` (Linux) in your meeting/streaming software.

*Note: You do not need to install OBS Studio or any other external software. The first time you launch the executable, it will guide you through an automated wizard to install the required virtual webcam driver.*

---

## Architecture & Code Structure

The desktop application is written in **Python**, utilizing **PyQt6** for the graphical user interface, **OpenCV** to decode the HTTP MJPEG stream, and **pyvirtualcam** to interface with the virtual webcam driver.

### Project Structure

```
DeskEye/
├── main.py                      # Entry point; invokes the wizard if needed
├── requirements.txt
├── README.md
├── core/
│   ├── mjpeg_reader.py          # Reads the MJPEG stream with automatic reconnection
│   └── virtual_camera.py        # pyvirtualcam wrapper; selects backend by OS
├── setup/
│   └── driver_installer.py      # Automatic detection and installation of the driver
└── ui/
    ├── main_window.py           # PyQt6 main window
    └── setup_wizard.py          # First-time setup wizard
```

### Driver Integration

Depending on your operating system, the application installs and interfaces with different virtual webcam drivers:

| OS | Automatically installed driver | Requires |
|---|---|---|
| **Windows** | Unity Capture (~600 KB, DirectShow DLL) | A single administrator permission (UAC) |
| **Linux** | v4l2loopback (kernel module) | Sudo password |
| **macOS** | Not automatable (Apple signature required) | Manual instructions in the wizard |

Once the driver is successfully installed, the configuration wizard will not appear again.

---

## Troubleshooting

| Symptom | Cause | Solution |
|---|---|---|
| Wizard does not appear but Discord does not see the camera | Driver installed but `pyvirtualcam` fails | Restart the PC |
| "Unity Video Capture" does not appear in Discord | DLL not registered | Run the wizard again from the app menu |
| Very low FPS | Saturated WiFi network | Reduce resolution or FPS in the Android app |
| Linux: `/dev/video10` does not exist | `modprobe` did not persist | Add `v4l2loopback` to `/etc/modules` |
