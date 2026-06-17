"""
WiFi Manager - 轻量 WiFi 连接管理工具
扫描、连接、断线自动重连、开机自启动、系统托盘
"""

import subprocess
import threading
import tempfile
import json
import os
import sys
import winreg
import time
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from PIL import Image, ImageDraw
import pystray

APP_NAME = "WiFiManager"
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_ARG = "--startup"

_NETSH_ENCODINGS = ["utf-8", "gbk", "gb18030"]


def _config_path():
    """配置文件路径：exe 或 py 同目录"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "wifi_config.json")


# ── 持久化配置 ─────────────────────────────────────────────

class Config:
    _instance = None
    _data = {"passwords": {}, "auto_targets": {}}

    @classmethod
    def load(cls):
        path = _config_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cls._data = json.load(f)
            except Exception:
                cls._data = {"passwords": {}, "auto_targets": {}}
        cls._instance = cls

    @classmethod
    def save(cls):
        path = _config_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cls._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @classmethod
    def get_passwords(cls):
        return cls._data.get("passwords", {})

    @classmethod
    def set_password(cls, ssid, password):
        cls._data.setdefault("passwords", {})[ssid] = password
        cls.save()

    @classmethod
    def get_auto_targets(cls):
        return cls._data.get("auto_targets", {})

    @classmethod
    def set_auto_target(cls, ssid, info):
        cls._data.setdefault("auto_targets", {})[ssid] = info
        cls.save()

    @classmethod
    def remove_auto_target(cls, ssid):
        cls._data.get("auto_targets", {}).pop(ssid, None)
        cls.save()


Config.load()


def _run_netsh(args, timeout=15):
    result = subprocess.run(
        ["netsh"] + args,
        capture_output=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    for enc in _NETSH_ENCODINGS:
        try:
            decoded = result.stdout.decode(enc, errors="strict")
            if any('一' <= c <= '鿿' for c in decoded[:100]):
                return decoded
        except (UnicodeDecodeError, UnicodeError):
            continue
    return result.stdout.decode("utf-8", errors="replace")


def _is_launched_from_startup():
    return STARTUP_ARG in sys.argv


def _create_tray_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.arc([8, 20, 56, 60], 210, 330, fill="white", width=4)
    draw.arc([14, 14, 50, 54], 210, 330, fill="white", width=4)
    draw.arc([20, 8, 44, 48], 210, 330, fill="white", width=4)
    draw.ellipse([28, 42, 36, 50], fill="white")
    return img


# ── WiFi 后端 ──────────────────────────────────────────────

class WiFiBackend:

    @staticmethod
    def scan():
        try:
            stdout = _run_netsh(["wlan", "show", "networks", "mode=bssid"])
        except Exception:
            return []

        networks = []
        current = {}
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("SSID") and "BSSID" not in line:
                if current.get("ssid"):
                    networks.append(current)
                parts = line.split(":", 1)
                ssid = parts[1].strip() if len(parts) > 1 else ""
                current = {"ssid": ssid, "auth": "", "encryption": "", "signal": 0, "status": ""}
            elif "Authentication" in line or "身份验证" in line:
                current["auth"] = line.split(":", 1)[1].strip()
            elif "Encryption" in line or "加密" in line:
                current["encryption"] = line.split(":", 1)[1].strip()
            elif "Signal" in line or "信号" in line:
                pct = line.split(":", 1)[1].strip().replace("%", "")
                try:
                    current["signal"] = int(pct)
                except ValueError:
                    current["signal"] = 0
            elif "Network type" in line:
                current["status"] = line.split(":", 1)[1].strip()

        if current.get("ssid"):
            networks.append(current)

        networks.sort(key=lambda x: x["signal"], reverse=True)
        return networks

    @staticmethod
    def get_connected_ssid():
        try:
            stdout = _run_netsh(["wlan", "show", "interfaces"], timeout=10)
            connected = False
            for line in stdout.splitlines():
                line = line.strip()
                if ("State" in line or "状态" in line) and ("connected" in line.lower() or "已连接" in line):
                    connected = True
                if connected and "SSID" in line and "BSSID" not in line:
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return None

    @staticmethod
    def _profile_xml(ssid, auth, encryption, password="", auto_connect=True):
        if "WPA2" in auth:
            auth_map, encrypt_map = "WPA2PSK", "AES"
        elif "WPA3" in auth:
            auth_map, encrypt_map = "WPA3SAE", "AES"
        elif "WPA" in auth:
            auth_map, encrypt_map = "WPAPSK", "TKIP"
        elif "Open" in auth or auth == "":
            auth_map, encrypt_map = "open", "none"
        else:
            auth_map, encrypt_map = "WPA2PSK", "AES"

        conn_mode = "auto" if auto_connect else "manual"

        if password:
            auth_block = f"""
        <authEncryption>
            <authentication>{auth_map}</authentication>
            <encryption>{encrypt_map}</encryption>
            <useOneX>false</useOneX>
        </authEncryption>
        <sharedKey>
            <keyType>passPhrase</keyType>
            <protected>false</protected>
            <keyMaterial>{password}</keyMaterial>
        </sharedKey>"""
        else:
            auth_block = f"""
        <authEncryption>
            <authentication>{auth_map}</authentication>
            <encryption>{encrypt_map}</encryption>
            <useOneX>false</useOneX>
        </authEncryption>"""

        return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{ssid}</name>
    <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>{conn_mode}</connectionMode>
    <MSM>
        <security>{auth_block}
        </security>
    </MSM>
</WLANProfile>"""

    @classmethod
    def connect(cls, ssid, auth="", encryption="", password="", auto_connect=True):
        xml = cls._profile_xml(ssid, auth, encryption, password, auto_connect)
        tmp = os.path.join(tempfile.gettempdir(), f"{ssid}_profile.xml")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(xml)
            _run_netsh(["wlan", "add", "profile", f"filename={tmp}"], timeout=10)
            stdout = _run_netsh(["wlan", "connect", f"name={ssid}"], timeout=10)
            return "successfully" in stdout.lower() or "成功" in stdout
        except Exception:
            return False
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    @staticmethod
    def disconnect():
        try:
            _run_netsh(["wlan", "disconnect"], timeout=10)
            return True
        except Exception:
            return False


