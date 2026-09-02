# DeskEye (Android)

This directory contains the source code for the native Android application. It acts as an IP camera server, capturing video frames from the device's camera and streaming them over the local network.

---

## Architecture & Code Structure

The app is built using **Kotlin**, **Jetpack Compose** (for the UI), **CameraX** (for camera capture/analysis), and **NanoHTTPD** (to host the MJPEG stream).

Each HTTP client connecting to `/stream` subscribes independently to the `SharedFlow` of `FrameRepository`, so multiple PCs can view the stream at the same time without interfering with each other (if one is slow, frames are only discarded for *that* client).

## Technical Notes

- The server uses plain HTTP (not HTTPS) to optimize performance, which is appropriate for trusted local network usage.
