# SonatinaHost (fase U1)

Audio di Windows → VB-CABLE → SonatinaHost → LDAC/SBC → chiavetta USB dedicata (WinUSB + Bumble) → cuffie.
Nessun driver kernel nostro: Secure Boot resta attivo.

## File
- `sonatina_host.py`: il programma (cattura, jitter buffer, pump media con timer ad alta risoluzione,
  RTP con timestamp in campioni, AVDTP/A2DP via Bumble, riconnessione automatica).
- `codec/sonatina_codec.dll`: SBC, LDAC, AAC (FDK), aptX, mSBC e ricampionatore, compilato con
  mingw-w64 da `codec/build_codec.sh` (`codec/sonatina_codec.c` è l'interfaccia C; `codec/src` i
  wrapper SBC/LDAC nati per il driver kernel, ora in `archive/`; `codec/third_party` le librerie).
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

## Microfono delle cuffie (HFP)
Il microfono arriva alle app attraverso un secondo cavo virtuale gratuito, **Hi-Fi Cable**
(vb-audio.com/Cable, `HiFiCableAsioBridgeSetup`); vanno bene anche VB-CABLE A+B o il microfono
di Steam. Nelle app si sceglie **Hi-Fi Cable Output** come microfono.
- `sonatina_hfp.py`: Audio Gateway HFP sempre collegato accanto all'A2DP. Quando un'app apre il
  microfono virtuale, Sonatina sospende l'A2DP, apre un link eSCO (mSBC 16 kHz, o CVSD 8 kHz),
  scrive la voce in "Hi-Fi Cable Input" e manda l'audio di Windows alle cuffie sul link; quando
  l'app lo chiude torna l'A2DP. Il tasto "riaggancia" delle cuffie torna alla musica.
- Menu del tray: Microfono delle cuffie → Automatico / Sempre attivo / Disattivato, voce mSBC.
  Icona verde = chiamata in corso.
- USB: serve libusb >= 1.0.27 (`third_party/libusb`, 1.0.29) e l'alternate setting 2 per lo SCO.
  Correzioni a Bumble/usb1 (lettura ISO, trasferimenti in volo) descritte in testa a `sonatina_hfp.py`.
- Impostazioni (`settings.json`): `hfp_mode` (auto/on/off), `hfp_msbc`, `mic_device`,
  `mic_gain_db` (6), `sco_alt` (2).
- `tests/hfp_virtual.py`: SLC aperto da Sonatina o dalle cuffie, chiamata mSBC e CVSD con tono
  in entrambi i versi, ritorno all'A2DP, riaggancio, ripiego automatico da mSBC a CVSD.
