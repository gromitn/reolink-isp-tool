"""
Reolink ISP Tool

A small Windows-friendly desktop app for reading and writing Reolink ISP
(image) settings using the working inline-auth CGI pattern discovered during
troubleshooting.

No third-party packages are required for the source app itself.
It uses only the Python standard library:
- tkinter / ttk for the UI
- urllib for HTTP(S)
- json for payloads and backups

Typical usage:
1. Enter protocol, IP, username, and password.
2. Click "Read ISP".
3. Adjust values.
4. Click "Write ISP".
5. Optionally save/load JSON backups.

Packaging to EXE (on Windows):
    py -m pip install pyinstaller
    py -m PyInstaller --noconsole --onefile reolink_isp_tool.py

Notes:
- This tool talks to the camera directly over your LAN.
- It uses the working POST body format with a JSON array wrapper.
- For HTTPS with a self-signed certificate, certificate verification is
  disabled in the tool's HTTPS requests.
"""

from __future__ import annotations

import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime

APP_TITLE = "Reolink ISP Tool"
APP_VERSION = "1.0.0"
DEFAULT_SAVE_FILE = "reolink_isp_backup.json"


class ReolinkApiError(Exception):
    pass

# Thin HTTP/HTTPS CGI wrapper for the small subset of the Reolink API used by this tool.
class ReolinkClient:
    def __init__(self, protocol: str, host: str, username: str, password: str):
        self.protocol = protocol.strip().lower()
        self.host = host.strip()
        self.username = username
        self.password = password

        if self.protocol not in {"http", "https"}:
            raise ValueError("Protocol must be http or https")
        if not self.host:
            raise ValueError("Host/IP is required")
        if not self.username:
            raise ValueError("Username is required")

    @property
    def base_url(self) -> str:
        user = urllib.parse.quote(self.username, safe="")
        pw = urllib.parse.quote(self.password, safe="")
        return f"{self.protocol}://{self.host}/cgi-bin/api.cgi?user={user}&password={pw}"

    # The Reolink CGI API expects a JSON array of command objects, even for single commands.
    # May return either a dict or a list root, and the method normalizes/validates to always return a list of dicts.
    def _post(self, commands: list[dict]) -> list[dict]:
        body = json.dumps(commands, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        context = None
        if self.protocol == "https":
            context = ssl._create_unverified_context()

        try:
            with urllib.request.urlopen(req, context=context, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise ReolinkApiError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ReolinkApiError(f"Connection failed: {e.reason}") from e
        except Exception as e:
            raise ReolinkApiError(str(e)) from e

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            snippet = raw[:500].strip()
            raise ReolinkApiError(f"Invalid JSON response: {snippet}") from e

        if isinstance(parsed, dict):
            parsed = [parsed]

        if not isinstance(parsed, list):
            snippet = raw[:500].strip()
            raise ReolinkApiError(
                f"Unexpected response shape: {type(parsed).__name__}. Raw: {snippet}"
            )

        if not parsed:
            raise ReolinkApiError("Empty JSON response from camera")

        first = parsed[0]
        if not isinstance(first, dict):
            raise ReolinkApiError(
                f"Unexpected response item type: {type(first).__name__}"
            )

        return parsed

    def get_isp(self) -> dict:
        commands = [{"cmd": "GetIsp", "action": 1, "param": {"channel": 0}}]
        resp = self._post(commands)
        if not resp:
            raise ReolinkApiError("Empty response")

        item = resp[0]
        if item.get("code") != 0:
            err = item.get("error", {})
            raise ReolinkApiError(
                f"GetIsp failed: rspCode={err.get('rspCode')} detail={err.get('detail')}"
            )

        value = item.get("value")
        if not isinstance(value, dict):
            raise ReolinkApiError(
                f"GetIsp response missing value block. Keys: {sorted(item.keys())}"
            )

        isp = value.get("Isp")
        if not isinstance(isp, dict):
            raise ReolinkApiError(
                f"GetIsp response missing Isp block. Value keys: {sorted(value.keys())}"
            )

        return isp

    def get_dev_info(self) -> dict:
        commands = [{"cmd": "GetDevInfo", "action": 1}]
        resp = self._post(commands)
        if not resp:
            raise ReolinkApiError("Empty response")

        item = resp[0]
        if item.get("code") != 0:
            err = item.get("error", {})
            raise ReolinkApiError(
                f"GetDevInfo failed: rspCode={err.get('rspCode')} detail={err.get('detail')}"
            )

        value = item.get("value")
        if not isinstance(value, dict):
            raise ReolinkApiError(
                f"GetDevInfo response missing value block. Keys: {sorted(item.keys())}"
            )

        dev_info = value.get("DevInfo")
        if not isinstance(dev_info, dict):
            raise ReolinkApiError(
                f"GetDevInfo response missing DevInfo block. Value keys: {sorted(value.keys())}"
            )

        return dev_info


    def set_isp(self, isp: dict) -> dict:
        commands = [{"cmd": "SetIsp", "action": 0, "param": {"Isp": isp}}]
        resp = self._post(commands)
        if not resp:
            raise ReolinkApiError("Empty response")

        item = resp[0]
        if item.get("code") != 0:
            err = item.get("error", {})
            raise ReolinkApiError(
                f"SetIsp failed: rspCode={err.get('rspCode')} detail={err.get('detail')}"
            )
        return item

class ToolTip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tipwindow = None

        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None):
        if self.tipwindow or not self.text:
            return

        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#fff8dc",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            wraplength=260,
        )
        label.pack()

    def hide(self, _event=None):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master = master
        self.grid(sticky="nsew")

        self.current_isp: dict | None = None
        self.last_read_isp: dict | None = None

        # Last successful live read from the currently connected camera.
        # This stays separate from any loaded backup so writes can be based on
        # the target camera's own supported ISP structure.
        self.camera_isp: dict | None = None
        self.camera_dev_info: dict | None = None

        self.current_dev_info: dict | None = None
        self.last_read_dev_info: dict | None = None

        # Metadata from the most recently loaded backup file, if any.
        self.loaded_backup_dev_info: dict | None = None
        self.loaded_backup_path: str | None = None

        self.protocol_var = tk.StringVar(value="http")
        self.host_var = tk.StringVar(value="192.168.1.198")
        self.user_var = tk.StringVar(value="admin")
        self.password_var = tk.StringVar()

        self.daynight_var = tk.StringVar(value="")
        self.daynight_threshold_var = tk.StringVar(value="")
        self.exposure_var = tk.StringVar(value="")
        self.antiflicker_var = tk.StringVar(value="")
        self.backlight_var = tk.StringVar(value="")
        self.white_balance_var = tk.StringVar(value="")
        self.hdr_var = tk.BooleanVar(value=False)
        self.constant_frame_rate_var = tk.BooleanVar(value=False)
        self.enc_type_var = tk.StringVar(value="")

        self.gain_min_var = tk.StringVar(value="")
        self.gain_max_var = tk.StringVar(value="")
        self.shutter_min_var = tk.StringVar(value="")
        self.shutter_max_var = tk.StringVar(value="")

        self.bd_day_mode_var = tk.StringVar(value="")
        self.bd_day_bright_var = tk.StringVar(value="")
        self.bd_day_dark_var = tk.StringVar(value="")

        self.bd_night_mode_var = tk.StringVar(value="")
        self.bd_night_bright_var = tk.StringVar(value="")
        self.bd_night_dark_var = tk.StringVar(value="")
        self.bd_led_color_mode_var = tk.StringVar(value="")
        self.bd_led_color_bright_var = tk.StringVar(value="")
        self.bd_led_color_dark_var = tk.StringVar(value="")

        self.blc_var = tk.StringVar(value="")
        self.drc_var = tk.StringVar(value="")
        self.red_gain_var = tk.StringVar(value="")
        self.blue_gain_var = tk.StringVar(value="")

        self.mirroring_var = tk.BooleanVar(value=False)
        self.rotation_var = tk.BooleanVar(value=False)
        self.nr3d_var = tk.BooleanVar(value=False)
        self.vcmd = (self.master.register(self._validate_int), "%P")

        self._build_ui()
        self._configure_grid()

    def _configure_grid(self) -> None:
        self.master.title(f"{APP_TITLE} v{APP_VERSION}")
        self.master.minsize(980, 590)
        self.after(0, self._fit_window_to_content)
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)
        top.columnconfigure(2, weight=2)

        conn = ttk.LabelFrame(top, text="Connection", padding=10)
        conn.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        for i in range(4):
            conn.columnconfigure(i, weight=1)

        ttk.Label(conn, text="Protocol").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            conn,
            textvariable=self.protocol_var,
            values=["http", "https"],
            width=8,
            state="readonly",
        ).grid(row=1, column=0, sticky="ew", padx=(0, 8))

        ttk.Label(conn, text="IP / Host[:port]").grid(row=0, column=1, sticky="w")
        ttk.Entry(conn, textvariable=self.host_var).grid(row=1, column=1, sticky="ew", padx=(0, 8))

        ttk.Label(conn, text="Username").grid(row=0, column=2, sticky="w")
        ttk.Entry(conn, textvariable=self.user_var).grid(row=1, column=2, sticky="ew", padx=(0, 8))

        ttk.Label(conn, text="Password").grid(row=0, column=3, sticky="w")
        ttk.Entry(conn, textvariable=self.password_var, show="*").grid(row=1, column=3, sticky="ew")

        ttk.Label(
            conn,
            text="Use just the IP for default ports. Add :port only if your camera uses a custom HTTP/HTTPS port.",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        ttk.Label(
            conn,
            text="Hover labels for tips on selected settings.",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        camera_actions = ttk.LabelFrame(top, text="Camera", padding=10)
        camera_actions.grid(row=0, column=1, sticky="nsew", padx=4)
        camera_actions.columnconfigure(0, weight=1)
        camera_actions.columnconfigure(1, weight=1)

        self.read_btn = ttk.Button(camera_actions, text="Read ISP", command=self.read_isp)
        self.read_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.write_btn = ttk.Button(
            camera_actions,
            text="Write ISP",
            command=self.write_isp,
            state="disabled",
        )
        self.write_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        camera_info_row = ttk.Frame(camera_actions)
        camera_info_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(
            camera_info_row,
            text="Connected:",
        ).grid(row=0, column=0, sticky="w")

        self.camera_info_var = tk.StringVar(value="Not read yet")
        self.camera_info_label = tk.Label(
            camera_info_row,
            textvariable=self.camera_info_var,
            fg="#0d47a1",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.camera_info_label.grid(row=0, column=1, sticky="w", padx=(6, 0))

        backup_actions = ttk.LabelFrame(top, text="Backup / Restore", padding=10)
        backup_actions.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        backup_actions.columnconfigure(0, weight=1)
        backup_actions.columnconfigure(1, weight=1)

        ttk.Button(backup_actions, text="Backup JSON", command=self.save_backup).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(backup_actions, text="Load JSON", command=self.load_backup).grid(
            row=0, column=1, sticky="ew", padx=(4, 0)
        )

        backup_info_row = ttk.Frame(backup_actions)
        backup_info_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(
            backup_info_row,
            text="Loaded:",
        ).grid(row=0, column=0, sticky="w")

        self.backup_info_var = tk.StringVar(value="None")
        self.backup_info_label = tk.Label(
            backup_info_row,
            textvariable=self.backup_info_var,
            fg="#8d6e63",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.backup_info_label.grid(row=0, column=1, sticky="w", padx=(6, 0))

        main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        left = ttk.Frame(main, padding=4)
        right = ttk.Frame(main, padding=4)
        main.add(left, weight=3)
        main.add(right, weight=2)

        self._build_settings_panel(left)
        self._build_log_panel(right)

    def _build_settings_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        status = ttk.LabelFrame(parent, text="Status", padding=10)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        status.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Ready. Click Read ISP or Load JSON to begin.")
        self.status_label = tk.Label(
            status,
            textvariable=self.status_var,
            wraplength=430,
            justify="left",
            anchor="w",
            padx=10,
            pady=6,
            bg="#e8f5e9",
            fg="#1b5e20",
            relief="flat",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")

        basics = ttk.LabelFrame(parent, text="Core ISP", padding=10)
        basics.grid(row=1, column=0, sticky="ew")
        for i in range(4):
            basics.columnconfigure(i, weight=1)

        self._combo(basics, "Day/Night", self.daynight_var, ["Auto", "Color", "Black&White"], 0, 0)
        self._entry(
            basics,
            "Day/Night Threshold",
            self.daynight_threshold_var,
            0,
            1,
            tooltip="Switch point for Auto day/night mode. Lower values usually make the camera stay in colour/day mode longer; higher values switch to night mode sooner.",
        )
        self._combo(
            basics,
            "Exposure",
            self.exposure_var,
            ["Auto", "LowNoise", "Anti-Smearing", "Manual"],
            1,
            0,
            tooltip="Overall exposure strategy. Auto lets the camera balance things itself. LowNoise favors cleaner low-light images. Anti-Smearing favors faster shutter behavior to reduce blur/smearing. Manual gives you direct control of the available gain/shutter ranges.",
        )
        self._combo(
            basics,
            "Anti-Flicker",
            self.antiflicker_var,
            ["Outdoor", "50HZ", "60HZ", "Off"],
            1,
            1,
            tooltip="Reduces flicker from mains-powered lights and screens. Try matching this to your local mains frequency (often 50Hz or 60Hz). If the camera is outdoors and the image looks overexposed, Off is often worth trying.",
        )
        self._combo(basics, "Backlight", self.backlight_var, ["Off", "BackLightControl", "DynamicRangeControl"], 2, 0)
        self._combo(basics, "White Balance", self.white_balance_var, ["Auto", "Manual"], 2, 1)

        numeric = ttk.LabelFrame(parent, text="Shutter / Gain / Tone", padding=10)
        numeric.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        for i in range(4):
            numeric.columnconfigure(i, weight=1)

        self._entry(numeric, "Gain Min", self.gain_min_var, 0, 0)
        self._entry(numeric, "Gain Max", self.gain_max_var, 0, 1)
        self._entry(
            numeric,
            "Shutter Min",
            self.shutter_min_var,
            1,
            0,
            tooltip="Minimum shutter value the camera may use. Lower / slower shutter values can brighten the image but may add motion blur. Higher / faster values help freeze movement but need more light.",
        )
        self._entry(
            numeric,
            "Shutter Max",
            self.shutter_max_var,
            1,
            1,
            tooltip="Maximum shutter value the camera may use. Keeping the allowed range tighter makes behaviour more predictable. For fast motion or plates, limiting the shutter to faster values can help reduce blur if there is enough light.",
        )
        self._entry(
            numeric,
            "BLC",
            self.blc_var,
            2,
            0,
            tooltip="Back Light Compensation. Helps brighten darker foreground subjects against a bright background. If highlights or reflective plates start blowing out, try lowering this.",
        )
        self._entry(
            numeric,
            "DRC",
            self.drc_var,
            2,
            1,
            tooltip="Dynamic Range Control. Tries to balance very bright and very dark parts of the image. Useful for harsh contrast, but too much can make the image look flatter or less natural.",
        )
        self._entry(numeric, "Red Gain", self.red_gain_var, 3, 0)
        self._entry(numeric, "Blue Gain", self.blue_gain_var, 3, 1)

        bd_day = ttk.LabelFrame(parent, text="Brightness & Shadows — Day", padding=10)
        bd_day.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for i in range(3):
            bd_day.columnconfigure(i, weight=1)
        self._combo(
            bd_day,
            "Mode",
            self.bd_day_mode_var,
            ["Auto", "Manual"],
            0,
            0,
            tooltip="Controls the bright/dark balance for the colour/day image. Auto lets the camera tune it itself. Manual lets you push the picture brighter or darker if daytime contrast is not quite right.",
        )
        self._entry(bd_day, "Bright", self.bd_day_bright_var, 0, 1)
        self._entry(bd_day, "Dark", self.bd_day_dark_var, 0, 2)

        bd_night = ttk.LabelFrame(parent, text="Brightness & Shadows — Night", padding=10)
        bd_night.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        for i in range(3):
            bd_night.columnconfigure(i, weight=1)
        self._combo(
            bd_night,
            "Mode",
            self.bd_night_mode_var,
            ["Auto", "Manual"],
            0,
            0,
            tooltip="Controls the bright/dark balance for the night / black-and-white image. Useful if the night image feels too flat, too crushed, or too washed out.",
        )
        self._entry(bd_night, "Bright", self.bd_night_bright_var, 0, 1)
        self._entry(bd_night, "Dark", self.bd_night_dark_var, 0, 2)

        self.bd_led_color = ttk.LabelFrame(parent, text="Brightness & Shadows — LED Color", padding=10)
        self.bd_led_color.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        for i in range(3):
            self.bd_led_color.columnconfigure(i, weight=1)
        self._combo(
            self.bd_led_color,
            "Mode",
            self.bd_led_color_mode_var,
            ["Auto", "Manual"],
            0,
            0,
            tooltip="Controls the bright/dark balance for colour night mode when the spotlight / colour night lighting is active.",
        )
        self._entry(self.bd_led_color, "Bright", self.bd_led_color_bright_var, 0, 1)
        self._entry(self.bd_led_color, "Dark", self.bd_led_color_dark_var, 0, 2)

        # Hidden until the current camera/backup actually exposes bd_led_color.
        self.bd_led_color.grid_remove()

        self.model_specific = ttk.LabelFrame(parent, text="Model-specific", padding=10)
        self.model_specific.grid(row=7, column=0, sticky="ew", pady=(10, 0))
        self.model_specific.columnconfigure(0, weight=1)
        self.model_specific.columnconfigure(1, weight=1)
        self.model_specific.columnconfigure(2, weight=1)

        hdr_chk = ttk.Checkbutton(
            self.model_specific,
            text="HDR",
            variable=self.hdr_var,
        )
        hdr_chk.grid(row=0, column=0, sticky="w", padx=(0, 12))
        ToolTip(
            hdr_chk,
            "High Dynamic Range. Combines multiple exposures to reveal more detail in bright and dark areas. Useful for harsh contrast, though sometimes it can change the look of moving subjects.",
        )

        cfr_chk = ttk.Checkbutton(
            self.model_specific,
            text="Constant Frame Rate",
            variable=self.constant_frame_rate_var,
        )
        cfr_chk.grid(row=0, column=1, sticky="w", padx=(0, 12))
        ToolTip(
            cfr_chk,
            "Keeps frame rate fixed instead of letting the camera reduce it in low light. This prioritizes smoothness, but in darker scenes the camera may have less room to increase exposure time.",
        )

        enc_lbl = ttk.Label(self.model_specific, text="Encoding Type")
        enc_lbl.grid(row=1, column=0, sticky="w", pady=(10, 4))
        ToolTip(
            enc_lbl,
            "CBR keeps bitrate fixed and predictable. VBR varies bitrate with scene complexity, which can improve image quality in busy scenes but makes bandwidth and storage less predictable.",
        )

        ttk.Combobox(
            self.model_specific,
            textvariable=self.enc_type_var,
            values=["CBR", "VBR"],
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(10, 4))

        # Hidden until the current camera/backup actually exposes any of these keys.
        self.model_specific.grid_remove()

        flags = ttk.LabelFrame(parent, text="Flags", padding=10)
        flags.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(flags, text="Mirroring", variable=self.mirroring_var).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Checkbutton(flags, text="Rotation", variable=self.rotation_var).grid(row=0, column=1, sticky="w", padx=(0, 12))
        nr3d_chk = ttk.Checkbutton(flags, text="3D Noise Reduction", variable=self.nr3d_var)
        nr3d_chk.grid(row=0, column=2, sticky="w")
        ToolTip(
            nr3d_chk,
            "3D Noise Reduction. Reduces visible image noise, especially in low light. Useful for cleaner images, though too much noise reduction can soften fine detail.",
        )

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        raw = ttk.LabelFrame(parent, text="Raw ISP JSON (last read / loaded)", padding=10)
        raw.grid(row=0, column=0, sticky="nsew")
        raw.columnconfigure(0, weight=1)
        raw.rowconfigure(0, weight=1)

        self.raw_text = tk.Text(
            raw,
            wrap="none",
            height=24,
            background="#f0f0f0",
            foreground="#666666",
        )
        self.raw_text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(raw, orient="vertical", command=self.raw_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.raw_text.configure(yscrollcommand=yscroll.set)

        self.raw_text.insert("1.0", "No ISP data loaded yet.\n\nClick Read ISP or Load JSON to begin.")
        self.raw_text.configure(state="disabled")

        ttk.Button(raw, text="Copy JSON", command=self.copy_raw_json).grid(
            row=1, column=0, columnspan=2, sticky="e", pady=(8, 0)
        )

    def _combo(self, parent, label, variable, values, row, col, tooltip: str | None = None):
        base_col = col * 2
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=row, column=base_col, sticky="w", pady=(0, 4))
        if tooltip:
            ToolTip(lbl, tooltip)

        ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            state="readonly",
        ).grid(row=row, column=base_col + 1, sticky="ew", padx=(0, 12), pady=(0, 8))

    def _entry(self, parent, label, variable, row, col, tooltip: str | None = None):
        base_col = col * 2
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=row, column=base_col, sticky="w", pady=(0, 4))
        if tooltip:
            ToolTip(lbl, tooltip)

        ttk.Entry(
            parent,
            textvariable=variable,
            validate="key",
            validatecommand=self.vcmd,
        ).grid(row=row, column=base_col + 1, sticky="ew", padx=(0, 12), pady=(0, 8))

    def _client(self) -> ReolinkClient:
        return ReolinkClient(
            protocol=self.protocol_var.get(),
            host=self.host_var.get(),
            username=self.user_var.get(),
            password=self.password_var.get(),
        )

    def _refresh_camera_info_label(self) -> None:
        if self.camera_dev_info and self.camera_dev_info.get("model"):
            model = str(self.camera_dev_info.get("model", "Unknown model"))
            name = str(self.camera_dev_info.get("name", "")).strip()
            if name:
                self.camera_info_var.set(f"{model} ({name})")
            else:
                self.camera_info_var.set(model)
        else:
            self.camera_info_var.set("Not read yet")

    def _refresh_backup_info_label(self) -> None:
        if self.loaded_backup_dev_info and self.loaded_backup_dev_info.get("model"):
            model = str(self.loaded_backup_dev_info.get("model", "Unknown model"))
            name = str(self.loaded_backup_dev_info.get("name", "")).strip()
            if name:
                self.backup_info_var.set(f"{model} ({name})")
            else:
                self.backup_info_var.set(model)
        else:
            self.backup_info_var.set("None")

    def _refresh_model_specific_visibility(self, isp: dict) -> None:
        capability_source = self.camera_isp if self.camera_isp is not None else isp
        has_model_specific = any(
            key in capability_source for key in ("hdr", "constantFrameRate", "encType")
        )

        if has_model_specific:
            self.model_specific.grid()
        else:
            self.model_specific.grid_remove()

        self.after(0, self._fit_window_to_content)

    def _refresh_bd_led_color_visibility(self, isp: dict) -> None:
        capability_source = self.camera_isp if self.camera_isp is not None else isp

        if "bd_led_color" in capability_source:
            self.bd_led_color.grid()
        else:
            self.bd_led_color.grid_remove()

        self.after(0, self._fit_window_to_content)

    def _fit_window_to_content(self) -> None:
        self.update_idletasks()

        req_w = self.winfo_reqwidth() + 20
        req_h = self.winfo_reqheight() + 20

        cur_w = self.master.winfo_width()
        cur_h = self.master.winfo_height()

        # Grow to fit new content, but do not shrink automatically.
        new_w = max(cur_w, req_w)
        new_h = max(cur_h, req_h)

        self.master.geometry(f"{new_w}x{new_h}")
        self.master.minsize(req_w, req_h)

    def _default_backup_filename(self) -> str:
        model = "unknown_model"
        if self.current_dev_info and self.current_dev_info.get("model"):
            model = str(self.current_dev_info["model"])

        safe_model = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in model)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return f"reolink_isp_backup_{safe_model}_{timestamp}.json"

    def _find_unsupported_backup_keys(self, backup_isp: dict) -> list[str]:
        if self.camera_isp is None:
            return []

        missing: list[str] = []

        def walk(backup_obj, camera_obj, prefix: str = "") -> None:
            if not isinstance(backup_obj, dict):
                return

            if not isinstance(camera_obj, dict):
                if prefix:
                    missing.append(prefix)
                return

            for key, value in backup_obj.items():
                path = f"{prefix}.{key}" if prefix else key
                if key not in camera_obj:
                    missing.append(path)
                else:
                    walk(value, camera_obj[key], path)

        walk(backup_isp, self.camera_isp)
        return missing

    # Show the given dict as formatted JSON in the display-only panel.
    # This always reflects the last read/loaded snapshot, not unsaved form edits.
    def log_json(self, data: dict) -> None:
        self.raw_text.configure(state="normal")
        self.raw_text.delete("1.0", tk.END)
        self.raw_text.insert("1.0", json.dumps(data, indent=2))
        self.raw_text.configure(state="disabled")

    # Used by numeric Entry widgets to reject non-digit typing while still allowing blank startup fields.
    def _validate_int(self, value_if_allowed: str) -> bool:
        """Allow only digits, or blank."""
        return value_if_allowed == "" or value_if_allowed.isdigit()

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

        lower = text.lower()
        if "failed" in lower or "error" in lower:
            bg, fg = "#ffebee", "#b71c1c"   # soft red
        elif "copied" in lower or "saved backup" in lower or "loaded backup" in lower or "reloaded" in lower:
            bg, fg = "#e3f2fd", "#0d47a1"   # soft blue
        elif "cancelled" in lower:
            bg, fg = "#fff8e1", "#8d6e63"   # soft amber
        else:
            bg, fg = "#e8f5e9", "#1b5e20"   # soft green

        if hasattr(self, "status_label"):
            self.status_label.configure(bg=bg, fg=fg)

    def copy_raw_json(self) -> None:
        text = self.raw_text.get("1.0", "end-1c")
        self.master.clipboard_clear()
        self.master.clipboard_append(text)
        self.set_status("Copied Raw ISP JSON to clipboard.")

    # Populate the UI fields from a camera snapshot or loaded backup, then enable writing.
    def populate_from_isp(self, isp: dict) -> None:
        self.current_isp = deepcopy(isp)
        self.write_btn.configure(state="normal")

        self.daynight_var.set(str(isp.get("dayNight", "Auto")))
        self.daynight_threshold_var.set(str(isp.get("dayNightThreshold", 50)))
        self.exposure_var.set(str(isp.get("exposure", "Auto")))
        self.antiflicker_var.set(str(isp.get("antiFlicker", "Off")))
        self.backlight_var.set(str(isp.get("backLight", "Off")))
        self.white_balance_var.set(str(isp.get("whiteBalance", "Auto")))
        self.hdr_var.set(bool(isp.get("hdr", 0)))
        self.constant_frame_rate_var.set(bool(isp.get("constantFrameRate", 0)))
        self.enc_type_var.set(str(isp.get("encType", "")))
        self._refresh_model_specific_visibility(isp)

        gain = isp.get("gain", {}) or {}
        shutter = isp.get("shutter", {}) or {}
        self.gain_min_var.set(str(gain.get("min", 1)))
        self.gain_max_var.set(str(gain.get("max", 62)))
        self.shutter_min_var.set(str(shutter.get("min", 0)))
        self.shutter_max_var.set(str(shutter.get("max", 125)))

        self.blc_var.set(str(isp.get("blc", 128)))
        self.drc_var.set(str(isp.get("drc", 128)))
        self.red_gain_var.set(str(isp.get("redGain", 128)))
        self.blue_gain_var.set(str(isp.get("blueGain", 128)))

        bd_day = isp.get("bd_day", {}) or {}
        self.bd_day_mode_var.set(str(bd_day.get("mode", "Auto")))
        self.bd_day_bright_var.set(str(bd_day.get("bright", 128)))
        self.bd_day_dark_var.set(str(bd_day.get("dark", 128)))

        bd_night = isp.get("bd_night", {}) or {}
        self.bd_night_mode_var.set(str(bd_night.get("mode", "Auto")))
        self.bd_night_bright_var.set(str(bd_night.get("bright", 128)))
        self.bd_night_dark_var.set(str(bd_night.get("dark", 128)))
        bd_led_color = isp.get("bd_led_color", {}) or {}
        self.bd_led_color_mode_var.set(str(bd_led_color.get("mode", "Auto")))
        self.bd_led_color_bright_var.set(str(bd_led_color.get("bright", 128)))
        self.bd_led_color_dark_var.set(str(bd_led_color.get("dark", 128)))
        self._refresh_bd_led_color_visibility(isp)

        self.mirroring_var.set(bool(isp.get("mirroring", 0)))
        self.rotation_var.set(bool(isp.get("rotation", 0)))
        self.nr3d_var.set(bool(isp.get("nr3d", 1)))

        self.log_json(isp)

    # Build a complete ISP payload from the current UI field values,
    # validating numeric entries as needed.
    def build_isp_from_fields(self) -> dict:
        if self.current_isp is None:
            raise ReolinkApiError("Read ISP first, or load a backup JSON first.")

        # Prefer the last live camera snapshot as the write base so unsupported
        # keys from another model are not blindly introduced by a loaded backup.
        base_isp = self.camera_isp if self.camera_isp is not None else self.current_isp
        isp = deepcopy(base_isp)

        def intv(var: tk.StringVar, name: str) -> int:
            try:
                return int(var.get().strip())
            except ValueError as e:
                raise ReolinkApiError(f"{name} must be an integer") from e

        isp["dayNight"] = self.daynight_var.get().strip()
        isp["dayNightThreshold"] = intv(self.daynight_threshold_var, "Day/Night Threshold")
        isp["exposure"] = self.exposure_var.get().strip()
        isp["antiFlicker"] = self.antiflicker_var.get().strip()
        isp["backLight"] = self.backlight_var.get().strip()
        isp["whiteBalance"] = self.white_balance_var.get().strip()
        if "hdr" in isp:
            isp["hdr"] = 1 if self.hdr_var.get() else 0

        if "constantFrameRate" in isp:
            isp["constantFrameRate"] = 1 if self.constant_frame_rate_var.get() else 0

        if "encType" in isp and self.enc_type_var.get().strip():
            isp["encType"] = self.enc_type_var.get().strip()


        isp.setdefault("gain", {})
        isp["gain"]["min"] = intv(self.gain_min_var, "Gain Min")
        isp["gain"]["max"] = intv(self.gain_max_var, "Gain Max")

        isp.setdefault("shutter", {})
        isp["shutter"]["min"] = intv(self.shutter_min_var, "Shutter Min")
        isp["shutter"]["max"] = intv(self.shutter_max_var, "Shutter Max")

        isp["blc"] = intv(self.blc_var, "BLC")
        isp["drc"] = intv(self.drc_var, "DRC")
        isp["redGain"] = intv(self.red_gain_var, "Red Gain")
        isp["blueGain"] = intv(self.blue_gain_var, "Blue Gain")

        isp.setdefault("bd_day", {})
        isp["bd_day"]["mode"] = self.bd_day_mode_var.get().strip()
        isp["bd_day"]["bright"] = intv(self.bd_day_bright_var, "Day Bright")
        isp["bd_day"]["dark"] = intv(self.bd_day_dark_var, "Day Dark")

        isp.setdefault("bd_night", {})
        isp["bd_night"]["mode"] = self.bd_night_mode_var.get().strip()
        isp["bd_night"]["bright"] = intv(self.bd_night_bright_var, "Night Bright")
        isp["bd_night"]["dark"] = intv(self.bd_night_dark_var, "Night Dark")
        if "bd_led_color" in isp:
            isp.setdefault("bd_led_color", {})
            isp["bd_led_color"]["mode"] = self.bd_led_color_mode_var.get().strip()
            isp["bd_led_color"]["bright"] = intv(self.bd_led_color_bright_var, "LED Color Bright")
            isp["bd_led_color"]["dark"] = intv(self.bd_led_color_dark_var, "LED Color Dark")

        isp["mirroring"] = 1 if self.mirroring_var.get() else 0
        isp["rotation"] = 1 if self.rotation_var.get() else 0
        isp["nr3d"] = 1 if self.nr3d_var.get() else 0

        return isp

    # Read the current ISP settings from the camera in a background thread.
    # Device info is also fetched so backups and model-specific UI can use it later.
    def read_isp(self) -> None:
        self.read_btn.configure(state="disabled")
        self.write_btn.configure(state="disabled")
        self.set_status("Reading ISP settings from camera... please wait.")

        def background_task():
            try:
                client = self._client()
                isp = client.get_isp()
                dev_info = client.get_dev_info()
                self.master.after(0, self._on_read_success, isp, dev_info)
            except Exception as e:
                self.master.after(0, self._on_read_error, str(e))

        threading.Thread(target=background_task, daemon=True).start()

    # Called back on the Tk main thread after a background Read ISP operation finishes.
    def _on_read_success(self, isp: dict, dev_info: dict) -> None:
        self.last_read_isp = deepcopy(isp)
        self.camera_isp = deepcopy(isp)

        self.current_dev_info = deepcopy(dev_info)
        self.last_read_dev_info = deepcopy(dev_info)
        self.camera_dev_info = deepcopy(dev_info)

        self.loaded_backup_dev_info = None
        self.loaded_backup_path = None

        self.populate_from_isp(isp)
        self.read_btn.configure(state="normal")
        self._refresh_camera_info_label()
        self._refresh_backup_info_label()
        self.set_status("Read ISP successfully.")

    # Called back on the Tk main thread if a background Read ISP operation fails.
    def _on_read_error(self, error_message: str) -> None:
        self.read_btn.configure(state="normal")
        if self.current_isp is not None:
            self.write_btn.configure(state="normal")
        messagebox.showerror(APP_TITLE, error_message)
        self.set_status(f"Read failed: {error_message}")

    def _on_write_success(self, verified: dict) -> None:
        self.last_read_isp = deepcopy(verified)
        self.camera_isp = deepcopy(verified)

        self.populate_from_isp(verified)
        self.read_btn.configure(state="normal")
        self.write_btn.configure(state="normal")
        self.set_status("Wrote ISP successfully and verified camera settings.")
        messagebox.showinfo(APP_TITLE, "Settings written and verified successfully.")

    def _on_write_error(self, error_message: str) -> None:
        self.read_btn.configure(state="normal")
        if self.current_isp is not None:
            self.write_btn.configure(state="normal")
        messagebox.showerror(APP_TITLE, error_message)
        self.set_status(f"Write failed: {error_message}")

    # Write the current UI field values to the camera in a background thread,
    # then read back to verify and update the UI with the actual saved state.
    def write_isp(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "Write the currently displayed ISP settings to the camera?",
        ):
            self.set_status("Write cancelled.")
            return

        try:
            isp = self.build_isp_from_fields()
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
            self.set_status(f"Write failed: {e}")
            return

        self.read_btn.configure(state="disabled")
        self.write_btn.configure(state="disabled")
        self.set_status("Writing ISP settings to camera... please wait.")

        def background_task():
            try:
                client = self._client()
                client.set_isp(isp)
                verified = client.get_isp()
                self.master.after(0, self._on_write_success, verified)
            except Exception as e:
                self.master.after(0, self._on_write_error, str(e))

        threading.Thread(target=background_task, daemon=True).start()

    def save_backup(self) -> None:
        try:
            isp = self.build_isp_from_fields() if self.current_isp else None
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
            return

        if isp is None:
            messagebox.showwarning(APP_TITLE, "Nothing to save yet. Read ISP or load a JSON file first.")
            return

        backup = {
            "tool": APP_TITLE,
            "version": APP_VERSION,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "dev_info": deepcopy(self.current_dev_info) if self.current_dev_info else None,
            "isp": isp,
        }

        path = filedialog.asksaveasfilename(
            title="Save ISP backup",
            defaultextension=".json",
            initialfile=self._default_backup_filename(),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        Path(path).write_text(json.dumps(backup, indent=2), encoding="utf-8")
        self.set_status(f"Saved backup: {path}")

    def load_backup(self) -> None:
        path = filedialog.askopenfilename(
            title="Load ISP backup",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not load JSON: {e}")
            return

        try:
            if isinstance(payload, dict) and "isp" in payload:
                isp = payload.get("isp")
                dev_info = payload.get("dev_info")
                if not isinstance(isp, dict):
                    raise ValueError("Backup file contains an invalid 'isp' block")
                if dev_info is not None and not isinstance(dev_info, dict):
                    raise ValueError("Backup file contains an invalid 'dev_info' block")
            elif isinstance(payload, dict):
                # Backward compatibility: old backups were just the ISP dict itself.
                isp = payload
                dev_info = None
            else:
                raise ValueError("JSON root must be an object")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not load backup: {e}")
            return

        backup_model = None
        if dev_info and dev_info.get("model"):
            backup_model = str(dev_info["model"])

        camera_model = None
        if self.camera_dev_info and self.camera_dev_info.get("model"):
            camera_model = str(self.camera_dev_info["model"])

        unsupported_keys = self._find_unsupported_backup_keys(isp)

        warning_parts = []

        if backup_model and camera_model and backup_model != camera_model:
            warning_parts.append(
                f"This backup was saved from {backup_model}, but the current camera is {camera_model}."
            )

        if unsupported_keys:
            shown = ", ".join(unsupported_keys[:12])
            if len(unsupported_keys) > 12:
                shown += ", ..."
            warning_parts.append(
                f"Unsupported backup keys for the current camera: {shown}"
            )

        if warning_parts:
            proceed = messagebox.askyesno(
                APP_TITLE,
                "\n\n".join(warning_parts)
                + "\n\n"
                + "The backup can still be loaded into the form for review/editing.\n"
                + "When writing, unsupported keys from the backup will be ignored because writes are based on the current camera's last live-read ISP structure.\n\n"
                + "Load this backup anyway?",
            )
            if not proceed:
                self.set_status("Load backup cancelled.")
                return

        self.loaded_backup_dev_info = deepcopy(dev_info) if dev_info else None
        self.loaded_backup_path = path
        self._refresh_backup_info_label()
        self.last_read_isp = deepcopy(isp)

        # Keep the currently connected camera metadata as the active device
        # context when available; otherwise fall back to the loaded backup.
        active_dev_info = self.camera_dev_info if self.camera_dev_info is not None else dev_info
        self.current_dev_info = deepcopy(active_dev_info) if active_dev_info else None
        self.last_read_dev_info = deepcopy(active_dev_info) if active_dev_info else None

        self.populate_from_isp(isp)

        if backup_model and camera_model and backup_model != camera_model:
            self.set_status(f"Loaded backup from {backup_model} while connected camera is {camera_model}.")
        else:
            self.set_status(f"Loaded backup: {path}")

def main() -> None:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.1)
    except Exception:
        pass
    ttk.Style().theme_use("vista" if "vista" in ttk.Style().theme_names() else ttk.Style().theme_use())
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
