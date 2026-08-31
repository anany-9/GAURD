# core/ai_engine.py
"""
GUARD AI Engine — Groq-powered analysis + enhanced local rule engine.

Local (always-on, no API key needed):
  - EnhancedRuleEngine: distance zones, scheduled rules, rssi_anomaly rules
  - Fatigue detection: step-count thresholds per worker with cooldown alerts
  - Fall detection integration: cross-references wearable fall_detected state
  - RSSI z-score anomaly detection + distance-jump detection
  - Per-beacon risk scoring [0.0–1.0]

Groq (requires API key):
  - Security briefings, location history analysis, rule suggestions,
    event summarisation, freeform Q&A
"""

import os
import csv
import json
import time
import logging
import threading
import statistics
from datetime import datetime
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple, Callable

import requests

logger = logging.getLogger(__name__)

# ── Groq ───────────────────────────────────────────────────────────────────────
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 30

# ── Local engine thresholds ────────────────────────────────────────────────────
RSSI_WINDOW_SIZE       = 20     # rolling window size per beacon
RSSI_ANOMALY_ZSCORE    = 2.5    # z-score to flag RSSI spike
DIST_JUMP_THRESHOLD_M  = 8.0   # metres — sudden jump triggers anomaly
MIN_READINGS_FOR_STATS = 5     # minimum readings before scoring

# Fatigue thresholds (steps)
FATIGUE_WARN_STEPS  = 8_000    # yellow — approaching fatigue
FATIGUE_HIGH_STEPS  = 12_000   # orange — fatigued
FATIGUE_CRIT_STEPS  = 16_000   # red — critically fatigued
FATIGUE_ALERT_COOLDOWN = 1800  # seconds between repeat fatigue alerts per worker

RISK_HIGH_THRESHOLD   = 0.70
RISK_MEDIUM_THRESHOLD = 0.40

_FATIGUE_COLORS = {
    "OK":       "#22d3a0",   # SUCCESS green
    "WARN":     "#f59e0b",   # WARNING amber
    "HIGH":     "#f97316",   # orange
    "CRITICAL": "#f43f5e",   # DANGER red
}
_FATIGUE_ICONS = {
    "OK":       "",
    "WARN":     "⚠",
    "HIGH":     "⬦",
    "CRITICAL": "◈",
}


# ══════════════════════════════════════════════════════════════════════════════
# Groq Client
# ══════════════════════════════════════════════════════════════════════════════

class GroqClient:
    """Thin, robust wrapper around the Groq chat-completion API."""

    def __init__(self):
        self._api_key: Optional[str] = None
        self._lock    = threading.Lock()
        self._session = requests.Session()
        # No auto-retry: Groq failures should surface immediately to the user
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=3, pool_maxsize=3, max_retries=0)
        self._session.mount("https://", adapter)

    def set_api_key(self, key: str):
        with self._lock:
            self._api_key = key.strip()

    @property
    def is_configured(self) -> bool:
        with self._lock:
            return bool(self._api_key)

    def chat(self, system_prompt: str, user_message: str,
             max_tokens: int = 800,
             temperature: float = 0.35) -> Tuple[Optional[str], Optional[str]]:
        """
        Send one chat completion request.
        Returns (text, None) on success, (None, error_str) on failure.
        """
        with self._lock:
            key = self._api_key
        if not key:
            return None, "Groq API key not configured — go to AI Settings tab."

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       GROQ_MODEL,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
        }
        try:
            r = self._session.post(
                GROQ_API_URL, headers=headers,
                json=payload, timeout=(5.0, GROQ_TIMEOUT))
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"].strip()
            return text, None
        except requests.exceptions.ConnectTimeout:
            return None, "Cannot reach Groq API — check internet connection."
        except requests.exceptions.ReadTimeout:
            return None, "Groq API timed out — try again."
        except requests.exceptions.ConnectionError:
            return None, "No connection to Groq API — check internet."
        except requests.exceptions.HTTPError as e:
            try:
                detail = e.response.json().get("error", {}).get("message", e.response.reason)
            except Exception:
                detail = getattr(e.response, "reason", str(e))
            return None, f"Groq API error {e.response.status_code}: {detail}"
        except (KeyError, IndexError):
            return None, "Unexpected Groq response structure."
        except json.JSONDecodeError:
            return None, "Groq returned non-JSON response."
        except Exception as e:
            return None, f"Groq request failed: {e}"


