#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_monitor.py
=================
Monitor dei rendering di DaVinci Resolve con notifiche via email.

Cosa fa:
  - Si collega a DaVinci Resolve tramite l'API di scripting.
  - Resta in ascolto: quando parte un rendering se ne accorge da solo.
  - Per ogni job traccia stato, percentuale, tempi.
  - Invia una mail QUANDO UN JOB FALLISCE (con percentuale raggiunta e,
    in best-effort, la causa estratta dal log di Resolve).
  - Invia una mail SE UN JOB SI BLOCCA (percentuale ferma troppo a lungo).
  - Invia una mail RIASSUNTIVA A FINE RENDERING con l'esito di OGNI job
    (compresi quelli andati a buon fine, con tempo impiegato).

Modalita' di avvio:
  python render_monitor.py            -> modalita' "watcher": resta attivo
                                         e monitora tutti i rendering che partono.
  python render_monitor.py --once     -> monitora la sessione di rendering
                                         corrente/prossima e poi esce.
  python render_monitor.py --test-email  -> invia una mail di prova e esce.

La configurazione (SMTP, destinatari, tempi) sta in config.ini
(vedi config.example.ini). Le credenziali NON vanno nel codice.

Requisiti:
  - DaVinci Resolve in esecuzione.
  - In Resolve: Preferences > System > General > "External scripting using"
    impostato su "Local" (o "Network").
  - Solo libreria standard di Python: nessun pip install necessario.
