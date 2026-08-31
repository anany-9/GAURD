# ui/views/rtls_nodes.py
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import requests
import time
import json
import os
import uuid as uuid_lib
from datetime import datetime

from ui.styles import *
from core.data_mgr import db
from core.api_client import api
from core.ai_engine import rule_engine   # Use the shared EnhancedRuleEngine

# ── HTTP session ──────────────────────────────────────────────────────────────
http_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=3)
http_session.mount("http://",  _adapter)
http_session.mount("https://", _adapter)

CONFIG_FILE = "node_filters_config.json"

# ── Treeview dark style ───────────────────────────────────────────────────────
def _apply_tree_style():
    style = ttk.Style()
    style.theme_use("default")
    for name in ("Nodes.Treeview", "Links.Treeview", "Rules.Treeview"):
        style.configure(name,
                        background=SURFACE3, foreground=TEXT,
                        fieldbackground=SURFACE3, borderwidth=0,
                        font=("Outfit", 11), rowheight=30)
        style.configure(f"{name}.Heading",
                        background=SURFACE2, foreground=TEXT_MUTED,
                        font=("Outfit", 10, "bold"), borderwidth=0, relief="flat")
        style.map(name,
                  background=[("selected", ACCENT2_DIM)],
                  foreground=[("selected", TEXT)])
    # Sash (paned window divider) styling
    style.configure("TPanedwindow", background=BORDER)
    style.configure("Sash", sashrelief="flat", sashpad=4,
                    background=BORDER2, activebackground=ACCENT_DIM)

# ── AnchorNode polling object ─────────────────────────────────────────────────
class AnchorNode:
    def __init__(self, ip, custom_alias, view_ref):
        self.ip           = ip
        self.custom_alias = custom_alias
        self.name         = custom_alias if custom_alias else "Fetching..."
        self.mac          = "N/A"
        self.firmware     = "N/A"
        self.running      = False
        self.online       = False
        self.view_ref     = view_ref

    def fetch_identity(self):
        """Fetch node identity with improved error handling"""
        result, error = api.scanner_get_identity_safe(self.ip)
        if result is not None:
            if not self.custom_alias:
                self.name = result.get("node_name", "Unknown")
            self.mac      = result.get("mac", "N/A")
            self.firmware = result.get("firmware_version", "N/A")
            return True, ""
        return False, error or "Unknown error"

    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self.poll, daemon=True).start()

    def stop(self):
        self.running = False
        self.online  = False

    def poll(self):
        while self.running:
            try:
                try:
                    major = int(self.view_ref.major_entry.get())
                    minor = int(self.view_ref.minor_entry.get())
                except ValueError:
                    major, minor = -1, -1
                
                # Use safe API call
                result, error = api.scanner_get_devices_safe(self.ip)
                
                if result is not None:
                    self.online = True
                    for device in result:
                        if device.get("major") == major and device.get("minor") == minor:
                            dev_uuid = device.get("uuid")
                            rssi     = device.get("rssi", -100)
                            dist     = device.get("distance_m", device.get("distance", 0.0))
                            
                            # Update local view state - now with anchor name to track separately
                            self.view_ref.update_beacon_state(dev_uuid, self.name, rssi, dist)
                            
                            # Update global data manager for dashboard
                            db.update_beacon_state(dev_uuid, self.name, rssi, dist, time.time())
                            db.log_location(dev_uuid, self.name, rssi)
                            
                            # Feed to rule engine (pass RSSI for anomaly detection)
                            rule_engine.update_distance(dev_uuid, dist, rssi)
                else:
                    self.online = False
            except Exception as e:
                self.online = False
                print(f"[AnchorNode] Poll error for {self.ip}: {e}")
            
            time.sleep(0.2)


# ── Helper widgets ────────────────────────────────────────────────────────────
def _section_label(parent, text):
    ctk.CTkLabel(parent, text=text.upper(), font=("Outfit", 9, "bold"),
                 text_color=TEXT_DIM, anchor="w").pack(anchor="w", padx=14, pady=(12, 4))

def _tree_in_frame(parent, style, columns, widths, height=None):
    wrap = ctk.CTkFrame(parent, fg_color=SURFACE3, corner_radius=INNER_RADIUS)
    kw = {"height": height} if height else {}
    tree = ttk.Treeview(wrap, style=style, columns=columns, show="headings", **kw)
    for col, w in zip(columns, widths):
        tree.heading(col, text=col)
        tree.column(col, width=w, anchor="center")
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
    sb.pack(side="right", fill="y", pady=4)
    return wrap, tree