groq = GroqClient()


# ══════════════════════════════════════════════════════════════════════════════
# Fatigue Tracker
# ══════════════════════════════════════════════════════════════════════════════

class FatigueTracker:
    """
    Tracks step counts per wearable IP and fires notifications when
    configurable thresholds are crossed. Uses cooldown so alerts are
    not repeated every poll cycle.

    Integrates fall_detected state: a fall after high step-count is
    treated as a high-confidence fatigue-related incident.
    """

    def __init__(self):
        self._lock              = threading.Lock()
        # ip -> current step count reported by device
        self._steps: Dict[str, int]   = {}
        # ip -> last level alerted (0=none, 1=warn, 2=high, 3=crit)
        self._alerted: Dict[str, int] = {}
        # ip -> epoch of last alert sent
        self._last_alert: Dict[str, float] = {}
        # ip -> {level, steps, ts} — latest fatigue state
        self._state: Dict[str, Dict]  = {}
        # Callbacks: callable(ip, worker_name, level_str, steps)
        self._callbacks: List[Callable] = []

    def register_callback(self, cb: Callable):
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    def unregister_callback(self, cb: Callable):
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    def update(self, ip: str, worker_name: str, steps: int,
               fall_detected: bool = False):
        """
        Called by the dashboard/device-mgr poll with latest wearable data.
        Determines fatigue level and fires callbacks + wearable notifications
        when thresholds are crossed.
        """
        with self._lock:
            self._steps[ip] = steps
            prev_level = self._alerted.get(ip, 0)
            last_ts    = self._last_alert.get(ip, 0)

        level = 0
        if steps >= FATIGUE_CRIT_STEPS:
            level = 3
        elif steps >= FATIGUE_HIGH_STEPS:
            level = 2
        elif steps >= FATIGUE_WARN_STEPS:
            level = 1

        # Fall after high step count is treated as level-3 regardless
        if fall_detected and steps >= FATIGUE_WARN_STEPS:
            level = max(level, 3)

        now = time.time()
        level_labels = {0: "OK", 1: "WARN", 2: "HIGH", 3: "CRITICAL"}

        with self._lock:
            self._state[ip] = {
                "level": level,
                "label": level_labels[level],
                "steps": steps,
                "worker": worker_name,
                "ts": now,
            }

        # Fire alert only when level increases OR cooldown has expired
        cooldown_ok = (now - last_ts) > FATIGUE_ALERT_COOLDOWN
        should_alert = (level > 0) and (level > prev_level or
                                         (level == prev_level and cooldown_ok))

        if should_alert:
            with self._lock:
                self._alerted[ip]    = level
                self._last_alert[ip] = now
            self._fire(ip, worker_name, level_labels[level], steps)

        # Reset alert level when worker rests (steps go back below warn)
        elif level == 0 and prev_level > 0:
            with self._lock:
                self._alerted[ip] = 0

    def _fire(self, ip: str, worker_name: str, level_str: str, steps: int):
        """Fire callbacks and push a wearable notification in background."""
        for cb in list(self._callbacks):
            try:
                cb(ip, worker_name, level_str, steps)
            except Exception as e:
                logger.error(f"[FatigueTracker] Callback error: {e}")

        # Push notification to the wearable itself
        def _notify():
            try:
                from core.api_client import api
                from core.data_mgr import db
                titles = {
                    "WARN":     "⚠ Fatigue Warning",
                    "HIGH":     "⬦ High Fatigue Alert",
                    "CRITICAL": "◈ CRITICAL Fatigue",
                }
                bodies = {
                    "WARN":     f"{steps:,} steps — consider a short break.",
                    "HIGH":     f"{steps:,} steps — rest recommended immediately.",
                    "CRITICAL": f"{steps:,} steps — stop activity, rest now!",
                }
                is_alert = level_str in ("HIGH", "CRITICAL")
                api.wearable_send_notification(
                    ip, titles[level_str], bodies[level_str], is_alert)
                db.log_event("FATIGUE_ALERT", ip,
                             f"{worker_name} — {level_str} ({steps:,} steps)")
            except Exception as e:
                logger.warning(f"[FatigueTracker] Notification to {ip} failed: {e}")

        threading.Thread(target=_notify, daemon=True).start()

    def get_state(self, ip: str) -> Optional[Dict]:
        with self._lock:
            return dict(self._state.get(ip, {}))

    def get_all_states(self) -> Dict[str, Dict]:
        with self._lock:
            return {ip: dict(s) for ip, s in self._state.items()}

    def reset_worker(self, ip: str):
        """Call when a device goes offline to clear its state."""
        with self._lock:
            self._steps.pop(ip, None)
            self._alerted.pop(ip, None)
            self._last_alert.pop(ip, None)
            self._state.pop(ip, None)


