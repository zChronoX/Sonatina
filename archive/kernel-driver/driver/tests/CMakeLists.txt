cmake_minimum_required(VERSION 3.16)
project(sonatina_host_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_executable(host_test
    host_test.c
    ../src/abr.c
    ../src/avdtp.c
    ../src/avdtp_session.c
    ../src/rtp.c
    ../src/codec_sbc.c
    ../src/codec_ldac.c
    ../src/media_stream.c
    ../src/profile_validate.c
    ../src/trace_ring.c
    ../../service/apply_policy.c
    ../third_party/sbc/src/bits.c
    ../third_party/sbc/src/sbc.c
    ../third_party/ldac/src/ldaclib.c
    ../third_party/ldac/src/ldacBT.c
)

target_include_directories(host_test PRIVATE
    ../src
    ../third_party/sbc/include
    ../third_party/ldac/inc
    ../third_party/ldac/src
)
target_compile_definitions(host_test PRIVATE
    SONATINA_HOST
    SONATINA_WITH_LIBSBC
    SONATINA_WITH_LIBLDAC
    _32BIT_FIXED_POINT
)

if(MSVC)
    target_compile_options(host_test PRIVATE /W4 /WX)
    set_source_files_properties(../third_party/ldac/src/ldaclib.c ../third_party/ldac/src/ldacBT.c
        PROPERTIES COMPILE_OPTIONS "/wd4100;/wd4127;/wd4189;/wd4244;/wd4245;/wd4267;/wd4456;/wd4457;/wd4459;/wd4701;/wd4702;/wd4706")
else()
    target_compile_options(host_test PRIVATE -Wall -Wextra -Werror)
    if(NOT WIN32 OR MINGW)
        target_link_libraries(host_test PRIVATE m)
    endif()
endif()
