"""
Hamlib GUI - a small launcher for rigctld on Windows.

Features
  * Downloads the latest official Hamlib Windows build from GitHub
  * Lists all supported radios (from rigctl -l) with a search box
  * Detects COM ports, sets baud rate / TCP port / CI-V address / PTT
  * Start / Stop rigctld, live log output, "Read frequency" test button
  * Remembers your settings
  * Remote TCP relay: lets another computer use this radio by selecting
    "Hamlib NET rigctl" with this PC's IP:port, with a live CAT traffic monitor
  * Dark UI in the style of OpsLog (Graphite dark / Warm dark / High contrast)
  * Reverse connection: the radio PC dials OUT to the remote PC (no inbound port
    forwarding needed); the remote PC runs this app in "Accept" mode

Run:    python hamlib_gui.py
Build:  pyinstaller --onefile --noconsole hamlib_gui.py   (see build_exe.bat)
"""
import glob
import json
import os
import queue
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk

IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                          else os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "hamlib_gui.json")
DEFAULT_HAMLIB_DIR = os.path.join(APP_DIR, "hamlib")
API_LATEST = "https://api.github.com/repos/Hamlib/Hamlib/releases/latest"
BAUDS = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]
REV_MODES = ["Connect out (radio PC)", "Accept from radio PC (remote PC)"]
PTT_TYPES = ["(default)", "RIG", "RTS", "DTR", "CM108", "GPIO", "NONE"]
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

# All colours live here - tweak a value and the whole UI follows.
THEMES = {
    "Graphite dark": dict(
        bg="#15171a", surface="#1c1f24", surface2="#252930", border="#333842",
        text="#e4e7eb", muted="#8a919c", accent="#3b9eff", accent_fg="#06101a",
        ok="#3ecf8e", warn="#f2b14a", bad="#ff6b6b",
        log_bg="#0f1113", tx="#7cc4ff", rx="#8be0a5"),
    "Warm dark": dict(
        bg="#1a1613", surface="#221d19", surface2="#2c2621", border="#3d352e",
        text="#ece3d9", muted="#9a8e82", accent="#e8a04a", accent_fg="#1a1006",
        ok="#8fcf6a", warn="#f0c05a", bad="#ef6f5e",
        log_bg="#13100d", tx="#f0b36a", rx="#9ad19a"),
    "High contrast": dict(
        bg="#000000", surface="#0a0a0a", surface2="#1a1a1a", border="#ffffff",
        text="#ffffff", muted="#c8c8c8", accent="#ffd400", accent_fg="#000000",
        ok="#00ff7f", warn="#ffd400", bad="#ff5050",
        log_bg="#000000", tx="#4dd2ff", rx="#00ff7f"),
}
DEFAULT_THEME = "Graphite dark"
UI_FONTS = ("Segoe UI", "Inter", "Helvetica Neue", "DejaVu Sans", "Arial")
MONO_FONTS = ("Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", "Courier New")
STATUS_STYLE = {"ok": "Ok.TLabel", "warn": "Warn.TLabel", "bad": "Bad.TLabel"}


def mix(c1, c2, t):
    """Blend colour c1 towards c2 by t (0..1)."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


# ---------------------------------------------------------------- helpers
def find_bin_dir(root):
    """Return the folder that contains rigctld(.exe) somewhere under root."""
    if not root or not os.path.isdir(root):
        return None
    for dirpath, _dirs, files in os.walk(root):
        if "rigctld" + EXE in files:
            return dirpath
    return None


def autodetect_bin_dir(saved=None):
    candidates = [saved, DEFAULT_HAMLIB_DIR]
    candidates += glob.glob(r"C:\Program Files\hamlib*") + glob.glob(r"C:\Program Files (x86)\hamlib*")
    candidates += glob.glob(r"C:\hamlib*")
    for c in candidates:
        found = find_bin_dir(c)
        if found:
            return found
    which = shutil.which("rigctld")
    return os.path.dirname(which) if which else None


def list_serial_ports():
    ports = []
    if IS_WIN:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
            i = 0
            while True:
                try:
                    ports.append(winreg.EnumValue(key, i)[1])
                    i += 1
                except OSError:
                    break
        except OSError:
            pass
    else:
        ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    return sorted(ports, key=lambda p: (len(p), p))


def parse_rig_list(text):
    """Parse `rigctl -l` output into [(id, mfg, model)]."""
    rigs = []
    for line in text.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 3 and parts[0].isdigit():
            rigs.append((int(parts[0]), parts[1], parts[2]))
    return rigs


def local_ip():
    """Best guess at this PC's LAN address (no packets are actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class PipeMixin:
    """Shared socket helpers. Needs self.log and self.monitor."""

    @staticmethod
    def _close(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    @staticmethod
    def _tune(sock):
        for lvl, opt in ((socket.IPPROTO_TCP, socket.TCP_NODELAY),
                         (socket.SOL_SOCKET, socket.SO_KEEPALIVE)):
            try:
                sock.setsockopt(lvl, opt, 1)
            except OSError:
                pass

    def _pipe(self, src, dst, tag):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                if self.monitor():
                    text = data.decode(errors="replace").strip().replace("\n", " | ")
                    self.log(f"  {tag} {text}")
                dst.sendall(data)
        except OSError:
            pass
        finally:
            # make the opposite direction end too
            self._close(src)
            self._close(dst)


class TcpRelay(PipeMixin):
    """Accepts remote TCP clients and pipes them to the local rigctld.

    Remote PC -> relay -> 127.0.0.1:<rigctld port> and the replies back.
    Every client gets its own connection to rigctld, so several programs
    can share the radio just like they can with rigctld itself.
    """

    def __init__(self, bind, port, target_port, allowed, log, monitor, on_change=None):
        self.bind, self.port, self.target_port = bind, port, target_port
        self.allowed = set(allowed)
        self.log, self.monitor = log, monitor      # monitor: callable -> bool
        self.on_change = on_change or (lambda: None)
        self.server = None
        self.clients = {}                          # client socket -> address
        self.lock = threading.Lock()
        self.stopping = False

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.bind, self.port))
        self.server.listen(5)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self.stopping = True
        try:
            self.server.close()
        except (OSError, AttributeError):
            pass
        with self.lock:
            socks = list(self.clients)
        for c in socks:
            self._close(c)

    def count(self):
        with self.lock:
            return len(self.clients)

    def _accept_loop(self):
        while not self.stopping:
            try:
                client, addr = self.server.accept()
            except OSError:
                break
            ip = addr[0]
            if self.allowed and ip not in self.allowed and ip != "127.0.0.1":
                self.log(f"Relay: rejected {ip} (not in allowed list)")
                self._close(client)
                continue
            threading.Thread(target=self._handle, args=(client, addr), daemon=True).start()

    def _handle(self, client, addr):
        tag = f"{addr[0]}:{addr[1]}"
        try:
            rig = socket.create_connection(("127.0.0.1", self.target_port), timeout=5)
        except OSError as e:
            self.log(f"Relay: {tag} connected but rigctld is unreachable: {e}")
            self._close(client)
            return
        rig.settimeout(None)
        for sk in (client, rig):
            try:
                sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        with self.lock:
            self.clients[client] = tag
        self.log(f"Relay: client connected {tag}")
        self.on_change()
        t = threading.Thread(target=self._pipe, args=(rig, client, f"{tag} <<"), daemon=True)
        t.start()
        self._pipe(client, rig, f"{tag} >>")       # blocks until the client leaves
        self._close(client)
        self._close(rig)
        with self.lock:
            self.clients.pop(client, None)
        self.log(f"Relay: client disconnected {tag}")
        self.on_change()

