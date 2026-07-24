#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
davinci_silent_render.py
=========================
"Davinci Silente Rendering": avvia DaVinci Resolve in modalita' GUI,
manda in rendering la coda gia' presente nel progetto e chiude tutto
da solo, inviando sempre una mail con l'esito.

Cosa fa:
  - Avvia DaVinci Resolve in modalita' GUI (se non e' gia' aperto).
  - Attende che lo scripting sia disponibile e che un progetto sia
    caricato: di default quello che si apre da solo all'avvio di
    Resolve (l'ultimo usato); se viene passato un nome sulla riga di
    comando, prova ad aprire esplicitamente quel progetto.
  - Avvia il rendering dei job GIA' PRESENTI nella coda del progetto
    (non ne crea di nuovi).
  - Da quel momento in poi il controllo passa a render_monitor.py:
    stesso monitoraggio di fallimenti/blocchi/crash e stessa mail di
    riepilogo finale, senza duplicare la logica.
  - A fine lavoro (rendering riuscito, fallito o Resolve crashato)
    chiude DaVinci Resolve, per lasciare la macchina pronta senza
    bisogno di intervento manuale ("silente").

Uso:
  python davinci_silent_render.py
      -> usa il progetto che si apre da solo all'avvio di Resolve
         (l'ultimo utilizzato).

  python davinci_silent_render.py "Nome Progetto"
      -> apre esplicitamente quel progetto prima di renderizzare.

Configurazione: stesso config.ini di render_monitor.py (stessa cartella,
vedi config.example.ini), con in piu' la sezione opzionale
[silent_render] per i tempi di attesa dell'avvio di Resolve/progetto.

Requisiti: come render_monitor.py (solo libreria standard di Python).
render_monitor.py NON viene modificato: questo script lo importa e ne
riusa le funzioni di connessione, configurazione, email e monitoraggio.
"""

import sys
import os
import time
import argparse
import subprocess

import render_monitor as rm


# ----------------------------------------------------------------------------
# 1) AVVIO DI DAVINCI RESOLVE IN MODALITA' GUI
# ----------------------------------------------------------------------------

def _resolve_launch_command():
    """Comando per avviare l'app di DaVinci Resolve in GUI, per OS."""
    if sys.platform.startswith("win"):
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        exe = os.path.join(program_files, "Blackmagic Design",
                            "DaVinci Resolve", "Resolve.exe")
        return [exe]
    elif sys.platform == "darwin":
        return ["open", "-a", "/Applications/DaVinci Resolve/DaVinci Resolve.app"]
    else:  # Linux
        return ["/opt/resolve/bin/resolve"]


def launch_resolve_gui():
    """Avvia DaVinci Resolve in GUI (se e' gia' aperto, lo riporta in primo piano)."""
    cmd = _resolve_launch_command()
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        rm.log(f"Impossibile avviare DaVinci Resolve ({' '.join(cmd)}): {e}")
        return False


def quit_resolve(resolve):
    """Chiude DaVinci Resolve. Best-effort: non solleva eccezioni."""
    if resolve is None:
        return
    rm.log("Chiusura di DaVinci Resolve...")
    try:
        resolve.Quit()
    except Exception as e:
        rm.log(f"Non sono riuscito a chiudere Resolve automaticamente: {e}")


# ----------------------------------------------------------------------------
# 2) ATTESA CONNESSIONE E PROGETTO
# ----------------------------------------------------------------------------

def wait_for_resolve(cfg):
    """Prova a collegarsi allo scripting di Resolve finche' non risponde
    (entro launch_wait_seconds), utile subito dopo averlo avviato."""
    timeout = int(rm.cfg_value(cfg, "silent_render", "launch_wait_seconds", default="90"))
    poll = int(rm.cfg_value(cfg, "silent_render", "poll_interval_seconds", default="3"))
    deadline = time.time() + timeout
    while True:
        resolve = rm.get_resolve()
        if resolve is not None:
            return resolve
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def wait_for_project(cfg, resolve, project_name):
    """
    Attende che un progetto sia caricato in Resolve.
    - Se project_name e' vuoto: aspetta il progetto che si apre da solo
      (di norma l'ultimo utilizzato).
    - Se project_name e' specificato: prova ad aprirlo esplicitamente e
      aspetta che risulti quello attivo.
    Ritorna il progetto trovato (puo' non coincidere con project_name se
    l'apertura e' fallita: il chiamante verifica il nome).
    """
    timeout = int(rm.cfg_value(cfg, "silent_render", "project_wait_seconds", default="60"))
    poll = int(rm.cfg_value(cfg, "silent_render", "poll_interval_seconds", default="3"))
    deadline = time.time() + timeout

    pm = resolve.GetProjectManager()
    if pm is None:
        return None

    if project_name:
        try:
            pm.LoadProject(project_name)
        except Exception as e:
            rm.log(f"Errore durante l'apertura del progetto '{project_name}': {e}")

    project = None
    while time.time() < deadline:
        try:
            project = pm.GetCurrentProject()
        except Exception:
            project = None
        if project is not None:
            try:
                nome_attuale = project.GetName()
            except Exception:
                nome_attuale = None
            if not project_name or nome_attuale == project_name:
                return project
        time.sleep(poll)
    return project


# ----------------------------------------------------------------------------
# 3) AVVIO DEL RENDERING (job gia' presenti in coda)
# ----------------------------------------------------------------------------

def start_render(project):
    """Avvia il rendering di tutti i job gia' presenti nella coda del
    progetto. Non crea nuovi job. Ritorna (True, None) o (False, motivo)."""
    try:
        jobs = project.GetRenderJobList() or []
    except Exception as e:
        return False, f"Impossibile leggere la coda di rendering: {e}"

    if not jobs:
        return False, "La coda di rendering del progetto e' vuota: nessun job da avviare."

    try:
        ok = project.StartRendering()
    except Exception as e:
        return False, f"Errore nell'avvio del rendering: {e}"

    if not ok:
        return False, "project.StartRendering() non e' riuscito ad avviare la coda."

    return True, None


# ----------------------------------------------------------------------------
# 4) MAIN
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Avvia DaVinci Resolve in GUI, manda in rendering la coda "
                     "gia' presente nel progetto e invia una mail con l'esito "
                     "(riusa i controlli di render_monitor.py).")
    parser.add_argument(
        "project_name", nargs="?", default=None,
        help="Nome del progetto da aprire. Se omesso, usa il progetto che si "
             "apre da solo all'avvio di Resolve (di norma l'ultimo utilizzato).")
    args = parser.parse_args()

    cfg = rm.load_config()

    rm.log("Avvio DaVinci Resolve in modalita' GUI...")
    launch_resolve_gui()

    resolve = wait_for_resolve(cfg)
    if resolve is None:
        body = (
            "Impossibile collegarsi a DaVinci Resolve entro il tempo previsto.\n"
            "Il rendering NON e' stato avviato.\n\n"
            "Verifica che Resolve si avvii correttamente e che lo scripting "
            "sia abilitato (Preferences > System > General > "
            "\"External scripting using\" = Local)."
        )
        rm.notify(cfg, "Rendering NON avviato - Resolve non raggiungibile", body)
        sys.exit(1)

    project = wait_for_project(cfg, resolve, args.project_name)
    if project is None:
        body = (
            "Nessun progetto risulta caricato in DaVinci Resolve entro il "
            "tempo previsto. Il rendering NON e' stato avviato."
        )
        rm.notify(cfg, "Rendering NON avviato - nessun progetto caricato", body)
        quit_resolve(resolve)
        sys.exit(1)

    nome_progetto = project.GetName()
    if args.project_name and nome_progetto != args.project_name:
        body = (
            f"Non sono riuscito ad aprire il progetto richiesto "
            f"'{args.project_name}'.\n"
            f"Progetto attualmente caricato: '{nome_progetto}'.\n"
            "Il rendering NON e' stato avviato."
        )
        rm.notify(cfg, "Rendering NON avviato - progetto non trovato", body)
        quit_resolve(resolve)
        sys.exit(1)

    rm.log(f"Progetto in uso: {nome_progetto}")

    started, motivo = start_render(project)
    if not started:
        body = f"Progetto: {nome_progetto}\n\n{motivo}"
        rm.notify(cfg, "Rendering NON avviato", body)
        quit_resolve(resolve)
        sys.exit(1)

    rm.log("Rendering avviato. Passo il controllo al monitor "
           "(stesse regole di render_monitor.py: fallimenti, blocchi, crash, "
           "riepilogo finale via email).")
    try:
        rm.monitor(cfg, run_once=True)
    finally:
        quit_resolve(resolve)


if __name__ == "__main__":
    main()
