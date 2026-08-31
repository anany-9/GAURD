# core/data_mgr.py
import os
import csv
import logging
from datetime import datetime
from core.config import DATA_DIR, CSV_WORKERS, CSV_NODES, CSV_WEARABLES, CSV_HISTORY
from typing import Dict, Optional, Callable, List

logger = logging.getLogger(__name__)

CSV_EVENTS       = os.path.join(DATA_DIR, "system_events.csv")
CSV_BEACON_LINKS = os.path.join(DATA_DIR, "beacon_links.csv")
CSV_AUTO_RULES   = os.path.join(DATA_DIR, "automation_rules.csv")


class DataManager:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.workers       = {}
        self.nodes         = {}
        self.wearables     = {}
        self.beacon_links  = {}
        self.auto_rules    = {}
        self.system_stats  = {
            "active_anchors": 0,
            "unique_workers": 0,
            "live_beacons":   {}
        }
        self._update_callbacks: List[Callable] = []
        self._init_files()
        self.load_all()

    def register_update_callback(self, callback: Callable):
        if callback not in self._update_callbacks:
            self._update_callbacks.append(callback)

    def unregister_update_callback(self, callback: Callable):
        if callback in self._update_callbacks:
            self._update_callbacks.remove(callback)

    def _notify_updates(self, update_type: str, data: Dict):
        for callback in list(self._update_callbacks):
            try:
                callback(update_type, data)
            except Exception as e:
                logger.warning(f"[DataManager] Callback error: {e}")

    def _init_files(self):
        files = {
            CSV_WORKERS:      ["UUID", "Name", "WorkerID", "Role", "Department"],
            CSV_NODES:        ["IP", "Name", "Location", "Status"],
            CSV_WEARABLES:    ["IP", "AssignedWorkerUUID", "DeviceAlias"],
            CSV_HISTORY:      ["Timestamp", "UUID", "WorkerName", "Location", "RSSI"],
            CSV_EVENTS:       ["Timestamp", "EventType", "Source", "Description"],
            CSV_BEACON_LINKS: ["BeaconUUID", "WearableIP", "WorkerName", "Notes"],
            CSV_AUTO_RULES:   ["RuleID","Enabled","RuleName","RuleType",
                               "BeaconUUID","TargetWearableIP",
                               "ConditionValue","NotifTitle","NotifBody","IsAlert"],
        }
        for filepath, headers in files.items():
            if not os.path.exists(filepath):
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
                    with open(filepath, 'w', newline='', encoding='utf-8') as f:
                        csv.writer(f).writerow(headers)
                except Exception as e:
                    logger.error(f"[DataManager] Could not create {filepath}: {e}")

    def _safe_load(self, filepath: str, key_field: str, target_dict: Dict):
        """Robustly load CSV: handles BOM, encodings, blank lines, whitespace."""
        if not os.path.exists(filepath):
            return
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                with open(filepath, 'r', encoding=encoding, newline='') as f:
                    reader = csv.DictReader(f)
                    if reader.fieldnames is None:
                        return
                    loaded = 0
                    for row in reader:
                        cleaned = {(k.strip() if k else k): (v.strip() if v else "")
                                   for k, v in row.items()}
                        k_val = cleaned.get(key_field, "").strip()
                        if k_val:
                            target_dict[k_val] = cleaned
                            loaded += 1
                    logger.debug(f"[DataManager] {loaded} rows from {filepath}")
                return
            except UnicodeDecodeError:
                continue
            except Exception as exc:
                logger.error(f"[DataManager] Failed to load {filepath}: {exc}")
                return

    def load_all(self):
        self.workers.clear(); self.nodes.clear()
        self.wearables.clear(); self.beacon_links.clear(); self.auto_rules.clear()
        self._safe_load(CSV_WORKERS,      'UUID',       self.workers)
        self._safe_load(CSV_NODES,        'IP',         self.nodes)
        self._safe_load(CSV_WEARABLES,    'IP',         self.wearables)
        self._safe_load(CSV_BEACON_LINKS, 'BeaconUUID', self.beacon_links)
        self._safe_load(CSV_AUTO_RULES,   'RuleID',     self.auto_rules)

    def reload(self):
        self.load_all()
        self._notify_updates("reload", {})

    def load_location_history(self, limit: int = 5000) -> list:
        from core.ai_engine import load_csv_safe
        rows = load_csv_safe(CSV_HISTORY)
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return list(reversed(rows))

    def load_system_events(self, limit: int = 500) -> list:
        from core.ai_engine import load_csv_safe
        rows = load_csv_safe(CSV_EVENTS)
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return list(reversed(rows))

    def save_worker(self, uuid, data):
        self.workers[uuid] = data
        self._write_dict_to_csv(CSV_WORKERS,
            ["UUID","Name","WorkerID","Role","Department"], self.workers)
        self._notify_updates("workers", {"uuid": uuid, "data": data})

    def save_node(self, ip, data):
        self.nodes[ip] = data
        self._write_dict_to_csv(CSV_NODES,
            ["IP","Name","Location","Status"], self.nodes)
        self._notify_updates("nodes", {"ip": ip, "data": data})

    def remove_node(self, ip):
        if ip in self.nodes:
            del self.nodes[ip]
            self._write_dict_to_csv(CSV_NODES,
                ["IP","Name","Location","Status"], self.nodes)
            self._notify_updates("nodes_removed", {"ip": ip})

    def save_wearable(self, ip, data):
        self.wearables[ip] = data
        self._write_dict_to_csv(CSV_WEARABLES,
            ["IP","AssignedWorkerUUID","DeviceAlias"], self.wearables)
        self._notify_updates("wearables", {"ip": ip, "data": data})

    def remove_wearable(self, ip):
        if ip in self.wearables:
            del self.wearables[ip]
            self._write_dict_to_csv(CSV_WEARABLES,
                ["IP","AssignedWorkerUUID","DeviceAlias"], self.wearables)
            self._notify_updates("wearables_removed", {"ip": ip})

    def log_location(self, uuid, location, rssi):
        worker_name = self.workers.get(uuid, {}).get("Name", "Unknown")
        entry = {"Timestamp": datetime.now().isoformat(), "UUID": uuid,
                 "WorkerName": worker_name, "Location": location, "RSSI": rssi}
        try:
            with open(CSV_HISTORY, 'a', newline='', encoding='utf-8') as f:
                csv.DictWriter(f, fieldnames=list(entry.keys())).writerow(entry)
        except Exception as e:
            logger.error(f"[DataManager] log_location failed: {e}")

    def log_event(self, event_type: str, source: str, description: str):
        entry = {"Timestamp": datetime.now().isoformat(), "EventType": event_type,
                 "Source": source, "Description": description}
        try:
            with open(CSV_EVENTS, 'a', newline='', encoding='utf-8') as f:
                csv.DictWriter(f, fieldnames=list(entry.keys())).writerow(entry)
        except Exception as e:
            logger.error(f"[DataManager] log_event failed: {e}")
        self._notify_updates("events", entry)

    def _write_dict_to_csv(self, filepath: str, headers: list, data_dict: Dict):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
            tmp = filepath + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                for row in data_dict.values():
                    writer.writerow(row)
            os.replace(tmp, filepath)
        except Exception as exc:
            logger.error(f"[DataManager] ERROR writing {filepath}: {exc}")
            raise

    def save_beacon_link(self, uuid, wearable_ip, worker_name, notes=''):
        self.beacon_links[uuid] = {'BeaconUUID': uuid, 'WearableIP': wearable_ip,
                                   'WorkerName': worker_name, 'Notes': notes}
        self._write_dict_to_csv(CSV_BEACON_LINKS,
            ['BeaconUUID','WearableIP','WorkerName','Notes'], self.beacon_links)
        self._notify_updates("beacon_links", {"uuid": uuid})

    def remove_beacon_link(self, uuid):
        if uuid in self.beacon_links:
            del self.beacon_links[uuid]
            self._write_dict_to_csv(CSV_BEACON_LINKS,
                ['BeaconUUID','WearableIP','WorkerName','Notes'], self.beacon_links)
            self._notify_updates("beacon_links_removed", {"uuid": uuid})

    def get_wearable_ip_for_beacon(self, uuid):
        link = self.beacon_links.get(uuid)
        return link['WearableIP'] if link else None

    def save_auto_rule(self, rule):
        self.auto_rules[rule['RuleID']] = rule
        self._write_dict_to_csv(CSV_AUTO_RULES, [
            'RuleID','Enabled','RuleName','RuleType','BeaconUUID',
            'TargetWearableIP','ConditionValue','NotifTitle','NotifBody','IsAlert'
        ], self.auto_rules)
        self._notify_updates("auto_rules", {"rule_id": rule['RuleID']})

    def remove_auto_rule(self, rule_id):
        if rule_id in self.auto_rules:
            del self.auto_rules[rule_id]
            self._write_dict_to_csv(CSV_AUTO_RULES, [
                'RuleID','Enabled','RuleName','RuleType','BeaconUUID',
                'TargetWearableIP','ConditionValue','NotifTitle','NotifBody','IsAlert'
            ], self.auto_rules)
            self._notify_updates("auto_rules_removed", {"rule_id": rule_id})

    def set_rule_enabled(self, rule_id, enabled):
        if rule_id in self.auto_rules:
            self.auto_rules[rule_id]['Enabled'] = '1' if enabled else '0'
            self.save_auto_rule(self.auto_rules[rule_id])

    def update_beacon_state(self, uuid, anchor_name, rssi, distance, timestamp):
        self.system_stats["live_beacons"][uuid] = {
            "anchor": anchor_name, "rssi": rssi,
            "distance": distance, "timestamp": timestamp
        }
        self._notify_updates("beacon_state", {
            "uuid": uuid, "anchor": anchor_name,
            "rssi": rssi, "distance": distance
        })

    def update_anchor_stats(self, active_count, unique_workers):
        self.system_stats["active_anchors"] = active_count
        self.system_stats["unique_workers"] = unique_workers
        self._notify_updates("anchor_stats", {
            "active_anchors": active_count,
            "unique_workers": unique_workers
        })


db = DataManager()
