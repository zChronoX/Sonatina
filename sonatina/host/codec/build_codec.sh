#!/bin/sh
# Builds sonatina_codec.dll (Windows x64, mingw-w64) and/or sonatina_codec.so (Linux, tests)
# from the driver's codec sources. Run from this directory.
#   sh build_codec.sh          # both
#   sh build_codec.sh so       # Linux only (CI tests)
#   sh build_codec.sh dll      # Windows only
set -e
TARGET=${1:-all}
D=../../driver
SRC="sonatina_codec.c $D/src/codec_sbc.c $D/src/codec_ldac.c \
     $D/third_party/sbc/src/sbc.c $D/third_party/sbc/src/bits.c \
     $D/third_party/ldac/src/ldacBT.c $D/third_party/ldac/src/ldaclib.c"
INC="-I$D/src -I$D/third_party/sbc/include -I$D/third_party/ldac/inc -I$D/third_party/ldac/src"
DEF="-DSONATINA_WITH_LIBSBC -DSONATINA_WITH_LIBLDAC -D_32BIT_FIXED_POINT"
if [ "$TARGET" = all ] || [ "$TARGET" = dll ]; then
    x86_64-w64-mingw32-gcc -O2 -shared -static-libgcc $DEF $INC $SRC -o sonatina_codec.dll -Wl,--kill-at
fi
if [ "$TARGET" = all ] || [ "$TARGET" = so ]; then
    gcc -O2 -shared -fPIC $DEF $INC $SRC -o sonatina_codec.so -lm
fi
echo built
