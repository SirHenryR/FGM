#!/usr/bin/python3
"""
FEMS GridMode Monitoring mit Pushover-Alarmierung (Emergency Priority) und forensischem Audit-Log.
Konfiguration wird aus .fems_config.ini geladen.
"""

import requests
import json
import argparse
import os
import sys
import configparser
from datetime import datetime, timedelta

# --- KONFIGURATION LADEN ---

def load_config(config_path):
    """Lädt die Konfiguration aus einer INI-Datei."""
    if not os.path.exists(config_path):
        print(f"❌ Fehler: Konfigurationsdatei nicht gefunden: {config_path}", file=sys.stderr)
        print("   Erstelle eine Datei mit dem Namen '.fems_config.ini' im selben Verzeichnis.", file=sys.stderr)
        sys.exit(1)

    config = configparser.ConfigParser()
    try:
        config.read(config_path)
    except configparser.Error as e:
        print(f"❌ Fehler beim Lesen der Konfiguration: {e}", file=sys.stderr)
        sys.exit(1)

    # Validierung: Sind alle nötigen Keys da?
    required_keys = {
        'pushover': ['app_token', 'user_key'],
        'fems': ['ip', 'port', 'username', 'password']
    }

    for section, keys in required_keys.items():
        if section not in config:
            print(f"❌ Fehler: Missing section [{section}] in config.", file=sys.stderr)
            sys.exit(1)
        for key in keys:
            if key not in config[section]:
                print(f"❌ Fehler: Missing key '{key}' in section [{section}].", file=sys.stderr)
                sys.exit(1)

    return config

# Pfad zur Config-Datei (im selben Verzeichnis wie das Skript)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, ".fems_config.ini")

config = load_config(CONFIG_FILE)

# Werte aus Config extrahieren
PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_APP_TOKEN = config['pushover']['app_token']
PUSHOVER_USER_KEY = config['pushover']['user_key']

# Prioritäts-Konfiguration
PUSHOVER_PRIORITY_OUTAGE = int(config.get('pushover', 'priority_outage', fallback='2'))
PUSHOVER_PRIORITY_RECOVERY = int(config.get('pushover', 'priority_recovery', fallback='0'))
PUSHOVER_PRIORITY_RETRY = int(config.get('pushover', 'priority_retry', fallback='30'))
PUSHOVER_PRIORITY_EXPIRE = int(config.get('pushover', 'priority_expire', fallback='3600'))
PUSHOVER_PRIORITY_SOUND = config.get('pushover', 'priority_sound', fallback='siren')

FEMS_IP = config['fems']['ip']
FEMS_PORT = int(config['fems']['port'])
USERNAME = config['fems']['username']
PASSWORD = config['fems']['password']

# Pfade aus Config oder Defaults
STATE_FILE = config.get('paths', 'state_file', fallback="/var/lib/fems/state.json")
AUDIT_LOG_FILE = config.get('paths', 'audit_log', fallback="/var/log/fems/outages.log")

# --- ARGUMENTE ---

def parse_args():
    parser = argparse.ArgumentParser(
        description="""
FEMS GridMode Monitoring mit Pushover-Alarmierung (Emergency Priority).

Features:
  - Sofort-Alarm bei Statuswechsel (ON <-> OFF)
  - Update-Alarm alle X Minuten bei anhaltendem Ausfall
  - Persistente Dokumentation aller Ausfälle in JSON (Audit-Log)
  - Berechnung der Ausfalldauer in allen Benachrichtigungen
  - Konfiguration in: {config_file}

Audit-Log Datei:
  {audit_log}

Beispiele für Audit-Log Auswertungen (jq vorausgesetzt):
  # Letzte 10 Ausfälle anzeigen:
  jq -r 'select(.event == "OUTAGE_START") | .timestamp' {audit_log} | tail -10

  # Gesamte Ausfalldauer in Minuten (Jahr 2026):
  jq -r 'select(.event == "OUTAGE_END" and .timestamp | startswith("2026")) | .total_duration_minutes // 0' {audit_log} | awk '{{sum+=$1}} END {{print sum " Min"}}'

  # Anzahl der Ausfälle pro Monat im Jahr 2026:
  jq -r 'select(.event == "OUTAGE_START" and .timestamp | startswith("2026")) | .timestamp[0:7]' {audit_log} | sort | uniq -c

  # Durchschnittliche Ausfalldauer (in Minuten) für alle abgeschlossenen Ausfälle:
  jq -r 'select(.event == "OUTAGE_END") | .total_duration_minutes // 0' {audit_log} | awk '{{sum+=$1; count++}} END {{if(count>0) print sum/count; else print 0}}'

Cron-Eintrag (Beispiel):
  */5 * * * * ALERT_INTERVAL_MINUTES=30 /usr/bin/python3 /srv/scripts/grid_power.py 2>&1 | /usr/bin/logger -t FEMS_CRON
        """.format(config_file=CONFIG_FILE, audit_log=AUDIT_LOG_FILE),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-t", "--test", action="store_true",
                        help="Testmodus: Keine Pushover-Nachricht versenden, nur Ausgabe")
    parser.add_argument("-i", "--interval", type=int,
                        help="Intervall für Wiederholungs-Alerts in Minuten (Default: 30)")
    return parser.parse_args()

