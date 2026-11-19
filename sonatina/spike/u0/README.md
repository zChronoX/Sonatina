# U0 – prova di fattibilità: stack Bluetooth user-mode (Bumble) su chiavetta dedicata

Obiettivo: dimostrare che una chiavetta USB con driver WinUSB + Bumble (Python)
riesce ad abbinarsi alle CMF Headphone Pro e a riprodurre audio A2DP, con
Secure Boot attivo e senza alcun driver kernel nostro.

Chiavetta di prova: Realtek Bluetooth 5.0 Adapter `0BDA:8771` (RTL8761BUV,
supportata dal driver `rtk` di Bumble, serve il firmware `rtl8761bu`).
La UGREEN (Barrot `33FA:0012`) resta a Windows con il driver kernel Sonatina.

File di prova (generati, 64 s, accordi/arpeggio, 48 kHz stereo):
- `test.sbc`: SBC 48 kHz, stereo, 16 blocchi, 8 sottobande, bitpool 27 (il massimo delle CMF)
- `test.aac`: AAC-LC ADTS 48 kHz stereo 256 kbit/s

Esito atteso:
1. `bumble-rtk-util info` mostra il controller con firmware caricato.
2. `pair` completa l'abbinamento.
3. `inquire` elenca gli endpoint delle cuffie (SBC, AAC, LDAC).
4. `play` riproduce test.sbc e test.aac.

Rollback chiavetta: Gestione dispositivi → Dispositivi USB (Universal Serial
Bus devices) → Realtek → Disinstalla dispositivo (spuntare "Tenta di rimuovere
il driver") → scollegare e ricollegare: torna a Windows come Bluetooth.
