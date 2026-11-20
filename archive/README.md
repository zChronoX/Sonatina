# Archivio

Codice che Sonatina non usa più, tenuto come riferimento. Niente di questo viene compilato o
installato: l'app è tutta in `sonatina/host` (stack Bluetooth in user mode con Bumble), il
setup in `sonatina/installer`. I percorsi dentro questi file sono quelli di prima dello
spostamento (novembre 2026), quindi script e test qui non girano senza modifiche.

| Cartella | Cos'era | Perché è in archivio |
|---|---|---|
| `kernel-driver/driver` | Driver audio kernel (WaveRT + A2DP su BRB del Bluetooth di Windows), con test host e loopback | Richiede test signing con Secure Boot spento: incompatibile con Vanguard. Sostituito da `sonatina/host` (fase U). |
| `kernel-driver/service` | Servizio Windows che applicava i profili al driver | Serviva solo al driver. |
| `kernel-driver/scripts` | Installazione, stato e rimozione del driver, preparazione VM | Idem. `cleanup-kernel-driver.ps1` toglie un vecchio driver ancora installato. |
| `kernel-driver/Sonatina.wxs` | Pacchetto MSI (WiX) di driver + servizio | Sostituito da `sonatina/installer/sonatina.iss`. |
| `kernel-driver/driver-ci.yml` | Workflow CI del driver (era già solo manuale) | Fuori da `.github/workflows`: non parte più. |
| `python-stack` | Primo prototipo in Python puro: sorgente/sink su UDP loopback, servizio, GUI Tkinter, test | Superato da `sonatina/host`. |
| `notes` | PLAN, PROGRESS, ROADMAP delle fasi driver/prototipo | Storia del progetto. |
| `spike` | Prova U0 (Bumble su chiavetta dedicata) | Conclusa: è diventata `sonatina/host`. |

I sorgenti dei codec SBC e LDAC (`codec_sbc.*`, `codec_ldac.*`, `portable.h`, libsbc, libldac)
non sono qui: sono stati spostati in `sonatina/host/codec`, dove li usa `sonatina_codec.dll`.
Tutta la storia resta in git (`git log --follow <file>`).
