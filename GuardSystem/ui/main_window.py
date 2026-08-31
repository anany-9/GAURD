# ui/main_window.py
import customtkinter as ctk
import time
from ui.styles import *
from ui.views.dashboard import DashboardView
from ui.views.rtls_nodes import NodesView
from ui.views.device_mgr import WearablesView
from ui.views.audio_cast import AudioCastView
from ui.views.ai_panel import AIPanelView


class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("GUARD // Command Center")
        self.geometry("1500x940")
        self.minsize(1280, 820)
        self.configure(fg_color=BG)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.views = {}
        self.current_view = None
        self._build_sidebar()
        self._build_main_frame()
        self._init_views()
        self.show_view("dashboard")
        self._tick_clock()

    def _build_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color=SURFACE)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(8, weight=1)

        # Top logo area
        logo_frame = ctk.CTkFrame(self.sidebar, fg_color=SURFACE2, corner_radius=0, height=72)
        logo_frame.pack(fill="x")
        logo_frame.pack_propagate(False)
        inner_logo = ctk.CTkFrame(logo_frame, fg_color="transparent")
        inner_logo.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(inner_logo, text="◈", text_color=ACCENT, font=("Outfit", 26, "bold")).pack(side="left")
        ctk.CTkLabel(inner_logo, text=" GUARD", text_color=TEXT, font=("Outfit", 22, "bold")).pack(side="left")

        # Thin accent line under logo
        ctk.CTkFrame(self.sidebar, fg_color=ACCENT, height=2, corner_radius=0).pack(fill="x")

        # Spacer
        ctk.CTkFrame(self.sidebar, fg_color="transparent", height=20).pack()

        # Nav section label
        ctk.CTkLabel(self.sidebar, text="NAVIGATION", text_color=TEXT_DIM,
                     font=("Outfit", 9, "bold"), anchor="w").pack(fill="x", padx=22, pady=(0, 6))

        # Navigation Buttons
        self.nav_buttons = {}
        nav_items = [
            ("dashboard", "dashboard", "Fleet Dashboard"),
            ("nodes",     "scanner",   "Scanner Nodes"),
            ("wearables", "wearable",  "Wearable Devices"),
            ("audio",     "audio",     "Audio Broadcast"),
            ("ai",        "ai",        "AI Intelligence"),
        ]
        nav_icons = {
            "dashboard": "⬡",
            "scanner":   "◎",
            "wearable":  "⌚",
            "audio":     "◉",
            "ai":        "⬡",
        }

        for key, icon_key, label in nav_items:
            row = ctk.CTkFrame(self.sidebar, fg_color="transparent", height=46, corner_radius=10)
            row.pack(fill="x", padx=12, pady=3)
            row.pack_propagate(False)

            # Indicator bar (left)
            indicator = ctk.CTkFrame(row, fg_color="transparent", width=3, corner_radius=2)
            indicator.pack(side="left", fill="y", padx=(0, 0))

            btn = ctk.CTkButton(
                row,
                text=f"  {nav_icons[icon_key]}   {label}",
                font=FONT_LABEL_BOLD,
                fg_color="transparent",
                text_color=TEXT_MUTED,
                hover_color=SURFACE3,
                anchor="w",
                corner_radius=8,
                height=46,
                command=lambda k=key: self.show_view(k)
            )
            btn.pack(side="left", fill="both", expand=True)
            self.nav_buttons[key] = (btn, indicator)

        # Spacer (flexible)
        ctk.CTkFrame(self.sidebar, fg_color="transparent").pack(fill="both", expand=True)

        # System Status Card at bottom
        status_card = ctk.CTkFrame(self.sidebar, fg_color=SURFACE2, corner_radius=12,
                                   border_width=1, border_color=BORDER)
        status_card.pack(fill="x", padx=14, pady=(0, 18))

        ctk.CTkLabel(status_card, text="SYSTEM STATUS", text_color=TEXT_DIM,
                     font=("Outfit", 9, "bold")).pack(pady=(12, 4))

        status_row = ctk.CTkFrame(status_card, fg_color="transparent")
        status_row.pack(pady=(0, 4))
        ctk.CTkLabel(status_row, text="●", text_color=SUCCESS, font=("Outfit", 14)).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(status_row, text="ONLINE", text_color=SUCCESS, font=("Outfit", 12, "bold")).pack(side="left")

        self.lbl_clock = ctk.CTkLabel(status_card, text="--:--:--",
                                      text_color=TEXT_MUTED, font=("JetBrains Mono", 11))
        self.lbl_clock.pack(pady=(0, 12))

    def _tick_clock(self):
        self.lbl_clock.configure(text=time.strftime("%H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _build_main_frame(self):
        self.main_container = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.main_container.grid(row=0, column=1, sticky="nsew")

    def _init_views(self):
        self.views["dashboard"] = DashboardView(self.main_container, self)
        self.views["nodes"]     = NodesView(self.main_container, self)
        self.views["wearables"] = WearablesView(self.main_container, self)
        self.views["audio"]     = AudioCastView(self.main_container, self)
        self.views["ai"]        = AIPanelView(self.main_container, self)

    def show_view(self, view_key):
        if view_key not in self.views:
            return
        if self.current_view and self.current_view in self.views:
            self.views[self.current_view].pack_forget()

        for key, (btn, indicator) in self.nav_buttons.items():
            if key == view_key:
                btn.configure(fg_color=SURFACE3, text_color=ACCENT)
                indicator.configure(fg_color=ACCENT)
            else:
                btn.configure(fg_color="transparent", text_color=TEXT_MUTED)
                indicator.configure(fg_color="transparent")

        self.views[view_key].pack(fill="both", expand=True)
        self.current_view = view_key