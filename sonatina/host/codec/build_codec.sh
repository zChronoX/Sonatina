#!/bin/sh
# Builds sonatina_codec.dll (Windows x64, mingw-w64) and/or sonatina_codec.so (Linux, tests)
# from the codec sources in src/ (SBC, LDAC wrappers, once part of the kernel driver), libsbc and
# libldac (AOSP), the vendored FDK AAC encoder and libfreeaptx (third_party/).
# Run from this directory.
#   sh build_codec.sh          # both
#   sh build_codec.sh so       # Linux only (CI tests)
#   sh build_codec.sh dll      # Windows only
set -e
TARGET=${1:-all}
FDK=third_party/fdk-aac
SRC="sonatina_codec.c src/codec_sbc.c src/codec_ldac.c \
     third_party/sbc/src/sbc.c third_party/sbc/src/bits.c \
     third_party/ldac/src/ldacBT.c third_party/ldac/src/ldaclib.c \
     third_party/libfreeaptx/freeaptx.c"
FDK_INC=$(for d in $FDK/lib*/include; do printf -- "-I%s " "$d"; done)
INC="-Ithird_party/libfreeaptx -Isrc -Ithird_party/sbc/include -Ithird_party/ldac/inc -Ithird_party/ldac/src $FDK_INC"
DEF="-DSONATINA_WITH_LIBSBC -DSONATINA_WITH_LIBLDAC -D_32BIT_FIXED_POINT"
FDK_FLAGS="-O2 -fno-exceptions -fno-rtti -w $FDK_INC"

# build <cc> <cxx> <extra cflags> <objdir> <output> <link flags>
build() {
    CC=$1; CXX=$2; EXTRA=$3; OBJ=$4; OUT=$5; LDFLAGS=$6
    mkdir -p "$OBJ"
    objs=""
    for f in $(cat $FDK/sources.txt); do
        o="$OBJ/$(echo "$f" | tr '/' '_' | sed 's/\.cpp$/.o/')"
        if [ ! -f "$o" ] || [ "$FDK/$f" -nt "$o" ]; then
            $CXX $FDK_FLAGS $EXTRA -c "$FDK/$f" -o "$o"
        fi
        objs="$objs $o"
    done
    for f in $SRC; do
        o="$OBJ/$(basename "$f" .c).o"
        $CC -O2 $EXTRA $DEF $INC -c "$f" -o "$o"
        objs="$objs $o"
    done
    $CXX -shared $objs -o "$OUT" $LDFLAGS
}

if [ "$TARGET" = all ] || [ "$TARGET" = dll ]; then
    build x86_64-w64-mingw32-gcc x86_64-w64-mingw32-g++ "" ../../../build/codec-obj-win \
        sonatina_codec.dll "-static-libgcc -static-libstdc++ -Wl,--kill-at -s"
fi
if [ "$TARGET" = all ] || [ "$TARGET" = so ]; then
    build gcc g++ "-fPIC" ../../../build/codec-obj-linux sonatina_codec.so "-lm"
fi
echo built
