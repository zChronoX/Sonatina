/* Sonatina native driver - shared user/kernel contract.
 *
 * This header is included by the kernel driver and by the user-mode service and
 * configuration tool. Keep it free of kernel-only types so both sides can use
 * it unchanged.
 */
#ifndef SONATINA_PUBLIC_H
#define SONATINA_PUBLIC_H

#ifdef __cplusplus
extern "C" {
#endif

/* Private control interface exposed by the driver. The service and the GUI open
 * it with CreateFileW and talk to it through the IOCTLs below. */
DEFINE_GUID(GUID_DEVINTERFACE_SONATINA_CONTROL,
    0x6e1f2c7a, 0x9b4d, 0x4e6a, 0x8c, 0x31, 0x2f, 0x5b, 0x7d, 0x9a, 0x1e, 0x42);

#define SONATINA_MAX_CODECS 4

/* Codec identifiers, shared with the Python package. */
typedef enum _SONATINA_CODEC {
    SonatinaCodecPcm  = 0,
    SonatinaCodecSbc  = 1,
    SonatinaCodecAac  = 2,
    SonatinaCodecAptx = 3,
    SonatinaCodecLdac = 4
} SONATINA_CODEC;

typedef struct _SONATINA_PROFILE {
    ULONG Size;                 /* sizeof(SONATINA_PROFILE) */
    ULONG Codec;                /* SONATINA_CODEC */
    ULONG SbcSampling;          /* 16000/32000/44100/48000 */
    ULONG SbcChannelMode;       /* 0 mono, 1 dual, 2 stereo, 3 joint */
    ULONG SbcBlocks;            /* 4/8/12/16 */
    ULONG SbcSubbands;          /* 4/8 */
    ULONG SbcAllocation;        /* 0 SNR, 1 loudness */
    ULONG SbcMinBitpool;
    ULONG SbcMaxBitpool;
    ULONG AacSampling;
    ULONG AacChannels;
    ULONG AacBitrate;           /* kbit/s */
} SONATINA_PROFILE, *PSONATINA_PROFILE;

typedef struct _SONATINA_STATE {
    ULONG Size;
    ULONG Codec;                /* active codec */
    ULONG BitrateKbps;
    ULONG Opened;
    ULONG Packets;
    ULONG Errors;
    ULONG LostPackets;
    ULONG TxTimeUs;
    ULONG DelayMs;
    ULONG NegotiatedSampling;
    ULONG NegotiatedChannels;
} SONATINA_STATE, *PSONATINA_STATE;

typedef struct _SONATINA_DEVICE_INFO {
    ULONG Size;                 /* sizeof(SONATINA_DEVICE_INFO) */
    ULONGLONG RemoteAddress;    /* 48-bit Bluetooth address (BTH_ADDR) */
    CHAR AddressHex[16];        /* 12 uppercase hex digits + NUL */
} SONATINA_DEVICE_INFO, *PSONATINA_DEVICE_INFO;

#define SONATINA_DEVICE_TYPE 0x8331

#define IOCTL_SONATINA_GET_STATE \
    CTL_CODE(SONATINA_DEVICE_TYPE, 0x801, METHOD_BUFFERED, FILE_READ_ACCESS)
#define IOCTL_SONATINA_GET_PROFILE \
    CTL_CODE(SONATINA_DEVICE_TYPE, 0x802, METHOD_BUFFERED, FILE_READ_ACCESS)
#define IOCTL_SONATINA_SET_PROFILE \
    CTL_CODE(SONATINA_DEVICE_TYPE, 0x803, METHOD_BUFFERED, FILE_WRITE_ACCESS)
#define IOCTL_SONATINA_GET_STATS \
    CTL_CODE(SONATINA_DEVICE_TYPE, 0x804, METHOD_BUFFERED, FILE_READ_ACCESS)
#define IOCTL_SONATINA_RECONNECT \
    CTL_CODE(SONATINA_DEVICE_TYPE, 0x805, METHOD_NEITHER, FILE_WRITE_ACCESS)
#define IOCTL_SONATINA_GET_ADDRESS \
    CTL_CODE(SONATINA_DEVICE_TYPE, 0x806, METHOD_BUFFERED, FILE_READ_ACCESS)

#ifdef __cplusplus
}
#endif

#endif /* SONATINA_PUBLIC_H */