class ReverseConnector(PipeMixin):
    """Radio PC side. Dials OUT to the remote PC and serves rigctld through it.

    One idle connection is always kept open to the remote PC. When the remote
    sends its first bytes (a client wants the radio) the idle connection is
    bridged to the local rigctld and a fresh idle connection is dialled.
    """

    def __init__(self, host, port, target_port, log, monitor, on_change=None):
        self.host, self.port, self.target_port = host, port, target_port
        self.log, self.monitor = log, monitor
        self.on_change = on_change or (lambda: None)
        self.stopping = False
        self.up = False            # idle link to remote PC established
        self.warned = False
        self.active = 0            # sessions currently bridged to rigctld
        self.socks = set()
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._session, daemon=True).start()

    def stop(self):
        self.stopping = True
        with self.lock:
            socks = list(self.socks)
        for sk in socks:
            self._close(sk)

    def status_text(self):
        if self.active:
            return f"Reverse: {self.active} session(s) active", "ok"
        if self.up:
            return "Reverse: linked, waiting for remote client", "ok"
        return "Reverse: connecting...", "warn"

    def _track(self, sk, add=True):
        with self.lock:
            (self.socks.add if add else self.socks.discard)(sk)

    def _session(self):
        while not self.stopping:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=8)
            except OSError as e:
                if not self.warned:
                    self.log(f"Reverse: cannot reach {self.host}:{self.port} ({e}); retrying...")
                self.warned = True
                self.up = False
                self.on_change()
                time.sleep(5)
                continue
            sock.settimeout(None)
            self._tune(sock)
            self._track(sock)
            if not self.up:
                self.log(f"Reverse: linked to {self.host}:{self.port}, waiting for remote client")
            self.up, self.warned = True, False
            self.on_change()
            try:
                data = sock.recv(4096)               # idle until the remote needs us
            except OSError:
                data = b""
            if not data:
                self._track(sock, False)
                self._close(sock)
                if self.stopping:
                    return
                self.up = False
                self.log("Reverse: link to remote PC lost, reconnecting...")
                self.on_change()
                time.sleep(3)
                continue
            # a remote client arrived: keep one idle link ready, then bridge this one
            threading.Thread(target=self._session, daemon=True).start()
            self.up = False
            try:
                rig = socket.create_connection(("127.0.0.1", self.target_port), timeout=5)
            except OSError as e:
                self.log(f"Reverse: remote client arrived but rigctld is unreachable: {e}")
                self._track(sock, False)
                self._close(sock)
                return
            rig.settimeout(None)
            self._tune(rig)
            self._track(rig)
            with self.lock:
                self.active += 1
            self.log("Reverse: remote client session started")
            self.on_change()
            try:
                if self.monitor():
                    self.log("  rev >> " + data.decode(errors="replace").strip().replace("\n", " | "))
                rig.sendall(data)
                threading.Thread(target=self._pipe, args=(rig, sock, "rev <<"), daemon=True).start()
                self._pipe(sock, rig, "rev >>")
            except OSError:
                pass
            self._close(sock)
            self._close(rig)
            self._track(sock, False)
            self._track(rig, False)
            with self.lock:
                self.active -= 1
            self.log("Reverse: remote client session ended")
            self.on_change()
            return


