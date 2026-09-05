# RoomSync

**RoomSync** is a high-performance, background service designed to synchronize BLE-controlled LED room lights with your PC's screen content and system audio. It provides an immersive, zero-latency lighting experience controlled entirely through a persistent custom web dashboard.

## Key Features

- 🎵 **Zero-Latency Audio-Reactive Mode**: Real-time audio analysis via WASAPI loopback for instant light reactions to music and sound effects.
- 📺 **Screen-Sync**: Dynamic color matching that samples your screen content to extend your display's atmosphere into your room.
- 🎛️ **Persistent Custom Web Dashboard**: A sleek, real-time web interface for controlling modes, brightness, and colors.
- 👻 **Hidden Background Execution**: Runs completely invisibly as a Windows background service—no stray console windows or taskbar clutter.

## Tech Stack

RoomSync is built for performance and reliability using:

- **Python**: Core engine and logic.
- **Uvicorn**: High-performance ASGI server for hosting the backend.
- **WebSockets**: Real-time, bidirectional communication between the dashboard and the engine.
- **Bleak**: Asynchronous Bluetooth Low Energy (BLE) communication.
- **HTML / CSS / JS**: Vanilla web technologies for a lightweight, responsive frontend dashboard.

## Quick Start

To get RoomSync up and running, you'll need to set up a Python virtual environment and configure your Windows Task Scheduler for the hidden background service.

For complete, step-by-step instructions, please read the full [Setup and Autostart Guide](docs/SETUP_AND_AUTOSTART.md).
