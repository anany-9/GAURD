# ui/views/audio_cast.py
import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import os
import requests

from ui.styles import *
from core.audio_mgr import audio_system
from core.api_client import api
from core.data_mgr import db


class AudioCastView(ctk.CTkFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.controller   = controller
        self.selected_file = None
        self.device_vars   = {}
        self._build_ui()

    def _build_ui(self):
        # ── Header ──────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="◉  AUDIO BROADCAST",
                     font=("Outfit", 17, "bold"), text_color=TEXT).pack(side="left", padx=30, pady=18)
        ctk.CTkLabel(header, text="Stream voice or audio directly to wearable devices",
                     font=("Outfit", 12), text_color=TEXT_DIM).pack(side="left", padx=(0, 0), pady=18)

        # ── Body ─────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=14)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        # ── LEFT: Target selection ────────────────────────
        target_card = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                                   border_width=1, border_color=BORDER)
        target_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        thdr = ctk.CTkFrame(target_card, fg_color=SURFACE2, corner_radius=0, height=42)
        thdr.pack(fill="x")
        thdr.pack_propagate(False)
        ctk.CTkLabel(thdr, text="Target Devices", font=FONT_LABEL_BOLD,
                     text_color=TEXT).pack(side="left", padx=16, pady=10)

        # Broadcast ALL toggle
        toggle_row = ctk.CTkFrame(target_card, fg_color=SURFACE2, corner_radius=8)
        toggle_row.pack(fill="x", padx=14, pady=(12, 6))
        self.broadcast_all_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(toggle_row,
                        text="  Broadcast to ALL Active Devices",
                        variable=self.broadcast_all_var,
                        text_color=ACCENT, fg_color=ACCENT,
                        hover_color=ACCENT_DIM,
                        font=("Outfit", 12, "bold"),
                        command=self._toggle_specific_list
                        ).pack(anchor="w", padx=14, pady=10)

        ctk.CTkLabel(target_card, text="OR SELECT SPECIFIC DEVICES",
                     font=("Outfit", 9, "bold"), text_color=TEXT_DIM).pack(anchor="w", padx=16, pady=(6, 2))

        self.device_list_frame = ctk.CTkScrollableFrame(target_card, fg_color=SURFACE2,
                                                        corner_radius=INNER_RADIUS,
                                                        scrollbar_button_color=BORDER2)
        self.device_list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        ctk.CTkButton(target_card, text="⟳  Refresh Device List",
                      fg_color=SURFACE3, text_color=TEXT_MUTED,
                      hover_color=BORDER2, font=("Outfit", 11),
                      height=BTN_HEIGHT, corner_radius=8,
                      command=self._populate_devices).pack(fill="x", padx=14, pady=(0, 14))

        # ── RIGHT: Audio source options ───────────────────
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        right.grid_rowconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        # Live voice recording card
        voice_card = ctk.CTkFrame(right, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                                  border_width=1, border_color=BORDER)
        voice_card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        vc_hdr = ctk.CTkFrame(voice_card, fg_color=SURFACE2, corner_radius=0, height=42)
        vc_hdr.pack(fill="x")
        vc_hdr.pack_propagate(False)
        ctk.CTkLabel(vc_hdr, text="◉  Live Voice Paging", font=FONT_LABEL_BOLD,
                     text_color=TEXT).pack(side="left", padx=16, pady=10)

        vc_body = ctk.CTkFrame(voice_card, fg_color="transparent")
        vc_body.pack(fill="both", expand=True, padx=20, pady=14)

        desc_text = ("Records 5 seconds of audio from your microphone, applies\n"
                     "RMS normalization + soft clipping, then encodes to MP3\n"
                     "and streams directly to all target devices.")
        ctk.CTkLabel(vc_body, text=desc_text, font=("Outfit", 11),
                     text_color=TEXT_MUTED, justify="left").pack(anchor="w", pady=(0, 14))

        # Recording indicator
        self.record_indicator = ctk.CTkFrame(vc_body, fg_color=SURFACE2,
                                             corner_radius=INNER_RADIUS, height=40)
        self.record_indicator.pack(fill="x", pady=(0, 10))
        self.record_indicator.pack_propagate(False)
        self.lbl_rec_status = ctk.CTkLabel(self.record_indicator, text="  Ready to record",
                                           font=("Outfit", 11), text_color=TEXT_DIM)
        self.lbl_rec_status.pack(side="left", padx=12, pady=8)

        self.btn_record = ctk.CTkButton(vc_body, text="  ● Record 5s & Broadcast",
                                        fg_color=DANGER, hover_color="#c0192e",
                                        text_color=TEXT, font=("Outfit", 13, "bold"),
                                        height=52, corner_radius=10,
                                        command=self._start_recording)
        self.btn_record.pack(fill="x")

        # File upload card
        file_card = ctk.CTkFrame(right, fg_color=SURFACE, corner_radius=CARD_RADIUS,
                                 border_width=1, border_color=BORDER)
        file_card.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        fc_hdr = ctk.CTkFrame(file_card, fg_color=SURFACE2, corner_radius=0, height=42)
        fc_hdr.pack(fill="x")
        fc_hdr.pack_propagate(False)
        ctk.CTkLabel(fc_hdr, text="⬆  Upload MP3 File", font=FONT_LABEL_BOLD,
                     text_color=TEXT).pack(side="left", padx=16, pady=10)

        fc_body = ctk.CTkFrame(file_card, fg_color="transparent")
        fc_body.pack(fill="both", expand=True, padx=20, pady=14)

        ctk.CTkLabel(fc_body, text="Send a pre-recorded MP3 — siren, alert tone, or announcement.",
                     font=("Outfit", 11), text_color=TEXT_MUTED).pack(anchor="w", pady=(0, 12))

        file_select_row = ctk.CTkFrame(fc_body, fg_color=SURFACE2, corner_radius=INNER_RADIUS)
        file_select_row.pack(fill="x", pady=(0, 10))
        self.lbl_filename = ctk.CTkLabel(file_select_row, text="  No file selected…",
                                         font=("JetBrains Mono", 11), text_color=TEXT_DIM,
                                         anchor="w")
        self.lbl_filename.pack(side="left", fill="x", expand=True, padx=12, pady=10)
        ctk.CTkButton(file_select_row, text="Browse…", width=90, height=30,
                      fg_color=SURFACE3, text_color=TEXT, hover_color=BORDER2,
                      font=("Outfit", 11), corner_radius=6,
                      command=self._browse_file).pack(side="right", padx=10)

        self.btn_send_file = ctk.CTkButton(fc_body, text="⬆  Send MP3 to Devices",
                                           fg_color=ACCENT2, hover_color=ACCENT2_DIM,
                                           text_color=TEXT, font=("Outfit", 13, "bold"),
                                           height=48, corner_radius=10,
                                           state="disabled",
                                           command=self._send_mp3_file)
        self.btn_send_file.pack(fill="x")

        self._populate_devices()
        self._toggle_specific_list()

    def _populate_devices(self):
        for w in self.device_list_frame.winfo_children():
            w.destroy()
        self.device_vars.clear()
        if not db.wearables:
            ctk.CTkLabel(self.device_list_frame, text="No devices registered.",
                         text_color=TEXT_DIM, font=FONT_SMALL).pack(pady=20)
            return
        for ip, dev in db.wearables.items():
            var   = ctk.BooleanVar(value=False)
            alias = dev.get("DeviceAlias", ip)
            row   = ctk.CTkFrame(self.device_list_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkCheckBox(row,
                            text=f"  {alias}",
                            variable=var,
                            font=("Outfit", 12, "bold"),
                            text_color=TEXT,
                            fg_color=ACCENT2,
                            hover_color=ACCENT2_DIM
                            ).pack(side="left")
            ctk.CTkLabel(row, text=ip, font=("JetBrains Mono", 10),
                         text_color=TEXT_DIM).pack(side="right")
            self.device_vars[ip] = var
        self._toggle_specific_list()

    def _toggle_specific_list(self):
        state = "disabled" if self.broadcast_all_var.get() else "normal"
        for w in self.device_list_frame.winfo_children():
            if isinstance(w, ctk.CTkFrame):
                for child in w.winfo_children():
                    if isinstance(child, ctk.CTkCheckBox):
                        child.configure(state=state)
            elif isinstance(w, ctk.CTkCheckBox):
                w.configure(state=state)

    def _start_recording(self):
        self.btn_record.configure(text="  ● Recording… Speak now!", state="disabled",
                                  fg_color=WARNING)
        self.lbl_rec_status.configure(text="  ⟳  Recording audio (5s)…", text_color=WARNING)

        def task():
            try:
                mp3 = audio_system.record_audio_sync(duration=5)
                self.after(0, lambda p=mp3: self._on_record_complete(p))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Recording Error", str(e)))
                self.after(0, self._reset_buttons)

        threading.Thread(target=task, daemon=True).start()

    def _on_record_complete(self, mp3_path):
        self.btn_record.configure(text="  ↑  Broadcasting…", fg_color=ACCENT)
        self.lbl_rec_status.configure(text="  ⟳  Uploading to targets…", text_color=ACCENT)
        self._dispatch_audio_to_targets(mp3_path)

    def _browse_file(self):
        fn = filedialog.askopenfilename(title="Select MP3", filetypes=[("MP3", "*.mp3")])
        if fn:
            self.selected_file = fn
            kb = os.path.getsize(fn) / 1024
            self.lbl_filename.configure(
                text=f"  {os.path.basename(fn)}  ({kb:.1f} KB)", text_color=TEXT)
            self.btn_send_file.configure(state="normal")

    def _send_mp3_file(self):
        if not self.selected_file:
            return
        self.btn_send_file.configure(text="⬆  Uploading… (may take ~30s)", state="disabled")
        self._dispatch_audio_to_targets(self.selected_file, from_file=True)

    def _dispatch_audio_to_targets(self, filepath, from_file=False):
        if self.broadcast_all_var.get():
            targets = list(db.wearables.keys())
        else:
            targets = [ip for ip, var in self.device_vars.items() if var.get()]

        if not targets:
            messagebox.showwarning("No Targets",
                                   "No devices selected. Enable 'Broadcast All' or check specific devices.")
            self._reset_buttons()
            return

        def task():
            successes = []
            failures  = []
            for ip in targets:
                try:
                    api.wearable_send_audio(ip, filepath)
                    db.log_event("AUDIO_BROADCAST", ip,
                                 f"Sent: {os.path.basename(filepath)}")
                    successes.append(ip)
                except requests.exceptions.ConnectTimeout:
                    failures.append((ip, "Connection timed out"))
                except requests.exceptions.ConnectionError:
                    failures.append((ip, "Network unreachable"))
                except requests.exceptions.ReadTimeout:
                    failures.append((ip, "Upload read timeout"))
                except Exception as exc:
                    err_str = str(exc)
                    failures.append((ip, err_str))

            self.after(0, self._reset_buttons)

            if failures:
                fail_lines = "\n".join(f"  • {ip}: {reason}" for ip, reason in failures)
                summary = (
                    f"Sent to {len(successes)} device(s).\n\n"
                    f"Failed ({len(failures)}):\n{fail_lines}"
                )
                self.after(0, lambda m=summary: messagebox.showwarning("Broadcast Partial", m))
            else:
                ok_msg = f"Audio dispatched to all {len(successes)} device(s) successfully."
                self.after(0, lambda m=ok_msg: messagebox.showinfo("Broadcast Complete", m))

        threading.Thread(target=task, daemon=True).start()

    def _reset_buttons(self):
        self.btn_record.configure(text="  ● Record 5s & Broadcast",
                                  state="normal", fg_color=DANGER)
        self.lbl_rec_status.configure(text="  Ready to record", text_color=TEXT_DIM)
        if self.selected_file:
            self.btn_send_file.configure(text="⬆  Send MP3 to Devices", state="normal")