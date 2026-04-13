# Reolink ISP Tool

A small Windows desktop app for reading, editing, backing up, and writing Reolink ISP (image) settings.

## What it does

- Reads ISP settings directly from a Reolink camera over your LAN
- Lets you edit common ISP settings in a simple GUI
- Preserves unsupported/model-specific keys where possible
- Saves timestamped JSON backups with camera metadata
- Warns when loading a backup from a different camera model
- Includes hover tips for selected settings

## Current features

- Read ISP from camera
- Write ISP back to camera with confirmation and read-back verification
- Save / load JSON backups
- Connected camera and loaded backup indicators
- Model-specific fields shown only when relevant
- Safer cross-model backup handling
- Tooltips for selected settings

## Requirements

- Windows
- Python 3.x for running the source version
- A Reolink camera reachable on your local network
- Valid camera username and password

## Download

When releases are available, download the latest Windows `.exe` from the **Releases** section.

## Running from source

Open the project folder in VS Code or a terminal and run:

    py reolink_isp_tool.py

## Building the EXE

From the project folder:

    py -m pip install pyinstaller
    py -m PyInstaller --clean --noconfirm --onefile --windowed --name "Reolink ISP Tool" reolink_isp_tool.py

The built executable will appear in the `dist` folder.

## Notes

- This tool talks directly to the camera over your LAN.
- It uses Reolink’s CGI API with a JSON array request body.
- For HTTPS with a self-signed certificate, certificate verification is disabled by the tool.
- Hover selected setting labels for tips.

## Backup compatibility

- Backups include ISP settings plus camera metadata when available.
- Loading a backup from a different camera model is allowed, but the app warns you first.
- Writes are based on the currently connected camera’s last live-read ISP structure so unsupported keys from another model are ignored.

## Reporting unsupported camera models

If your camera exposes settings that are not shown in the UI yet, please include:

- the camera model and firmware version
- the JSON backup file saved by this tool
- a short note describing what seems missing or wrong

That gives enough information to compare the camera’s ISP structure and add support for additional fields.

## Disclaimer

Use at your own risk. Always save a backup before changing camera settings.