fatigue_tracker = FatigueTracker()


# ══════════════════════════════════════════════════════════════════════════════
# Enhanced Rule Engine
# ══════════════════════════════════════════════════════════════════════════════

class EnhancedRuleEngine:
    """
    Local, always-on automation engine:
      - distance rules  (leading-edge zone entry)
      - scheduled rules (daily HH:MM)
      - rssi_anomaly rules (triggered by local anomaly detector)
    Plus real-time risk scoring and anomaly detection per beacon.

    This replaces the old RuleEngine in rtls_nodes.py completely.
    _in_zone is kept public so the UI can clear it on rule deletion.
    """

    def __init__(self):
        self._lock              = threading.Lock()
        # uuid -> deque of (dist, ts, rssi)
        self._readings: Dict[str, deque]  = defaultdict(
            lambda: deque(maxlen=RSSI_WINDOW_SIZE))
        self._in_zone: Dict[str, bool]    = {}   # rule_id -> bool
        self._fired_sched: Dict[str, str] = {}   # rule_id -> date_str
        self._risk_scores: Dict[str, float] = {}
        self._anomalies: Dict[str, Dict]  = {}
        self._anomaly_callbacks: List[Callable] = []
        self._running = True
        threading.Thread(target=self._loop, daemon=True,
                         name="EnhancedRuleEngine").start()

    # ── Public API ────────────────────────────────────────────────────────────

    def register_anomaly_callback(self, cb: Callable):
        if cb not in self._anomaly_callbacks:
            self._anomaly_callbacks.append(cb)

    def unregister_anomaly_callback(self, cb: Callable):
        if cb in self._anomaly_callbacks:
            self._anomaly_callbacks.remove(cb)

    def update_distance(self, uuid: str, dist: float, rssi: float = -999):
        """Called by AnchorNode.poll — thread-safe, very frequent."""
        with self._lock:
            self._readings[uuid].append((dist, time.time(), rssi))

    def clear_beacon(self, uuid: str):
        """Call when a beacon goes stale so zone states reset cleanly."""
        with self._lock:
            self._readings.pop(uuid, None)
            self._risk_scores.pop(uuid, None)
            self._anomalies.pop(uuid, None)
        # Clear in_zone for any rules watching this beacon
        try:
            from core.data_mgr import db
            for rule_id, rule in list(db.auto_rules.items()):
                if rule.get("BeaconUUID", "").strip() == uuid:
                    self._in_zone[rule_id] = False
        except Exception:
            pass

    def get_risk_score(self, uuid: str) -> float:
        return self._risk_scores.get(uuid, 0.0)

    def get_anomaly(self, uuid: str) -> Optional[Dict]:
        return self._anomalies.get(uuid)

    def get_all_risk_scores(self) -> Dict[str, float]:
        return dict(self._risk_scores)

    def stop(self):
        self._running = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        from core.data_mgr import db
        from core.api_client import api

        while self._running:
            try:
                now      = datetime.now()
                hhmm     = now.strftime("%H:%M")
                date_str = now.strftime("%Y-%m-%d")

                for rule_id, rule in list(db.auto_rules.items()):
                    if rule.get("Enabled", "0") != "1":
                        self._in_zone.pop(rule_id, None)
                        continue
                    try:
                        rtype    = rule.get("RuleType", "")
                        target   = rule.get("TargetWearableIP", "").strip()
                        title    = rule.get("NotifTitle", "Alert")
                        body     = rule.get("NotifBody", "")
                        is_alert = rule.get("IsAlert", "0") == "1"

                        if rtype == "distance":
                            self._eval_distance(
                                rule_id, rule, target, title,
                                body, is_alert, api, db)

                        elif rtype == "scheduled":
                            cond = rule.get("ConditionValue", "").strip()
                            if (cond == hhmm and
                                    self._fired_sched.get(rule_id) != date_str):
                                self._fired_sched[rule_id] = date_str
                                self._send(target, title, body, is_alert,
                                           rule_id, f"scheduled={cond}", api, db)

                        elif rtype == "rssi_anomaly":
                            watch_uuid = self._resolve_uuid(rule, db)
                            if watch_uuid:
                                anomaly = self._anomalies.get(watch_uuid)
                                if anomaly and not self._in_zone.get(rule_id, False):
                                    self._in_zone[rule_id] = True
                                    self._send(target, title, body, is_alert,
                                               rule_id,
                                               f"anomaly={anomaly.get('type')} uuid={watch_uuid}",
                                               api, db)
                                elif not anomaly:
                                    self._in_zone[rule_id] = False

                    except Exception as exc:
                        db.log_event("RULE_ERROR", rule_id, str(exc))

                self._update_risk_scores()

            except Exception as e:
                logger.error(f"[RuleEngine] Loop error: {e}")

            time.sleep(1)

    def _eval_distance(self, rule_id, rule, target, title,
                        body, is_alert, api, db):
        threshold  = float(rule.get("ConditionValue", "5.0") or "5.0")
        watch_uuid = self._resolve_uuid(rule, db)
        if not watch_uuid:
            return

        entry = self._latest_reading(watch_uuid)
        if entry is None:
            self._in_zone[rule_id] = False
            return

        dist, ts, _ = entry
        if time.time() - ts > 10.0:          # stale reading
            self._in_zone[rule_id] = False
            return

        currently_in = dist <= threshold
        was_in       = self._in_zone.get(rule_id, False)

        if currently_in and not was_in:
            self._in_zone[rule_id] = True
            self._send(target, title, body, is_alert, rule_id,
                       f"dist={dist:.2f}m threshold={threshold}m uuid={watch_uuid}",
                       api, db)
        elif not currently_in:
            self._in_zone[rule_id] = False

    def _update_risk_scores(self):
        with self._lock:
            snapshot = {uuid: list(dq) for uuid, dq in self._readings.items()}

        for uuid, readings in snapshot.items():
            if len(readings) < MIN_READINGS_FOR_STATS:
                self._risk_scores[uuid] = 0.0
                continue

            risk      = 0.0
            distances = [r[0] for r in readings]
            rssies    = [r[2] for r in readings if r[2] != -999]

            # 1. RSSI instability (std-dev > ~15 dBm = very unstable)
            if len(rssies) >= MIN_READINGS_FOR_STATS:
                try:
                    rssi_std = statistics.stdev(rssies)
                    risk = max(risk, min(rssi_std / 20.0, 1.0) * 0.4)
                except statistics.StatisticsError:
                    pass

            # 2. Sudden distance jump
            if len(distances) >= 2:
                max_jump = max(
                    abs(distances[i] - distances[i - 1])
                    for i in range(1, len(distances)))
                if max_jump > DIST_JUMP_THRESHOLD_M:
                    risk = max(risk, min(max_jump / 20.0, 1.0) * 0.6)
                    anom = {
                        "type":   "distance_jump",
                        "detail": f"Sudden position jump of {max_jump:.1f} m",
                        "ts":     time.time(),
                    }
                    self._anomalies[uuid] = anom
                    self._fire_anomaly_cb(uuid, anom)

            # 3. RSSI z-score spike
            if len(rssies) >= MIN_READINGS_FOR_STATS:
                try:
                    mu = statistics.mean(rssies)
                    sd = statistics.stdev(rssies)
                    if sd > 0:
                        z = abs((rssies[-1] - mu) / sd)
                        if z > RSSI_ANOMALY_ZSCORE:
                            risk = max(risk, min(z / 5.0, 1.0) * 0.5)
                            anom = {
                                "type":   "rssi_spike",
                                "detail": f"RSSI z={z:.1f} (latest={rssies[-1]} dBm)",
                                "ts":     time.time(),
                            }
                            self._anomalies[uuid] = anom
                            self._fire_anomaly_cb(uuid, anom)
                except statistics.StatisticsError:
                    pass

            # Expire stale anomalies (> 30 s)
            if uuid in self._anomalies:
                if time.time() - self._anomalies[uuid]["ts"] > 30:
                    del self._anomalies[uuid]

            self._risk_scores[uuid] = round(min(risk, 1.0), 3)

    def _latest_reading(self, uuid: str) -> Optional[Tuple]:
        with self._lock:
            dq = self._readings.get(uuid)
            return dq[-1] if dq else None

    def _resolve_uuid(self, rule, db) -> Optional[str]:
        buuid = rule.get("BeaconUUID", "").strip()
        if buuid:
            return buuid
        target = rule.get("TargetWearableIP", "").strip()
        for uid, link in db.beacon_links.items():
            if link.get("WearableIP", "").strip() == target:
                return uid
        return None

    def _send(self, target_ip, title, body, is_alert,
              rule_id, context, api, db):
        def _do():
            try:
                api.wearable_send_notification(target_ip, title, body, is_alert)
                db.log_event("AUTO_RULE_FIRED", rule_id,
                             f"Sent '{title}' to {target_ip} [{context}]")
            except Exception as exc:
                db.log_event("AUTO_RULE_FAIL", rule_id, str(exc))
        threading.Thread(target=_do, daemon=True).start()

    def _fire_anomaly_cb(self, uuid, anomaly):
        for cb in list(self._anomaly_callbacks):
            try:
                cb(uuid, anomaly)
            except Exception as e:
                logger.error(f"[RuleEngine] Anomaly callback error: {e}")


