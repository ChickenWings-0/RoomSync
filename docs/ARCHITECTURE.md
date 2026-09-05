# RoomSync Architecture

This document provides a technical breakdown of the RoomSync engine to help future contributors understand the core components and data flow.

## 1. Frontend-to-Backend WebSocket Communication

RoomSync hosts its own web dashboard using **Uvicorn**, a lightning-fast ASGI server. Instead of relying on traditional HTTP polling, the frontend and backend communicate exclusively via **WebSockets**.

- **Real-Time State:** The WebSocket connection ensures that any changes made on the dashboard (mode switches, brightness adjustments, color changes) are pushed to the backend instantly.
- **Bi-Directional Sync:** If the engine state changes internally or via a tray icon, the backend pushes the new state to all connected web clients, ensuring the dashboard is always perfectly in sync with the hardware.

## 2. Hardware Communication

Interfacing with multiple BLE LED strips concurrently on Windows requires careful connection management to avoid overwhelming the OS Bluetooth stack.

- **BLE Worker Threads:** RoomSync utilizes the `Bleak` library to maintain asynchronous, persistent connections to the LED strips. Each strip operates on its own loop/thread to ensure that latency or drops on one device do not stall the others.
- **Watchdog Connection Manager:** A dedicated watchdog monitors the health of all BLE connections. If a strip drops off, the watchdog automatically handles the reconnection logic silently in the background.
- **Staggered Booting:** During startup or mass-reconnect events, the engine intentionally staggers the initial handshake connections (configured via `stagger_delay_ms` in `config.toml`). Simultaneously opening three or more BLE handshakes can crash the Windows BLE stack; staggering spaces out the boot sequence for stable initialization.

## 3. Sampler Backends

RoomSync relies on specialized, high-performance backends to sample the PC's current state with minimal CPU overhead and zero perceptible latency.

- **Audio Sampler (WASAPI Loopback):** To achieve true zero-latency audio reactivity, RoomSync captures system audio directly from the Windows Audio Session API (WASAPI) loopback interface. This bypasses microphone inputs and captures the exact digital signal headed to your speakers, analyzing frequencies (Bass, Mids, Highs) to drive the lights.
- **Screen Sampler (MSS):** For the Screen-Sync mode, RoomSync uses the `mss` (Multiple Screen Shots) library. MSS relies on native OS APIs (such as `gdi32` on Windows) to capture screen frames incredibly fast. The engine subsamples these frames (analyzing the edges of the screen) to calculate the dominant colors and propagate them to the room lights in real-time.
