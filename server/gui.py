"""Small Tk window for the Phone Tablet server (status, PIN, settings, log)."""
import queue
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import capture
import common
from server_core import TabletServer, local_ips


class App:
    def __init__(self, root, cfg, settings):
        self.root = root
        self.cfg = cfg
        self.server = None
        self.logq = queue.Queue()
        common.set_log_sink(self.logq.put)

        # saved settings (CLI flags already merged by the caller)
        self.pin = settings["pin"]
        self.port = settings["port"]
        self.cfg.setdefault("pin", self.pin)
        self.monitor_var = tk.StringVar(value=str(settings["monitor"]))
        self.fps_var = tk.StringVar(value=str(settings["fps"]))
        self.quality_var = tk.StringVar(value=str(settings["quality"]))
        self.mouse_var = tk.BooleanVar(value=bool(settings["mouse"]))
        self.use_pin_var = tk.BooleanVar(value=bool(self.pin))
        self.status_var = tk.StringVar(value="Starting...")
        self.pin_var = tk.StringVar(value=self.pin or "off")
        if not self.pin:
            self.pin = self.cfg.get("pin") or common.new_pin()

        root.title("Phone Tablet Server")
        root.geometry("460x600")
        root.minsize(420, 520)
        self._build()
        self._save()
        self.start_server()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.poll)

    # ---- layout ---------------------------------------------------------
    def _build(self):
        pad = {"padx": 12, "pady": 6}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Phone Tablet", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 10)).pack(anchor="w")

        conn = ttk.LabelFrame(self.root, text="Connect from the phone")
        conn.pack(fill="x", **pad)
        ttk.Label(conn, text="Tap Scan in the app, or type one of these addresses:").pack(
            anchor="w", padx=8, pady=(6, 0))
        self.ip_box = ttk.Entry(conn, state="readonly", font=("Consolas", 11))
        self.ip_box.pack(fill="x", padx=8, pady=4)
        self.port_label = ttk.Label(conn, text="")
        self.port_label.pack(anchor="w", padx=8, pady=(0, 6))

        pinf = ttk.LabelFrame(self.root, text="PIN (the phone must enter it)")
        pinf.pack(fill="x", **pad)
        ttk.Label(pinf, textvariable=self.pin_var, font=("Consolas", 24, "bold")).pack(
            side="left", padx=12, pady=6)
        ttk.Button(pinf, text="New PIN", command=self.new_pin).pack(side="left", padx=4)
        ttk.Checkbutton(pinf, text="Require PIN", variable=self.use_pin_var,
                        command=self.toggle_pin).pack(side="left", padx=12)

        sett = ttk.LabelFrame(self.root, text="Settings")
        sett.pack(fill="x", **pad)
        try:
            n = len(capture.list_monitors(False))
        except BaseException:
            n = 1
        rows = [
            ("Start on screen", self.monitor_var, 1, max(1, n), self.restart_server),
            ("Max frames / sec", self.fps_var, 5, 60, self.apply_live),
            ("Image quality", self.quality_var, 30, 95, self.apply_live),
        ]
        for i, (label, var, lo, hi, cb) in enumerate(rows):
            ttk.Label(sett, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            sp = ttk.Spinbox(sett, from_=lo, to=hi, textvariable=var, width=6, command=cb)
            sp.grid(row=i, column=1, sticky="w", pady=3)
            sp.bind("<Return>", lambda _e, f=cb: f())
            sp.bind("<FocusOut>", lambda _e, f=cb: f())
        ttk.Checkbutton(sett, text="Mouse mode (no pen pressure)", variable=self.mouse_var,
                        command=self.restart_server).grid(
            row=len(rows), column=0, columnspan=2, sticky="w", padx=8, pady=(3, 8))

        self.toggle_btn = ttk.Button(self.root, text="Stop", command=self.toggle_server)
        self.toggle_btn.pack(fill="x", **pad)

        self.log = ScrolledText(self.root, height=9, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ---- server control -------------------------------------------------
    def _int(self, var, default, lo, hi):
        try:
            return min(max(int(var.get()), lo), hi)
        except ValueError:
            return default

    def _save(self):
        self.cfg.update(
            pin=self.pin, port=self.port,
            monitor=self._int(self.monitor_var, 1, 1, 16),
            fps=self._int(self.fps_var, 30, 5, 60),
            quality=self._int(self.quality_var, 70, 30, 95),
            mouse=bool(self.mouse_var.get()),
        )
        common.save_config(self.cfg)

    def start_server(self):
        self._save()
        self.server = TabletServer(
            port=self.port,
            pin=self.pin if self.use_pin_var.get() else "",
            monitor=self.cfg["monitor"], fps=self.cfg["fps"], quality=self.cfg["quality"],
            mouse=self.cfg["mouse"],
        )
        try:
            self.server.start()
        except OSError as exc:
            self.server = None
            self.status_var.set("Could not start")
            self.toggle_btn.config(text="Start")
            messagebox.showerror("Phone Tablet", f"Cannot listen on port {self.port}:\n{exc}\n\n"
                                 "Is another copy of the server already running?")
            return
        self.pin_var.set(self.pin if self.use_pin_var.get() else "off")
        self.ip_box.config(state="normal")
        self.ip_box.delete(0, "end")
        self.ip_box.insert(0, "   ".join(local_ips()) or "(no network found)")
        self.ip_box.config(state="readonly")
        self.port_label.config(text=f"Port: {self.server.port}")
        self.toggle_btn.config(text="Stop")

    def stop_server(self):
        if self.server:
            self.server.stop()
            self.server = None
        self.status_var.set("Stopped")
        self.toggle_btn.config(text="Start")

    def toggle_server(self):
        if self.server:
            self.stop_server()
        else:
            self.start_server()

    def restart_server(self):
        if self.server:
            self.stop_server()
            self.root.after(200, self.start_server)
        else:
            self._save()

    def apply_live(self):
        self._save()
        if self.server:
            self.server.fps = self.cfg["fps"]
            self.server.quality = self.cfg["quality"]

    def new_pin(self):
        self.pin = common.new_pin()
        self.use_pin_var.set(True)
        self.toggle_pin()

    def toggle_pin(self):
        self._save()
        if self.server:
            self.server.pin = self.pin if self.use_pin_var.get() else ""
        self.pin_var.set(self.pin if self.use_pin_var.get() else "off")

    # ---- periodic UI refresh -------------------------------------------
    def poll(self):
        try:
            while True:
                line = self.logq.get_nowait()
                self.log.config(state="normal")
                self.log.insert("end", line + "\n")
                self.log.see("end")
                self.log.config(state="disabled")
        except queue.Empty:
            pass
        if self.server:
            addr = self.server.active_addr
            self.status_var.set(f"Phone connected: {addr}" if addr else "Waiting for the phone...")
        self.root.after(250, self.poll)

    def on_close(self):
        self.stop_server()
        self.root.destroy()


def run(_args, cfg, settings):
    root = tk.Tk()
    App(root, cfg, settings)
    root.mainloop()