rule_engine = EnhancedRuleEngine()


# ══════════════════════════════════════════════════════════════════════════════
# AI Analysis Engine (Groq)
# ══════════════════════════════════════════════════════════════════════════════

class AIAnalysisEngine:
    """
    High-level AI features backed by Groq.
    All public methods are async — they accept a callback(text, error).
    """

    SYSTEM_PROMPT = (
        "You are a security operations AI assistant for GUARD — "
        "an enterprise worker-safety and real-time-location platform. "
        "Analyse telemetry, RSSI, location history, and system events to give "
        "concise, actionable safety insights. "
        "Be professional and direct. Only flag genuine risks. "
        "If data is insufficient, say so briefly. "
        "Keep responses under 300 words unless asked for more."
    )

    def analyze_location_history(self, rows: List[Dict], callback: Callable):
        threading.Thread(target=self._loc_analysis, args=(rows, callback),
                         daemon=True).start()

    def generate_security_briefing(self, snapshot: Dict, callback: Callable):
        threading.Thread(target=self._briefing, args=(snapshot, callback),
                         daemon=True).start()

    def suggest_rules(self, rows: List[Dict], existing: List[Dict],
                       callback: Callable):
        threading.Thread(target=self._rule_suggest, args=(rows, existing, callback),
                         daemon=True).start()

    def summarize_events(self, rows: List[Dict], callback: Callable):
        threading.Thread(target=self._event_summary, args=(rows, callback),
                         daemon=True).start()

    def ask_freeform(self, question: str, context: Dict, callback: Callable):
        threading.Thread(target=self._freeform, args=(question, context, callback),
                         daemon=True).start()

    # ── Runners ───────────────────────────────────────────────────────────────

    def _loc_analysis(self, rows, callback):
        if not rows:
            callback(None, "No location history data available.")
            return
        summary = self._summarize_history(rows)
        prompt  = (
            "Analyse this RTLS location history summary. Identify movement patterns, "
            "unusual dwell times, potential safety concerns, and any worker who may "
            "be inactive or isolated:\n\n" + summary
        )
        callback(*groq.chat(self.SYSTEM_PROMPT, prompt, max_tokens=600))

    def _briefing(self, snapshot, callback):
        prompt = (
            "Generate a concise security briefing for the current shift.\n\n"
            f"System snapshot:\n{json.dumps(snapshot, indent=2, default=str)}\n\n"
            "Include: active worker count, fatigue alerts, anomalies detected, "
            "fall/SOS events, and top 2–3 recommendations."
        )
        callback(*groq.chat(self.SYSTEM_PROMPT, prompt, max_tokens=500))

    def _rule_suggest(self, rows, existing, callback):
        summary  = self._summarize_history(rows)
        rules_js = json.dumps(existing[:20], default=str)
        prompt   = (
            "Based on this location history and existing automation rules, "
            "suggest 3 new rules to improve worker safety. "
            "Format: Rule Name | Type (distance/scheduled/rssi_anomaly) | Condition | Rationale\n\n"
            f"Location summary:\n{summary}\n\n"
            f"Existing rules:\n{rules_js}"
        )
        callback(*groq.chat(self.SYSTEM_PROMPT, prompt, max_tokens=600))

    def _event_summary(self, rows, callback):
        if not rows:
            callback(None, "No system events to summarize.")
            return
        recent = rows[-50:]
        lines  = [
            f"{r.get('Timestamp','')} [{r.get('EventType','')}] "
            f"{r.get('Source','')} — {r.get('Description','')}"
            for r in recent
        ]
        prompt = (
            "Summarize these GUARD system events in 3–5 bullet points. "
            "Highlight safety concerns, fatigue alerts, fall/SOS patterns:\n\n"
            + "\n".join(lines)
        )
        callback(*groq.chat(self.SYSTEM_PROMPT, prompt, max_tokens=400))

    def _freeform(self, question, context, callback):
        ctx_str = json.dumps(context, indent=2, default=str)
        prompt  = f"System context:\n{ctx_str}\n\nQuestion: {question}"
        callback(*groq.chat(self.SYSTEM_PROMPT, prompt, max_tokens=700))

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _summarize_history(rows: List[Dict]) -> str:
        if not rows:
            return "No data."
        worker_locs: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        timestamps  = []
        for row in rows:
            worker = row.get("WorkerName") or row.get("UUID", "Unknown")
            loc    = row.get("Location", "Unknown")
            ts_str = row.get("Timestamp", "")
            worker_locs[worker][loc] += 1
            if ts_str:
                try:
                    timestamps.append(datetime.fromisoformat(ts_str))
                except ValueError:
                    pass
        lines = [f"Total readings: {len(rows)}"]
        if timestamps:
            lines.append(
                f"Time range: {min(timestamps).strftime('%Y-%m-%d %H:%M')} "
                f"to {max(timestamps).strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"Unique workers: {len(worker_locs)}")
        for worker, locs in list(worker_locs.items())[:10]:
            top = max(locs, key=locs.get)
            lines.append(
                f"  {worker}: {sum(locs.values())} readings, "
                f"most common zone='{top}' ({locs[top]}x)")
        return "\n".join(lines)


ai_engine = AIAnalysisEngine()


# ══════════════════════════════════════════════════════════════════════════════
# Robust CSV Loader
# ══════════════════════════════════════════════════════════════════════════════

def load_csv_safe(filepath: str) -> List[Dict]:
    """
    Robustly load a CSV:
      - Tries utf-8-sig → utf-8 → latin-1
      - Strips whitespace from keys and values
      - Skips entirely blank rows
      - Returns [] on any error
    """
    if not filepath or not os.path.exists(filepath):
        return []
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            rows = []
            with open(filepath, "r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return []
                for row in reader:
                    cleaned = {
                        (k.strip() if k else k): (v.strip() if v else "")
                        for k, v in row.items()
                    }
                    if any(cleaned.values()):
                        rows.append(cleaned)
            return rows
        except UnicodeDecodeError:
            continue
        except Exception as e:
            logger.error(f"[CSVLoader] Error reading {filepath}: {e}")
            return []
    return []