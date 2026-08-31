# ui/views/dashboard.py
import customtkinter as ctk
import threading
import time

from ui.styles import *
from core.data_mgr import db
from core.api_client import api
from core.ai_engine import fatigue_tracker, _FATIGUE_COLORS, _FATIGUE_ICONS


def _poll_wearable(ip):
    """Fetch status from a single wearable using the safe API tuple-return."""
    return api.wearable_get_status_safe(ip)


# ── Widgets ───────────────────────────────────────────────────────────────────

class StatCard(ctk.CTkFrame):
    """Premium metric card with accent bar and live value."""
    def __init__(self, parent, label, value, icon, color, subtitle=""):
        super().__init__(parent, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                         border_width=1, border_color=BORDER)
        self.pack_propagate(False)
        self.configure(height=110)

        ctk.CTkFrame(self, fg_color=color, height=3, corner_radius=0).pack(
            fill="x", side="top")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=10)

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=icon, font=("Outfit", 18),
                     text_color=color).pack(side="left")
        ctk.CTkLabel(top, text=f"  {label.upper()}", font=("Outfit", 9, "bold"),
                     text_color=TEXT_DIM).pack(side="left", pady=(2, 0))

        self.val_label = ctk.CTkLabel(body, text=str(value),
                                      font=("Outfit", 30, "bold"),
                                      text_color=TEXT, anchor="w")
        self.val_label.pack(anchor="w", pady=(4, 0))

        if subtitle:
            ctk.CTkLabel(body, text=subtitle, font=("Outfit", 10),
                         text_color=TEXT_MUTED, anchor="w").pack(anchor="w")

    def set_value(self, val):
        self.val_label.configure(text=str(val))


