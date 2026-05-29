# Fenecon Grid Monitoring

Ein Python-Skript zur Überwachung des Grid-Status eines FEMS-Systems (z.B. Fronius) mit Pushover-Benachrichtigungen und Audit-Log.

---

## Features

- **Sofort-Alarm** bei Statuswechsel (ON_GRID ↔ OFF_GRID)
- **Update-Alarm** alle X Minuten bei anhaltendem Ausfall (verhindert Spam)
- **Persistente Dokumentation** aller Ausfälle in JSON (Audit-Log)
- **Ausfalldauer-Berechnung** in allen Benachrichtigungen
- **Konfiguration** aus separater INI-Datei (keine Hardcoded Credentials)
- **Test-Modus** für sichere Tests ohne Pushover-Versand

---

## Voraussetzungen

### Software

| Komponente | Version | Hinweis |
| :--- | :--- | :--- |
| Python | 3.9+ | `python3` |
| requests | Latest | `pip install requests` |
| jq | Optional | Für Audit-Log Auswertungen |
| Cron | Standard | Für automatisierten Betrieb |

### Dienste

| Dienst | Zweck |
| :--- | :--- |
| FEMS-System | Muss HTTP-API auf Port 80 bereitstellen |
| Pushover | Account mit App-Token und User-Key |

### Berechtigungen

| Pfad | Berechtigung | Zweck |
| :--- | :--- | :--- |
| `/srv/scripts/` | 755 | Skript-Verzeichnis |
| `.fems_config.ini` | 600 | Nur Owner lesbar/schreibbar |
| `/var/lib/fems/` | 755 | State-Datei |
| `/var/log/fems/` | 755 | Audit-Log |

---

## Installation

### 1. Verzeichnisse erstellen

```bash
sudo mkdir -p /srv/scripts
sudo mkdir -p /var/lib/fems
sudo mkdir -p /var/log/fems
sudo chown dein_user:dein_user /var/lib/fems /var/log/fems
```

### 2. Skript speichern

```bash
cd /srv/scripts
nano grid_power.py
# Inhalt des Skripts einfügen
chmod +x grid_power.py
```

### 3. Konfigurationsdatei erstellen

```bash
nano .fems_config.ini
```

**Inhalt von .fems_config.ini:**

```ini
[pushover]
app_token = dein_pushover_app_token
user_key = dein_pushover_user_key

[fems]
ip = 192.168.0.9
port = 80
username = dein_fems_user
password = dein_fems_pass

[paths]
state_file = /var/lib/fems/state.json
audit_log = /var/log/fems/outages.log
```

### 4. Berechtigungen setzen

```bash
chmod 600 .fems_config.ini
chown dein_user:dein_user .fems_config.ini
```

### 5. Python-Abhängigkeiten installieren

```bash
pip3 install requests
```

## Nutzung

### Funktion testen

```bash
# Test ohne Pushover-Versand
./grid_power.py -t

# Test mit eigenem Intervall (z.B. 10 Minuten)
./grid_power.py -t --interval 10

# Hilfe anzeigen
./grid_power.py -h
```

### Crontab-Eintrag

```bash
# Alle 5 Minuten prüfen, Alerts alle 30 Minuten
*/5 * * * * ALERT_INTERVAL_MINUTES=30 /usr/bin/python3 /srv/scripts/grid_power.py 2>&1 | /usr/bin/logger -t FEMS_CRON
```

### Auditlog-Auswertung

```bash
# Letzte 10 Ausfälle anzeigen
jq -r 'select(.event == "OUTAGE_START") | .timestamp' /var/log/fems/outages.log | tail -10

# Gesamte Ausfalldauer in Minuten (Jahr 2026)
jq -r 'select(.event == "OUTAGE_END" and .timestamp | startswith("2026")) | .total_duration_minutes // 0' /var/log/fems/outages.log | awk '{sum+=$1} END {print sum " Min"}'

# Anzahl der Ausfälle pro Monat im Jahr 2026
jq -r 'select(.event == "OUTAGE_START" and .timestamp | startswith("2026")) | .timestamp[0:7]' /var/log/fems/outages.log | sort | uniq -c

# Durchschnittliche Ausfalldauer (in Minuten) für alle abgeschlossenen Ausfälle
jq -r 'select(.event == "OUTAGE_END") | .total_duration_minutes // 0' /var/log/fems/outages.log | awk '{sum+=$1; count++} END {if(count>0) print sum/count; else print 0}'

# Alle Events des heutigen Tages
jq -r 'select(.timestamp | startswith("'"$(date +%Y-%m-%d)"'"))' /var/log/fems/outages.log
```





