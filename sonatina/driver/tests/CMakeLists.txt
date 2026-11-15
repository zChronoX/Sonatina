cmake_minimum_required(VERSION 3.16)
project(sonatina_host_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_executable(host_test
    host_test.c
    ../src/avdtp.c
    ../src/avdtp_session.c
    ../src/rtp.c
    ../src/codec_sbc.c
)

target_compile_definitions(host_test PRIVATE SONATINA_HOST)

if(MSVC)
    target_compile_options(host_test PRIVATE /W4 /WX)
else()
    target_compile_options(host_test PRIVATE -Wall -Wextra -Werror)
    if(NOT WIN32 OR MINGW)
        target_link_libraries(host_test PRIVATE m)
    endif()
endif()