# ── Main View ─────────────────────────────────────────────────────────────────
class NodesView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.controller    = controller
        self.anchors       = {}
        # Changed: now tracks beacon detections per anchor
        # Key format: "beacon_uuid|anchor_name"
        self.active_beacon_detections = {}
        self.config        = self._load_config()
        _apply_tree_style()
        self._build_ui()
        self._load_saved_nodes()
        self._update_ui_loop()
        self._cleanup_stale_beacons()

    # ── Config I/O ────────────────────────────────────────────────────────────

    def _load_config(self):
        default = {"major_filter": "1217", "minor_filter": "23", "saved_nodes": {}}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    return {**default, **json.load(f)}
            except:
                pass
        return default

    def _save_config(self):
        try:
            self.config["major_filter"] = self.major_entry.get().strip()
            self.config["minor_filter"] = self.minor_entry.get().strip()
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"Config save error: {e}")

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Fixed header bar
        header = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="◎  RTLS SCANNER NODES",
                     font=("Outfit", 17, "bold"), text_color=TEXT).pack(side="left", padx=30, pady=18)

        fbar = ctk.CTkFrame(header, fg_color=SURFACE2, corner_radius=8)
        fbar.pack(side="right", padx=20, pady=12)
        ctk.CTkLabel(fbar, text="  iBeacon Filter  |  Major:",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(side="left", padx=(10, 4))
        self.major_entry = ctk.CTkEntry(fbar, width=70, font=("JetBrains Mono", 12),
                                        fg_color=SURFACE3, border_color=BORDER2,
                                        text_color=TEXT, height=32)
        self.major_entry.insert(0, self.config["major_filter"])
        self.major_entry.pack(side="left", padx=4)
        ctk.CTkLabel(fbar, text="Minor:", font=("Outfit", 11),
                     text_color=TEXT_MUTED).pack(side="left", padx=4)
        self.minor_entry = ctk.CTkEntry(fbar, width=70, font=("JetBrains Mono", 12),
                                        fg_color=SURFACE3, border_color=BORDER2,
                                        text_color=TEXT, height=32)
        self.minor_entry.insert(0, self.config["minor_filter"])
        self.minor_entry.pack(side="left", padx=4)
        ctk.CTkButton(fbar, text="Apply", width=64, height=32,
                      fg_color=ACCENT, text_color=BG, font=("Outfit", 11, "bold"),
                      corner_radius=6, hover_color=ACCENT_DIM,
                      command=self._save_config).pack(side="left", padx=(8, 10))

        # ── Outer vertical PanedWindow (top = nodes+links+rules | bottom = tracker) ──
        outer_pane = tk.PanedWindow(self, orient=tk.VERTICAL,
                                    bg=BORDER, sashwidth=6, sashpad=2,
                                    sashrelief=tk.FLAT, opaqueresize=True)
        outer_pane.pack(fill="both", expand=True, padx=0, pady=0)

        # Top composite frame
        top_frame = tk.Frame(outer_pane, bg=BG)
        outer_pane.add(top_frame, minsize=260)

        # Bottom: Live tracker
        bottom_frame = tk.Frame(outer_pane, bg=BG)
        outer_pane.add(bottom_frame, minsize=120)
        outer_pane.paneconfigure(top_frame,    stretch="always")
        outer_pane.paneconfigure(bottom_frame, stretch="always")

        self._build_tracker_panel(bottom_frame)

        # ── Inner horizontal PanedWindow (nodes | tabs) ──────────────────────
        inner_pane = tk.PanedWindow(top_frame, orient=tk.HORIZONTAL,
                                    bg=BORDER, sashwidth=6, sashpad=2,
                                    sashrelief=tk.FLAT, opaqueresize=True)
        inner_pane.pack(fill="both", expand=True)

        # Left: enrollment + control panel
        left_frame = tk.Frame(inner_pane, bg=BG, width=240)
        inner_pane.add(left_frame, minsize=200)

        # Right: tabbed area (node table | beacon links | automation)
        right_frame = tk.Frame(inner_pane, bg=BG)
        inner_pane.add(right_frame, minsize=300)
        inner_pane.paneconfigure(left_frame,  stretch="never")
        inner_pane.paneconfigure(right_frame, stretch="always")

        self._build_enrollment_panel(left_frame)
        self._build_right_tabs(right_frame)

    # ── Left enrollment panel ─────────────────────────────────────────────────

    def _build_enrollment_panel(self, parent):
        enroll = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                              border_width=1, border_color=BORDER)
        enroll.pack(fill="both", expand=True, padx=(14, 0), pady=14)

        _section_label(enroll, "Node Enrollment")
        self.node_ip_entry = ctk.CTkEntry(enroll, placeholder_text="Device IP",
                                          font=("JetBrains Mono", 12),
                                          fg_color=SURFACE3, border_color=BORDER2,
                                          text_color=TEXT, height=INPUT_HEIGHT)
        self.node_ip_entry.pack(fill="x", padx=14, pady=(0, 6))
        self.alias_entry = ctk.CTkEntry(enroll, placeholder_text="Zone / Alias Name",
                                        fg_color=SURFACE3, border_color=BORDER2,
                                        text_color=TEXT, height=INPUT_HEIGHT)
        self.alias_entry.pack(fill="x", padx=14, pady=(0, 10))
        ctk.CTkButton(enroll, text="+ Register Node", fg_color=ACCENT, text_color=BG,
                      font=("Outfit", 12, "bold"), height=BTN_HEIGHT, corner_radius=8,
                      hover_color=ACCENT_DIM, command=self._add_node).pack(fill="x", padx=14, pady=(0, 14))

        ctk.CTkFrame(enroll, fg_color=BORDER, height=1).pack(fill="x", padx=14)
        _section_label(enroll, "Node Control")

        for text, color, cb in [
            ("▶  Start Tracking", ACCENT2,  lambda: self._set_tracking_state(True, True)),
            ("⏸  Stop Tracking",  WARNING,  lambda: self._set_tracking_state(True, False)),
            ("✕  Remove Node",    DANGER,   self._delete_node),
        ]:
            ctk.CTkButton(enroll, text=text, fg_color=SURFACE3, text_color=color,
                          border_width=1, border_color=color,
                          font=("Outfit", 11, "bold"), height=36, corner_radius=7,
                          hover_color=SURFACE2, command=cb).pack(fill="x", padx=14, pady=4)

        ctk.CTkFrame(enroll, fg_color="transparent").pack(expand=True, fill="both")

    # ── Right tabview ─────────────────────────────────────────────────────────

    def _build_right_tabs(self, parent):
        tabs = ctk.CTkTabview(parent,
                              fg_color=SURFACE,
                              segmented_button_fg_color=SURFACE2,
                              segmented_button_selected_color=SURFACE3,
                              segmented_button_unselected_color=SURFACE2,
                              text_color=TEXT_MUTED,
                              corner_radius=CARD_RADIUS,
                              border_width=1, border_color=BORDER)
        tabs.pack(fill="both", expand=True, padx=(6, 14), pady=14)

        tab_nodes = tabs.add("◎  Anchor Nodes")
        tab_links = tabs.add("⌚  Beacon Links")
        tab_rules = tabs.add("⚡  Automation")

        self._build_nodes_tab(tab_nodes)
        self._build_links_tab(tab_links)
        self._build_rules_tab(tab_rules)

    # ── Anchor Nodes tab ──────────────────────────────────────────────────────

    def _build_nodes_tab(self, tab):
        ctk.CTkLabel(tab, text="Registered Anchor Nodes",
                     font=FONT_LABEL_BOLD, text_color=TEXT).pack(anchor="w", padx=12, pady=(10, 6))
        wrap, self.node_tree = _tree_in_frame(
            tab, "Nodes.Treeview",
            ("Alias/Zone", "IP Address", "Status", "Firmware"),
            (160, 130, 110, 100), height=8)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ── Beacon Links tab ──────────────────────────────────────────────────────

    def _build_links_tab(self, tab):
        # Form to add/edit a link
        form = ctk.CTkFrame(tab, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                            border_width=1, border_color=BORDER)
        form.pack(fill="x", padx=12, pady=(10, 6))
        _section_label(form, "Link iBeacon UUID → Wearable")

        r1 = ctk.CTkFrame(form, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(0, 6))
        self.link_uuid_entry = ctk.CTkEntry(r1, placeholder_text="iBeacon UUID (e.g. FDA50693…)",
                                            font=("JetBrains Mono", 11),
                                            fg_color=SURFACE3, border_color=BORDER2,
                                            text_color=TEXT, height=INPUT_HEIGHT)
        self.link_uuid_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # Wearable dropdown - populated from db.wearables
        self.link_wearable_var = ctk.StringVar(value="Select wearable…")
        self.link_wearable_menu = ctk.CTkOptionMenu(
            r1, variable=self.link_wearable_var,
            values=self._get_wearable_options(),
            fg_color=SURFACE3, button_color=ACCENT2,
            button_hover_color=ACCENT2_DIM, text_color=TEXT,
            font=("Outfit", 11), width=180, height=INPUT_HEIGHT)
        self.link_wearable_menu.pack(side="left")

        r2 = ctk.CTkFrame(form, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(0, 4))
        self.link_worker_entry = ctk.CTkEntry(r2, placeholder_text="Worker Name (label only)",
                                              fg_color=SURFACE3, border_color=BORDER2,
                                              text_color=TEXT, height=INPUT_HEIGHT)
        self.link_worker_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.link_notes_entry = ctk.CTkEntry(r2, placeholder_text="Notes (optional)",
                                             fg_color=SURFACE3, border_color=BORDER2,
                                             text_color=TEXT, height=INPUT_HEIGHT)
        self.link_notes_entry.pack(side="left", fill="x", expand=True)

        btn_row = ctk.CTkFrame(form, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(btn_row, text="Save Link", fg_color=ACCENT, text_color=BG,
                      font=("Outfit", 11, "bold"), height=BTN_HEIGHT, corner_radius=8,
                      hover_color=ACCENT_DIM, command=self._save_link).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Delete Selected", fg_color=SURFACE3,
                      text_color=DANGER, border_width=1, border_color=DANGER,
                      font=("Outfit", 11), height=BTN_HEIGHT, corner_radius=8,
                      hover_color=SURFACE2, command=self._delete_link).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="⟳ Refresh Wearables", fg_color=SURFACE3,
                      text_color=TEXT_MUTED, font=("Outfit", 10), height=BTN_HEIGHT,
                      corner_radius=8, hover_color=SURFACE2,
                      command=self._refresh_link_dropdowns).pack(side="left")

        # Links table
        wrap, self.links_tree = _tree_in_frame(
            tab, "Links.Treeview",
            ("iBeacon UUID", "Wearable IP", "Worker Name", "Notes"),
            (230, 130, 130, 150))
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.links_tree.bind("<<TreeviewSelect>>", self._on_link_select)
        self._reload_links_table()

    # ── Automation Rules tab ──────────────────────────────────────────────────

    def _build_rules_tab(self, tab):
        # Add rule form
        form = ctk.CTkFrame(tab, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                            border_width=1, border_color=BORDER)
        form.pack(fill="x", padx=12, pady=(10, 6))
        _section_label(form, "Add Automation Rule")

        r1 = ctk.CTkFrame(form, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(0, 6))
        self.rule_name_entry = ctk.CTkEntry(r1, placeholder_text="Rule Name",
                                            fg_color=SURFACE3, border_color=BORDER2,
                                            text_color=TEXT, height=INPUT_HEIGHT)
        self.rule_name_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.rule_type_var = ctk.StringVar(value="distance")
        type_menu = ctk.CTkOptionMenu(r1, variable=self.rule_type_var,
                                      values=["distance", "scheduled"],
                                      fg_color=SURFACE3, button_color=ACCENT2,
                                      button_hover_color=ACCENT2_DIM, text_color=TEXT,
                                      font=("Outfit", 11), width=130, height=INPUT_HEIGHT,
                                      command=self._on_rule_type_change)
        type_menu.pack(side="left")

        r2 = ctk.CTkFrame(form, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(0, 6))
        self.rule_beacon_entry = ctk.CTkEntry(r2,
            placeholder_text="iBeacon UUID (distance rules) or leave blank (scheduled)",
            font=("JetBrains Mono", 11),
            fg_color=SURFACE3, border_color=BORDER2, text_color=TEXT, height=INPUT_HEIGHT)
        self.rule_beacon_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.rule_target_var = ctk.StringVar(value="Select target wearable…")
        self.rule_target_menu = ctk.CTkOptionMenu(
            r2, variable=self.rule_target_var,
            values=self._get_wearable_options(),
            fg_color=SURFACE3, button_color=ACCENT2,
            button_hover_color=ACCENT2_DIM, text_color=TEXT,
            font=("Outfit", 11), width=180, height=INPUT_HEIGHT)
        self.rule_target_menu.pack(side="left")

        r3 = ctk.CTkFrame(form, fg_color="transparent")
        r3.pack(fill="x", padx=12, pady=(0, 6))
        self.rule_cond_label = ctk.CTkLabel(r3, text="Threshold (m):",
                                            font=("Outfit", 11), text_color=TEXT_MUTED, width=110, anchor="w")
        self.rule_cond_label.pack(side="left")
        self.rule_cond_entry = ctk.CTkEntry(r3, placeholder_text="e.g. 3.5  or  08:30",
                                            fg_color=SURFACE3, border_color=BORDER2,
                                            text_color=TEXT, height=INPUT_HEIGHT, width=130)
        self.rule_cond_entry.pack(side="left", padx=(0, 12))
        self.rule_alert_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r3, text="Critical Alert", variable=self.rule_alert_var,
                        fg_color=DANGER, hover_color="#c02040",
                        text_color=DANGER, font=("Outfit", 11)).pack(side="left")

        r4 = ctk.CTkFrame(form, fg_color="transparent")
        r4.pack(fill="x", padx=12, pady=(0, 6))
        self.rule_title_entry = ctk.CTkEntry(r4, placeholder_text="Notification Title",
                                             fg_color=SURFACE3, border_color=BORDER2,
                                             text_color=TEXT, height=INPUT_HEIGHT)
        self.rule_title_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.rule_body_entry = ctk.CTkEntry(r4, placeholder_text="Notification Body",
                                            fg_color=SURFACE3, border_color=BORDER2,
                                            text_color=TEXT, height=INPUT_HEIGHT)
        self.rule_body_entry.pack(side="left", fill="x", expand=True)

        btn_row = ctk.CTkFrame(form, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(btn_row, text="Add Rule", fg_color=ACCENT, text_color=BG,
                      font=("Outfit", 11, "bold"), height=BTN_HEIGHT, corner_radius=8,
                      hover_color=ACCENT_DIM, command=self._add_rule).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Delete Selected", fg_color=SURFACE3,
                      text_color=DANGER, border_width=1, border_color=DANGER,
                      font=("Outfit", 11), height=BTN_HEIGHT, corner_radius=8,
                      hover_color=SURFACE2, command=self._delete_rule).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Toggle Enable/Disable", fg_color=SURFACE3,
                      text_color=ACCENT_GOLD, border_width=1, border_color=ACCENT_GOLD,
                      font=("Outfit", 11), height=BTN_HEIGHT, corner_radius=8,
                      hover_color=SURFACE2, command=self._toggle_rule).pack(side="left")

        # Rules table
        wrap, self.rules_tree = _tree_in_frame(
            tab, "Rules.Treeview",
            ("Name", "Type", "Condition", "Target", "Beacon UUID", "State"),
            (140, 90, 110, 130, 220, 70))
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self._reload_rules_table()

    # ── Live tracker panel ────────────────────────────────────────────────────

    def _build_tracker_panel(self, parent):
        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                            border_width=1, border_color=BORDER)
        card.pack(fill="both", expand=True, padx=14, pady=(6, 14))

        hdr = ctk.CTkFrame(card, fg_color=SURFACE2, corner_radius=0, height=42)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="◎  Live Asset Monitor (Multi-Scanner View)",
                     font=FONT_LABEL_BOLD, text_color=ACCENT2).pack(side="left", padx=16, pady=10)
        self.lbl_detection_count = ctk.CTkLabel(hdr, text="0 detections",
                                             font=("JetBrains Mono", 11), text_color=TEXT_DIM)
        self.lbl_detection_count.pack(side="right", padx=16)

        wrap, self.tracker_tree = _tree_in_frame(
            card, "Nodes.Treeview",
            ("iBeacon UUID", "Linked Worker", "Zone / Anchor", "RSSI", "Distance", "Last Seen"),
            (240, 130, 150, 80, 90, 100))
        wrap.pack(fill="both", expand=True, padx=12, pady=(6, 12))

    # ══════════════════════════════════════════════
    # NODE MANAGEMENT
    # ══════════════════════════════════════════════

    def _load_saved_nodes(self):
        for ip, d in self.config.get("saved_nodes", {}).items():
            node = AnchorNode(ip, d["Name"], self)
            node.mac, node.firmware = d.get("MAC","N/A"), d.get("Firmware","N/A")
            self.anchors[ip] = node
            self.node_tree.insert("", "end", iid=ip,
                                  values=(node.name, ip, "⊘ Stopped", node.firmware))

    def _add_node(self):
        ip    = self.node_ip_entry.get().strip()
        alias = self.alias_entry.get().strip()
        if not ip or ip in self.anchors:
            return
        tid = f"tmp_{ip}"
        self.node_tree.insert("", "end", iid=tid, values=("Connecting…", ip, "⟳", "—"))
        def worker():
            node = AnchorNode(ip, alias, self)
            success, _ = node.fetch_identity()
            self.after(0, lambda: self._finalize_add(success, node, tid))
        threading.Thread(target=worker, daemon=True).start()

    def _finalize_add(self, success, node, tid):
        if self.node_tree.exists(tid):
            self.node_tree.delete(tid)
        if success:
            self.anchors[node.ip] = node
            self.config.setdefault("saved_nodes", {})[node.ip] = {
                "Name": node.name, "MAC": node.mac, "Firmware": node.firmware
            }
            self._save_config()
            self.node_tree.insert("", "end", iid=node.ip,
                                  values=(node.name, node.ip, "⊘ Ready", node.firmware))

    def _delete_node(self):
        sid = self.node_tree.focus()
        if sid and sid in self.anchors:
            anchor_name = self.anchors[sid].name
            self.anchors[sid].stop()
            del self.anchors[sid]
            self.config.get("saved_nodes", {}).pop(sid, None)
            self._save_config()
            self.node_tree.delete(sid)
            
            # Remove all detections from this anchor
            to_remove = [key for key in self.active_beacon_detections.keys() 
                        if key.endswith(f"|{anchor_name}")]
            for key in to_remove:
                del self.active_beacon_detections[key]

    def _set_tracking_state(self, selected, start):
        targets = [self.node_tree.focus()] if selected else list(self.anchors.keys())
        for ip in targets:
            if ip in self.anchors:
                if start: self.anchors[ip].start()
                else:     self.anchors[ip].stop()

    # ══════════════════════════════════════════════
    # BEACON LINKS CRUD
    # ══════════════════════════════════════════════

    def _get_wearable_options(self):
        opts = [f"{v.get('DeviceAlias',ip)}  ({ip})" for ip, v in db.wearables.items()]
        return opts if opts else ["No wearables registered"]

    def _get_ip_from_option(self, opt_str):
        """Extract IP from 'Alias  (IP)' option string."""
        if "(" in opt_str and ")" in opt_str:
            return opt_str.split("(")[-1].rstrip(")")
        return opt_str.strip()

    def _refresh_link_dropdowns(self):
        opts = self._get_wearable_options()
        self.link_wearable_menu.configure(values=opts)
        self.rule_target_menu.configure(values=opts)

    def _save_link(self):
        uuid_val = self.link_uuid_entry.get().strip()
        opt      = self.link_wearable_var.get()
        wip      = self._get_ip_from_option(opt)
        worker   = self.link_worker_entry.get().strip()
        notes    = self.link_notes_entry.get().strip()
        if not uuid_val or not wip or "No wearables" in wip:
            messagebox.showwarning("Incomplete", "UUID and a valid wearable are required.")
            return
        db.save_beacon_link(uuid_val, wip, worker, notes)
        self._reload_links_table()
        self.link_uuid_entry.delete(0, tk.END)
        self.link_worker_entry.delete(0, tk.END)
        self.link_notes_entry.delete(0, tk.END)
        messagebox.showinfo("Saved", f"Beacon link saved for UUID {uuid_val[:20]}…")

    def _delete_link(self):
        sel = self.links_tree.selection()
        if not sel:
            return
        uuid_val = self.links_tree.item(sel[0])["values"][0]
        if messagebox.askyesno("Delete Link", f"Remove link for UUID {str(uuid_val)[:30]}…?"):
            db.remove_beacon_link(str(uuid_val))
            self._reload_links_table()

    def _on_link_select(self, _=None):
        sel = self.links_tree.selection()
        if not sel:
            return
        vals = self.links_tree.item(sel[0])["values"]
        self.link_uuid_entry.delete(0, tk.END)
        self.link_uuid_entry.insert(0, str(vals[0]))
        self.link_worker_entry.delete(0, tk.END)
        self.link_worker_entry.insert(0, str(vals[2]))
        self.link_notes_entry.delete(0, tk.END)
        self.link_notes_entry.insert(0, str(vals[3]))

    def _reload_links_table(self):
        for item in self.links_tree.get_children():
            self.links_tree.delete(item)
        for uuid_val, link in db.beacon_links.items():
            self.links_tree.insert("", "end", values=(
                link["BeaconUUID"], link["WearableIP"],
                link["WorkerName"], link["Notes"]))

    # ══════════════════════════════════════════════
    # AUTOMATION RULES CRUD
    # ══════════════════════════════════════════════

    def _on_rule_type_change(self, val):
        if val == "distance":
            self.rule_cond_label.configure(text="Threshold (m):")
            self.rule_cond_entry.configure(placeholder_text="e.g. 3.5")
        else:
            self.rule_cond_label.configure(text="Time (HH:MM):")
            self.rule_cond_entry.configure(placeholder_text="e.g. 08:30")

    def _add_rule(self):
        name  = self.rule_name_entry.get().strip()
        rtype = self.rule_type_var.get()
        buuid = self.rule_beacon_entry.get().strip()
        cond  = self.rule_cond_entry.get().strip()
        title = self.rule_title_entry.get().strip()
        body  = self.rule_body_entry.get().strip()
        opt   = self.rule_target_var.get()
        target= self._get_ip_from_option(opt)

        if not name or not cond or not title or "No wearables" in target:
            messagebox.showwarning("Incomplete", "Name, Condition, Notification Title and a Target wearable are required.")
            return
        if rtype == "scheduled":
            parts = cond.split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                messagebox.showwarning("Bad Format", "Scheduled time must be HH:MM (24-h), e.g. 08:30")
                return
        if rtype == "distance":
            try:
                float(cond)
            except ValueError:
                messagebox.showwarning("Bad Format", "Distance threshold must be a number, e.g. 3.5")
                return

        rule_id = str(uuid_lib.uuid4())[:8]
        rule = {
            "RuleID":          rule_id,
            "Enabled":         "1",
            "RuleName":        name,
            "RuleType":        rtype,
            "BeaconUUID":      buuid,
            "TargetWearableIP":target,
            "ConditionValue":  cond,
            "NotifTitle":      title,
            "NotifBody":       body,
            "IsAlert":         "1" if self.rule_alert_var.get() else "0",
        }
        db.save_auto_rule(rule)
        self._reload_rules_table()
        # Clear form
        for w in [self.rule_name_entry, self.rule_beacon_entry,
                  self.rule_cond_entry, self.rule_title_entry, self.rule_body_entry]:
            w.delete(0, tk.END)
        self.rule_alert_var.set(False)
        messagebox.showinfo("Rule Added", f"Rule '{name}' added and enabled.")

    def _delete_rule(self):
        sel = self.rules_tree.selection()
        if not sel:
            return
        rule_id   = sel[0]   # iid == RuleID set in _reload_rules_table
        rule_name = str(self.rules_tree.item(sel[0])["values"][0])
        if messagebox.askyesno("Delete Rule", f"Delete rule '{rule_name}'?"):
            db.remove_auto_rule(rule_id)
            rule_engine._in_zone.pop(rule_id, None)
            self._reload_rules_table()

    def _toggle_rule(self):
        sel = self.rules_tree.selection()
        if not sel:
            return
        rule_id = sel[0]   # iid == RuleID
        rule = db.auto_rules.get(rule_id)
        if rule:
            new_enabled = rule.get("Enabled", "1") != "1"
            db.set_rule_enabled(rule_id, new_enabled)
            self._reload_rules_table()

    def _reload_rules_table(self):
        for item in self.rules_tree.get_children():
            self.rules_tree.delete(item)
        for rid, rule in db.auto_rules.items():
            cond_disp = rule.get("ConditionValue", "")
            if rule.get("RuleType") == "distance":
                cond_disp = f"<= {cond_disp} m"
            elif rule.get("RuleType") == "scheduled":
                cond_disp = f"Daily @ {cond_disp}"
            enabled    = "ON" if rule.get("Enabled", "0") == "1" else "OFF"
            uuid_disp  = rule.get("BeaconUUID", "").strip() or "-- auto"
            # iid = RuleID so delete/toggle never rely on fragile name lookup
            self.rules_tree.insert("", "end", iid=rid, values=(
                rule.get("RuleName", ""),
                rule.get("RuleType", ""),
                cond_disp,
                rule.get("TargetWearableIP", ""),
                uuid_disp,
                enabled,
            ))

    # ══════════════════════════════════════════════
    # BEACON STATE & AUTOMATION FEED
    # ══════════════════════════════════════════════

    def update_beacon_state(self, uuid, anchor_name, rssi, distance):
        """
        Update beacon detection state.
        Now uses composite key: "beacon_uuid|anchor_name" to track each
        detection separately, allowing the same beacon to appear multiple
        times if detected by different scanners.
        """
        detection_key = f"{uuid}|{anchor_name}"
        self.active_beacon_detections[detection_key] = {
            "uuid": uuid,
            "anchor": anchor_name,
            "rssi":   rssi,
            "dist":   distance,
            "time":   time.time(),
        }

    # ══════════════════════════════════════════════
    # UI LOOP & CLEANUP
    # ══════════════════════════════════════════════

    def _cleanup_stale_beacons(self):
        """Remove beacon detections that haven't been updated in 120 seconds"""
        now   = time.time()
        stale = [key for key, data in self.active_beacon_detections.items() 
                 if (now - data["time"]) > 120]
        
        for key in stale:
            if self.tracker_tree.exists(key):
                self.tracker_tree.delete(key)
            
            # Extract UUID from composite key for rule engine cleanup
            uuid = key.split("|")[0]
            del self.active_beacon_detections[key]
            
            # Only clear from rule engine if no other anchors are tracking this beacon
            still_tracked = any(k.startswith(f"{uuid}|") 
                              for k in self.active_beacon_detections.keys())
            if not still_tracked:
                rule_engine.clear_beacon(uuid)
        
        self.after(1000, self._cleanup_stale_beacons)

    def _update_ui_loop(self):
        """Enhanced UI loop with real-time stats updates and multi-scanner tracking"""
        active_nodes = 0
        for ip, anchor in self.anchors.items():
            if self.node_tree.exists(ip):
                if anchor.running and anchor.online:
                    status = "● Active"
                    active_nodes += 1
                elif anchor.running:
                    status = "✕ Offline"
                else:
                    status = "⊘ Stopped"
                self.node_tree.set(ip, "Status", status)

        # Calculate unique beacons (not detections)
        unique_beacons = len(set(data["uuid"] for data in self.active_beacon_detections.values()))
        
        # Update global stats for dashboard
        db.update_anchor_stats(active_nodes, unique_beacons)

        now = time.time()
        
        # Update tracker tree with all detections (including duplicates from different anchors)
        for detection_key, data in self.active_beacon_detections.items():
            sec = int(now - data["time"])
            uuid_val = data["uuid"]
            
            # Resolve linked worker name
            link = db.beacon_links.get(uuid_val)
            worker_name = link["WorkerName"] if link else "—"
            
            vals = (
                uuid_val,
                worker_name,
                data["anchor"],
                f"{data['rssi']} dBm",
                f"{data['dist']:.2f} m",
                f"{sec}s ago",
            )
            
            if self.tracker_tree.exists(detection_key):
                self.tracker_tree.item(detection_key, values=vals)
            else:
                self.tracker_tree.insert("", "end", iid=detection_key, values=vals)

        # Update detection count label
        total_detections = len(self.active_beacon_detections)
        detection_text = f"{total_detections} detection{'s' if total_detections != 1 else ''}"
        if unique_beacons != total_detections:
            detection_text += f"  ({unique_beacons} unique beacon{'s' if unique_beacons != 1 else ''})"
        self.lbl_detection_count.configure(text=detection_text)

        self.after(500, self._update_ui_loop)