"""

import os
import sys
import time
import glob
import argparse
import smtplib
import datetime
import configparser
from email.message import EmailMessage

# ----------------------------------------------------------------------------
# 1) CONNESSIONE A DAVINCI RESOLVE
# ----------------------------------------------------------------------------

def _default_module_and_lib():
    """Restituisce (module_path, lib_path) di default in base al sistema operativo."""
    if sys.platform.startswith("win"):
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        module_path = os.path.join(
            program_data, "Blackmagic Design", "DaVinci Resolve",
            "Support", "Developer", "Scripting", "Modules")
        lib_path = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
    elif sys.platform == "darwin":
        module_path = ("/Library/Application Support/Blackmagic Design/"
                       "DaVinci Resolve/Developer/Scripting/Modules")
        lib_path = ("/Applications/DaVinci Resolve/DaVinci Resolve.app/"
                    "Contents/Libraries/Fusion/fusionscript.so")
    else:  # Linux
        module_path = "/opt/resolve/Developer/Scripting/Modules"
        lib_path = "/opt/resolve/libs/Fusion/fusionscript.so"
    return module_path, lib_path


def get_resolve():
    """
    Ritorna l'oggetto Resolve, oppure None se non riesce a collegarsi
    (Resolve chiuso o scripting non abilitato).
    """
    # Rispetta eventuali variabili d'ambiente gia' impostate dall'utente.
    module_path, lib_path = _default_module_and_lib()
    if os.environ.get("RESOLVE_SCRIPT_API"):
        module_path = os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules")
    if os.environ.get("RESOLVE_SCRIPT_LIB"):
        lib_path = os.environ["RESOLVE_SCRIPT_LIB"]

    if module_path not in sys.path and os.path.isdir(module_path):
        sys.path.append(module_path)

    try:
        import DaVinciResolveScript as bmd  # noqa: E402
    except ImportError:
        # Import manuale della libreria come fallback.
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "DaVinciResolveScript", lib_path)
            bmd = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bmd)
        except Exception:
            return None

    try:
        return bmd.scriptapp("Resolve")
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 2) LETTURA CONFIGURAZIONE
# ----------------------------------------------------------------------------

def load_config():
    """Carica config.ini dalla stessa cartella dello script."""
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(here, "config.ini")
    if not os.path.isfile(cfg_path):
        sys.exit(
            "ERRORE: config.ini non trovato.\n"
            "Copia config.example.ini in config.ini e inserisci i tuoi dati SMTP."
        )
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")
    return cfg


def cfg_value(cfg, section, key, env_var=None, default=None):
    """
    Legge un valore dalla config; se vuoto e c'e' una variabile d'ambiente
    corrispondente, usa quella (utile per non committare le password).
    """
    val = ""
    if cfg.has_option(section, key):
        val = cfg.get(section, key).strip()
    if not val and env_var:
        val = os.environ.get(env_var, "").strip()
    return val if val else default


# ----------------------------------------------------------------------------
# 3) INVIO EMAIL
# ----------------------------------------------------------------------------

def send_email(cfg, subject, body):
    """Invia una mail di testo. Non solleva eccezioni: logga e prosegue."""
    host = cfg_value(cfg, "smtp", "host")
    port = int(cfg_value(cfg, "smtp", "port", default="587"))
    username = cfg_value(cfg, "smtp", "username")
    password = cfg_value(cfg, "smtp", "password", env_var="RENDER_MONITOR_SMTP_PASSWORD")
    use_tls = cfg_value(cfg, "smtp", "use_tls", default="true").lower() in ("true", "1", "yes")
    use_ssl = cfg_value(cfg, "smtp", "use_ssl", default="false").lower() in ("true", "1", "yes")
    from_addr = cfg_value(cfg, "smtp", "from_addr", default=username)

    to_raw = cfg_value(cfg, "email", "to", default="")
    to_list = [a.strip() for a in to_raw.split(",") if a.strip()]
    prefix = cfg_value(cfg, "email", "subject_prefix", default="[Resolve Render]")

    if not (host and to_list and from_addr):
        log("EMAIL NON INVIATA: configurazione SMTP incompleta (host/from/to).")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"{prefix} {subject}".strip()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                if username and password:
                    s.login(username, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                if use_tls:
                    s.starttls()
                    s.ehlo()
                if username and password:
                    s.login(username, password)
                s.send_message(msg)
        log(f"Email inviata: {subject}")
        return True
    except Exception as e:
        log(f"ERRORE invio email: {e}")
        return False


# ----------------------------------------------------------------------------
# 4) ESTRAZIONE CAUSA ERRORE DAI LOG DI RESOLVE (best-effort)
# ----------------------------------------------------------------------------

def _resolve_log_dirs():
    """Directory tipiche dei log di Resolve per ogni OS."""
    dirs = []
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        dirs.append(os.path.join(appdata, "Blackmagic Design", "DaVinci Resolve",
                                 "Support", "logs"))
    elif sys.platform == "darwin":
        dirs.append(os.path.join(home, "Library", "Application Support",
                                 "Blackmagic Design", "DaVinci Resolve", "logs"))
    else:
        dirs.append(os.path.join(home, ".local", "share", "DaVinciResolve", "logs"))
        dirs.append("/opt/resolve/logs")
    return [d for d in dirs if os.path.isdir(d)]


def find_recent_error_in_logs(max_lines=8):
    """
    Cerca nelle ultime righe del log piu' recente di Resolve eventuali
    righe che sembrano descrivere un errore. Best-effort: non garantito.
    """
    keywords = ("error", "failed", "could not", "cannot", "unable",
                "offline", "not be processed", "decode", "render job")
    candidates = []
    for d in _resolve_log_dirs():
        candidates.extend(glob.glob(os.path.join(d, "**", "*.log"), recursive=True))
    if not candidates:
        return None
    newest = max(candidates, key=os.path.getmtime)
    try:
        with open(newest, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return None

    # Scorri dal fondo e prendi le righe "sospette" piu' recenti.
    found = []
    for line in reversed(lines[-4000:]):
        low = line.lower()
        if any(k in low for k in keywords):
            found.append(line.rstrip())
            if len(found) >= max_lines:
                break
    if not found:
        return None
    found.reverse()
    return "File di log: {}\n{}".format(newest, "\n".join(found))


# ----------------------------------------------------------------------------
# 5) LOGICA DI MONITORAGGIO
# ----------------------------------------------------------------------------

def log(message):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def job_label(index, info):
    """Etichetta leggibile di un job: numero in coda + nome + JobId."""
    number = index + 1
    name = info.get("RenderJobName") or info.get("TimelineName") or "(senza nome)"
    job_id = info.get("JobId", "?")
    return number, name, job_id


def job_file(info):
    """Nome file di output + cartella di destinazione."""
    out = info.get("OutputFilename") or "(file non specificato)"
    target = info.get("TargetDir") or ""
    return out, target


def ms_to_human(ms):
    try:
        secs = int(ms) / 1000.0
    except (TypeError, ValueError):
        return "n/d"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def monitor(cfg, run_once=False):
    poll = int(cfg_value(cfg, "monitor", "poll_interval_seconds", default="10"))
    stall_timeout = int(cfg_value(cfg, "monitor", "stall_timeout_seconds", default="600"))

    log("Avvio monitor. In attesa di collegamento a DaVinci Resolve...")

    was_rendering = False
    # Stato per-job entro la sessione corrente.
    last_pct = {}         # job_id -> percentuale
    last_change = {}      # job_id -> timestamp ultimo cambio percentuale
    alerted_failed = set()
    alerted_stalled = set()
    session_active = False

    while True:
        resolve = get_resolve()
        if resolve is None:
            log("Resolve non raggiungibile (chiuso o scripting non abilitato). Riprovo...")
            time.sleep(poll)
            continue

        try:
            pm = resolve.GetProjectManager()
            project = pm.GetCurrentProject() if pm else None
        except Exception as e:
            log(f"Errore accesso al progetto: {e}")
            time.sleep(poll)
            continue

        if project is None:
            time.sleep(poll)
            continue

        try:
            rendering = bool(project.IsRenderingInProgress())
            jobs = project.GetRenderJobList() or []
        except Exception as e:
            log(f"Errore lettura coda render: {e}")
            time.sleep(poll)
            continue

        now = time.time()

        # --- Inizio di una nuova sessione di rendering ---
        if rendering and not was_rendering:
            session_active = True
            last_pct.clear()
            last_change.clear()
            alerted_failed.clear()
            alerted_stalled.clear()
            log(f"Rendering AVVIATO. Job in coda: {len(jobs)}")

        # --- Durante il rendering: controllo fallimenti e blocchi ---
        if rendering:
            for i, info in enumerate(jobs):
                number, name, job_id = job_label(i, info)
                out, target = job_file(info)
                try:
                    status = project.GetRenderJobStatus(job_id) or {}
                except Exception:
                    status = {}
                jstatus = status.get("JobStatus", "Unknown")
                pct = status.get("CompletionPercentage", 0)

                # Traccia variazioni di percentuale per rilevare i blocchi.
                if job_id not in last_pct or last_pct[job_id] != pct:
                    last_pct[job_id] = pct
                    last_change[job_id] = now

                # FALLIMENTO
                if jstatus == "Failed" and job_id not in alerted_failed:
                    alerted_failed.add(job_id)
                    cause = find_recent_error_in_logs() or \
                        "Causa non estraibile automaticamente dai log."
                    body = (
                        "Un job di rendering e' FALLITO.\n\n"
                        f"Job numero:  {number}\n"
                        f"Nome job:    {name}\n"
                        f"Job ID:      {job_id}\n"
                        f"File:        {out}\n"
                        f"Cartella:    {target}\n"
                        f"Percentuale raggiunta: {pct}%\n"
                        f"Stato:       {jstatus}\n\n"
                        "Probabile causa (best-effort dai log di Resolve):\n"
                        f"{cause}\n"
                    )
                    log(f"FALLITO job #{number} '{name}' al {pct}%")
                    send_email(cfg, f"ERRORE job #{number} '{name}' ({pct}%)", body)

                # BLOCCO (percentuale ferma troppo a lungo mentre e' in rendering)
                elif jstatus == "Rendering" and job_id not in alerted_stalled:
                    stuck_for = now - last_change.get(job_id, now)
                    if stuck_for >= stall_timeout and 0 < pct < 100:
                        alerted_stalled.add(job_id)
                        cause = find_recent_error_in_logs() or \
                            "Nessuna riga d'errore evidente nei log (potrebbe essere solo lento)."
                        body = (
                            "Un job di rendering sembra BLOCCATO "
                            f"(percentuale ferma da {int(stuck_for // 60)} min).\n\n"
                            f"Job numero:  {number}\n"
                            f"Nome job:    {name}\n"
                            f"Job ID:      {job_id}\n"
                            f"File:        {out}\n"
                            f"Cartella:    {target}\n"
                            f"Percentuale ferma a: {pct}%\n\n"
                            "Eventuali indizi dai log di Resolve:\n"
                            f"{cause}\n"
                        )
                        log(f"BLOCCO job #{number} '{name}' fermo al {pct}%")
                        send_email(cfg, f"BLOCCO job #{number} '{name}' fermo al {pct}%", body)

        # --- Fine sessione: riepilogo di tutti i job ---
        if not rendering and was_rendering and session_active:
            session_active = False
            summary = build_summary(project, jobs)
            log("Rendering TERMINATO. Invio riepilogo.")
            send_email(cfg, "Rendering terminato - riepilogo", summary)
            if run_once:
                log("Modalita' --once: esco dopo il riepilogo.")
                return

        was_rendering = rendering
        time.sleep(poll)


def build_summary(project, jobs):
    """Costruisce il testo del riepilogo con l'esito di ogni job."""
    righe = []
    ok = fail = other = 0
    for i, info in enumerate(jobs):
        number, name, job_id = job_label(i, info)
        out, target = job_file(info)
        try:
            status = project.GetRenderJobStatus(job_id) or {}
        except Exception:
            status = {}
        jstatus = status.get("JobStatus", "Unknown")
        pct = status.get("CompletionPercentage", 0)
        tempo = ms_to_human(status.get("TimeTakenToRenderInMs")) \
            if status.get("TimeTakenToRenderInMs") else "n/d"

        if jstatus == "Complete":
            ok += 1
            esito = f"OK  ({pct}%, tempo {tempo})"
        elif jstatus == "Failed":
            fail += 1
            esito = f"FALLITO  (fermo al {pct}%)"
        elif jstatus == "Cancelled":
            other += 1
            esito = f"ANNULLATO  (al {pct}%)"
        elif jstatus == "Ready":
            other += 1
            esito = "NON AVVIATO"
        else:
            other += 1
            esito = f"{jstatus} ({pct}%)"

        righe.append(
            f"#{number}  {name}\n"
            f"      File:  {out}\n"
            f"      Dest:  {target}\n"
            f"      Esito: {esito}\n"
        )

    intestazione = (
        "Riepilogo rendering DaVinci Resolve\n"
        f"Completati: {ok}   Falliti: {fail}   Altro: {other}\n"
        + "=" * 48 + "\n\n"
    )
    return intestazione + "\n".join(righe) if righe else intestazione + "(coda vuota)"


# ----------------------------------------------------------------------------
# 6) MAIN
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Monitor rendering DaVinci Resolve con notifiche email.")
    parser.add_argument("--once", action="store_true",
                        help="Monitora la sessione corrente/prossima e poi esce.")
    parser.add_argument("--test-email", action="store_true",
                        help="Invia una mail di prova per verificare la configurazione SMTP.")
    args = parser.parse_args()

    cfg = load_config()

    if args.test_email:
        ok = send_email(
            cfg,
            "Email di prova",
            "Se leggi questo messaggio, la configurazione SMTP funziona.\n"
            "Puoi avviare il monitor con:  python render_monitor.py")
        sys.exit(0 if ok else 1)

    try:
        monitor(cfg, run_once=args.once)
    except KeyboardInterrupt:
        log("Interrotto dall'utente. Chiusura.")


if __name__ == "__main__":
    main()
