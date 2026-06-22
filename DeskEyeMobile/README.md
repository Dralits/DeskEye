# DeskEye (Android)

This directory contains the source code for the native Android application. It acts as an IP camera server, capturing video frames from the device's camera and streaming them over the local network.

## Installation & Usage

To install and use this application on your mobile device:
1. Go to the project's **[Releases](../../releases)** section.
2. Download the latest `.apk` file.
3. Install the APK on your Android device (ensure "Install from unknown sources" is enabled if prompted).
4. Run the app, grant camera permissions, and press **Start server**.

---

## Architecture & Code Structure

The app is built using **Kotlin**, **Jetpack Compose** (for the UI), **CameraX** (for camera capture/analysis), and **NanoHTTPD** (to host the MJPEG stream).

### Project Structure

```
app/src/main/java/com/dralit/DeskEye/
├── MainActivity.kt        # Compose UI: preview, permissions, Start/Stop
├── CameraViewModel.kt      # UI State, CameraX analyzer, server control
├── ImageUtils.kt           # ImageProxy (YUV_420_888) -> JPEG, with rotation correction
├── FrameRepository.kt      # Thread-safe rendezvous point (SharedFlow) producer/consumer
├── MjpegHttpServer.kt      # NanoHTTPD Server: serves multipart/x-mixed-replace at /stream
└── ui/theme/                # Material 3 Theme (Color, Theme, Type)
```

### Data Flow

```
CameraX (PreviewView) ──────────────► phone screen
        │
        ▼
ImageAnalysis.Analyzer (CameraViewModel)
        │  throttled to ~15 fps
        ▼
ImageUtils.imageProxyToJpeg()  →  JPEG bytes
        │
        ▼
FrameRepository (SharedFlow, replay=1, DROP_OLDEST)
        │
        ▼
MjpegHttpServer  →  multipart/x-mixed-replace  →  GET /stream
```

Each HTTP client connecting to `/stream` subscribes independently to the `SharedFlow` of `FrameRepository`, so multiple PCs can view the stream at the same time without interfering with each other (if one is slow, frames are only discarded for *that* client).

## Adjustable Parameters

These parameters can be configured in the source code to adjust the server performance:

- **Target FPS**: `CameraViewModel.TARGET_FPS` (default: 15).
- **JPEG quality**: `CameraViewModel.JPEG_QUALITY` (default: 70).
- **Capture resolution**: `Size(640, 480)` in `MainActivity.CameraPreview`.
- **Port**: Configurable from the UI itself before starting the server.

Lowering resolution/quality or FPS reduces CPU load and network bandwidth; it is the most direct way to improve smoothness on saturated WiFi networks.

## Technical Notes

- The server uses plain HTTP (not HTTPS) to optimize performance, which is appropriate for trusted local network usage.
