# ui/views/device_mgr.py
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import os
from datetime import datetime

import requests  # needed for specific timeout exception catching

from ui.styles import *
from core.api_client import api
from core.data_mgr import db
from core.ai_engine import fatigue_tracker, _FATIGUE_COLORS, _FATIGUE_ICONS


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_api(fn, *args, **kwargs):
    """
    Call an API function and return (result, None) on success,
    or (None, error_string) on any failure — including timeouts.
    Never raises; safe to call from any thread.
    """
    try:
        return fn(*args, **kwargs), None
    except requests.exceptions.ConnectTimeout:
        return None, "Connection timed out — device unreachable."
    except requests.exceptions.ConnectionError:
        return None, "Network error — check device IP and Wi-Fi."
    except requests.exceptions.ReadTimeout:
        return None, "Device responded too slowly (read timeout)."
    except requests.exceptions.HTTPError as exc:
        return None, f"HTTP error {exc.response.status_code}: {exc.response.reason}"
    except Exception as exc:
        return None, str(exc)


def _make_treeview_style():
    style = ttk.Style()
    style.theme_use("default")
    style.configure("Guard.Treeview",
                    background=SURFACE3, foreground=TEXT,
                    fieldbackground=SURFACE3, borderwidth=0,
                    font=("Outfit", 11), rowheight=32)
    style.configure("Guard.Treeview.Heading",
                    background=SURFACE2, foreground=TEXT_MUTED,
                    font=("Outfit", 10, "bold"), borderwidth=0, relief="flat")
    style.map("Guard.Treeview",
              background=[("selected", ACCENT2_DIM)],
              foreground=[("selected", TEXT)])


class SectionLabel(ctk.CTkLabel):
    def __init__(self, parent, text, **kwargs):
        super().__init__(parent, text=text.upper(), font=("Outfit", 9, "bold"),
                         text_color=TEXT_DIM, anchor="w", **kwargs)


# ── Main View ──────────────────────────────────────────────────────────────────