class TunnelServer(PipeMixin):
    """Remote PC side. Waits for the radio PC to dial in, and offers a local
    port that Hamlib (model "NET rigctl") connects to. Each Hamlib client is
    paired with one idle connection from the radio PC."""

    def __init__(self, tunnel_bind, tunnel_port, client_bind, client_port,
                 allowed, log, monitor, on_change=None):
        self.tunnel_addr = (tunnel_bind, tunnel_port)
        self.client_addr = (client_bind, client_port)
        self.allowed = set(allowed)
        self.log, self.monitor = log, monitor
        self.on_change = on_change or (lambda: None)
        self.stopping = False
        self.servers = []
        self.pool = []             # idle connections from the radio PC
        self.active = 0
        self.socks = set()
        self.cond = threading.Condition()

    def start(self):
        for addr, target in ((self.tunnel_addr, self._tunnel_accept),
                             (self.client_addr, self._client_accept)):
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(addr)
            except OSError:
                srv.close()
                self.stop()
                raise
            srv.listen(5)
            self.servers.append(srv)
            threading.Thread(target=target, args=(srv,), daemon=True).start()

    def stop(self):
        self.stopping = True
        for srv in self.servers:
            try:
                srv.close()
            except OSError:
                pass
        with self.cond:
            socks = list(self.pool) + list(self.socks)
            self.pool.clear()
            self.cond.notify_all()
        for sk in socks:
            self._close(sk)

    @staticmethod
    def _alive(sk):
        try:
            readable, _, _ = select.select([sk], [], [], 0)
        except (OSError, ValueError):
            return False
        return not readable        # idle link should be silent; readable == closed

    def _prune(self):
        with self.cond:
            dead = [sk for sk in self.pool if not self._alive(sk)]
            for sk in dead:
                self.pool.remove(sk)
        for sk in dead:
            self._close(sk)

    def status_text(self):
        self._prune()
        with self.cond:
            idle = len(self.pool)
        if self.active:
            return f"Accept: {self.active} client(s) active", "ok"
        if idle:
            return "Accept: radio PC linked", "ok"
        return "Accept: waiting for radio PC...", "warn"

    def _tunnel_accept(self, srv):
        while not self.stopping:
            try:
                sk, addr = srv.accept()
            except OSError:
                break
            if self.allowed and addr[0] not in self.allowed:
                self.log(f"Accept: rejected radio PC connection from {addr[0]} (not in allowed list)")
                self._close(sk)
                continue
            self._tune(sk)
            with self.cond:
                first = not self.pool and not self.active
                self.pool.append(sk)
                self.cond.notify_all()
            if first:
                self.log(f"Accept: radio PC linked from {addr[0]}")
            self.on_change()

    def _take_tunnel(self, timeout=10):
        deadline = time.time() + timeout
        with self.cond:
            while not self.stopping:
                while self.pool:
                    sk = self.pool.pop(0)
                    if self._alive(sk):
                        return sk
                    self._close(sk)
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)
        return None

    def _client_accept(self, srv):
        while not self.stopping:
            try:
                client, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_client, args=(client, addr), daemon=True).start()

    def _serve_client(self, client, addr):
        tag = f"{addr[0]}:{addr[1]}"
        self._tune(client)
        tun = self._take_tunnel()
        if tun is None:
            self.log(f"Accept: client {tag} refused - radio PC is not linked")
            self._close(client)
            return
        with self.cond:
            self.socks.update((client, tun))
            self.active += 1
        self.log(f"Accept: client {tag} connected to the radio")
        self.on_change()
        threading.Thread(target=self._pipe, args=(tun, client, f"{tag} <<"), daemon=True).start()
        self._pipe(client, tun, f"{tag} >>")
        with self.cond:
            self.socks.discard(client)
            self.socks.discard(tun)
            self.active -= 1
        self.log(f"Accept: client {tag} disconnected")
        self.on_change()


