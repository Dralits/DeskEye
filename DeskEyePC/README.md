# DeskEye (Desktop Client)

This directory contains the source code for the DeskEye desktop client. The application connects to the MJPEG HTTP stream exposed by the mobile device, decodes it, and feeds it into a virtual webcam driver so it can be selected as a camera input in applications like Discord, Zoom, Teams, Google Meet, OBS, etc.


---

## Architecture & Code Structure

The desktop application is written in **Python**, utilizing **PyQt6** for the graphical user interface, **OpenCV** to decode the HTTP MJPEG stream, and **pyvirtualcam** to interface with the virtual webcam driver.

---

## Driver Integration

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
| Linux: `/dev/video10` does not exist | `modprobe` did not persist | Add `v4l2loopback` to `/etc/modules` |
