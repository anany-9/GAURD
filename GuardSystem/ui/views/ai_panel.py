# ui/views/ai_panel.py
"""
GUARD AI Panel — Groq-powered AI Intelligence Center.

Tabs:
  Settings      — Groq API key + editable fatigue thresholds (persisted)
  Risk Monitor  — live per-beacon risk scores + how-it-works explanation + anomaly feed
  Fatigue       — live fatigue levels per worker with step history
  Briefing      — AI-generated security briefing
  History       — AI analysis of location_history.csv
  Rule Suggest  — AI-generated automation rule suggestions
  Event Summary — AI summary of system_events.csv
  AI Chat       — freeform Q&A with full system context
"""

import tkinter as tk
from tkinter import ttk
import customtkinter as ctk
import threading
import time
import json
import os
from datetime import datetime

from ui.styles import *
from core.data_mgr import db
from core.ai_engine import groq, ai_engine, rule_engine, fatigue_tracker
from core.ai_engine import (FATIGUE_WARN_STEPS, FATIGUE_HIGH_STEPS,
                              FATIGUE_CRIT_STEPS, _FATIGUE_COLORS, _FATIGUE_ICONS,
                              RSSI_ANOMALY_ZSCORE, DIST_JUMP_THRESHOLD_M,
                              RISK_HIGH_THRESHOLD, RISK_MEDIUM_THRESHOLD)
import core.ai_engine as _ai_mod

_CONFIG_PATH = os.path.join("data", "system_config.json")


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_config(cfg: dict):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(_CONFIG_PATH)), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[AIPanel] Could not save config: {e}")


def _load_api_key() -> str:
    return _load_config().get("groq_api_key", "")


def _save_api_key(key: str):
    cfg = _load_config()
    cfg["groq_api_key"] = key
    _save_config(cfg)


def _load_fatigue_thresholds() -> dict:
    cfg = _load_config()
    return cfg.get("fatigue_thresholds", {})


def _save_fatigue_thresholds(warn: int, high: int, crit: int, cooldown: int):
    cfg = _load_config()
    cfg["fatigue_thresholds"] = {
        "warn": warn, "high": high,
        "crit": crit, "cooldown": cooldown,
    }
    _save_config(cfg)


def _apply_fatigue_thresholds(warn: int, high: int, crit: int, cooldown: int):
    """Patch the live module-level constants and the tracker's cooldown."""
    _ai_mod.FATIGUE_WARN_STEPS     = warn
    _ai_mod.FATIGUE_HIGH_STEPS     = high
    _ai_mod.FATIGUE_CRIT_STEPS     = crit
    _ai_mod.FATIGUE_ALERT_COOLDOWN = cooldown
    # FatigueTracker reads the module-level names at call time via _ai_mod,
    # so no tracker restart is needed — changes take effect on the next poll.


def _apply_saved_fatigue_thresholds():
    saved = _load_fatigue_thresholds()
    if saved:
        _apply_fatigue_thresholds(
            saved.get("warn",     _ai_mod.FATIGUE_WARN_STEPS),
            saved.get("high",     _ai_mod.FATIGUE_HIGH_STEPS),
            saved.get("crit",     _ai_mod.FATIGUE_CRIT_STEPS),
            saved.get("cooldown", _ai_mod.FATIGUE_ALERT_COOLDOWN),
        )


# ── Tree style ────────────────────────────────────────────────────────────────

def _apply_ai_tree_style():
    style = ttk.Style()
    style.theme_use("default")
    for name in ("AI.Treeview", "Risk.Treeview", "Fat.Treeview"):
        style.configure(name,
                        background=SURFACE3, foreground=TEXT,
                        fieldbackground=SURFACE3, borderwidth=0,
                        font=("Outfit", 11), rowheight=28)
        style.configure(f"{name}.Heading",
                        background=SURFACE2, foreground=TEXT_MUTED,
                        font=("Outfit", 10, "bold"), borderwidth=0, relief="flat")
        style.map(name,
                  background=[("selected", ACCENT2_DIM)],
                  foreground=[("selected", TEXT)])


# ── View ──────────────────────────────────────────────────────────────────────