def get_alert_interval(args):
    """Ermittelt das Intervall: CLI > Umgebungsvariable > Default"""
    if args.interval is not None:
        return args.interval
    
    env_val = os.getenv("ALERT_INTERVAL_MINUTES")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            print(f"⚠️ Warnung: ALERT_INTERVAL_MINUTES ist ungültig ('{env_val}'). Verwende Default 30.", file=sys.stderr)
            return 30
            
    return 30

# --- STATE MANAGEMENT ---

def load_state():
    """Lädt den gespeicherten State oder gibt Default zurück."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "last_status": None,
        "last_alert_time": None,
        "first_outage_time": None
    }

def save_state(state):
    """Speichert den State mit sicheren Berechtigungen."""
    state_dir = os.path.dirname(STATE_FILE)
    if state_dir and not os.path.exists(state_dir):
        try:
            os.makedirs(state_dir, mode=0o755)
        except OSError:
            pass

    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)
    
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass

def should_send_alert(current_status, state, interval_minutes):
    """Entscheidet, ob ein Alert gesendet werden soll."""
    last_status = state.get("last_status")
    last_alert_time_str = state.get("last_alert_time")
    
    now = datetime.now()
    
    # Fall 1: Statuswechsel
    if last_status != current_status:
        return True, "Statuswechsel"
    
    # Fall 2: Update-Intervall
    if current_status == "OFF_GRID" and last_alert_time_str:
        last_alert = datetime.fromisoformat(last_alert_time_str)
        elapsed = now - last_alert
        
        if elapsed >= timedelta(minutes=interval_minutes):
            return True, f"Update nach {int(elapsed.total_seconds()/60)} Min"
            
    return False, "Kein Grund"

def update_alert_state(state, current_status):
    """Aktualisiert den State nach einem gesendeten Alert."""
    state["last_status"] = current_status
    state["last_alert_time"] = datetime.now().isoformat()
    
    # Erstmalige Feststellung des Ausfalls speichern
    if current_status == "OFF_GRID" and state.get("first_outage_time") is None:
        state["first_outage_time"] = datetime.now().isoformat()
    
    # Netz wieder da -> erster Ausfall-Zeitstempel zurücksetzen
    if current_status == "ON_GRID":
        state["first_outage_time"] = None
    
    save_state(state)

def format_duration(start_time_str, end_time=None):
    """Formatiert die Dauer zwischen zwei Zeitstempeln als 'Xh Ymin'."""
    if not start_time_str:
        return "Unbekannt"
    
    start = datetime.fromisoformat(start_time_str)
    end = end_time or datetime.now()
    duration = end - start
    
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    
    if hours > 0:
        return f"{hours}h {minutes}min"
    else:
        return f"{minutes}min"

def get_duration_minutes(start_time_str, end_time=None):
    """Berechnet die Dauer in Minuten (für Audit-Log)."""
    if not start_time_str:
        return 0
    start = datetime.fromisoformat(start_time_str)
    end = end_time or datetime.now()
    return int((end - start).total_seconds() / 60)

# --- AUDIT LOGGING ---

def ensure_audit_log_dir():
    log_dir = os.path.dirname(AUDIT_LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, mode=0o755)
        except OSError:
            pass

def write_audit_log(event_type, details):
    """Schreibt einen Eintrag in das Audit-Log (append-only)."""
    ensure_audit_log_dir()
    
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": event_type,
        **details
    }
    
    try:
        with open(AUDIT_LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + "\n")
        try:
            os.chmod(AUDIT_LOG_FILE, 0o644)
        except OSError:
            pass
    except IOError as e:
        print(f"⚠️ Warnung: Audit-Log konnte nicht geschrieben werden: {e}", file=sys.stderr)

# --- HILFSFUNKTIONEN ---

def send_pushover_notification(title, message, priority=0, sound=None, dry_run=False):
    """Versendet eine Pushover-Benachrichtigung."""
    if dry_run:
        print(f"[TEST] Pushover würde versendet werden:")
        print(f"       Titel:    {title}")
        print(f"       Nachricht: {message}")
        print(f"       Priorität: {priority}")
        if sound:
            print(f"       Sound:    {sound}")
        if priority == 2:
            print(f"       Retry:    {PUSHOVER_PRIORITY_RETRY}s")
            print(f"       Expire:   {PUSHOVER_PRIORITY_EXPIRE}s")
        return True

    params = {
        'token': PUSHOVER_APP_TOKEN,
        'user': PUSHOVER_USER_KEY,
        'title': title,
        'message': message,
        'priority': str(priority),
    }
    
    # Sound nur hinzufügen wenn definiert
    if sound:
        params['sound'] = sound
    
    # Emergency-Priority (2) benötigt Retry und Expire
    if priority == 2:
        params['retry'] = str(PUSHOVER_PRIORITY_RETRY)
        params['expire'] = str(PUSHOVER_PRIORITY_EXPIRE)

    try:
        response = requests.post(PUSHOVER_API_URL, data=params, timeout=5)
        response_data = response.json()
        return response_data.get('status') == 1
    except requests.exceptions.RequestException as e:
        print(f"✗ Fehler beim Versenden: {e}", file=sys.stderr)
        return False

def check_grid_status():
    """Prüft den GridMode Status des FEMS Systems."""
    url = f'http://{FEMS_IP}:{FEMS_PORT}/rest/channel/_sum/GridMode'
    session = requests.Session()
    session.auth = (USERNAME, PASSWORD)

    try:
        response = session.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        grid_mode_value = data.get('value')

        if grid_mode_value == 1:
            return "ON_GRID", "Netz verfügbar - normaler Betrieb", False, 1
        elif grid_mode_value == 2:
            return "OFF_GRID", "Stromausfall erkannt - Inselbetrieb", True, 2
        else:
            return "UNDEFINED", "Status unbekannt", None, grid_mode_value
    except requests.exceptions.RequestException as e:
        print(f"✗ Fehler bei FEMS-Abfrage: {e}", file=sys.stderr)
        return None, None, None, None

# --- HAUPTLOGIK ---

def check_and_notify(dry_run=False, interval_minutes=30):
    """Hauptfunktion: Prüft Status und sendet bei Bedarf Alert."""
    status_val, description, is_power_outage, grid_mode_raw = check_grid_status()

    if status_val is None:
        print("Konnte FEMS nicht abfragen")
        return

    current_status = status_val
    mode_tag = "[TEST] " if dry_run else ""
    timestamp = datetime.now().isoformat()
    
    print(f"{mode_tag}[{timestamp}] GridMode: {current_status} - {description}")

    state = load_state()
    send_alert, reason = should_send_alert(current_status, state, interval_minutes)

    if send_alert:
        if current_status == "OFF_GRID":
            # Dauer berechnen
            first_outage_str = state.get("first_outage_time")
            duration_str = format_duration(first_outage_str)
            duration_min = get_duration_minutes(first_outage_str)
            
            title = "⚠️ STROMAUSFALL ERKANNT"
            msg = (f"Das FEMS-System ist im Off-Grid-Modus.\n"
                   f"Erstfeststellung: {first_outage_str or timestamp}\n"
                   f"Ausfalldauer: {duration_str}\n"
                   f"Zeitstempel: {timestamp}\n"
                   f"Grund: {reason}")
            
            # Priorität und Sound aus Config
            priority = PUSHOVER_PRIORITY_OUTAGE
            sound = PUSHOVER_PRIORITY_SOUND
            
            # Audit-Log
            if reason == "Statuswechsel":
                write_audit_log("OUTAGE_START", {
                    "grid_mode": grid_mode_raw,
                    "first_outage_time": first_outage_str,
                    "priority": priority
                })
            else:
                write_audit_log("OUTAGE_UPDATE", {
                    "grid_mode": grid_mode_raw,
                    "duration_minutes": duration_min,
                    "priority": priority
                })

        else:  # ON_GRID
            # Netz wiederhergestellt
            first_outage_str = state.get("first_outage_time")
            duration_str = format_duration(first_outage_str)
            duration_min = get_duration_minutes(first_outage_str)
            
            title = "✅ NETZ WIEDERHERGESTELLT"
            msg = (f"Das FEMS-System ist zurück im On-Grid-Modus.\n"
                   f"Ausfall begann: {first_outage_str or 'Unbekannt'}\n"
                   f"Gesamtausfalldauer: {duration_str} ({duration_min} Min)\n"
                   f"Wiederherstellung: {timestamp}\n"
                   f"Grund: {reason}")
            
            # Priorität und Sound aus Config (Normal für Recovery)
            priority = PUSHOVER_PRIORITY_RECOVERY
            sound = None  # Kein Sound bei Recovery
            
            # Audit-Log
            write_audit_log("OUTAGE_END", {
                "grid_mode": grid_mode_raw,
                "total_duration": duration_str,
                "total_duration_minutes": duration_min,
                "first_outage_time": first_outage_str,
                "priority": priority
            })

        send_pushover_notification(title, msg, priority, sound, dry_run)
        
        if not dry_run:
            update_alert_state(state, current_status)
            print(f"   >> Alert gesendet ({reason}). State aktualisiert.")
        else:
            print(f"   >> [TEST] Alert würde gesendet werden ({reason}). State bleibt unverändert.")
    else:
        print(f"   >> Kein Alert nötig ({reason}).")

if __name__ == "__main__":
    args = parse_args()
    
    # Intervall ermitteln
    interval = get_alert_interval(args)
    
    # Warnung bei LibreSSL unterdrücken (optional)
    import warnings
    warnings.filterwarnings("ignore", category=Warning, module="urllib3")

    check_and_notify(dry_run=args.test, interval_minutes=interval)

