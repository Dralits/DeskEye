# DeskEye

DeskEye is an open-source system that allows you to reuse your Android smartphone's camera as a high-quality virtual webcam on your computer, without relying on heavy third-party software.

The project is split into two independent components that communicate over the local network:

1. **[DeskEyeMobile](./DeskEyeMobile)**: A native Android app acting as a server, broadcasting a real-time video stream.
2. **[DeskEyePC](./DeskEyePC)**: A cross-platform desktop application that receives the stream and injects it as a virtual system camera.

---

## Installation & Usage

To install and use the applications, you do not need to compile the code. Please go to the **[Releases](../../releases)** page of this repository and download the corresponding pre-compiled files:

* 📱 **DeskEyeMobile**: Download and install the `.apk` file on your Android device.
* 💻 **DeskEyePC**: Download and run the `.exe` file on your computer.

1. **On your phone**: Install `DeskEye.apk`, open it, grant camera permissions, and click **Start server**. Note the displayed IP address and port.
2. **On your PC**: Run `DeskEye.exe`. On the first run, if you don't have the virtual webcam driver installed, it will automatically install it, so grant administrator permissions.
3. **Connect**: Enter the IP and port from the mobile app, and click **Connect** in the PC app.
4. **Enjoy**: Open The app which you want to use the webcam and select **"DeskEye"** or **"Unity Video Capture"** as your webcam!

---

## Architecture and Operation

For technical details about the architecture of each component, you can refer to their respective directories:
* **[DeskEyeMobile Architecture](./DeskEyeMobile/README.md)**
* **[DeskEyePC Architecture](./DeskEyePC/README.md)**

```
  Mobile Device (Android)                      Desktop PC (Windows / Linux)
┌───────────────────────────┐                ┌─────────────────────────┐
│     [ DeskEyeMobile ]     │                │      [ DeskEyePC ]      │
│  - Capture (CameraX)      │  MJPEG Stream  │  - HTTP Reader          │
│  - NanoHTTPD Server       │───────────────►│  - Virtual Camera       │
│  - Local broadcast        │     (HTTP)     │    Injector             │
└───────────────────────────┘                │    (pyvirtualcam)       │
                                             └────────────┬────────────┘
                                                          │
                                                          ▼
                                                    System Camera
                                               (Discord, Teams, OBS...)
```

---

## Tech Stack

* **Mobile**: Kotlin, Jetpack Compose, CameraX, NanoHTTPD (Embedded web server).
* **PC**: Python 3, PyQt6 (Graphical user interface), OpenCV (Image processing), PyVirtualCam (Virtual camera controller).