class AIPanelView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.controller = controller
        _apply_ai_tree_style()
        _apply_saved_fatigue_thresholds()   # restore persisted thresholds on open

        saved_key = _load_api_key()
        if saved_key:
            groq.set_api_key(saved_key)

        rule_engine.register_anomaly_callback(self._on_anomaly)
        self._build_ui()
        self._start_live_refresh()

    def destroy(self):
        rule_engine.unregister_anomaly_callback(self._on_anomaly)
        fatigue_tracker.unregister_callback(self._on_fatigue_event)
        super().destroy()

    # ── UI shell ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="⬡  AI INTELLIGENCE CENTER",
                     font=("Outfit", 17, "bold"), text_color=TEXT).pack(
            side="left", padx=30, pady=18)
        self._status_lbl = ctk.CTkLabel(
            header,
            text="● Groq Ready" if groq.is_configured else "○ API Key Required",
            font=("JetBrains Mono", 11),
            text_color=SUCCESS if groq.is_configured else WARNING)
        self._status_lbl.pack(side="right", padx=30)

        tabs = ctk.CTkTabview(
            self,
            fg_color=SURFACE,
            segmented_button_fg_color=SURFACE2,
            segmented_button_selected_color=SURFACE3,
            segmented_button_unselected_color=SURFACE2,
            text_color=TEXT_MUTED,
            corner_radius=CARD_RADIUS,
            border_width=1, border_color=BORDER)
        tabs.pack(fill="both", expand=True, padx=16, pady=14)

        self._build_settings_tab(tabs.add("⚙  Settings"))
        self._build_risk_tab(tabs.add("◈  Risk Monitor"))
        self._build_fatigue_tab(tabs.add("◌  Fatigue"))
        self._build_briefing_tab(tabs.add("◉  Briefing"))
        self._build_history_tab(tabs.add("⬡  History"))
        self._build_rules_tab(tabs.add("⚡  Rule Suggest"))
        self._build_events_tab(tabs.add("⬦  Events"))
        self._build_chat_tab(tabs.add("✦  AI Chat"))

    # ═══════════════════════════════════════════════════════════════
    # SETTINGS TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_settings_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent",
                                        scrollbar_button_color=BORDER2)
        scroll.pack(fill="both", expand=True)

        # ── Groq API ──────────────────────────────────────────────
        api_card = self._card(scroll, "GROQ API CONFIGURATION")
        ctk.CTkLabel(api_card,
                     text="Enter your Groq API key. Get one free at https://console.groq.com",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=16, pady=(0, 8))
        row = ctk.CTkFrame(api_card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        self._key_entry = ctk.CTkEntry(
            row, placeholder_text="gsk_…",
            font=("JetBrains Mono", 12), show="*",
            fg_color=SURFACE3, border_color=BORDER2,
            text_color=TEXT, height=INPUT_HEIGHT)
        self._key_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        saved = _load_api_key()
        if saved:
            self._key_entry.insert(0, saved)
        ctk.CTkButton(row, text="Save & Apply", width=140, height=INPUT_HEIGHT,
                      fg_color=ACCENT, text_color=BG,
                      font=("Outfit", 12, "bold"), corner_radius=8,
                      hover_color=ACCENT_DIM,
                      command=self._save_key).pack(side="left")
        self._key_status = ctk.CTkLabel(api_card, text="",
                                        font=("Outfit", 11), text_color=SUCCESS)
        self._key_status.pack(anchor="w", padx=16, pady=(0, 14))

        # ── Fatigue Thresholds ────────────────────────────────────
        fat_card = self._card(scroll, "FATIGUE DETECTION THRESHOLDS")
        ctk.CTkLabel(fat_card,
                     text="Set the step-count at which each fatigue level triggers.\n"
                          "Changes take effect immediately and persist across restarts.",
                     font=("Outfit", 11), text_color=TEXT_MUTED, justify="left").pack(
            anchor="w", padx=16, pady=(0, 12))

        # threshold rows
        saved_t = _load_fatigue_thresholds()
        defaults = {
            "warn":     saved_t.get("warn",     _ai_mod.FATIGUE_WARN_STEPS),
            "high":     saved_t.get("high",     _ai_mod.FATIGUE_HIGH_STEPS),
            "crit":     saved_t.get("crit",     _ai_mod.FATIGUE_CRIT_STEPS),
            "cooldown": saved_t.get("cooldown", _ai_mod.FATIGUE_ALERT_COOLDOWN),
        }

        self._thresh_entries = {}
        fields = [
            ("warn",     "⚠  WARN threshold (steps)",
             WARNING,  "Workers are approaching fatigue — consider a break."),
            ("high",     "⬦  HIGH threshold (steps)",
             "#f97316", "Fatigue confirmed — rest recommended immediately."),
            ("crit",     "◈  CRITICAL threshold (steps)",
             DANGER,   "Severe fatigue — stop activity now."),
            ("cooldown", "⏱  Alert cooldown (seconds)",
             TEXT_MUTED, "Minimum gap between repeated alerts per worker."),
        ]

        for key, label, color, hint in fields:
            row = ctk.CTkFrame(fat_card, fg_color=SURFACE3, corner_radius=8)
            row.pack(fill="x", padx=16, pady=4)

            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", fill="y", padx=12, pady=10)
            ctk.CTkLabel(left, text=label, font=("Outfit", 12, "bold"),
                         text_color=color, anchor="w").pack(anchor="w")
            ctk.CTkLabel(left, text=hint, font=("Outfit", 10),
                         text_color=TEXT_DIM, anchor="w").pack(anchor="w")

            entry = ctk.CTkEntry(row, width=110, height=36,
                                 font=("JetBrains Mono", 13),
                                 fg_color=SURFACE2, border_color=BORDER2,
                                 text_color=color, justify="center")
            entry.insert(0, str(defaults[key]))
            entry.pack(side="right", padx=12, pady=10)
            self._thresh_entries[key] = entry

        btn_row = ctk.CTkFrame(fat_card, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(8, 16))
        ctk.CTkButton(btn_row, text="Apply & Save Thresholds",
                      fg_color=ACCENT_STEPS, text_color=BG,
                      font=("Outfit", 12, "bold"), height=BTN_HEIGHT,
                      corner_radius=8,
                      command=self._apply_thresholds).pack(side="left")
        ctk.CTkButton(btn_row, text="Reset to Defaults",
                      fg_color=SURFACE3, text_color=TEXT_MUTED,
                      font=("Outfit", 11), height=BTN_HEIGHT,
                      corner_radius=8, border_width=1, border_color=BORDER2,
                      hover_color=SURFACE2,
                      command=self._reset_thresholds).pack(side="left", padx=(10, 0))
        self._thresh_status = ctk.CTkLabel(btn_row, text="",
                                           font=("Outfit", 11), text_color=SUCCESS)
        self._thresh_status.pack(side="left", padx=14)

        # ── Model info ────────────────────────────────────────────
        info_card = self._card(scroll, "MODEL INFO")
        for line in [
            "  LLM    llama-3.3-70b-versatile  via  Groq",
            "  RSSI anomaly window   20 readings per beacon",
            f"  RSSI z-score threshold   {RSSI_ANOMALY_ZSCORE}",
            f"  Distance jump threshold  {DIST_JUMP_THRESHOLD_M} m",
            f"  Risk HIGH  ≥ {RISK_HIGH_THRESHOLD}   MEDIUM ≥ {RISK_MEDIUM_THRESHOLD}",
        ]:
            ctk.CTkLabel(info_card, text=line, font=("JetBrains Mono", 11),
                         text_color=TEXT_DIM, anchor="w").pack(
                anchor="w", padx=16, pady=2)
        ctk.CTkFrame(info_card, fg_color="transparent", height=8).pack()

    def _save_key(self):
        key = self._key_entry.get().strip()
        if not key:
            self._key_status.configure(text="Key cannot be empty.", text_color=DANGER)
            return
        groq.set_api_key(key)
        _save_api_key(key)
        self._key_status.configure(text="✓ API key saved and applied.", text_color=SUCCESS)
        self._status_lbl.configure(text="● Groq Ready", text_color=SUCCESS)

    def _apply_thresholds(self):
        try:
            warn     = int(self._thresh_entries["warn"].get().strip())
            high     = int(self._thresh_entries["high"].get().strip())
            crit     = int(self._thresh_entries["crit"].get().strip())
            cooldown = int(self._thresh_entries["cooldown"].get().strip())
        except ValueError:
            self._thresh_status.configure(
                text="✕ All values must be whole numbers.", text_color=DANGER)
            return
        if not (0 < warn < high < crit):
            self._thresh_status.configure(
                text="✕ Must satisfy: WARN < HIGH < CRITICAL (all > 0).",
                text_color=DANGER)
            return
        if cooldown < 60:
            self._thresh_status.configure(
                text="✕ Cooldown must be at least 60 s.", text_color=DANGER)
            return
        _apply_fatigue_thresholds(warn, high, crit, cooldown)
        _save_fatigue_thresholds(warn, high, crit, cooldown)
        self._thresh_status.configure(
            text=f"✓ Applied — WARN {warn:,} / HIGH {high:,} / CRIT {crit:,}",
            text_color=SUCCESS)

    def _reset_thresholds(self):
        defaults = {"warn": 8000, "high": 12000, "crit": 16000, "cooldown": 1800}
        for k, v in defaults.items():
            e = self._thresh_entries[k]
            e.delete(0, tk.END)
            e.insert(0, str(v))
        _apply_fatigue_thresholds(8000, 12000, 16000, 1800)
        _save_fatigue_thresholds(8000, 12000, 16000, 1800)
        self._thresh_status.configure(text="✓ Defaults restored.", text_color=SUCCESS)

    # ═══════════════════════════════════════════════════════════════
    # RISK MONITOR TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_risk_tab(self, tab):
        # ── How risk is calculated ────────────────────────────────
        explain = ctk.CTkFrame(tab, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                               border_width=1, border_color=BORDER)
        explain.pack(fill="x", padx=12, pady=(10, 6))

        # Collapsible header
        self._risk_explain_open = False
        toggle_row = ctk.CTkFrame(explain, fg_color="transparent")
        toggle_row.pack(fill="x")
        ctk.CTkLabel(toggle_row, text="◈  HOW RISK SCORE IS CALCULATED",
                     font=("Outfit", 10, "bold"), text_color=ACCENT2).pack(
            side="left", padx=16, pady=10)
        self._toggle_btn = ctk.CTkButton(
            toggle_row, text="▼ Show", width=80, height=26,
            fg_color=SURFACE3, text_color=TEXT_MUTED,
            font=("Outfit", 10), corner_radius=6, hover_color=SURFACE2,
            command=self._toggle_risk_explain)
        self._toggle_btn.pack(side="right", padx=12, pady=8)

        self._explain_body = ctk.CTkFrame(explain, fg_color="transparent")
        # Not packed initially (collapsed)

        explain_lines = [
            ("Each beacon's risk score [0.00 – 1.00] is recalculated every second",
             TEXT_MUTED, False),
            ("from its last 20 RSSI readings and distance samples.", TEXT_MUTED, False),
            ("", TEXT_DIM, False),
            ("COMPONENT 1 — RSSI Signal Instability  (weight 40%)", ACCENT2, True),
            (f"  • Standard deviation of the last 20 RSSI readings is computed.",
             TEXT_MUTED, False),
            (f"  • Risk contribution = min(std_dev / 20, 1.0) × 0.4",
             TEXT_MUTED, False),
            (f"  • A std-dev of 20 dBm or more maxes this component.",
             TEXT_DIM, False),
            ("", TEXT_DIM, False),
            ("COMPONENT 2 — Sudden Distance Jump  (weight 60%)", DANGER, True),
            (f"  • Max single-step change across all consecutive readings is found.",
             TEXT_MUTED, False),
            (f"  • If the jump exceeds {DIST_JUMP_THRESHOLD_M} m it's treated as anomalous.",
             TEXT_MUTED, False),
            (f"  • Risk contribution = min(jump / 20, 1.0) × 0.6",
             TEXT_MUTED, False),
            (f"  • A jump of 20 m or more maxes this component.",
             TEXT_DIM, False),
            ("", TEXT_DIM, False),
            ("COMPONENT 3 — RSSI Z-Score Spike  (weight 50%)", WARNING, True),
            (f"  • Z-score of the latest reading vs rolling mean/std is computed.",
             TEXT_MUTED, False),
            (f"  • If z-score > {RSSI_ANOMALY_ZSCORE}, the reading is flagged as a spike.",
             TEXT_MUTED, False),
            (f"  • Risk contribution = min(z / 5.0, 1.0) × 0.5",
             TEXT_MUTED, False),
            ("", TEXT_DIM, False),
            ("FINAL SCORE = max of all active components (not additive).", TEXT, True),
            (f"  LOW < {RISK_MEDIUM_THRESHOLD:.2f}   "
             f"MEDIUM {RISK_MEDIUM_THRESHOLD:.2f}–{RISK_HIGH_THRESHOLD:.2f}   "
             f"HIGH ≥ {RISK_HIGH_THRESHOLD:.2f}", ACCENT, True),
            ("  Anomalies expire after 30 s of clean readings.", TEXT_DIM, False),
            ("  Minimum 5 readings required before scoring begins.", TEXT_DIM, False),
        ]
        for text, color, bold in explain_lines:
            ctk.CTkLabel(self._explain_body, text=text,
                         font=("JetBrains Mono", 10, "bold" if bold else "normal"),
                         text_color=color, anchor="w").pack(
                anchor="w", padx=18, pady=0)
        ctk.CTkFrame(self._explain_body, fg_color="transparent", height=8).pack()

        # ── Live table ────────────────────────────────────────────
        hdr = ctk.CTkFrame(tab, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(4, 4))
        ctk.CTkLabel(hdr, text="Live Beacon Risk — auto-refreshes every 2 s",
                     font=FONT_LABEL_BOLD, text_color=TEXT).pack(side="left")
        ctk.CTkButton(hdr, text="⟳ Refresh", width=90, height=30,
                      fg_color=SURFACE3, text_color=ACCENT2,
                      font=("Outfit", 11), corner_radius=8, hover_color=SURFACE2,
                      command=self._refresh_risk).pack(side="right")

        wrap = ctk.CTkFrame(tab, fg_color=SURFACE3, corner_radius=INNER_RADIUS)
        wrap.pack(fill="both", expand=False, padx=12, pady=(0, 6))
        cols   = ("UUID", "Worker", "Risk", "Score", "Dominant Factor", "Zone", "Last Seen")
        widths = (200, 120, 75, 65, 220, 110, 90)
        self._risk_tree = ttk.Treeview(wrap, style="Risk.Treeview",
                                       columns=cols, show="headings", height=8)
        for col, w in zip(cols, widths):
            self._risk_tree.heading(col, text=col)
            self._risk_tree.column(col, width=w, anchor="center")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._risk_tree.yview)
        self._risk_tree.configure(yscrollcommand=sb.set)
        self._risk_tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb.pack(side="right", fill="y", pady=4)

        # ── Anomaly feed ──────────────────────────────────────────
        ctk.CTkLabel(tab, text="ANOMALY FEED",
                     font=("Outfit", 9, "bold"), text_color=TEXT_DIM).pack(
            anchor="w", padx=14, pady=(6, 2))
        self._anomaly_log = ctk.CTkTextbox(
            tab, height=90, fg_color=SURFACE2,
            text_color=WARNING, font=("JetBrains Mono", 11),
            scrollbar_button_color=BORDER2)
        self._anomaly_log.pack(fill="x", padx=12, pady=(0, 8))
        self._anomaly_log.insert("0.0", "Anomaly detection active — monitoring beacons…\n")
        self._anomaly_log.configure(state="disabled")

    def _toggle_risk_explain(self):
        if self._risk_explain_open:
            self._explain_body.pack_forget()
            self._toggle_btn.configure(text="▼ Show")
            self._risk_explain_open = False
        else:
            self._explain_body.pack(fill="x")
            self._toggle_btn.configure(text="▲ Hide")
            self._risk_explain_open = True

    def _refresh_risk(self):
        scores  = rule_engine.get_all_risk_scores()
        beacons = db.system_stats.get("live_beacons", {})

        for item in self._risk_tree.get_children():
            self._risk_tree.delete(item)

        shown = set()
        for uuid, score in sorted(scores.items(), key=lambda x: -x[1]):
            shown.add(uuid)
            beacon  = beacons.get(uuid, {})
            anomaly = rule_engine.get_anomaly(uuid)
            link    = db.beacon_links.get(uuid, {})
            worker  = link.get("WorkerName", "—") if link else "—"
            risk_lbl = ("HIGH"   if score >= RISK_HIGH_THRESHOLD else
                        "MEDIUM" if score >= RISK_MEDIUM_THRESHOLD else "LOW")
            # Dominant factor description
            if anomaly:
                factor = {
                    "distance_jump": f"Jump {anomaly.get('detail','')[:28]}",
                    "rssi_spike":    f"RSSI spike {anomaly.get('detail','')[:20]}",
                }.get(anomaly.get("type", ""), anomaly.get("detail", "—")[:30])
            else:
                factor = "Signal instability" if score >= RISK_MEDIUM_THRESHOLD else "—"
            zone   = beacon.get("anchor", "—")
            ts     = beacon.get("timestamp")
            last_s = f"{int(time.time()-ts)}s ago" if ts else "—"
            self._risk_tree.insert("", "end", values=(
                uuid[:32], worker, risk_lbl, f"{score:.2f}", factor, zone, last_s))

        for uuid, beacon in beacons.items():
            if uuid in shown:
                continue
            link   = db.beacon_links.get(uuid, {})
            worker = link.get("WorkerName", "—") if link else "—"
            zone   = beacon.get("anchor", "—")
            ts     = beacon.get("timestamp")
            last_s = f"{int(time.time()-ts)}s ago" if ts else "—"
            self._risk_tree.insert("", "end",
                                   values=(uuid[:32], worker, "LOW", "0.00",
                                           "—", zone, last_s))

    def _on_anomaly(self, uuid, anomaly):
        msg = (f"{datetime.now().strftime('%H:%M:%S')}  "
               f"[{anomaly.get('type','').upper()}]  "
               f"UUID={uuid[:20]}…  {anomaly.get('detail','')}\n")
        self.after(0, lambda m=msg: self._append_anomaly(m))

    def _append_anomaly(self, msg):
        self._anomaly_log.configure(state="normal")
        self._anomaly_log.insert("0.0", msg)
        self._anomaly_log.configure(state="disabled")

    # ═══════════════════════════════════════════════════════════════
    # FATIGUE TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_fatigue_tab(self, tab):
        hdr = ctk.CTkFrame(tab, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(hdr, text="Worker Fatigue Monitor — live step-based detection",
                     font=FONT_LABEL_BOLD, text_color=TEXT).pack(side="left")
        ctk.CTkButton(hdr, text="⟳ Refresh", width=90, height=30,
                      fg_color=SURFACE3, text_color=ACCENT_STEPS,
                      font=("Outfit", 11), corner_radius=8, hover_color=SURFACE2,
                      command=self._refresh_fatigue).pack(side="right")

        # Active threshold display (live — updates when thresholds change)
        self._fat_thresh_lbl = ctk.CTkLabel(
            tab,
            text=self._thresh_summary(),
            font=("JetBrains Mono", 11), text_color=TEXT_DIM)
        self._fat_thresh_lbl.pack(anchor="w", padx=14, pady=(0, 6))

        wrap = ctk.CTkFrame(tab, fg_color=SURFACE3, corner_radius=INNER_RADIUS)
        wrap.pack(fill="both", expand=False, padx=12, pady=(0, 6))
        cols   = ("Worker", "Device IP", "Steps", "Level", "Progress", "Last Updated")
        widths = (160, 140, 90, 85, 220, 130)
        self._fat_tree = ttk.Treeview(wrap, style="Fat.Treeview",
                                      columns=cols, show="headings", height=9)
        for col, w in zip(cols, widths):
            self._fat_tree.heading(col, text=col)
            self._fat_tree.column(col, width=w, anchor="center")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._fat_tree.yview)
        self._fat_tree.configure(yscrollcommand=sb.set)
        self._fat_tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb.pack(side="right", fill="y", pady=4)

        ctk.CTkLabel(tab, text="FATIGUE EVENT LOG",
                     font=("Outfit", 9, "bold"), text_color=TEXT_DIM).pack(
            anchor="w", padx=14, pady=(6, 2))
        self._fatigue_log = ctk.CTkTextbox(
            tab, height=110, fg_color=SURFACE2,
            text_color=ACCENT_STEPS, font=("JetBrains Mono", 11),
            scrollbar_button_color=BORDER2)
        self._fatigue_log.pack(fill="x", padx=12, pady=(0, 8))
        self._fatigue_log.insert("0.0", "Fatigue detection active.\n")
        self._fatigue_log.configure(state="disabled")

        fatigue_tracker.register_callback(self._on_fatigue_event)

    def _thresh_summary(self) -> str:
        w = _ai_mod.FATIGUE_WARN_STEPS
        h = _ai_mod.FATIGUE_HIGH_STEPS
        c = _ai_mod.FATIGUE_CRIT_STEPS
        cd = _ai_mod.FATIGUE_ALERT_COOLDOWN
        return (f"  ⚠ WARN ≥ {w:,}   ⬦ HIGH ≥ {h:,}   "
                f"◈ CRITICAL ≥ {c:,}   cooldown {cd//60} min")

    def _refresh_fatigue(self):
        # Update threshold label with live values
        self._fat_thresh_lbl.configure(text=self._thresh_summary())

        states = fatigue_tracker.get_all_states()
        for item in self._fat_tree.get_children():
            self._fat_tree.delete(item)

        if not states:
            self._fat_tree.insert("", "end",
                                   values=("No wearables online", "", "", "", "", ""))
            return

        crit = _ai_mod.FATIGUE_CRIT_STEPS or 1
        for ip, state in sorted(states.items(),
                                 key=lambda x: -x[1].get("level", 0)):
            level  = state.get("label", "OK")
            steps  = state.get("steps", 0)
            worker = state.get("worker", ip)
            ts     = state.get("ts", 0)
            icon   = _FATIGUE_ICONS.get(level, "")
            last_s = (datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                      if ts else "—")
            pct    = min(int(steps / crit * 100), 100)
            bar    = "█" * min(int(pct / 5), 20) + "░" * max(0, 20 - min(int(pct / 5), 20))
            self._fat_tree.insert("", "end", values=(
                worker, ip, f"{steps:,}",
                f"{icon} {level}",
                f"{bar}  {pct}%",
                last_s,
            ))

    def _on_fatigue_event(self, ip, worker_name, level_str, steps):
        msg = (f"{datetime.now().strftime('%H:%M:%S')}  "
               f"[FATIGUE {level_str}]  {worker_name} ({ip})  "
               f"{steps:,} steps\n")
        self.after(0, lambda m=msg: self._append_fatigue_log(m))
        self.after(0, self._refresh_fatigue)

    def _append_fatigue_log(self, msg):
        self._fatigue_log.configure(state="normal")
        self._fatigue_log.insert("0.0", msg)
        self._fatigue_log.configure(state="disabled")

    # ═══════════════════════════════════════════════════════════════
    # BRIEFING TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_briefing_tab(self, tab):
        ctk.CTkLabel(tab,
                     text="AI generates a real-time security briefing from live system state.",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=16, pady=(12, 4))
        self._briefing_btn = ctk.CTkButton(
            tab, text="⬡ Generate Security Briefing",
            fg_color=ACCENT, text_color=BG,
            font=("Outfit", 12, "bold"), height=BTN_HEIGHT,
            corner_radius=8, hover_color=ACCENT_DIM,
            command=self._run_briefing)
        self._briefing_btn.pack(anchor="w", padx=16, pady=(0, 12))
        self._briefing_box = ctk.CTkTextbox(
            tab, fg_color=SURFACE2, text_color=TEXT,
            font=("Outfit", 12), scrollbar_button_color=BORDER2)
        self._briefing_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._briefing_box.insert("0.0",
            "Click 'Generate' to produce an AI briefing from live data.\n")
        self._briefing_box.configure(state="disabled")

    def _run_briefing(self):
        if not groq.is_configured:
            self._set_box(self._briefing_box,
                          "⚠ Groq API key not configured — go to Settings tab.")
            return
        self._briefing_btn.configure(state="disabled", text="Generating…")
        ai_engine.generate_security_briefing(
            self._build_snapshot(),
            callback=lambda t, e: self.after(0, lambda: self._done_briefing(t, e)))

    def _done_briefing(self, text, err):
        self._briefing_btn.configure(state="normal", text="⬡ Generate Security Briefing")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._set_box(self._briefing_box,
                      f"[{ts}]\n\n{text}" if not err else f"Error: {err}")

    # ═══════════════════════════════════════════════════════════════
    # HISTORY TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_history_tab(self, tab):
        ctk.CTkLabel(tab,
                     text="AI analyses location_history.csv for patterns, dwell times, and safety risks.",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=16, pady=(12, 4))
        self._hist_btn = ctk.CTkButton(
            tab, text="◌ Analyse Location History",
            fg_color=ACCENT2, text_color=BG,
            font=("Outfit", 12, "bold"), height=BTN_HEIGHT,
            corner_radius=8, hover_color=ACCENT2_DIM,
            command=self._run_history)
        self._hist_btn.pack(anchor="w", padx=16, pady=(0, 12))
        self._hist_box = ctk.CTkTextbox(
            tab, fg_color=SURFACE2, text_color=TEXT,
            font=("Outfit", 12), scrollbar_button_color=BORDER2)
        self._hist_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._hist_box.insert("0.0", "Click 'Analyse' to process location_history.csv.\n")
        self._hist_box.configure(state="disabled")

    def _run_history(self):
        if not groq.is_configured:
            self._set_box(self._hist_box,
                          "⚠ Groq API key not configured — go to Settings tab.")
            return
        self._hist_btn.configure(state="disabled", text="Analysing…")
        rows = db.load_location_history(limit=5000)
        if not rows:
            self._set_box(self._hist_box,
                          "No location history found in data/location_history.csv")
            self._hist_btn.configure(state="normal", text="◌ Analyse Location History")
            return
        ai_engine.analyze_location_history(
            rows,
            callback=lambda t, e: self.after(0, lambda: self._done_history(t, e, len(rows))))

    def _done_history(self, text, err, count):
        self._hist_btn.configure(state="normal", text="◌ Analyse Location History")
        if err:
            self._set_box(self._hist_box, f"Error: {err}")
        else:
            self._set_box(self._hist_box, f"Analysed {count:,} location records.\n\n{text}")

    # ═══════════════════════════════════════════════════════════════
    # RULE SUGGESTIONS TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_rules_tab(self, tab):
        ctk.CTkLabel(tab,
                     text="AI suggests new automation rules based on movement patterns.",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=16, pady=(12, 4))
        self._rules_btn = ctk.CTkButton(
            tab, text="⚡ Suggest Automation Rules",
            fg_color=ACCENT_GOLD, text_color=BG,
            font=("Outfit", 12, "bold"), height=BTN_HEIGHT,
            corner_radius=8, command=self._run_rules)
        self._rules_btn.pack(anchor="w", padx=16, pady=(0, 12))
        self._rules_box = ctk.CTkTextbox(
            tab, fg_color=SURFACE2, text_color=TEXT,
            font=("Outfit", 12), scrollbar_button_color=BORDER2)
        self._rules_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._rules_box.insert("0.0",
            "Click 'Suggest' to get AI-recommended automation rules.\n")
        self._rules_box.configure(state="disabled")

    def _run_rules(self):
        if not groq.is_configured:
            self._set_box(self._rules_box,
                          "⚠ Groq API key not configured — go to Settings tab.")
            return
        self._rules_btn.configure(state="disabled", text="Generating…")
        rows     = db.load_location_history(limit=3000)
        existing = list(db.auto_rules.values())
        ai_engine.suggest_rules(
            rows, existing,
            callback=lambda t, e: self.after(0, lambda: self._done_rules(t, e)))

    def _done_rules(self, text, err):
        self._rules_btn.configure(state="normal", text="⚡ Suggest Automation Rules")
        self._set_box(self._rules_box, text if not err else f"Error: {err}")

    # ═══════════════════════════════════════════════════════════════
    # EVENT SUMMARY TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_events_tab(self, tab):
        ctk.CTkLabel(tab,
                     text="AI summarizes recent system events (falls, SOS, fatigue, tasks).",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=16, pady=(12, 4))
        self._events_btn = ctk.CTkButton(
            tab, text="⬦ Summarize Events",
            fg_color=DANGER, text_color=BG,
            font=("Outfit", 12, "bold"), height=BTN_HEIGHT,
            corner_radius=8, command=self._run_events)
        self._events_btn.pack(anchor="w", padx=16, pady=(0, 12))
        self._events_box = ctk.CTkTextbox(
            tab, fg_color=SURFACE2, text_color=TEXT,
            font=("Outfit", 12), scrollbar_button_color=BORDER2)
        self._events_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._events_box.insert("0.0", "Click 'Summarize' to analyse system_events.csv.\n")
        self._events_box.configure(state="disabled")

    def _run_events(self):
        if not groq.is_configured:
            self._set_box(self._events_box,
                          "⚠ Groq API key not configured — go to Settings tab.")
            return
        self._events_btn.configure(state="disabled", text="Summarizing…")
        rows = db.load_system_events(limit=200)
        ai_engine.summarize_events(
            rows,
            callback=lambda t, e: self.after(0, lambda: self._done_events(t, e)))

    def _done_events(self, text, err):
        self._events_btn.configure(state="normal", text="⬦ Summarize Events")
        self._set_box(self._events_box, text if not err else f"Error: {err}")

    # ═══════════════════════════════════════════════════════════════
    # AI CHAT TAB
    # ═══════════════════════════════════════════════════════════════

    def _build_chat_tab(self, tab):
        ctk.CTkLabel(tab,
                     text="Ask the AI anything about your system, workers, or safety data.",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=16, pady=(12, 4))
        self._chat_box = ctk.CTkTextbox(
            tab, fg_color=SURFACE2, text_color=TEXT,
            font=("Outfit", 12), scrollbar_button_color=BORDER2)
        self._chat_box.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self._chat_box.insert("0.0",
            "AI Chat ready. Example questions:\n"
            "  • 'Which workers are at high fatigue risk right now?'\n"
            "  • 'Summarise today's fall and SOS events.'\n"
            "  • 'Are any beacons showing anomalous RSSI?'\n\n")
        self._chat_box.configure(state="disabled")

        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 16))
        self._chat_entry = ctk.CTkEntry(
            row, placeholder_text="Ask the AI…",
            font=("Outfit", 12), fg_color=SURFACE2,
            border_color=BORDER2, text_color=TEXT, height=INPUT_HEIGHT)
        self._chat_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self._chat_entry.bind("<Return>", lambda _: self._send_chat())
        self._chat_btn = ctk.CTkButton(
            row, text="Ask", width=80, height=INPUT_HEIGHT,
            fg_color=ACCENT, text_color=BG,
            font=("Outfit", 12, "bold"), corner_radius=8,
            hover_color=ACCENT_DIM, command=self._send_chat)
        self._chat_btn.pack(side="left")

    def _send_chat(self):
        question = self._chat_entry.get().strip()
        if not question:
            return
        if not groq.is_configured:
            self._append_chat("SYSTEM",
                              "⚠ Groq API key not configured — go to Settings tab.")
            return
        self._chat_entry.delete(0, tk.END)
        self._append_chat("YOU", question)
        self._chat_btn.configure(state="disabled", text="…")
        ai_engine.ask_freeform(
            question, self._build_snapshot(),
            callback=lambda t, e: self.after(0, lambda: self._done_chat(t, e)))

    def _done_chat(self, text, err):
        self._chat_btn.configure(state="normal", text="Ask")
        self._append_chat("AI" if not err else "ERROR", text or err)

    def _append_chat(self, role, text):
        self._chat_box.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self._chat_box.insert("end", f"\n[{ts}] {role}:\n{text}\n")
        self._chat_box.see("end")
        self._chat_box.configure(state="disabled")

    # ═══════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _card(self, parent, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=SURFACE2, corner_radius=INNER_RADIUS,
                            border_width=1, border_color=BORDER)
        card.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(card, text=title, font=("Outfit", 10, "bold"),
                     text_color=TEXT_DIM).pack(anchor="w", padx=16, pady=(12, 6))
        return card

    def _build_snapshot(self) -> dict:
        live_beacons   = db.system_stats.get("live_beacons", {})
        risk_scores    = rule_engine.get_all_risk_scores()
        fatigue_states = fatigue_tracker.get_all_states()
        anomalies      = {
            uuid: rule_engine.get_anomaly(uuid).get("detail", "")
            for uuid in live_beacons
            if rule_engine.get_anomaly(uuid)
        }
        return {
            "timestamp":        datetime.now().isoformat(),
            "active_wearables": len(db.wearables),
            "active_anchors":   db.system_stats.get("active_anchors", 0),
            "tracked_beacons":  len(live_beacons),
            "workers_in_db":    len(db.workers),
            "active_rules":     len([r for r in db.auto_rules.values()
                                     if r.get("Enabled") == "1"]),
            "risk_scores":      risk_scores,
            "anomalies":        anomalies,
            "fatigue_thresholds": {
                "warn": _ai_mod.FATIGUE_WARN_STEPS,
                "high": _ai_mod.FATIGUE_HIGH_STEPS,
                "crit": _ai_mod.FATIGUE_CRIT_STEPS,
            },
            "fatigue_states": {
                ip: {
                    "worker": s.get("worker", ip),
                    "level":  s.get("label", "OK"),
                    "steps":  s.get("steps", 0),
                }
                for ip, s in fatigue_states.items()
            },
            "beacon_links": {
                uuid: link.get("WorkerName", "")
                for uuid, link in db.beacon_links.items()
            },
        }

    def _start_live_refresh(self):
        def loop():
            while True:
                self.after(0, self._refresh_risk)
                self.after(0, self._refresh_fatigue)
                time.sleep(2)
        threading.Thread(target=loop, daemon=True).start()

    @staticmethod
    def _set_box(box: ctk.CTkTextbox, text: str):
        box.configure(state="normal")
        box.delete("0.0", "end")
        box.insert("0.0", text)
        box.configure(state="disabled")