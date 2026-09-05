# Setup and Autostart Guide

This guide will walk you through installing RoomSync and configuring it to run silently in the background every time you log into Windows.

## 1. Installation

First, clone the repository to your preferred location (e.g., `C:\ROOMLIGHTS-APP\RoomSync`).

### Create and Activate a Virtual Environment
It is highly recommended to install dependencies inside a virtual environment to avoid conflicts.

Open a terminal in the project root and run:
```cmd
python -m venv .venv
.\.venv\Scripts\activate
```

### Install Dependencies
With the virtual environment activated, install the required packages:
```cmd
pip install -r requirements.txt
```

### Configuration
RoomSync uses a `config.toml` file to manage MAC addresses, preferences, and tuning parameters.

1. Copy the example configuration file:
   ```cmd
   copy config.example.toml config.toml
   ```
2. Open `config.toml` in a text editor and replace the placeholder MAC addresses (`XX:XX:XX:XX:XX:01`) under `[[ble.strips]]` with the actual MAC addresses of your BLE LED strips.

---

## 2. Windows Background Service Setup

For the best experience, RoomSync should run silently in the background. Running Python scripts directly or using `pythonw.exe` can sometimes lead to console flashing or lingering errors. Instead, we use a VBScript wrapper combined with the Windows Task Scheduler.

### The VBScript Wrapper (`run-hidden.vbs`)
Included in the root directory is a file named `run-hidden.vbs`. This script launches the RoomSync engine via the virtual environment's Python executable and pipes all output to `NUL`, ensuring no console window ever appears.

*Note: If you moved the project directory, you must open `run-hidden.vbs` in a text editor and update the absolute path to point to your `.venv\Scripts\python.exe` and `main.py`.*

### Configuring Task Scheduler
To start RoomSync automatically upon login:

1. Press `Win + R`, type `taskschd.msc`, and hit Enter to open the **Task Scheduler**.
2. In the right pane, click **Create Basic Task...**
3. **Name**: Enter `RoomSync Background Engine` (or similar) and click Next.
4. **Trigger**: Select **When I log on** and click Next.
5. **Action**: Select **Start a program** and click Next.
6. **Program/script**: Type `wscript.exe`
7. **Add arguments (optional)**: Enter the full path to the VBScript wrapped in quotes. For example:
   `"C:\ROOMLIGHTS-APP\RoomSync\run-hidden.vbs"`
8. Click Next, check the box for **Open the Properties dialog for this task when I click Finish**, and click Finish.
9. In the Properties window, under the **General** tab, you can optionally select *Run whether user is logged on or not* for maximum stealth, though *Run only when user is logged on* works perfectly for desktop usage.
10. Click **OK** to save.

RoomSync will now start invisibly in the background every time you log into Windows!
