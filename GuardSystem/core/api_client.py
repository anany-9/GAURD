# core/api_client.py
"""
GUARD API Client — robust HTTP client for wearables and scanner nodes.

Per-IP circuit breaker: after N consecutive failures an IP is marked
"open" for a cooldown period — polling threads skip it instantly instead
of blocking on repeated timeouts.

No HTTPAdapter auto-retries (max_retries=0). Retries are handled at the
polling-loop level so a single bad device can't stall the whole thread.

Separate (connect, read) timeout tuple so TCP-refusals fail fast.
"""

import time
import json
import logging
import threading
from typing import Tuple, Optional, Dict

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

_CB_FAIL_THRESHOLD = 3      # consecutive failures before opening circuit
_CB_COOLDOWN_SEC   = 15.0   # seconds before retrying an open circuit


class _CircuitBreaker:
    """Thread-safe per-IP fail-fast circuit breaker."""

    def __init__(self):
        self._lock             = threading.Lock()
        self._failures: Dict[str, int]   = {}
        self._open_until: Dict[str, float] = {}

    def is_open(self, ip: str) -> bool:
        with self._lock:
            until = self._open_until.get(ip, 0)
            if until and time.time() < until:
                return True
            return False

    def record_success(self, ip: str):
        with self._lock:
            self._failures.pop(ip, None)
            self._open_until.pop(ip, None)

    def record_failure(self, ip: str):
        with self._lock:
            count = self._failures.get(ip, 0) + 1
            self._failures[ip] = count
            if count >= _CB_FAIL_THRESHOLD:
                self._open_until[ip] = time.time() + _CB_COOLDOWN_SEC

    def reset(self, ip: str):
        with self._lock:
            self._failures.pop(ip, None)
            self._open_until.pop(ip, None)

    def get_failures(self, ip: str) -> int:
        with self._lock:
            return self._failures.get(ip, 0)


_cb = _CircuitBreaker()