class AlertBanner(ctk.CTkFrame):
    """Compact alert banner (FALL / SOS)."""
    def __init__(self, parent, alert_type, worker_name, ip, ack_callback):
        is_fall = alert_type == "FALL"
        color   = DANGER if is_fall else WARNING
        bg_col  = "#1f0a10" if is_fall else "#1c1505"
        icon    = "⬦" if is_fall else "◈"
        label   = "FALL DETECTED" if is_fall else "SOS HELP REQUEST"

        super().__init__(parent, fg_color=bg_col, corner_radius=10,
                         border_width=1, border_color=color)

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="y", padx=14, pady=12)
        ctk.CTkLabel(left, text=icon, font=("Outfit", 20),
                     text_color=color).pack(side="left")

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(side="left", fill="y", padx=6, pady=12)
        ctk.CTkLabel(mid, text=label, font=("Outfit", 13, "bold"),
                     text_color=color).pack(anchor="w")
        ctk.CTkLabel(mid, text=f"{worker_name}  ·  {ip}",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(anchor="w")

        ctk.CTkButton(self, text="ACKNOWLEDGE", fg_color=color,
                      text_color=BG, font=("Outfit", 11, "bold"),
                      width=130, height=32, corner_radius=8,
                      command=ack_callback).pack(side="right", padx=14)


class FatigueBanner(ctk.CTkFrame):
    """Compact fatigue alert banner."""
    def __init__(self, parent, worker_name, ip, level_str, steps):
        color  = _FATIGUE_COLORS.get(level_str, WARNING)
        bg_col = {
            "WARN":     "#1c1a05",
            "HIGH":     "#1c1005",
            "CRITICAL": "#1f0a10",
        }.get(level_str, "#1c1505")
        icon  = _FATIGUE_ICONS.get(level_str, "⚠")
        label = f"FATIGUE {level_str}"

        super().__init__(parent, fg_color=bg_col, corner_radius=10,
                         border_width=1, border_color=color)

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="y", padx=14, pady=10)
        ctk.CTkLabel(left, text=icon, font=("Outfit", 18),
                     text_color=color).pack(side="left")

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(side="left", fill="y", padx=6, pady=10)
        ctk.CTkLabel(mid, text=label, font=("Outfit", 12, "bold"),
                     text_color=color).pack(anchor="w")
        ctk.CTkLabel(mid, text=f"{worker_name}  ·  {steps:,} steps  ·  {ip}",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(anchor="w")


# ── Dashboard View ────────────────────────────────────────────────────────────

class DashboardView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.controller      = controller
        self.polling_active  = True
        self.active_alert_states = set()

        self._build_ui()

        # Register for real-time data-manager callbacks
        db.register_update_callback(self._on_data_update)

        # Register for fatigue callbacks
        fatigue_tracker.register_callback(self._on_fatigue_alert)

        self._start_fleet_polling()

    # ── Data callbacks ────────────────────────────────────────────────────────

    def _on_data_update(self, update_type, data):
        try:
            if update_type == "anchor_stats":
                self.after(0, lambda: self.cards["nodes"].set_value(
                    data.get("active_anchors", 0)))
            elif update_type == "events":
                event_type = data.get("EventType", "")
                source     = data.get("Source", "")
                desc       = data.get("Description", "")
                level = "ALERT" if event_type in ("FALL", "SOS") else (
                         "WARN"  if "FATIGUE" in event_type else "INFO")
                msg = f"{event_type} from {source}: {desc}"
                self.after(0, lambda m=msg, lv=level: self.log_to_ui(m, lv))
        except Exception as e:
            print(f"[Dashboard] Update callback error: {e}")

    def _on_fatigue_alert(self, ip: str, worker_name: str,
                           level_str: str, steps: int):
        """Called from FatigueTracker when a fatigue threshold is crossed."""
        msg = (f"FATIGUE {level_str} — {worker_name} ({ip}) "
               f"{steps:,} steps")
        self.after(0, lambda m=msg: self.log_to_ui(m, "ALERT"))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="◈  FLEET COMMAND DASHBOARD",
                     font=("Outfit", 17, "bold"), text_color=TEXT).pack(
            side="left", padx=30, pady=18)
        self.lbl_last_update = ctk.CTkLabel(
            header, text="Last update: --",
            font=("JetBrains Mono", 11), text_color=TEXT_DIM)
        self.lbl_last_update.pack(side="right", padx=30)

        # Stat cards
        cards_wrap = ctk.CTkFrame(self, fg_color="transparent")
        cards_wrap.pack(fill="x", padx=22, pady=(18, 0))
        self.cards = {
            "devices": StatCard(cards_wrap, "Active Devices", "0", "⌚", ACCENT,
                                "wearables online"),
            "nodes":   StatCard(cards_wrap, "Scanner Nodes",  "0", "◎", ACCENT2,
                                "RTLS anchors"),
            "steps":   StatCard(cards_wrap, "Fleet Steps",    "0",  "◌", ACCENT_STEPS,
                                "total today"),
            "alerts":  StatCard(cards_wrap, "Active Alerts",  "0",  "⬦", DANGER,
                                "require attention"),
        }
        for i, card in enumerate(self.cards.values()):
            card.grid(row=0, column=i, padx=6, sticky="nsew")
            cards_wrap.grid_columnconfigure(i, weight=1)

        # Alert + fatigue banners container
        self.alert_container = ctk.CTkFrame(self, fg_color="transparent")
        self.alert_container.pack(fill="x", padx=28, pady=(16, 0))

        # Bottom two-column layout
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="both", expand=True, padx=22, pady=16)
        bottom.grid_columnconfigure(0, weight=3)
        bottom.grid_columnconfigure(1, weight=2)
        bottom.grid_rowconfigure(0, weight=1)

        # Event log
        log_card = ctk.CTkFrame(bottom, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                                border_width=1, border_color=BORDER)
        log_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        log_header = ctk.CTkFrame(log_card, fg_color=SURFACE2,
                                   corner_radius=0, height=42)
        log_header.pack(fill="x")
        log_header.pack_propagate(False)
        ctk.CTkLabel(log_header, text="◉  Real-Time Event Log",
                     font=FONT_LABEL_BOLD, text_color=TEXT).pack(
            side="left", padx=16, pady=10)
        ctk.CTkButton(log_header, text="Clear", width=60, height=26,
                      fg_color=SURFACE3, text_color=TEXT_MUTED,
                      font=("Outfit", 10), corner_radius=6,
                      command=self._clear_log).pack(side="right", padx=12, pady=8)
        self.activity_log = ctk.CTkTextbox(
            log_card, fg_color="transparent",
            text_color=TEXT_MUTED, font=("JetBrains Mono", 11),
            scrollbar_button_color=BORDER2)
        self.activity_log.pack(fill="both", expand=True, padx=14, pady=(8, 14))
        self.activity_log.insert(
            "0.0", "[SYSTEM]  Initializing GUARD fleet data polling...\n")
        self.activity_log.configure(state="disabled")

        # Fleet overview
        fleet_card = ctk.CTkFrame(bottom, fg_color=SURFACE,
                                   corner_radius=CARD_RADIUS,
                                   border_width=1, border_color=BORDER)
        fleet_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        fleet_header = ctk.CTkFrame(fleet_card, fg_color=SURFACE2,
                                     corner_radius=0, height=42)
        fleet_header.pack(fill="x")
        fleet_header.pack_propagate(False)
        ctk.CTkLabel(fleet_header, text="◎  Fleet Overview",
                     font=FONT_LABEL_BOLD, text_color=TEXT).pack(
            side="left", padx=16, pady=10)

        self.fleet_scroll = ctk.CTkScrollableFrame(
            fleet_card, fg_color="transparent",
            scrollbar_button_color=BORDER2)
        self.fleet_scroll.pack(fill="both", expand=True, padx=10, pady=8)
        self.lbl_no_fleet = ctk.CTkLabel(
            self.fleet_scroll, text="No devices online.",
            text_color=TEXT_DIM, font=FONT_SMALL)
        self.lbl_no_fleet.pack(pady=20)

    def _clear_log(self):
        self.activity_log.configure(state="normal")
        self.activity_log.delete("0.0", "end")
        self.activity_log.configure(state="disabled")

    def log_to_ui(self, message, level="INFO"):
        prefix = {"INFO": "[INFO] ", "WARN": "[WARN] ",
                  "ALERT": "[ALRT] ", "OK": "[OK]  "}
        self.activity_log.configure(state="normal")
        self.activity_log.insert(
            "0.0",
            f"{time.strftime('%H:%M:%S')}  {prefix.get(level,'')}{message}\n")
        self.activity_log.configure(state="disabled")

    # ── Fleet polling ─────────────────────────────────────────────────────────

    def _start_fleet_polling(self):
        def poll():
            while self.polling_active:
                active_count  = 0
                fleet_steps   = 0
                current_alerts = []
                fleet_data    = []

                for ip, dev_info in list(db.wearables.items()):
                    alias  = dev_info.get("DeviceAlias", ip)
                    status, err = _poll_wearable(ip)

                    if status is not None:
                        active_count += 1
                        steps    = status.get("step_count", 0)
                        fleet_steps += steps
                        w_name   = status.get("worker_name", alias)
                        battery  = status.get("battery_pct", 0)
                        charging = status.get("charging", False)
                        fall_det = status.get("fall_detected", False)

                        fleet_data.append({
                            "name":     w_name,
                            "ip":       ip,
                            "battery":  battery,
                            "charging": charging,
                            "steps":    steps,
                            "online":   True,
                        })

                        # --- Fatigue detection ---
                        fatigue_tracker.update(ip, w_name, steps,
                                               fall_detected=fall_det)

                        if fall_det:
                            current_alerts.append(
                                {"type": "FALL", "worker": w_name, "ip": ip})
                        if status.get("help_alert"):
                            current_alerts.append(
                                {"type": "SOS", "worker": w_name, "ip": ip})
                    else:
                        # Device offline — reset fatigue so stale state clears
                        fatigue_tracker.reset_worker(ip)
                        fleet_data.append({
                            "name":     alias,
                            "ip":       ip,
                            "battery":  0,
                            "charging": False,
                            "steps":    0,
                            "online":   False,
                            "err":      err,
                        })

                self.after(
                    0,
                    lambda ac=active_count, fs=fleet_steps,
                           al=current_alerts, fd=fleet_data:
                    self._update_dashboard(ac, fs, al, fd))

                time.sleep(4)

        threading.Thread(target=poll, daemon=True).start()

    # ── Dashboard update ──────────────────────────────────────────────────────

    def _update_dashboard(self, active_devices, fleet_steps, alerts, fleet_data):
        self.cards["devices"].set_value(active_devices)
        self.cards["nodes"].set_value(
            db.system_stats.get("active_anchors", len(db.nodes)))
        self.cards["steps"].set_value(f"{fleet_steps:,}")
        self.cards["alerts"].set_value(len(alerts))
        self.lbl_last_update.configure(
            text=f"Updated {time.strftime('%H:%M:%S')}")

        # Rebuild alert + fatigue banners
        for w in self.alert_container.winfo_children():
            w.destroy()

        new_alert_states = set()
        for alert in alerts:
            aid = f"{alert['ip']}_{alert['type']}"
            new_alert_states.add(aid)
            if aid not in self.active_alert_states:
                db.log_event(alert["type"], alert["ip"],
                             f"{alert['worker']} triggered {alert['type']} alert.")
                self.log_to_ui(
                    f"{alert['type']} alert — {alert['worker']} ({alert['ip']})",
                    "ALERT")

            def make_cb(a=alert):
                return lambda: self._ack_alert(a)

            AlertBanner(self.alert_container, alert["type"],
                        alert["worker"], alert["ip"],
                        make_cb()).pack(fill="x", pady=3)

        self.active_alert_states = new_alert_states

        # Show fatigue banners for HIGH / CRITICAL workers
        fatigue_states = fatigue_tracker.get_all_states()
        for ip, state in fatigue_states.items():
            level = state.get("label", "OK")
            if level in ("HIGH", "CRITICAL"):
                FatigueBanner(
                    self.alert_container,
                    state.get("worker", ip),
                    ip, level,
                    state.get("steps", 0),
                ).pack(fill="x", pady=2)

        # Rebuild fleet overview
        for w in self.fleet_scroll.winfo_children():
            w.destroy()

        if not fleet_data:
            ctk.CTkLabel(self.fleet_scroll, text="No devices configured.",
                         text_color=TEXT_DIM, font=FONT_SMALL).pack(pady=20)
            return

        for dev in fleet_data:
            ip = dev["ip"]
            fat_state  = fatigue_tracker.get_state(ip)
            fat_level  = fat_state.get("label", "OK") if fat_state else "OK"
            fat_color  = _FATIGUE_COLORS.get(fat_level, TEXT_MUTED)
            fat_icon   = _FATIGUE_ICONS.get(fat_level, "")

            row = ctk.CTkFrame(
                self.fleet_scroll, fg_color=SURFACE2,
                corner_radius=INNER_RADIUS, border_width=1,
                border_color=BORDER2 if dev["online"] else BORDER)
            row.pack(fill="x", pady=3)

            # Status dot
            dot_color = SUCCESS if dev["online"] else TEXT_DIM
            ctk.CTkLabel(row, text="●", font=("Outfit", 12),
                         text_color=dot_color, width=22).pack(
                side="left", padx=(10, 0), pady=10)

            # Name + IP
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", padx=8, pady=8, fill="y")
            ctk.CTkLabel(info, text=dev["name"],
                         font=("Outfit", 12, "bold"),
                         text_color=TEXT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(info, text=dev["ip"],
                         font=("JetBrains Mono", 10),
                         text_color=TEXT_DIM, anchor="w").pack(anchor="w")

            if dev["online"]:
                # Fatigue indicator
                if fat_level != "OK":
                    ctk.CTkLabel(row,
                                 text=f"{fat_icon} {fat_level}",
                                 font=("Outfit", 10, "bold"),
                                 text_color=fat_color).pack(
                        side="right", padx=(0, 6))

                # Battery
                bat_col = (SUCCESS if dev["battery"] > 40 else
                           WARNING if dev["battery"] > 15 else DANGER)
                charge_icon = " ⚡" if dev["charging"] else ""
                ctk.CTkLabel(row, text=f"{dev['battery']}%{charge_icon}",
                             font=("Outfit", 11, "bold"),
                             text_color=bat_col).pack(side="right", padx=14)
                ctk.CTkLabel(row, text=f"{dev['steps']:,} steps",
                             font=("Outfit", 10),
                             text_color=TEXT_MUTED).pack(side="right", padx=6)
            else:
                ctk.CTkLabel(row, text="OFFLINE",
                             font=("Outfit", 10, "bold"),
                             text_color=TEXT_DIM).pack(side="right", padx=14)

    def _ack_alert(self, alert):
        ip         = alert["ip"]
        alert_type = alert["type"]
        worker     = alert["worker"]

        def do():
            try:
                if alert_type == "FALL":
                    api.wearable_acknowledge_fall(ip)
                else:
                    api.wearable_acknowledge_help(ip)
                db.log_event(f"{alert_type}_ACK", ip,
                             f"{alert_type} acknowledged.")
                ok_msg = f"Acknowledged {alert_type} for {worker}"
                self.after(0, lambda m=ok_msg: self.log_to_ui(m, "OK"))
            except Exception as exc:
                err_msg = str(exc)
                self.after(0, lambda m=err_msg: self.log_to_ui(
                    f"ACK failed {ip}: {m}", "WARN"))

        threading.Thread(target=do, daemon=True).start()

    def destroy(self):
        self.polling_active = False
        db.unregister_update_callback(self._on_data_update)
        fatigue_tracker.unregister_callback(self._on_fatigue_alert)
        super().destroy()
