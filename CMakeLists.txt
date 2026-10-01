cmake_minimum_required(VERSION 3.16)

# ccache launcher must be set BEFORE project() so ESP-IDF picks it up.
# This is what makes repeat builds on cold GitHub runners near-instant:
# every unchanged .c file is served from ~/.cache/ccache instead of
# being recompiled from scratch.
find_program(CCACHE_PROGRAM ccache)
if(CCACHE_PROGRAM)
    set(CMAKE_C_COMPILER_LAUNCHER   ${CCACHE_PROGRAM})
    set(CMAKE_CXX_COMPILER_LAUNCHER ${CCACHE_PROGRAM})
    message(STATUS "ccache enabled: ${CCACHE_PROGRAM}")
else()
    message(STATUS "ccache not found - builds will be full rebuilds")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32-fastbuild)