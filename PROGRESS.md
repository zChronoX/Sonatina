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
- A2 completato via GitHub Actions CI (run #5, commit `650c4ce`):
  `msbuild SonatinaAudio.vcxproj /p:Platform=x64 /p:SignMode=Off` (Debug e
  Release) ha compilato e linkato `SonatinaAudio.sys` con 0 warning e 0 errori
  (WDK NuGet `10.0.28000.0`), e `InfVerif.exe /w sonatina\driver\inf\SonatinaAudio.inf`
  e' passato. `driver/build.ps1` aggiornato per build WDK locali.
- A5 completato: `.github/workflows/driver-ci.yml` esegue `python-and-host-tests`
  (77 test Python + A1 C vs Python) e `driver-compile-x64` (Debug/Release + `InfVerif /w`
  + upload artefatti).
- D1 parziale (compila e linka in WDK x64; caricamento su VM ancora da fare):
  corretto `portable.h` (tipi kernel senza CRT user-mode), ordine header
  `<ntifs.h>` / `<portcls.h>` / `<bthdef.h>` / `<bthsdpdef.h>` / `<bthioctl.h>`
  / `<bthddi.h>` / `<bthguid.h>` / `<initguid.h>`, `PcGetPhysicalDeviceObject`
  (2 argomenti), `L2CAP_CHANNEL_HANDLE *`, `FileDigestAlgorithm=sha256`,
  `EP\0` in `SonatinaAudio.inf`.
- D2 parziale (compila e linka con 0 warning e 0 errori in WDK CI Debug/Release
  x64, run #8 `c3b6513`; verifica runtime su VM in attesa di A3): vendoring
  minimale MS-PL di SysVAD in `driver/third_party/sysvad/` (`LICENSE`,
  `NOTICE.txt`, `NewDelete.h/.cpp`, `simple.h`), implementazione di
  `CMiniportTopologySonatina` (`src/miniport_topology.*`, ponte bridge +
  `KSPROPSETID_Jack` cuffie stereo) e `CMiniportWaveRTSonatina` /
  `CMiniportWaveRTStreamSonatina` (`src/miniport_wavert.*`, sink PCM 16-bit
  stereo 44.1-96 kHz, `EX_TIMER` ad alta risoluzione e progressione QPC intera),
  collegamento in `SontStartDevice` (`driver.c`) via `SontRegisterAudioSubdevices`,
  cleanup `IRP_MN_REMOVE_DEVICE` in `SontPnpDispatch`, `KSCATEGORY_REALTIME` +
  `Supports_EventDriven_Mode` in `SonatinaAudio.inf` e `SonatinaAudio.vcxproj`
  (`portcls.lib`, `stdunk.lib`, `libcntpr.lib`).
- D3 implementato lato host + kernel (compilazione WDK CI e verifica runtime su VM A3 da eseguire): aggiunti helper SEP/single-SEID e `SontAvdtpSelectSbcConfig` in `src/avdtp.*`, macchina a stati portabile `src/avdtp_session.h/.c` (Discover → GetCapabilities con probing SEID SNK e fallback → SetConfiguration → Open → Start + Suspend/Resume/Close/Abort), 36 vettori di parita' Python/C in `driver/tests/` + `test_driver_vectors.py`, ricezione L2CAP cancellabile `SontTransportReceive` in `src/brb_transport.*` e worker kernel `SontSignalingWorker` (`src/driver.c`, `src/device.h`) con stop pulito su `IRP_MN_SURPRISE_REMOVAL`/`IRP_MN_REMOVE_DEVICE`.
- Architettura confermata: function driver KS/PortCls su `BTHENUM\{0000110b}`.
- Trasporto kernel confermato dalla documentazione Microsoft: BTHDDI
  `GUID_BTHDDI_PROFILE_DRIVER_INTERFACE` + `IOCTL_INTERNAL_BTH_SUBMIT_BRB`
  (`BRB_L2CA_OPEN_CHANNEL`, PSM 0x0019/0x0017, `BRB_L2CA_ACL_TRANSFER`).
- Nota VM: nessun file `.sys` e' stato ancora caricato; prima di caricare driver
  kernel servono VM Windows + snapshot + `testsigning` + Driver Verifier (A3).
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
- 2026-11-15 Verificato A1/T1 con WinLibs `gcc 16.2.0`: `28 vectors + 5 math cases checked, 0 failures`; fix in `avdtp.c` (`uint8_t out[4]`) e auto-discovery WinGet in `run_host_tests.ps1`.
- 2026-11-15 Completati A2 + A5 in GitHub Actions CI (run #5, commit `650c4ce`): `SonatinaAudio.sys` compilato (Debug/Release x64) + `InfVerif /w` verdi + 77 test Python + test host A1 su clang verdi.
- 2026-11-15 Verificato D2 in WDK CI (run #8, commit `c3b6513`): `SonatinaAudio.sys` (WaveRT + Topology + BRB) compila e linka con 0 warning/errori; aggiunti `KSCATEGORY_REALTIME` + `Supports_EventDriven_Mode` in `SonatinaAudio.inf` e cleanup `IRP_MN_REMOVE_DEVICE` in `driver.c` durante l'audit pre-VM.
