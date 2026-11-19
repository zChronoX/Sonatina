# SonatinaHost (fase U1)

Audio di Windows → VB-CABLE → SonatinaHost → LDAC/SBC → chiavetta USB dedicata (WinUSB + Bumble) → cuffie.
Nessun driver kernel nostro: Secure Boot resta attivo.

## File
- `sonatina_host.py`: il programma (cattura, jitter buffer, pump media con timer ad alta risoluzione,
  RTP con timestamp in campioni, AVDTP/A2DP via Bumble, riconnessione automatica).
- `codec/sonatina_codec.dll`: encoder SBC + LDAC (stesso codice C del driver kernel), compilato con
  mingw-w64 da `codec/build_codec.sh` (`codec/sonatina_codec.c` è l'interfaccia C).
- `tests/e2e_virtual.py`: prova completa in-process con controller virtuali Bumble e "cuffie finte"
  (LDAC SQ: 125 pacchetti/s, payload 661 byte, timestamp +384; SBC decodificato: 1 kHz, SNR 69 dB;
  fallback SBC se manca LDAC; riconnessione dopo disconnessione).

## Requisiti
- Python + `pip install -r requirements.txt`
- VB-CABLE installato; formato 48000 Hz 16 bit su "CABLE Input" e "CABLE Output" (mmsys.cpl → Avanzate).
- Chiavetta Realtek `0BDA:8771` su WinUSB (Zadig); firmware `rtl8761bu_fw.bin` + `rtl8761bu_config.bin`
  in `%LOCALAPPDATA%\Google\bumble\firmware\realtek`.
- Cuffie già abbinate alla chiavetta (fatto nella prova U0).

## Installazione (avvio automatico, icona nell'area di notifica)
    powershell -ExecutionPolicy Bypass -File install-host.ps1              # installa / aggiorna
    powershell -ExecutionPolicy Bypass -File install-host.ps1 -Uninstall   # toglie l'avvio automatico
Crea un ambiente Python privato (`.venv`), un collegamento in Esecuzione automatica e avvia
`sonatina_tray.py` (pythonw, senza console). Icona: blu = in streaming, grigio = in attesa delle
cuffie, rosso = chiavetta non trovata / errore. Menu: Riavvia connessione, Apri log, Esci.
Log: `%LOCALAPPDATA%\Sonatina\sonatina.log`.

## Uso da console (debug)
    python sonatina_host.py                    # LDAC 660 kbit/s
    python sonatina_host.py --ldac-quality hq  # LDAC 990 kbit/s
    python sonatina_host.py --codec sbc        # SBC (bitpool fino a 53 / massimo delle cuffie)
    python sonatina_host.py --tone             # tono 1 kHz, senza VB-CABLE
    python sonatina_host.py --list-devices
Ogni 5 s stampa: stato, livello del buffer, underrun, pacchetti, ritardo massimo del pump.

Volume: VB-CABLE ignora il proprio cursore, quindi SonatinaHost legge volume e muto di Windows
sull'uscita VB-CABLE (pycaw) e applica il guadagno prima della codifica (rampa, niente scatti).
