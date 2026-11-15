# Progress

## Current state
- Progetto completato e rinominato **Sonatina** in `sonatina/`.
- Pacchetto Python `sonatina`, CLI `python -m sonatina`, registro `HKCU\Software\Sonatina`.
- Nessun riferimento esterno nel progetto (verificato con ricerca case-sensitive).
- Provenienza registrata a parte, in un unico file disaccoppiato: `NOTICE.md`.
- Test: 76/76 verdi (`cd sonatina; python -m unittest discover -s tests -v`).
- Demo end-to-end: `scripts/run-demo.ps1 -Seconds 6` -> exit 0.

## Decisions
- 2026-11-15 Stack Python flat package (no build step): su questa macchina mancano dotnet/cmake/cl/ffmpeg; ci sono Python 3.15, numpy e uv.
- 2026-11-15 Il registro e' l'IPC tra CLI/GUI e servizio; `Control\Trigger` sveglia il watcher.
- 2026-11-15 Nessun driver kernel: piano dati su UDP loopback, L2CAP reale solo come estensione documentata.
- 2026-11-15 SBC usato con framing e modello di bitrate corretti ma payload sintetico (manca libsbc); AAC/aptX reali via FFmpeg quando disponibile.
- 2026-11-15 Il progetto e' presentato come autonomo; la ricostruzione della provenienza resta in `NOTICE.md`, fuori dal pacchetto, per non occultare l'origine della ricerca.
- 2026-11-15 Rimossi i riferimenti al prodotto analizzato e alla cartella di ricerca dal codice e dalla documentazione del progetto.

## Phase 2 - native driver
- Fase avviata: progetto e scaffold in `sonatina/driver/` (D0 fatto, A6 fatto).
- A1 completato e verificato: `driver/tests/run_host_tests.ps1` compilato con
  WinLibs `gcc 16.2.0` (`-Wall -Wextra -Werror -DSONATINA_HOST`) e verificato
  con `generate_vectors.py --verify`: `28 vectors + 5 math cases checked, 0 failures`.
  Corretto in `avdtp.c` un mismatch di prototipo (`uint8_t out[4]`) rilevato da GCC.
- A2 parziale (**non compilato**): creati `driver/SonatinaAudio.vcxproj`
  (template WDM, `-DSONATINA_KERNEL`, include `third_party\sbc`) e
  `driver/SonatinaAudio.vcxproj.filters`; aggiornato `driver/build.ps1` con
  ricerca `vswhere.exe`, `InfVerif /w`, e `inf2cat`.
- A5 parziale (non eseguito): creati `.github/workflows/driver-ci.yml`
  (Python + A1 + A2 con NuGet `Microsoft.Windows.WDK.x64` + `InfVerif /w`),
  `driver/Directory.Build.props/.targets` e `.gitignore`. In attesa di repo GitHub.
- D1 parziale (**non compilato**): corretti in `driver.c`, `device.h`,
  `brb_transport.h/.c`, `codec_sbc.h/.c` i bug statici rispetto a PortCls/BTHDDI
  (`PORT_CLASS_DEVICE_EXTENSION_SIZE`, nessuna doppia `IoCompleteRequest` in
  `StartDevice`, `initguid.h`, `BTH_PROFILE_DRIVER_INTERFACE`, `<bthioctl.h>`,
  `CFG_MTU | CFG_FLUSHTO`, `PINDICATION_PARAMETERS`, helper intero
  `SontSbcBitrateKbpsInt` per evitare float nel kernel).
- Architettura confermata: function driver KS/PortCls su `BTHENUM\{0000110b}`.
- Trasporto kernel confermato dalla documentazione Microsoft: BTHDDI
  `GUID_BTHDDI_PROFILE_DRIVER_INTERFACE` + `IOCTL_INTERNAL_BTH_SUBMIT_BRB`
  (`BRB_L2CA_OPEN_CHANNEL`, PSM 0x0019/0x0017, `BRB_L2CA_ACL_TRANSFER`).
- Blocco per A2+: su questa macchina mancano Visual Studio Build Tools, WDK, SDK,
  `inf2cat`, `InfVerif` e `signtool`; nessun file `.sys` del driver e' stato
  compilato o caricato.
- Licenze: SBC e LDAC da AOSP (Apache-2.0) sono implementabili; AAC e aptX
  richiedono accordi commerciali.

## Open issues
- AAC/aptX: provare con FFmpeg installato (`winget install --id Gyan.FFmpeg -e`) e aggiungere test di round-trip reale.
- L2CAP reale non implementato (richiede che lo stack Bluetooth audio di Windows non occupi la connessione).
- `install-autostart.ps1` non e' stato eseguito (registra un task di sistema); va provato manualmente.

## Log
- 2026-11-15 Analisi statica del pacchetto di riferimento e archiviazione delle evidenze.
- 2026-11-15 Creato `report_analisi.md`.
- 2026-11-15 Corretto il parsing delle capability AVDTP (codec type in un ottavo separato) e reso il transport UDP tollerante a `WSAECONNRESET`: 76 test verdi.
- 2026-11-15 Rinominato il progetto in Sonatina, aggiunti README e script, verificati demo e percorsi negativi.
- 2026-11-15 Creato `ROADMAP.md`: backlog completo del driver (fasi A/D/S/G/T), motivazioni, fatti tecnici riutilizzabili, riferimenti pubblici e criteri di accettazione.
- 2026-11-15 Preparati A1 (`driver/tests/*`, `tests/test_driver_vectors.py`), A2 (`SonatinaAudio.vcxproj`, `build.ps1`), A6 (`driver/CONTRIBUTING.md`) e corretti bug PortCls/BTHDDI in `driver.c`/`brb_transport.c`/`codec_sbc.c`.
- 2026-11-15 Verificato A1/T1 con WinLibs `gcc 16.2.0`: `28 vectors + 5 math cases checked, 0 failures`; fix in `avdtp.c` (`uint8_t out[4]`) e auto-discovery WinGet in `run_host_tests.ps1`. A2 resta non compilato (manca WDK/VS Build Tools).