# -------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hamlib GUI - rigctld launcher")
        self.geometry("840x860")
        self.minsize(780, 780)

        self.cfg = self.load_config()
        self.setup_fonts()
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.theme_var = tk.StringVar(value=self.cfg.get("theme", DEFAULT_THEME))
        self.bin_dir = autodetect_bin_dir(self.cfg.get("bin_dir"))
        self.rigs = []
        self.proc = None
        self.relay = None
        self.reverse = None
        self.log_q = queue.Queue()

        self.build_ui()
        self.apply_theme()
        self.refresh_ports()
        self.after(100, self.pump_log)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if self.bin_dir:
            self.log(f"Using Hamlib in: {self.bin_dir}")
            self.load_radios()
        else:
            self.log("Hamlib not found. Click 'Download latest Hamlib' or 'Browse...'.")
        self.show_radio_info()
        self.update_buttons()

    # ---- config
    def load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_config(self):
        self.cfg.update({
            "bin_dir": self.bin_dir or "",
            "model": self.model_id,
            "port": self.port_var.get(),
            "baud": self.baud_var.get(),
            "tcp": self.tcp_var.get(),
            "civ": self.civ_var.get(),
            "ptt": self.ptt_var.get(),
            "ptt_port": self.ptt_port_var.get(),
            "extra": self.extra_var.get(),
            "verbose": self.verbose_var.get(),
            "theme": self.theme_var.get(),
            "relay_on": self.relay_on_var.get(),
            "relay_port": self.relay_port_var.get(),
            "relay_bind": self.relay_bind_var.get(),
            "relay_allow": self.relay_allow_var.get(),
            "relay_log": self.relay_log_var.get(),
            "rev_mode": self.rev_mode_var.get(),
            "rev_host": self.rev_host_var.get(),
            "rev_port": self.rev_port_var.get(),
            "rev_cport": self.rev_cport_var.get(),
            "rev_cbind": self.rev_cbind_var.get(),
            "rev_allow": self.rev_allow_var.get(),
        })
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except OSError:
            pass

    # ---- UI
    def build_ui(self):
        pad = {"padx": 6, "pady": 3}
        c = self.cfg
        self.model_id = c.get("model")

        # Header bar
        head = ttk.Frame(self, style="Header.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="Hamlib GUI", style="HeaderTitle.TLabel").pack(side="left", padx=(14, 6), pady=10)
        ttk.Label(head, text="rigctld launcher  \u00b7  TCP relay", style="HeaderSub.TLabel").pack(side="left")
        tcb = ttk.Combobox(head, textvariable=self.theme_var, values=list(THEMES), state="readonly", width=15)
        tcb.pack(side="right", padx=(6, 14))
        ttk.Label(head, text="Theme", style="HeaderSub.TLabel").pack(side="right")
        self.theme_var.trace_add("write", lambda *_: self.apply_theme())
        ttk.Frame(self, style="Border.TFrame", height=1).pack(fill="x")

        # Hamlib location
        top = ttk.LabelFrame(self, text="Hamlib installation")
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.dir_label = ttk.Label(top, text=self.bin_dir or "Not found", anchor="w", style="Mono.TLabel")
        self.dir_label.pack(side="left", fill="x", expand=True, **pad)
        ttk.Button(top, text="Browse...", command=self.browse_dir).pack(side="right", **pad)
        self.dl_btn = ttk.Button(top, text="Download latest Hamlib", command=self.download_hamlib)
        self.dl_btn.pack(side="right", **pad)

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=10, pady=4)

        # Radio list
        left = ttk.LabelFrame(mid, text="Radio")
        left.pack(side="left", fill="both", expand=True)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.fill_radio_list())
        row = ttk.Frame(left)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Search:").pack(side="left")
        ttk.Entry(row, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=4)
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True, **pad)
        self.radio_list = tk.Listbox(lf, exportselection=False, height=10, activestyle="none")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.radio_list.yview)
        self.radio_list.config(yscrollcommand=sb.set)
        self.radio_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.radio_list.bind("<<ListboxSelect>>", self.on_radio_select)
        self.radio_info = ttk.Label(left, text="No radio selected")
        self.radio_info.pack(fill="x", **pad)
        self.visible_rigs = []

        # Settings
        right = ttk.LabelFrame(mid, text="Connection")
        right.pack(side="right", fill="y", padx=(8, 0))
        self.port_var = tk.StringVar(value=c.get("port", ""))
        self.baud_var = tk.StringVar(value=c.get("baud", "9600"))
        self.tcp_var = tk.StringVar(value=c.get("tcp", "4532"))
        self.civ_var = tk.StringVar(value=c.get("civ", ""))
        self.ptt_var = tk.StringVar(value=c.get("ptt", PTT_TYPES[0]))
        self.ptt_port_var = tk.StringVar(value=c.get("ptt_port", ""))
        self.extra_var = tk.StringVar(value=c.get("extra", ""))
        self.verbose_var = tk.BooleanVar(value=c.get("verbose", True))

        def field(r, label, widget):
            ttk.Label(right, text=label).grid(row=r, column=0, sticky="w", **pad)
            widget.grid(row=r, column=1, sticky="ew", **pad)

        prow = ttk.Frame(right)
        self.port_cb = ttk.Combobox(prow, textvariable=self.port_var, width=12)
        self.port_cb.pack(side="left", fill="x", expand=True)
        ttk.Button(prow, text="\u21bb", width=3, command=self.refresh_ports).pack(side="left", padx=(3, 0))
        field(0, "COM port:", prow)
        field(1, "Baud rate:", ttk.Combobox(right, textvariable=self.baud_var, values=BAUDS, width=16))
        field(2, "TCP port:", ttk.Entry(right, textvariable=self.tcp_var, width=18))
        field(3, "CI-V address:", ttk.Entry(right, textvariable=self.civ_var, width=18))
        field(4, "PTT type:", ttk.Combobox(right, textvariable=self.ptt_var, values=PTT_TYPES,
                                          state="readonly", width=16))
        field(5, "PTT port:", ttk.Entry(right, textvariable=self.ptt_port_var, width=18))
        field(6, "Extra args:", ttk.Entry(right, textvariable=self.extra_var, width=18))
        ttk.Checkbutton(right, text="Verbose log (-v)", variable=self.verbose_var).grid(
            row=7, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(right, text="CI-V: e.g. 94 for IC-7300\n(leave empty if unsure)",
                  style="Muted.TLabel").grid(row=8, column=0, columnspan=2, sticky="w", **pad)

        # Remote relay
        net = ttk.LabelFrame(self, text="Remote access (TCP in/out relay)")
        net.pack(fill="x", padx=10, pady=4)
        self.relay_on_var = tk.BooleanVar(value=c.get("relay_on", False))
        self.relay_port_var = tk.StringVar(value=c.get("relay_port", "4533"))
        self.relay_bind_var = tk.StringVar(value=c.get("relay_bind", "0.0.0.0"))
        self.relay_allow_var = tk.StringVar(value=c.get("relay_allow", ""))
        self.relay_log_var = tk.BooleanVar(value=c.get("relay_log", True))
        r1 = ttk.Frame(net)
        r1.pack(fill="x", **pad)
        ttk.Checkbutton(r1, text="Enable relay", variable=self.relay_on_var).pack(side="left")
        ttk.Label(r1, text="Port:").pack(side="left", padx=(12, 2))
        ttk.Entry(r1, textvariable=self.relay_port_var, width=7).pack(side="left")
        ttk.Label(r1, text="Bind:").pack(side="left", padx=(12, 2))
        ttk.Entry(r1, textvariable=self.relay_bind_var, width=14).pack(side="left")
        ttk.Label(r1, text="Allowed IPs:").pack(side="left", padx=(12, 2))
        ttk.Entry(r1, textvariable=self.relay_allow_var, width=22).pack(side="left")
        ttk.Checkbutton(r1, text="Show CAT traffic", variable=self.relay_log_var).pack(side="left", padx=(12, 0))
        r2 = ttk.Frame(net)
        r2.pack(fill="x", **pad)
        self.relay_hint = ttk.Label(r2, style="Muted.TLabel", anchor="w")
        self.relay_hint.pack(side="left", fill="x", expand=True)
        self.relay_status = ttk.Label(r2, text="Relay off", style="Muted.TLabel")
        self.relay_status.pack(side="right")
        self.relay_port_var.trace_add("write", lambda *_: self.update_relay_hint())
        self.update_relay_hint()

        # Reverse connection
        rev = ttk.LabelFrame(self, text="Reverse connection (radio PC dials out to remote PC)")
        rev.pack(fill="x", padx=10, pady=4)
        self.rev_mode_var = tk.StringVar(value=c.get("rev_mode", REV_MODES[0]))
        self.rev_host_var = tk.StringVar(value=c.get("rev_host", ""))
        self.rev_port_var = tk.StringVar(value=c.get("rev_port", "4540"))
        self.rev_cport_var = tk.StringVar(value=c.get("rev_cport", "4532"))
        self.rev_cbind_var = tk.StringVar(value=c.get("rev_cbind", "127.0.0.1"))
        self.rev_allow_var = tk.StringVar(value=c.get("rev_allow", ""))
        q1 = ttk.Frame(rev)
        q1.pack(fill="x", **pad)
        ttk.Label(q1, text="Mode:").pack(side="left")
        ttk.Combobox(q1, textvariable=self.rev_mode_var, values=REV_MODES, state="readonly",
                     width=30).pack(side="left", padx=(2, 12))
        ttk.Label(q1, text="Remote host:").pack(side="left", padx=(0, 2))
        ttk.Entry(q1, textvariable=self.rev_host_var, width=18).pack(side="left")
        ttk.Label(q1, text="Tunnel port:").pack(side="left", padx=(12, 2))
        ttk.Entry(q1, textvariable=self.rev_port_var, width=7).pack(side="left")
        q2 = ttk.Frame(rev)
        q2.pack(fill="x", **pad)
        ttk.Label(q2, text="Local client port:").pack(side="left")
        ttk.Entry(q2, textvariable=self.rev_cport_var, width=7).pack(side="left", padx=(2, 12))
        ttk.Label(q2, text="Client bind:").pack(side="left", padx=(0, 2))
        ttk.Entry(q2, textvariable=self.rev_cbind_var, width=14).pack(side="left")
        ttk.Label(q2, text="Allowed IPs:").pack(side="left", padx=(12, 2))
        ttk.Entry(q2, textvariable=self.rev_allow_var, width=22).pack(side="left")
        q3 = ttk.Frame(rev)
        q3.pack(fill="x", **pad)
        self.rev_btn = ttk.Button(q3, text="Start reverse", command=self.toggle_reverse)
        self.rev_btn.pack(side="left")
        self.rev_status = ttk.Label(q3, text="Reverse off", style="Muted.TLabel")
        self.rev_status.pack(side="right")
        self.rev_hint = ttk.Label(rev, style="Muted.TLabel", anchor="w", wraplength=780, justify="left")
        self.rev_hint.pack(fill="x", padx=6, pady=(0, 3))
        for v in (self.rev_mode_var, self.rev_host_var, self.rev_port_var,
                  self.rev_cport_var, self.rev_cbind_var):
            v.trace_add("write", lambda *_: self.update_reverse_hint())
        self.update_reverse_hint()

        # Buttons
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=6)
        self.start_btn = ttk.Button(bar, text="\u25b6 Start rigctld", command=self.start, style="Accent.TButton")
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(bar, text="\u25a0 Stop", command=self.stop, style="Danger.TButton")
        self.stop_btn.pack(side="left", padx=4)
        self.freq_btn = ttk.Button(bar, text="Read frequency", command=self.read_freq)
        self.freq_btn.pack(side="left", padx=4)
        self.status = ttk.Label(bar, text="Stopped", style="ChipBad.TLabel")
        self.status.pack(side="right", padx=8)

        for v in (self.port_var, self.baud_var, self.tcp_var, self.civ_var,
                  self.ptt_var, self.ptt_port_var, self.extra_var, self.verbose_var,
                  self.relay_on_var, self.relay_port_var, self.relay_bind_var,
                  self.relay_allow_var, self.relay_log_var,
                  self.rev_mode_var, self.rev_host_var, self.rev_port_var,
                  self.rev_cport_var, self.rev_cbind_var, self.rev_allow_var):
            v.trace_add("write", lambda *_: self.save_config())

        # Log
        lg = ttk.LabelFrame(self, text="Log")
        lg.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_box = tk.Text(lg, height=9, state="disabled", wrap="word",
                               relief="flat", borderwidth=0, padx=8, pady=6)
        lsb = ttk.Scrollbar(lg, orient="vertical", command=self.log_box.yview)
        self.log_box.config(yscrollcommand=lsb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    # ---- look & feel
    def setup_fonts(self):
        have = {f.lower(): f for f in tkfont.families(self)}
        pick = lambda prefs, fallback: next((have[f.lower()] for f in prefs if f.lower() in have), fallback)
        self.font_ui = pick(UI_FONTS, "TkDefaultFont")
        self.font_mono = pick(MONO_FONTS, "TkFixedFont")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(family=self.font_ui, size=9)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont("TkFixedFont").configure(family=self.font_mono, size=9)
        except tk.TclError:
            pass

    def log_tag(self, msg):
        m = re.match(r"\s+\S+ (>>|<<) ", msg)
        if m:
            return "tx" if m.group(1) == ">>" else "rx"
        low = msg.lower()
        if any(w in low for w in ("error", "cannot", "could not", "failed", "refused", "rejected", "unreachable")):
            return "bad"
        if any(w in low for w in ("lost", "exited", "not running", "stopped")):
            return "warn"
        if msg.startswith(("Relay", "Reverse", "Accept")):
            return "accent"
        return ""

    def _walk(self, w):
        yield w
        for ch in w.winfo_children():
            yield from self._walk(ch)

    def apply_theme(self):
        t = THEMES.get(self.theme_var.get()) or THEMES[DEFAULT_THEME]
        self.t = t
        ui, mono = self.font_ui, self.font_mono
        st = self.style
        self.configure(background=t["bg"])
        st.configure(".", background=t["bg"], foreground=t["text"], font=(ui, 9),
                     fieldbackground=t["surface2"], troughcolor=t["bg"], bordercolor=t["border"],
                     focuscolor=t["bg"], insertcolor=t["text"], selectbackground=t["accent"],
                     selectforeground=t["accent_fg"])
        st.configure("TFrame", background=t["bg"])
        st.configure("Header.TFrame", background=t["surface"])
        st.configure("Border.TFrame", background=t["border"])

        # labels
        st.configure("TLabel", background=t["bg"], foreground=t["text"])
        for name, key in (("Muted", "muted"), ("Ok", "ok"), ("Warn", "warn"), ("Bad", "bad")):
            st.configure(f"{name}.TLabel", background=t["bg"], foreground=t[key])
        st.configure("Mono.TLabel", background=t["bg"], foreground=t["muted"], font=(mono, 9))
        st.configure("Header.TLabel", background=t["surface"])
        st.configure("HeaderTitle.TLabel", background=t["surface"], foreground=t["text"], font=(ui, 14, "bold"))
        st.configure("HeaderSub.TLabel", background=t["surface"], foreground=t["muted"])
        for name, key in (("ChipOk", "ok"), ("ChipBad", "bad")):
            st.configure(f"{name}.TLabel", background=t["surface2"], foreground=t[key],
                         font=(ui, 9, "bold"), padding=(12, 4))

        # panels
        st.configure("TLabelframe", background=t["bg"], bordercolor=t["border"],
                     lightcolor=t["border"], darkcolor=t["border"], relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=t["bg"], foreground=t["accent"], font=(ui, 9, "bold"))

        # buttons
        base = dict(background=t["surface2"], foreground=t["text"], bordercolor=t["border"],
                    lightcolor=t["surface2"], darkcolor=t["surface2"], focuscolor=t["surface2"],
                    padding=(12, 5), relief="flat", borderwidth=1)
        st.configure("TButton", **base)
        st.map("TButton",
               background=[("disabled", t["surface"]), ("pressed", t["border"]), ("active", mix(t["surface2"], t["text"], 0.10))],
               foreground=[("disabled", t["muted"])],
               bordercolor=[("active", t["accent"]), ("disabled", t["border"])])
        st.configure("Accent.TButton", **{**base, "background": t["accent"], "foreground": t["accent_fg"],
                                          "bordercolor": t["accent"], "lightcolor": t["accent"],
                                          "darkcolor": t["accent"], "font": (ui, 9, "bold")})
        st.map("Accent.TButton",
               background=[("disabled", t["surface2"]), ("pressed", mix(t["accent"], "#000000", 0.2)),
                           ("active", mix(t["accent"], "#ffffff", 0.18))],
               foreground=[("disabled", t["muted"])],
               bordercolor=[("disabled", t["border"])],
               lightcolor=[("disabled", t["surface2"])], darkcolor=[("disabled", t["surface2"])])
        st.configure("Danger.TButton", **{**base, "foreground": t["bad"], "bordercolor": t["bad"]})
        st.map("Danger.TButton",
               background=[("disabled", t["surface"]), ("pressed", mix(t["bad"], "#000000", 0.2)), ("active", t["bad"])],
               foreground=[("disabled", t["muted"]), ("active", t["accent_fg"])],
               bordercolor=[("disabled", t["border"])])

        # inputs
        field = dict(fieldbackground=t["surface2"], foreground=t["text"], insertcolor=t["text"],
                     bordercolor=t["border"], lightcolor=t["border"], darkcolor=t["border"], padding=4)
        focus = dict(bordercolor=[("focus", t["accent"])], lightcolor=[("focus", t["accent"])],
                     darkcolor=[("focus", t["accent"])])
        st.configure("TEntry", **field)
        st.map("TEntry", **focus, fieldbackground=[("disabled", t["surface"])],
               foreground=[("disabled", t["muted"])])
        st.configure("TCombobox", **field, background=t["surface2"], arrowcolor=t["text"],
                     selectbackground=t["surface2"], selectforeground=t["text"])
        st.map("TCombobox", **focus,
               fieldbackground=[("readonly", t["surface2"]), ("disabled", t["surface"])],
               foreground=[("disabled", t["muted"])],
               selectbackground=[("readonly", t["surface2"])], selectforeground=[("readonly", t["text"])],
               background=[("active", t["border"]), ("readonly", t["surface2"])],
               arrowcolor=[("disabled", t["muted"])])
        st.configure("TCheckbutton", background=t["bg"], foreground=t["text"],
                     indicatorbackground=t["surface2"], indicatorforeground=t["accent"],
                     upperbordercolor=t["border"], lowerbordercolor=t["border"], indicatormargin=(1, 1, 6, 1))
        st.map("TCheckbutton", background=[("active", t["bg"])],
               indicatorbackground=[("pressed", t["border"]), ("disabled", t["surface"])],
               indicatorforeground=[("disabled", t["muted"])], foreground=[("disabled", t["muted"])])
        st.configure("TScrollbar", background=t["surface2"], troughcolor=t["bg"], bordercolor=t["bg"],
                     lightcolor=t["surface2"], darkcolor=t["surface2"], arrowcolor=t["muted"],
                     relief="flat", arrowsize=12)
        st.map("TScrollbar", background=[("active", t["border"])], arrowcolor=[("active", t["text"])])

        # classic tk widgets
        for w in self._walk(self):
            if isinstance(w, tk.Listbox):
                w.config(background=t["surface"], foreground=t["text"], selectbackground=t["accent"],
                         selectforeground=t["accent_fg"], relief="flat", borderwidth=0,
                         highlightthickness=1, highlightbackground=t["border"], highlightcolor=t["accent"],
                         font=(ui, 9))
            elif isinstance(w, tk.Text):
                w.config(background=t["log_bg"], foreground=t["text"], insertbackground=t["text"],
                         selectbackground=t["accent"], selectforeground=t["accent_fg"],
                         highlightthickness=1, highlightbackground=t["border"], highlightcolor=t["border"],
                         font=(mono, 9))
                for tag, key in (("tx", "tx"), ("rx", "rx"), ("bad", "bad"), ("warn", "warn"), ("accent", "accent")):
                    w.tag_config(tag, foreground=t[key])
            elif isinstance(w, ttk.Combobox):
                try:     # the drop-down list is a separate classic Tk listbox
                    pd = self.tk.call("ttk::combobox::PopdownWindow", str(w))
                    self.tk.call(f"{pd}.f.l", "configure", "-background", t["surface2"],
                                 "-foreground", t["text"], "-selectbackground", t["accent"],
                                 "-selectforeground", t["accent_fg"], "-borderwidth", 0,
                                 "-highlightthickness", 0, "-font", (ui, 9))
                except tk.TclError:
                    pass
        self.set_dark_titlebar()

    def set_dark_titlebar(self):
        """Windows 10/11: dark caption bar tinted like the theme."""
        if not IS_WIN:
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi
            val = ctypes.c_int(1)
            for attr in (20, 19):                       # immersive dark mode (new / old id)
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))

            def colorref(hexcol):                       # COLORREF is 0x00BBGGRR
                r, g, b = (int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
                return ctypes.c_int(r | (g << 8) | (b << 16))
            for attr, key in ((35, "surface"), (36, "text"), (34, "border")):   # caption / text / border (Win11)
                c = colorref(self.t[key])
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c), ctypes.sizeof(c))
        except Exception:
            pass

    # ---- remote relay
    def update_relay_hint(self):
        self.relay_hint.config(
            text=f"On the remote PC choose radio \"Hamlib NET rigctl\" (model 2), "
                 f"host {local_ip()}, port {self.relay_port_var.get() or '4533'}")

    def update_relay_status(self):
        n = self.relay.count() if self.relay else 0
        if self.relay:
            self.relay_status.config(text=f"Relay on - {n} client(s)", style="Ok.TLabel")
        else:
            self.relay_status.config(text="Relay off", style="Muted.TLabel")

    def start_relay(self):
        if self.relay or not self.relay_on_var.get():
            return
        try:
            rport = int(self.relay_port_var.get() or 4533)
            tport = int(self.tcp_var.get() or 4532)
        except ValueError:
            self.log("Relay: port must be a number.")
            return
        if rport == tport:
            self.log("Relay: relay port must differ from the rigctld TCP port.")
            return
        allowed = [a.strip() for a in re.split(r"[,\s]+", self.relay_allow_var.get()) if a.strip()]
        relay = TcpRelay(self.relay_bind_var.get().strip() or "0.0.0.0", rport, tport, allowed,
                         self.log, lambda: self.relay_log_var.get(),
                         on_change=lambda: self.after(0, self.update_relay_status))
        try:
            relay.start()
        except OSError as e:
            self.log(f"Relay: could not listen on {relay.bind}:{rport}: {e}")
            return
        self.relay = relay
        self.log(f"Relay listening on {relay.bind}:{rport} -> rigctld 127.0.0.1:{tport}"
                 + (f" (allowed: {', '.join(allowed)})" if allowed else " (all IPs allowed)"))
        self.update_relay_status()

    def stop_relay(self):
        if self.relay:
            self.relay.stop()
            self.relay = None
            self.log("Relay stopped.")
        self.update_relay_status()

    # ---- reverse connection
    def rev_is_connect_out(self):
        return self.rev_mode_var.get() == REV_MODES[0]

    def update_reverse_hint(self):
        tport = self.rev_port_var.get() or "4540"
        if self.rev_is_connect_out():
            txt = (f"This PC has the radio. It dials out to {self.rev_host_var.get() or '<remote host>'}:"
                   f"{tport}, so no port forwarding is needed here. Start rigctld first, and run this app "
                   f"in 'Accept' mode on the remote PC with the same tunnel port.")
        else:
            txt = (f"This is the remote PC. It waits for the radio PC on tunnel port {tport} (open it in "
                   f"the firewall / router). In your software choose radio 'Hamlib NET rigctl' with host "
                   f"{self.rev_cbind_var.get() or '127.0.0.1'} and port {self.rev_cport_var.get() or '4532'}.")
        self.rev_hint.config(text=txt)

    def update_reverse_status(self):
        if self.reverse:
            text, color = self.reverse.status_text()
            self.rev_status.config(text=text, style=STATUS_STYLE.get(color, "Muted.TLabel"))
            self.rev_btn.config(text="Stop reverse")
        else:
            self.rev_status.config(text="Reverse off", style="Muted.TLabel")
            self.rev_btn.config(text="Start reverse")

    def toggle_reverse(self):
        if self.reverse:
            self.stop_reverse()
        else:
            self.start_reverse()

    def start_reverse(self):
        if self.reverse:
            return
        changed = lambda: self.after(0, self.update_reverse_status)
        try:
            tunnel_port = int(self.rev_port_var.get() or 4540)
            if self.rev_is_connect_out():
                host = self.rev_host_var.get().strip()
                if not host:
                    messagebox.showwarning("Remote host", "Enter the remote PC's address first.")
                    return
                target = int(self.tcp_var.get() or 4532)
                rev = ReverseConnector(host, tunnel_port, target, self.log,
                                       lambda: self.relay_log_var.get(), on_change=changed)
                rev.start()
                if not self.running():
                    self.log("Reverse: note - rigctld is not running yet; start it so the remote can reach the radio.")
                self.log(f"Reverse: dialling out to {host}:{tunnel_port} -> rigctld 127.0.0.1:{target}")
            else:
                cport = int(self.rev_cport_var.get() or 4532)
                if cport == tunnel_port:
                    messagebox.showwarning("Ports", "Tunnel port and local client port must differ.")
                    return
                allowed = [a.strip() for a in re.split(r"[,\s]+", self.rev_allow_var.get()) if a.strip()]
                rev = TunnelServer("0.0.0.0", tunnel_port,
                                   self.rev_cbind_var.get().strip() or "127.0.0.1", cport,
                                   allowed, self.log, lambda: self.relay_log_var.get(),
                                   on_change=changed)
                rev.start()
                self.log(f"Accept: waiting for radio PC on tunnel port {tunnel_port}; "
                         f"Hamlib clients connect to {rev.client_addr[0]}:{cport}"
                         + (f" (allowed: {', '.join(allowed)})" if allowed else " (all IPs allowed)"))
        except ValueError:
            messagebox.showerror("Ports", "Ports must be numbers.")
            return
        except OSError as e:
            messagebox.showerror("Reverse", f"Could not start: {e}")
            return
        self.reverse = rev
        self.update_reverse_status()
        self.after(2000, self._poll_reverse)

    def _poll_reverse(self):
        if self.reverse:
            self.update_reverse_status()
            self.after(2000, self._poll_reverse)

    def stop_reverse(self):
        if self.reverse:
            self.reverse.stop()
            self.reverse = None
            self.log("Reverse connection stopped.")
        self.update_reverse_status()

    # ---- logging (thread safe)
    def log(self, msg):
        self.log_q.put(msg)

    def pump_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_box.config(state="normal")
                self.log_box.insert("end", msg.rstrip("\n") + "\n", self.log_tag(msg))
                self.log_box.see("end")
                self.log_box.config(state="disabled")
        except queue.Empty:
            pass
        # detect process exit
        if self.proc and self.proc.poll() is not None:
            self.log(f"rigctld exited with code {self.proc.returncode}")
            self.proc = None
            self.stop_relay()
            self.update_buttons()
        self.after(100, self.pump_log)

    # ---- Hamlib install
    def set_bin_dir(self, path):
        self.bin_dir = path
        self.dir_label.config(text=path or "Not found")
        self.save_config()
        self.update_buttons()
        if path:
            self.load_radios()

    def browse_dir(self):
        d = filedialog.askdirectory(title="Select your Hamlib folder (contains bin\\rigctld.exe)")
        if not d:
            return
        found = find_bin_dir(d)
        if not found:
            messagebox.showerror("Not found", "rigctld" + EXE + " was not found in that folder.")
            return
        self.set_bin_dir(found)
        self.log(f"Using Hamlib in: {found}")

    def download_hamlib(self):
        self.dl_btn.config(state="disabled")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        try:
            self.log("Checking latest Hamlib release on GitHub...")
            req = urllib.request.Request(API_LATEST, headers={"User-Agent": "hamlib-gui"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rel = json.load(r)
            asset = next((a for a in rel.get("assets", [])
                          if re.match(r"hamlib-w64-.*\.zip$", a["name"])), None)
            if not asset:
                raise RuntimeError("No hamlib-w64 zip found in the latest release.")
            self.log(f"Downloading {asset['name']} ({asset['size'] // 1024} KB)...")
            os.makedirs(DEFAULT_HAMLIB_DIR, exist_ok=True)
            zpath = os.path.join(DEFAULT_HAMLIB_DIR, asset["name"])
            req = urllib.request.Request(asset["browser_download_url"],
                                         headers={"User-Agent": "hamlib-gui"})
            with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
            self.log("Extracting...")
            with zipfile.ZipFile(zpath) as z:
                z.extractall(DEFAULT_HAMLIB_DIR)
            os.remove(zpath)
            found = find_bin_dir(DEFAULT_HAMLIB_DIR)
            if not found:
                raise RuntimeError("Downloaded, but rigctld.exe not found in archive.")
            self.log(f"Installed Hamlib {rel.get('tag_name', '')} to {found}")
            self.after(0, lambda: self.set_bin_dir(found))
        except Exception as e:  # noqa: BLE001
            self.log(f"Download failed: {e}")
        finally:
            self.after(0, lambda: self.dl_btn.config(state="normal"))

    # ---- radio list
    def load_radios(self):
        def worker():
            try:
                out = subprocess.run([os.path.join(self.bin_dir, "rigctl" + EXE), "-l"],
                                     capture_output=True, text=True, timeout=60,
                                     cwd=self.bin_dir, creationflags=NO_WINDOW).stdout
                rigs = parse_rig_list(out)
                self.rigs = rigs
                self.log(f"Loaded {len(rigs)} supported radios.")
                self.after(0, self.fill_radio_list)
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not list radios: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def fill_radio_list(self):
        q = self.search_var.get().strip().lower()
        self.radio_list.delete(0, "end")
        self.visible_rigs = []
        for rid, mfg, model in self.rigs:
            label = f"{mfg} {model}  [{rid}]"
            if q and q not in label.lower():
                continue
            self.visible_rigs.append(rid)
            self.radio_list.insert("end", label)
            if rid == self.model_id:
                idx = self.radio_list.size() - 1
                self.radio_list.selection_set(idx)
                self.radio_list.see(idx)
        self.show_radio_info()

    def on_radio_select(self, _e=None):
        sel = self.radio_list.curselection()
        if sel:
            self.model_id = self.visible_rigs[sel[0]]
            for rid, mfg, model in self.rigs:
                if rid == self.model_id:
                    self.cfg["model_name"] = f"{mfg} {model}"
            self.show_radio_info()
            self.save_config()

    def show_radio_info(self):
        for rid, mfg, model in self.rigs:
            if rid == self.model_id:
                self.radio_info.config(text=f"Selected: {mfg} {model} (model {rid})")
                return
        if self.model_id and self.cfg.get("model_name"):
            self.radio_info.config(
                text=f"Selected: {self.cfg['model_name']} (model {self.model_id})")
        else:
            self.radio_info.config(text="No radio selected")

    def refresh_ports(self):
        ports = list_serial_ports()
        self.port_cb["values"] = ports
        if not self.port_var.get() and ports:
            self.port_var.set(ports[0])

    # ---- run / stop
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def update_buttons(self):
        run = self.running()
        have = bool(self.bin_dir)
        self.start_btn.config(state="normal" if have and not run else "disabled")
        self.stop_btn.config(state="normal" if run else "disabled")
        self.freq_btn.config(state="normal" if run else "disabled")
        self.status.config(text="Running" if run else "Stopped",
                           style="ChipOk.TLabel" if run else "ChipBad.TLabel")

    def build_command(self):
        cmd = [os.path.join(self.bin_dir, "rigctld" + EXE), "-m", str(self.model_id)]
        if self.port_var.get().strip():
            cmd += ["-r", self.port_var.get().strip()]
        if self.baud_var.get().strip():
            cmd += ["-s", self.baud_var.get().strip()]
        cmd += ["-t", self.tcp_var.get().strip() or "4532"]
        if self.civ_var.get().strip():
            cmd += ["-c", self.civ_var.get().strip()]
        if self.ptt_var.get() != PTT_TYPES[0]:
            cmd += ["-P", self.ptt_var.get()]
        if self.ptt_port_var.get().strip():
            cmd += ["-p", self.ptt_port_var.get().strip()]
        if self.verbose_var.get():
            cmd += ["-v"]
        if self.extra_var.get().strip():
            cmd += self.extra_var.get().split()
        return cmd

    def start(self):
        if self.running():
            return
        if not self.model_id:
            messagebox.showwarning("Select a radio", "Please choose your radio from the list first.")
            return
        cmd = self.build_command()
        self.save_config()
        self.log("Starting: " + " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.bin_dir, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         creationflags=NO_WINDOW)
        except OSError as e:
            messagebox.showerror("Failed to start", str(e))
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.start_relay()
        self.update_buttons()

    def _reader(self, proc):
        for line in proc.stdout:
            self.log(line)

    def stop(self):
        self.stop_relay()
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.log("rigctld stopped.")
        self.proc = None
        self.update_buttons()

    def read_freq(self):
        port = int(self.tcp_var.get() or 4532)

        def worker():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=4) as s:
                    s.sendall(b"f\n")
                    data = s.recv(256).decode(errors="replace").strip()
                self.log(f"Frequency reply: {data}")
            except OSError as e:
                self.log(f"Could not read frequency: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def on_close(self):
        self.save_config()
        self.stop_reverse()
        self.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