class APIClient:
    def __init__(self):
        self.session = requests.Session()
        # max_retries=0: blind retries on timeout just multiply the wait time
        adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=0)
        self.session.mount("http://",  adapter)
        self.session.mount("https://", adapter)
        # (connect_timeout, read_timeout) — connect can fail much faster
        self.default_timeout = (3.0, 4.0)

    def _safe_request(self, method: str, ip: str, endpoint: str,
                      data=None, files=None,
                      timeout=None) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Unified error-handling dispatcher.
        Returns (result_dict, None) on success, (None, error_str) on failure.
        Never raises.
        """
        if _cb.is_open(ip):
            return None, "Device temporarily unavailable (circuit open)"

        url = f"http://{ip}{endpoint}"
        t   = timeout if timeout is not None else self.default_timeout

        try:
            if method == "GET":
                r = self.session.get(url, timeout=t)
            elif method == "POST":
                if files:
                    r = self.session.post(url, files=files, timeout=t)
                else:
                    r = self.session.post(url, json=data, timeout=t)
            else:
                return None, f"Unsupported HTTP method: {method}"

            r.raise_for_status()
            _cb.record_success(ip)

            if not r.content:
                return {}, None
            return r.json(), None

        except requests.exceptions.ConnectTimeout:
            _cb.record_failure(ip)
            return None, "Connect timeout — device unreachable"
        except requests.exceptions.ReadTimeout:
            _cb.record_failure(ip)
            return None, "Read timeout — device too slow"
        except requests.exceptions.ConnectionError:
            _cb.record_failure(ip)
            return None, "Network error — check device IP / Wi-Fi"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            reason = e.response.reason     if e.response is not None else ""
            return None, f"HTTP {status}: {reason}"
        except requests.exceptions.RequestException as e:
            _cb.record_failure(ip)
            return None, f"Request failed: {e}"
        except json.JSONDecodeError:
            _cb.record_success(ip)   # device responded — just bad JSON
            return None, "Invalid JSON response from device"
        except Exception as e:
            logger.error(f"[APIClient] Unexpected error calling {url}: {e}")
            return None, f"Unexpected error: {e}"

    def _get(self, ip, endpoint, timeout=None):
        result, error = self._safe_request("GET", ip, endpoint, timeout=timeout)
        if error:
            raise Exception(error)
        return result

    def _post(self, ip, endpoint, data, timeout=None):
        result, error = self._safe_request("POST", ip, endpoint,
                                           data=data, timeout=timeout)
        if error:
            raise Exception(error)
        return result

    def _post_file(self, ip, endpoint, filepath,
                   mime_type="audio/mpeg", timeout=(5.0, 60.0)):
        try:
            with open(filepath, "rb") as f:
                files = {"file": (filepath, f, mime_type)}
                result, error = self._safe_request(
                    "POST", ip, endpoint, files=files, timeout=timeout)
            if error:
                raise Exception(error)
            return result
        except FileNotFoundError:
            raise Exception(f"File not found: {filepath}")
        except Exception as e:
            logger.error(f"[APIClient] File upload failed to {ip}{endpoint}: {e}")
            raise

    # ── Circuit breaker helpers ───────────────────────────────────────────────
    @staticmethod
    def reset_circuit(ip: str):
        _cb.reset(ip)

    @staticmethod
    def is_circuit_open(ip: str) -> bool:
        return _cb.is_open(ip)

    @staticmethod
    def get_failure_count(ip: str) -> int:
        return _cb.get_failures(ip)

    # ── Safe API methods (return tuple, never raise) ───────────────────────────
    def wearable_get_status_safe(self, ip) -> Tuple[Optional[Dict], Optional[str]]:
        return self._safe_request("GET", ip, "/status")

    def wearable_get_tasks_safe(self, ip) -> Tuple[Optional[Dict], Optional[str]]:
        return self._safe_request("GET", ip, "/tasks")

    def scanner_get_identity_safe(self, ip) -> Tuple[Optional[Dict], Optional[str]]:
        return self._safe_request("GET", ip, "/config", timeout=(2.5, 3.0))

    def scanner_get_devices_safe(self, ip) -> Tuple[Optional[Dict], Optional[str]]:
        return self._safe_request("GET", ip, "/devices", timeout=(1.5, 1.5))

    # ── Wearable telemetry & alerts ───────────────────────────────────────────
    def wearable_get_status(self, ip):
        return self._get(ip, "/status")

    def wearable_acknowledge_fall(self, ip):
        return self._post(ip, "/fall/reset", {})

    def wearable_acknowledge_help(self, ip):
        return self._post(ip, "/help/reset", {})

    # ── Wearable task management ──────────────────────────────────────────────
    def wearable_get_tasks(self, ip):
        return self._get(ip, "/tasks")

    def wearable_add_task(self, ip, title, desc, priority):
        return self._post(ip, "/task/add",
                          {"title": title, "desc": desc, "priority": priority})

    def wearable_complete_task(self, ip, index):
        return self._post(ip, "/task/complete", {"index": index})

    def wearable_approve_task(self, ip, index):
        return self._post(ip, "/task/approve", {"index": index})

    def wearable_approve_skip(self, ip, index):
        return self._post(ip, "/task/skip/approve", {"index": index})

    def wearable_deny_skip(self, ip, index):
        return self._post(ip, "/task/skip/deny", {"index": index})

    # ── Wearable settings & notifications ─────────────────────────────────────
    def wearable_send_notification(self, ip, title, body, is_alert):
        return self._post(ip, "/notify",
                          {"title": title, "body": body, "alert": is_alert})

    def wearable_set_worker(self, ip, name):
        return self._post(ip, "/worker", {"name": name})

    def wearable_set_rtc(self, ip, payload):
        return self._post(ip, "/rtc/set", payload)

    def wearable_push_setting(self, ip, key, value):
        return self._post(ip, "/settings", {key: value})

    def wearable_send_audio(self, ip, filepath):
        return self._post_file(ip, "/audio/message", filepath)

    # ── RTLS scanner node endpoints ───────────────────────────────────────────
    def scanner_get_identity(self, ip):
        return self._get(ip, "/config", timeout=(2.5, 3.0))

    def scanner_get_devices(self, ip):
        return self._get(ip, "/devices", timeout=(1.5, 1.5))


api = APIClient()