# ── 自动重连管理器 ──────────────────────────────────────────

class AutoReconnect:

    def __init__(self, on_status_change=None):
        self._targets = {}
        self._running = False
        self._thread = None
        self._on_status = on_status_change or (lambda msg: None)
        # 从配置恢复
        for ssid, info in Config.get_auto_targets().items():
            self._targets[ssid] = info
        # 如果有已保存的目标，立即启动监控
        if self._targets:
            self._running = True
            self._thread = threading.Thread(target=self._monitor, daemon=True)
            self._thread.start()

    @property
    def targets(self):
        return dict(self._targets)

    def add_target(self, ssid, auth="", encryption="", password=""):
        info = {"auth": auth, "encryption": encryption, "password": password}
        self._targets[ssid] = info
        Config.set_auto_target(ssid, info)
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._monitor, daemon=True)
            self._thread.start()

    def remove_target(self, ssid):
        self._targets.pop(ssid, None)
        Config.remove_auto_target(ssid)

    def has_target(self, ssid):
        return ssid in self._targets

    def _monitor(self):
        backoff = 5
        while self._running:
            time.sleep(5)
            if not self._targets:
                continue

            connected = WiFiBackend.get_connected_ssid()
            if connected and connected in self._targets:
                backoff = 5
                continue

            target_ssid = None
            for ssid in self._targets:
                if ssid != connected:
                    target_ssid = ssid
                    break

            if not target_ssid:
                continue

            info = self._targets[target_ssid]
            self._on_status(f"正在重连 {target_ssid}...")
            success = WiFiBackend.connect(
                target_ssid, info["auth"], info["encryption"], info["password"]
            )
            if success:
                time.sleep(3)
                if WiFiBackend.get_connected_ssid() == target_ssid:
                    self._on_status(f"已重新连接到 {target_ssid}")
                    backoff = 5
                    continue

            self._on_status(f"重连 {target_ssid} 失败，{backoff}秒后重试...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── 开机自启 ──────────────────────────────────────────────

class StartupManager:

    @staticmethod
    def is_enabled():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except FileNotFoundError:
            return False

    @staticmethod
    def enable():
        if getattr(sys, 'frozen', False):
            exe_path = f'"{sys.executable}" {STARTUP_ARG}'
        else:
            exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}" {STARTUP_ARG}'
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
            return True
        except Exception:
            return False

    @staticmethod
    def disable():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, APP_NAME)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            return False


# ── GUI ────────────────────────────────────────────────────

class WiFiApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("WiFi Manager")
        self.root.geometry("520x420")
        self.root.minsize(460, 350)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.networks = []
        self.connected_ssid = None
        # 从配置恢复密码
        self._passwords = dict(Config.get_passwords())
        self.auto_reconnect = AutoReconnect(on_status_change=self._update_status_async)
        self._tray_icon = None

        self._build_ui()
        self._sync_startup_checkbox()
        self._refresh_status()
        self.root.after(500, self.scan_wifi)

    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=5)
        toolbar.pack(fill=tk.X)

        self.btn_scan = ttk.Button(toolbar, text="扫描WiFi", command=self.scan_wifi)
        self.btn_scan.pack(side=tk.LEFT, padx=2)

        self.btn_refresh = ttk.Button(toolbar, text="刷新", command=self.scan_wifi)
        self.btn_refresh.pack(side=tk.LEFT, padx=2)

        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        columns = ("ssid", "signal", "auth", "auto", "status")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("ssid", text="WiFi 名称")
        self.tree.heading("signal", text="信号")
        self.tree.heading("auth", text="加密方式")
        self.tree.heading("auto", text="自动重连")
        self.tree.heading("status", text="状态")
        self.tree.column("ssid", width=170)
        self.tree.column("signal", width=50, anchor=tk.CENTER)
        self.tree.column("auth", width=100)
        self.tree.column("auto", width=70, anchor=tk.CENTER)
        self.tree.column("status", width=80, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", lambda e: self.connect_wifi())

        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill=tk.X)

        self.btn_connect = ttk.Button(btn_frame, text="连接", command=self.connect_wifi)
        self.btn_connect.pack(side=tk.LEFT, padx=2)

        self.btn_disconnect = ttk.Button(btn_frame, text="断开", command=self.disconnect_wifi)
        self.btn_disconnect.pack(side=tk.LEFT, padx=2)

        self.btn_toggle_auto = ttk.Button(btn_frame, text="开启自动重连", command=self._toggle_auto_reconnect)
        self.btn_toggle_auto.pack(side=tk.LEFT, padx=8)

        self.startup_var = tk.BooleanVar()
        self.chk_startup = ttk.Checkbutton(
            btn_frame, text="开机自启动",
            variable=self.startup_var,
            command=self._toggle_startup
        )
        self.chk_startup.pack(side=tk.RIGHT, padx=5)

        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, padding=3)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    # ── 系统托盘 ──────────────────────────────────────────

    def _create_tray(self):
        if self._tray_icon:
            return

        icon_image = _create_tray_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._tray_show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            APP_NAME, icon_image, "WiFi Manager", menu
        )
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _tray_show_window(self, icon=None, item=None):
        self.root.after(0, self._show_window)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self._quit)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_close(self):
        self._create_tray()
        self.root.withdraw()

    def _quit(self):
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None
        self.root.destroy()

    # ── 状态管理 ──────────────────────────────────────────

    def _update_status_async(self, msg):
        self.root.after(0, lambda: self.status_var.set(msg))

    def _refresh_status(self):
        def _check():
            ssid = WiFiBackend.get_connected_ssid()
            self.root.after(0, lambda: self._on_status_refresh(ssid))
        threading.Thread(target=_check, daemon=True).start()
        self.root.after(10000, self._refresh_status)

    def _on_status_refresh(self, ssid):
        self.connected_ssid = ssid
        targets = self.auto_reconnect.targets
        if ssid:
            auto = "开启" if ssid in targets else "关闭"
            self.status_var.set(f"已连接: {ssid}  |  自动重连: {auto}")
        elif targets:
            names = ", ".join(targets.keys())
            self.status_var.set(f"未连接  |  自动重连目标: {names}")
        else:
            self.status_var.set("未连接")
        self._update_tree_status()

    def _update_tree_status(self):
        for item in self.tree.get_children():
            vals = self.tree.item(item, "values")
            ssid = vals[0]
            signal = vals[1]
            auth = vals[2]
            auto = "ON" if self.auto_reconnect.has_target(ssid) else ""
            status = "✓ 已连接" if ssid == self.connected_ssid else ""
            self.tree.item(item, values=(ssid, signal, auth, auto, status))

    # ── WiFi 操作 ─────────────────────────────────────────

    def scan_wifi(self):
        self.btn_scan.configure(state=tk.DISABLED)
        self.status_var.set("正在扫描...")

        def _scan():
            nets = WiFiBackend.scan()
            connected = WiFiBackend.get_connected_ssid()
            self.root.after(0, lambda: self._on_scan_done(nets, connected))

        threading.Thread(target=_scan, daemon=True).start()

    def _on_scan_done(self, networks, connected_ssid):
        self.networks = networks
        self.connected_ssid = connected_ssid
        self.tree.delete(*self.tree.get_children())
        for net in networks:
            auto = "ON" if self.auto_reconnect.has_target(net["ssid"]) else ""
            status = "✓ 已连接" if net["ssid"] == connected_ssid else ""
            signal_display = f"{net['signal']}%"
            self.tree.insert("", tk.END, values=(net["ssid"], signal_display, net["auth"], auto, status))
        self.btn_scan.configure(state=tk.NORMAL)
        self.status_var.set(f"发现 {len(networks)} 个网络" + (f"  |  已连接: {connected_ssid}" if connected_ssid else ""))

    def connect_wifi(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个 WiFi 网络")
            return

        item = self.tree.item(sel[0])
        ssid = item["values"][0]
        auth = item["values"][2]

        net_info = next((n for n in self.networks if n["ssid"] == ssid), None)
        if not net_info:
            return

        password = self._passwords.get(ssid, "")
        if not password and "open" not in auth.lower() and auth:
            password = simpledialog.askstring("输入密码", f"请输入 {ssid} 的密码:", show="*")
            if password is None:
                return

        self.btn_connect.configure(state=tk.DISABLED)
        self.status_var.set(f"正在连接 {ssid}...")

        def _connect():
            success = WiFiBackend.connect(
                ssid, net_info["auth"], net_info["encryption"], password, auto_connect=True
            )
            time.sleep(2)
            actual = WiFiBackend.get_connected_ssid()
            self.root.after(0, lambda: self._on_connect_done(ssid, net_info, password, success, actual))

        threading.Thread(target=_connect, daemon=True).start()

    def _on_connect_done(self, ssid, net_info, password, success, actual_ssid):
        self.btn_connect.configure(state=tk.NORMAL)
        if actual_ssid == ssid:
            self.connected_ssid = ssid
            if password:
                self._passwords[ssid] = password
                Config.set_password(ssid, password)
            self.auto_reconnect.add_target(ssid, net_info["auth"], net_info["encryption"], password)
            self.status_var.set(f"已连接到 {ssid}  |  自动重连: 开启")
            self._update_tree_status()
        elif success:
            self.status_var.set(f"正在连接 {ssid}...")
        else:
            self.status_var.set(f"连接 {ssid} 失败")
            messagebox.showerror("连接失败", f"无法连接到 {ssid}，请检查密码是否正确。")

    def disconnect_wifi(self):
        WiFiBackend.disconnect()
        self.connected_ssid = None
        self.status_var.set("已断开")
        self._update_tree_status()

    def _toggle_auto_reconnect(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个 WiFi 网络")
            return

        ssid = self.tree.item(sel[0])["values"][0]
        if self.auto_reconnect.has_target(ssid):
            self.auto_reconnect.remove_target(ssid)
            self.status_var.set(f"已关闭 {ssid} 的自动重连")
        else:
            net_info = next((n for n in self.networks if n["ssid"] == ssid), None)
            auth = net_info["auth"] if net_info else ""
            password = self._passwords.get(ssid, "")
            if not password and "open" not in auth.lower() and auth:
                password = simpledialog.askstring("输入密码", f"请输入 {ssid} 的密码（用于自动重连）:", show="*")
                if password is None:
                    return
            enc = net_info["encryption"] if net_info else ""
            self.auto_reconnect.add_target(ssid, auth, enc, password)
            if password:
                self._passwords[ssid] = password
                Config.set_password(ssid, password)
            self.status_var.set(f"已开启 {ssid} 的自动重连")
        self._update_tree_status()

    # ── 开机自启 ──────────────────────────────────────────

    def _sync_startup_checkbox(self):
        self.startup_var.set(StartupManager.is_enabled())

    def _toggle_startup(self):
        if self.startup_var.get():
            if StartupManager.enable():
                self.status_var.set("已开启开机自启动")
            else:
                self.startup_var.set(False)
                messagebox.showerror("错误", "设置开机自启动失败")
        else:
            StartupManager.disable()
            self.status_var.set("已关闭开机自启动")

    def run(self):
        self.root.mainloop()


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    app = WiFiApp()

    if _is_launched_from_startup():
        app._create_tray()
        app.root.withdraw()

    app.run()