class WearablesView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.controller    = controller
        self.selected_ip   = None
        self.polling_active = False
        self.current_tasks  = []
        _make_treeview_style()
        self._build_ui()
        self._load_saved_devices()

    # ══════════════════════════════════════════════
    # BUILD UI
    # ══════════════════════════════════════════════

    def _build_ui(self):
        # Header bar
        header = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="⌚  WEARABLE DEVICES",
                     font=("Outfit", 17, "bold"), text_color=TEXT).pack(side="left", padx=30, pady=18)

        # Resizable horizontal PanedWindow: left fleet panel | right detail panel
        pane = tk.PanedWindow(self, orient=tk.HORIZONTAL,
                              bg=BORDER, sashwidth=7, sashpad=2,
                              sashrelief=tk.FLAT, opaqueresize=True)
        pane.pack(fill="both", expand=True, padx=0, pady=0)

        # LEFT: fleet list (fixed width, draggable)
        left_tk = tk.Frame(pane, bg=BG, width=260)
        pane.add(left_tk, minsize=200)
        pane.paneconfigure(left_tk, stretch="never")

        left = ctk.CTkFrame(left_tk, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                            border_width=1, border_color=BORDER)
        left.pack(fill="both", expand=True, padx=(14, 4), pady=14)

        add_card = ctk.CTkFrame(left, fg_color=SURFACE2, corner_radius=INNER_RADIUS)
        add_card.pack(fill="x", padx=14, pady=14)
        SectionLabel(add_card, "Register Device").pack(anchor="w", padx=12, pady=(10, 4))
        row = ctk.CTkFrame(add_card, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.ip_entry = ctk.CTkEntry(row, placeholder_text="192.168.1.X",
                                     font=("JetBrains Mono", 12),
                                     fg_color=SURFACE3, border_color=BORDER2,
                                     text_color=TEXT, height=INPUT_HEIGHT)
        self.ip_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row, text="Add", width=64, height=INPUT_HEIGHT,
                      fg_color=ACCENT, text_color=BG,
                      font=("Outfit", 12, "bold"), corner_radius=8,
                      hover_color=ACCENT_DIM,
                      command=self._add_device).pack(side="left")

        SectionLabel(left, "Configured Fleet").pack(anchor="w", padx=14, pady=(4, 4))

        list_wrap = ctk.CTkFrame(left, fg_color=SURFACE3, corner_radius=INNER_RADIUS)
        list_wrap.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        self.dev_tree = ttk.Treeview(list_wrap, style="Guard.Treeview",
                                     columns=("Alias", "IP", "Status"),
                                     show="headings", selectmode="browse")
        self.dev_tree.heading("Alias",  text="Worker / Alias")
        self.dev_tree.heading("IP",     text="IP Address")
        self.dev_tree.heading("Status", text="Status")
        self.dev_tree.column("Alias",  width=110, anchor="w",    minwidth=60)
        self.dev_tree.column("IP",     width=100, anchor="center",minwidth=60)
        self.dev_tree.column("Status", width=75,  anchor="center",minwidth=40)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.dev_tree.yview)
        self.dev_tree.configure(yscrollcommand=sb.set)
        self.dev_tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb.pack(side="right", fill="y", pady=4)
        self.dev_tree.bind("<<TreeviewSelect>>", self._on_device_select)

        self.btn_remove = ctk.CTkButton(left, text="Remove Selected",
                                        fg_color="#2a0b10", hover_color=DANGER,
                                        text_color=DANGER, font=("Outfit", 12),
                                        height=BTN_HEIGHT, corner_radius=8,
                                        border_width=1, border_color=DANGER,
                                        state="disabled",
                                        command=self._remove_device)
        self.btn_remove.pack(fill="x", padx=14, pady=(0, 14))

        # RIGHT: device detail panel (fills remaining space)
        right_tk = tk.Frame(pane, bg=BG)
        pane.add(right_tk, minsize=400)
        pane.paneconfigure(right_tk, stretch="always")

        right_wrap = ctk.CTkFrame(right_tk, fg_color="transparent")
        right_wrap.pack(fill="both", expand=True, padx=(4, 14), pady=14)
        right_wrap.grid_rowconfigure(1, weight=1)
        right_wrap.grid_columnconfigure(0, weight=1)

        self.alert_banner_frame = ctk.CTkFrame(right_wrap, fg_color="transparent")
        self.alert_banner_frame.grid(row=0, column=0, sticky="ew")

        self.right_panel = ctk.CTkTabview(
            right_wrap,
            fg_color=SURFACE,
            segmented_button_fg_color=SURFACE2,
            segmented_button_selected_color=SURFACE3,
            segmented_button_unselected_color=SURFACE2,
            segmented_button_selected_hover_color=SURFACE3,
            text_color=TEXT_MUTED,
            text_color_disabled=TEXT_DIM,
            corner_radius=CARD_RADIUS,
            border_width=1,
            border_color=BORDER,
        )
        self.right_panel.grid(row=1, column=0, sticky="nsew")

        self.tab_telemetry = self.right_panel.add("◎  Telemetry")
        self.tab_tasks     = self.right_panel.add("▣  Tasks")
        self.tab_notify    = self.right_panel.add("◉  Notify")
        self.tab_settings  = self.right_panel.add("⚙  Settings")

        self._build_telemetry_tab()
        self._build_tasks_tab()
        self._build_notify_tab()
        self._build_settings_tab()

    # ── Tab builders ──────────────────────────────

    def _card(self, parent, padx=16, pady=10):
        f = ctk.CTkFrame(parent, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                         border_width=1, border_color=BORDER)
        f.pack(fill="x", padx=padx, pady=pady)
        return f

    def _build_telemetry_tab(self):
        tab = self.tab_telemetry

        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 6))
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)

        bat_card = ctk.CTkFrame(top, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                                border_width=1, border_color=BORDER, height=72)
        bat_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        bat_card.pack_propagate(False)
        ctk.CTkLabel(bat_card, text="Battery", font=("Outfit", 9, "bold"),
                     text_color=TEXT_DIM).pack(anchor="w", padx=14, pady=(10, 0))
        self.lbl_battery = ctk.CTkLabel(bat_card, text="-- %",
                                        font=("Outfit", 22, "bold"), text_color=SUCCESS)
        self.lbl_battery.pack(anchor="w", padx=14)
        self.lbl_charging = ctk.CTkLabel(bat_card, text="", font=("Outfit", 10),
                                         text_color=ACCENT_GOLD)
        self.lbl_charging.pack(anchor="w", padx=14, pady=(0, 8))

        step_card = ctk.CTkFrame(top, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                                 border_width=1, border_color=BORDER, height=72)
        step_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        step_card.pack_propagate(False)
        ctk.CTkLabel(step_card, text="Steps Today", font=("Outfit", 9, "bold"),
                     text_color=TEXT_DIM).pack(anchor="w", padx=14, pady=(10, 0))
        self.lbl_steps = ctk.CTkLabel(step_card, text="--",
                                      font=("Outfit", 22, "bold"), text_color=ACCENT_STEPS)
        self.lbl_steps.pack(anchor="w", padx=14, pady=(0, 8))

        imu_card = self._card(tab)
        SectionLabel(imu_card, "QMI8658 IMU — Live Data").pack(anchor="w", padx=14, pady=(12, 6))
        imu_grid = ctk.CTkFrame(imu_card, fg_color="transparent")
        imu_grid.pack(fill="x", padx=14, pady=(0, 14))
        for label, attr, color in [("ACCEL", "lbl_accel", ACCENT), ("GYRO ", "lbl_gyro", ACCENT2)]:
            row = ctk.CTkFrame(imu_grid, fg_color=SURFACE3, corner_radius=6)
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text=label, font=("Outfit", 9, "bold"),
                         text_color=TEXT_DIM, width=50).pack(side="left", padx=10, pady=8)
            lbl = ctk.CTkLabel(row, text="x: --   y: --   z: --",
                               font=("JetBrains Mono", 12), text_color=color)
            lbl.pack(side="left", padx=8, pady=8)
            setattr(self, attr, lbl)

        status_card = self._card(tab)
        SectionLabel(status_card, "Device Info").pack(anchor="w", padx=14, pady=(12, 6))
        info_grid = ctk.CTkFrame(status_card, fg_color="transparent")
        info_grid.pack(fill="x", padx=14, pady=(0, 14))
        self.lbl_worker_name  = self._info_row(info_grid, "Worker Name",    "--")
        self.lbl_uptime       = self._info_row(info_grid, "Uptime",         "--")
        self.lbl_notif_count  = self._info_row(info_grid, "Notifications",  "--")
        self.lbl_task_count   = self._info_row(info_grid, "Tasks",          "--")
        self.lbl_fatigue      = self._info_row(info_grid, "Fatigue Level",  "--")

    def _info_row(self, parent, label, default):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text=label, font=("Outfit", 11), text_color=TEXT_MUTED,
                     width=130, anchor="w").pack(side="left")
        lbl = ctk.CTkLabel(row, text=default, font=("Outfit", 11, "bold"),
                           text_color=TEXT, anchor="w")
        lbl.pack(side="left")
        return lbl

    def _build_tasks_tab(self):
        tab = self.tab_tasks
        form = self._card(tab, pady=(14, 6))
        SectionLabel(form, "Assign New Task").pack(anchor="w", padx=14, pady=(12, 8))

        r1 = ctk.CTkFrame(form, fg_color="transparent")
        r1.pack(fill="x", padx=14, pady=(0, 6))
        self.task_title = ctk.CTkEntry(r1, placeholder_text="Task Title",
                                       fg_color=SURFACE3, border_color=BORDER2,
                                       text_color=TEXT, height=INPUT_HEIGHT)
        self.task_title.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.task_desc = ctk.CTkEntry(r1, placeholder_text="Description",
                                      fg_color=SURFACE3, border_color=BORDER2,
                                      text_color=TEXT, height=INPUT_HEIGHT)
        self.task_desc.pack(side="left", fill="x", expand=True)

        r2 = ctk.CTkFrame(form, fg_color="transparent")
        r2.pack(fill="x", padx=14, pady=(0, 14))
        ctk.CTkLabel(r2, text="Priority:", font=FONT_LABEL_BOLD,
                     text_color=TEXT_MUTED).pack(side="left", padx=(0, 10))
        self.task_priority = ctk.CTkSegmentedButton(
            r2, values=["Low", "Normal", "High"],
            selected_color=ACCENT2, selected_hover_color=ACCENT2_DIM,
            unselected_color=SURFACE3, unselected_hover_color=BORDER2,
            font=("Outfit", 11, "bold"), text_color=TEXT
        )
        self.task_priority.set("Normal")
        self.task_priority.pack(side="left", padx=(0, 16))
        ctk.CTkButton(r2, text="Assign Task →", fg_color=ACCENT2, text_color=TEXT,
                      font=("Outfit", 12, "bold"), height=BTN_HEIGHT, corner_radius=8,
                      hover_color=ACCENT2_DIM, command=self._assign_task).pack(side="left")

        SectionLabel(self.tab_tasks, "Current Tasks").pack(anchor="w", padx=16, pady=(8, 2))
        self.task_scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent",
                                                  scrollbar_button_color=BORDER2)
        self.task_scroll.pack(fill="both", expand=True, padx=16, pady=(0, 8))

    def _build_notify_tab(self):
        tab = self.tab_notify
        form = ctk.CTkFrame(tab, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=16, pady=14)

        notif_card = ctk.CTkFrame(form, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                                  border_width=1, border_color=BORDER)
        notif_card.pack(fill="x")
        SectionLabel(notif_card, "Push Text Notification").pack(anchor="w", padx=16, pady=(14, 8))

        self.notif_title = ctk.CTkEntry(notif_card, placeholder_text="Notification Title",
                                        fg_color=SURFACE3, border_color=BORDER2,
                                        text_color=TEXT, height=INPUT_HEIGHT,
                                        font=("Outfit", 13))
        self.notif_title.pack(fill="x", padx=16, pady=(0, 8))

        self.notif_body = ctk.CTkTextbox(notif_card, fg_color=SURFACE3,
                                          border_color=BORDER2, border_width=1,
                                          text_color=TEXT, font=("Outfit", 12), height=100)
        self.notif_body.pack(fill="x", padx=16, pady=(0, 8))
        self.notif_body.insert("0.0", "Notification body...")

        self.notif_alert_var = ctk.BooleanVar(value=False)
        alert_row = ctk.CTkFrame(notif_card, fg_color=SURFACE3, corner_radius=6)
        alert_row.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkCheckBox(alert_row,
                        text="  Send as CRITICAL ALERT (triggers siren + max volume)",
                        variable=self.notif_alert_var,
                        text_color=DANGER, fg_color=DANGER, hover_color="#c02040",
                        font=("Outfit", 11, "bold")
                        ).pack(anchor="w", padx=12, pady=10)

        self.btn_send_notif = ctk.CTkButton(notif_card, text="Send Notification →",
                                            fg_color=ACCENT2, text_color=TEXT,
                                            font=("Outfit", 12, "bold"),
                                            height=BTN_HEIGHT + 4, corner_radius=8,
                                            hover_color=ACCENT2_DIM,
                                            command=self._send_notification)
        self.btn_send_notif.pack(fill="x", padx=16, pady=(0, 16))

    def _build_settings_tab(self):
        tab = self.tab_settings

        wc = self._card(tab, pady=(14, 6))
        SectionLabel(wc, "Worker Assignment").pack(anchor="w", padx=14, pady=(12, 8))
        r = ctk.CTkFrame(wc, fg_color="transparent")
        r.pack(fill="x", padx=14, pady=(0, 14))
        self.worker_entry = ctk.CTkEntry(r, placeholder_text="Worker full name",
                                         fg_color=SURFACE3, border_color=BORDER2,
                                         text_color=TEXT, height=INPUT_HEIGHT)
        self.worker_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(r, text="Assign", width=90, height=INPUT_HEIGHT,
                      fg_color=ACCENT, text_color=BG, font=("Outfit", 11, "bold"),
                      corner_radius=8, hover_color=ACCENT_DIM,
                      command=self._set_worker).pack(side="left")

        sc = self._card(tab, pady=6)
        SectionLabel(sc, "Remote Device Controls").pack(anchor="w", padx=14, pady=(12, 4))
        for label, attr_name, key in [("Brightness", "bright_slider", "brightness"),
                                       ("Volume",     "vol_slider",    "volume")]:
            srow = ctk.CTkFrame(sc, fg_color="transparent")
            srow.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(srow, text=label, font=FONT_LABEL, text_color=TEXT_MUTED,
                         width=90, anchor="w").pack(side="left")
            slider = ctk.CTkSlider(srow, from_=0, to=100,
                                   button_color=ACCENT, button_hover_color=ACCENT_DIM,
                                   progress_color=ACCENT_DIM, fg_color=SURFACE3)
            slider.set(50)
            slider.pack(side="left", fill="x", expand=True, padx=(0, 12))
            k = key
            ctk.CTkButton(srow, text="Push", width=70, height=32,
                          fg_color=SURFACE3, text_color=TEXT,
                          hover_color=BORDER2, font=("Outfit", 11),
                          command=lambda kk=k: self._push_setting(kk)).pack(side="left")
            setattr(self, attr_name, slider)
        ctk.CTkFrame(sc, fg_color="transparent", height=6).pack()

        rtc = self._card(tab, pady=6)
        SectionLabel(rtc, "Hardware Clock (RTC)").pack(anchor="w", padx=14, pady=(12, 8))
        self.btn_sync_rtc = ctk.CTkButton(
            rtc, text="Sync RTC with System Clock",
            fg_color=ACCENT2, text_color=TEXT, font=("Outfit", 12, "bold"),
            height=BTN_HEIGHT, corner_radius=8, hover_color=ACCENT2_DIM,
            command=self._sync_rtc)
        self.btn_sync_rtc.pack(anchor="w", padx=14, pady=(0, 14))

    # ══════════════════════════════════════════════
    # DEVICE FLEET MANAGEMENT
    # ══════════════════════════════════════════════

    def _load_saved_devices(self):
        for ip, dev in db.wearables.items():
            alias = dev.get("DeviceAlias", ip)
            self.dev_tree.insert("", "end", iid=ip, values=(alias, ip, "⊘ Offline"))

    def _add_device(self):
        ip = self.ip_entry.get().strip()
        if not ip:
            return
        if ip in db.wearables:
            messagebox.showinfo("Duplicate", "Device already registered.")
            return
        rec = {"IP": ip, "AssignedWorkerUUID": "", "DeviceAlias": f"Worker_{ip.split('.')[-1]}"}
        db.save_wearable(ip, rec)
        self.dev_tree.insert("", "end", iid=ip, values=(rec["DeviceAlias"], ip, "⊘ Offline"))
        self.ip_entry.delete(0, tk.END)

    def _remove_device(self):
        if not self.selected_ip:
            return
        if messagebox.askyesno("Remove Device", f"Remove {self.selected_ip} from fleet?"):
            db.remove_wearable(self.selected_ip)
            self.dev_tree.delete(self.selected_ip)
            self.selected_ip = None
            self.polling_active = False
            self.btn_remove.configure(state="disabled")
            for w in self.alert_banner_frame.winfo_children():
                w.destroy()
            for w in self.task_scroll.winfo_children():
                w.destroy()
            self.lbl_battery.configure(text="-- %")
            self.lbl_steps.configure(text="--")

    def _on_device_select(self, _event=None):
        sel = self.dev_tree.selection()
        if not sel:
            self.btn_remove.configure(state="disabled")
            return
        self.selected_ip = sel[0]
        self.btn_remove.configure(state="normal")
        for w in self.alert_banner_frame.winfo_children():
            w.destroy()
        for w in self.task_scroll.winfo_children():
            w.destroy()
        self._start_device_poll()

    def _start_device_poll(self):
        # Signal any existing poll thread to stop, then start a new one.
        # Use a generation counter so the old thread detects the change
        # immediately without any blocking sleep on the main thread.
        self._poll_gen = getattr(self, "_poll_gen", 0) + 1
        self.polling_active = True
        gen = self._poll_gen

        def poll():
            while self.polling_active and self.selected_ip and gen == self._poll_gen:
                ip = self.selected_ip
                status_result, status_err = _safe_api(api.wearable_get_status, ip)
                tasks_result,  tasks_err  = _safe_api(api.wearable_get_tasks,  ip)

                if status_result is not None:
                    self.current_tasks = (tasks_result or {}).get("tasks", [])
                    self.after(0, lambda d=status_result: self._update_telemetry_ui(d))
                    self.after(0, self._render_task_list)
                    self.after(0, lambda i=ip: self.dev_tree.set(i, "Status", "● Online"))
                else:
                    if "circuit open" in (status_err or "").lower():
                        status_label = "⊘ Unavailable"
                    elif "timeout" in (status_err or "").lower():
                        status_label = "⏱ Timeout"
                    elif "network" in (status_err or "").lower():
                        status_label = "✕ Net Err"
                    else:
                        status_label = "✕ Offline"
                    self.after(0, lambda i=ip, s=status_label:
                               self.dev_tree.set(i, "Status", s))
                time.sleep(3)

        threading.Thread(target=poll, daemon=True).start()

    # ══════════════════════════════════════════════
    # UI UPDATERS
    # ══════════════════════════════════════════════

    def _update_telemetry_ui(self, data):
        pct      = data.get("battery_pct", 0)
        v        = data.get("battery_v", 0.0)
        charging = data.get("charging", False)
        bat_col  = SUCCESS if pct > 40 else (WARNING if pct > 15 else DANGER)
        self.lbl_battery.configure(text=f"{pct} %", text_color=bat_col)
        self.lbl_charging.configure(
            text=f"{v:.2f}V  {'⚡ Charging' if charging else ''}")

        steps = data.get("step_count", 0)
        self.lbl_steps.configure(text=f"{steps:,}")

        ax, ay, az = (data.get("imu_ax", 0),
                      data.get("imu_ay", 0),
                      data.get("imu_az", 0))
        gx, gy, gz = (data.get("imu_gx", 0),
                      data.get("imu_gy", 0),
                      data.get("imu_gz", 0))
        self.lbl_accel.configure(
            text=f"x: {ax:+.2f}   y: {ay:+.2f}   z: {az:+.2f}")
        self.lbl_gyro.configure(
            text=f"x: {gx:+.1f}   y: {gy:+.1f}   z: {gz:+.1f}")

        w_name = data.get("worker_name", "--")
        self.lbl_worker_name.configure(text=w_name)
        uptime = data.get("uptime_s", 0)
        h, rem = divmod(uptime, 3600)
        m, s   = divmod(rem, 60)
        self.lbl_uptime.configure(text=f"{h}h {m}m {s}s")
        self.lbl_notif_count.configure(text=str(data.get("notification_count", "--")))
        self.lbl_task_count.configure(text=str(data.get("task_count", "--")))

        # ── Fatigue detection ─────────────────────────────────────────────────
        if self.selected_ip:
            fall_det = data.get("fall_detected", False)
            fatigue_tracker.update(
                self.selected_ip, w_name, steps, fall_detected=fall_det)
            fat = fatigue_tracker.get_state(self.selected_ip)
            if fat:
                level  = fat.get("label", "OK")
                fcolor = _FATIGUE_COLORS.get(level, TEXT_MUTED)
                ficon  = _FATIGUE_ICONS.get(level, "")
                self.lbl_fatigue.configure(
                    text=f"{ficon} {level}  ({steps:,} steps)",
                    text_color=fcolor)
            else:
                self.lbl_fatigue.configure(text="--", text_color=TEXT_MUTED)

        for w in self.alert_banner_frame.winfo_children():
            w.destroy()
        if data.get("fall_detected"):
            self._make_alert_banner(
                "FALL", data.get("worker_name", self.selected_ip),
                self.selected_ip, self._ack_fall)
        if data.get("help_alert"):
            self._make_alert_banner(
                "SOS", data.get("worker_name", self.selected_ip),
                self.selected_ip, self._ack_help)

    def _make_alert_banner(self, alert_type, name, ip, cb):
        is_fall = alert_type == "FALL"
        color   = DANGER if is_fall else WARNING
        bg      = "#1f0a10" if is_fall else "#1c1505"
        label   = "FALL DETECTED" if is_fall else "SOS HELP REQUEST"
        f = ctk.CTkFrame(self.alert_banner_frame, fg_color=bg, corner_radius=8,
                         border_width=1, border_color=color)
        f.pack(fill="x", pady=3)
        ctk.CTkLabel(f, text=f"  ⬦  {label}  —  {name}", font=("Outfit", 12, "bold"),
                     text_color=color).pack(side="left", padx=12, pady=10)
        ctk.CTkButton(f, text="Acknowledge", fg_color=color, text_color=BG,
                      font=("Outfit", 11, "bold"), width=110, height=30,
                      corner_radius=6, command=cb).pack(side="right", padx=12)

    def _render_task_list(self):
        for w in self.task_scroll.winfo_children():
            w.destroy()
        if not self.current_tasks:
            ctk.CTkLabel(self.task_scroll, text="No tasks assigned.",
                         text_color=TEXT_DIM, font=FONT_SMALL).pack(pady=20)
            return

        pending_done = [t for t in self.current_tasks if t.get("pending_approval")]
        pending_skip = [t for t in self.current_tasks if t.get("pending_skip")]
        active = [t for t in self.current_tasks
                  if not t.get("pending_approval") and not t.get("approved")
                  and not t.get("pending_skip") and not t.get("skipped")]
        completed = [t for t in self.current_tasks if t.get("approved")]

        def section_lbl(text, color=TEXT_MUTED):
            lf = ctk.CTkFrame(self.task_scroll, fg_color="transparent")
            lf.pack(fill="x", pady=(8, 2))
            ctk.CTkLabel(lf, text=text.upper(), font=("Outfit", 9, "bold"),
                         text_color=color).pack(anchor="w")

        if pending_done:
            section_lbl("Awaiting Approval", ACCENT)
            for t in pending_done:
                self._task_row(t, "#0a1e18", ACCENT, [
                    ("Approve ✓", ACCENT, lambda idx=t["index"]: self._approve_task(idx))
                ])

        if pending_skip:
            section_lbl("Skip Requests", WARNING)
            for t in pending_skip:
                self._task_row(t, "#1c1505", WARNING, [
                    ("Approve", SUCCESS, lambda idx=t["index"]: self._approve_skip(idx)),
                    ("Deny ✕",  DANGER,  lambda idx=t["index"]: self._deny_skip(idx)),
                ])

        if active:
            section_lbl("Active Tasks")
            for t in active:
                pri = t.get("priority", 1)
                self._task_row(t, SURFACE2, BORDER2, [
                    ("Mark Done", ACCENT2, lambda idx=t["index"]: self._complete_task(idx))
                ], prefix=f"[{PRIORITY_LABELS[pri]}]  ", prefix_color=PRIORITY_COLORS[pri])

        if completed:
            section_lbl("Completed", SUCCESS)
            for t in completed:
                f = ctk.CTkFrame(self.task_scroll, fg_color=SURFACE2,
                                 corner_radius=6, border_width=1, border_color=BORDER)
                f.pack(fill="x", pady=2)
                ctk.CTkLabel(f, text=f"  ✓  {t['title']}", font=("Outfit", 12),
                             text_color=SUCCESS).pack(anchor="w", padx=8, pady=8)

    def _task_row(self, task, bg, border_color, buttons, prefix="", prefix_color=TEXT):
        f = ctk.CTkFrame(self.task_scroll, fg_color=bg, corner_radius=6,
                         border_width=1, border_color=border_color)
        f.pack(fill="x", pady=3)
        left = ctk.CTkFrame(f, fg_color="transparent")
        left.pack(side="left", fill="y", padx=10, pady=8)
        if prefix:
            ctk.CTkLabel(left, text=prefix, font=("Outfit", 9, "bold"),
                         text_color=prefix_color).pack(anchor="w")
        ctk.CTkLabel(left, text=task.get("title", ""), font=("Outfit", 12, "bold"),
                     text_color=TEXT).pack(anchor="w")
        desc = task.get("description", "")
        if desc:
            ctk.CTkLabel(left, text=desc, font=("Outfit", 10),
                         text_color=TEXT_MUTED).pack(anchor="w")
        for btn_text, btn_color, btn_cmd in buttons:
            ctk.CTkButton(f, text=btn_text, fg_color=btn_color,
                          text_color=BG if btn_color not in (SURFACE, SURFACE2, SURFACE3) else TEXT,
                          font=("Outfit", 10, "bold"), width=90, height=28,
                          corner_radius=6, command=btn_cmd).pack(side="right", padx=6)

    # ══════════════════════════════════════════════
    # API CALLS — all use _safe_api; no bare lambdas
    # capturing mutable exception variables
    # ══════════════════════════════════════════════

    def _send_notification(self):
        if not self.selected_ip:
            return
        title    = self.notif_title.get().strip()
        body     = self.notif_body.get("1.0", "end-1c").strip()
        is_alert = self.notif_alert_var.get()
        if not title:
            return
        ip = self.selected_ip
        self.btn_send_notif.configure(text="Sending…", state="disabled")

        def do():
            # Step 1: send text notification
            _, err = _safe_api(api.wearable_send_notification, ip, title, body, is_alert)
            if err:
                msg = f"Notification failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Send Failed", m))
                self.after(0, lambda: self.btn_send_notif.configure(
                    text="Send Notification →", state="normal"))
                return

            db.log_event("NOTIFICATION", ip, f"Sent: {title}")

            # Step 2 (optional): critical alert sequence
            if is_alert:
                _safe_api(api.wearable_push_setting, ip, "volume", 100)
                time.sleep(0.5)
                if os.path.exists("siren.mp3"):
                    _, audio_err = _safe_api(api.wearable_send_audio, ip, "siren.mp3")
                    if audio_err:
                        warn_msg = f"Alert sent but audio upload failed:\n{audio_err}"
                        self.after(0, lambda m=warn_msg: messagebox.showwarning("Audio Warning", m))
                    else:
                        db.log_event("SIREN_AUDIO", ip, "siren.mp3 dispatched")
                else:
                    self.after(0, lambda: messagebox.showwarning(
                        "Missing File", "Alert sent, but siren.mp3 not found in project root."))

            self.after(0, lambda: messagebox.showinfo("Success", "Notification delivered."))
            self.after(0, lambda: self.notif_title.delete(0, tk.END))
            self.after(0, lambda: self.notif_body.delete("1.0", tk.END))
            self.after(0, lambda: self.notif_alert_var.set(False))
            self.after(0, lambda: self.btn_send_notif.configure(
                text="Send Notification →", state="normal"))

        threading.Thread(target=do, daemon=True).start()

    def _assign_task(self):
        if not self.selected_ip:
            return
        title, desc = self.task_title.get().strip(), self.task_desc.get().strip()
        if not title:
            return
        ip = self.selected_ip
        pri_map = {"Low": 0, "Normal": 1, "High": 2}
        priority = pri_map[self.task_priority.get()]

        def do():
            _, err = _safe_api(api.wearable_add_task, ip, title, desc, priority)
            if err:
                msg = f"Failed to assign task:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Task Error", m))
            else:
                db.log_event("TASK_ASSIGNED", ip, f"Task: {title}")

        threading.Thread(target=do, daemon=True).start()
        self.task_title.delete(0, tk.END)
        self.task_desc.delete(0, tk.END)

    def _complete_task(self, idx):
        ip = self.selected_ip
        def do():
            _, err = _safe_api(api.wearable_complete_task, ip, idx)
            if err:
                msg = f"Could not mark task done:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Task Error", m))
        threading.Thread(target=do, daemon=True).start()

    def _approve_task(self, idx):
        ip = self.selected_ip
        def do():
            _, err = _safe_api(api.wearable_approve_task, ip, idx)
            if err:
                msg = f"Approval failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Task Error", m))
        threading.Thread(target=do, daemon=True).start()

    def _approve_skip(self, idx):
        ip = self.selected_ip
        def do():
            _, err = _safe_api(api.wearable_approve_skip, ip, idx)
            if err:
                msg = f"Skip approval failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Task Error", m))
        threading.Thread(target=do, daemon=True).start()

    def _deny_skip(self, idx):
        ip = self.selected_ip
        def do():
            _, err = _safe_api(api.wearable_deny_skip, ip, idx)
            if err:
                msg = f"Skip denial failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Task Error", m))
        threading.Thread(target=do, daemon=True).start()

    def _ack_fall(self):
        ip = self.selected_ip
        def do():
            _, err = _safe_api(api.wearable_acknowledge_fall, ip)
            if err:
                msg = f"Fall acknowledgement failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Alert Error", m))
        threading.Thread(target=do, daemon=True).start()

    def _ack_help(self):
        ip = self.selected_ip
        def do():
            _, err = _safe_api(api.wearable_acknowledge_help, ip)
            if err:
                msg = f"SOS acknowledgement failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Alert Error", m))
        threading.Thread(target=do, daemon=True).start()

    def _push_setting(self, key):
        if not self.selected_ip:
            return
        ip  = self.selected_ip
        val = int(self.bright_slider.get() if key == "brightness" else self.vol_slider.get())

        def do():
            _, err = _safe_api(api.wearable_push_setting, ip, key, val)
            if err:
                msg = f"Setting '{key}' push failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Settings Error", m))

        threading.Thread(target=do, daemon=True).start()

    def _set_worker(self):
        if not self.selected_ip:
            return
        name = self.worker_entry.get().strip()
        if not name:
            return
        ip = self.selected_ip

        def do():
            _, err = _safe_api(api.wearable_set_worker, ip, name)
            if err:
                msg = f"Worker name update failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("Settings Error", m))
            else:
                db.wearables[ip]["DeviceAlias"] = name
                db.save_wearable(ip, db.wearables[ip])
                self.after(0, lambda: self.dev_tree.item(ip, values=(name, ip, "● Online")))

        threading.Thread(target=do, daemon=True).start()

    def _sync_rtc(self):
        if not self.selected_ip:
            return
        ip  = self.selected_ip
        now = datetime.now()
        dow_map = [6, 0, 1, 2, 3, 4, 5]
        payload = {
            "year": now.year, "month": now.month, "day": now.day,
            "dotw": dow_map[now.weekday()],
            "hour": now.hour, "minute": now.minute, "second": now.second
        }
        self.btn_sync_rtc.configure(text="Syncing…", state="disabled")

        def do():
            _, err = _safe_api(api.wearable_set_rtc, ip, payload)
            if err:
                msg = f"RTC sync failed:\n{err}"
                self.after(0, lambda m=msg: messagebox.showerror("RTC Error", m))
            else:
                self.after(0, lambda: messagebox.showinfo("RTC Synced",
                    f"Clock synced to {now.strftime('%H:%M:%S %d/%m/%Y')}"))
            self.after(0, lambda: self.btn_sync_rtc.configure(
                text="Sync RTC with System Clock", state="normal"))

        threading.Thread(target=do, daemon=True).start()