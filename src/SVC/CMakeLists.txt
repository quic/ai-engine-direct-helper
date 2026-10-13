#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================

cmake_minimum_required(VERSION 3.4...3.18)
project(QAIAppSvc)

set(APP "QAIAppSvc")
set(APP_SOURCES "main.cpp")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

LINK_DIRECTORIES(../../lib/Release ../../lib/RelWithDebInfo ../../lib)

add_executable(${APP} ${APP_SOURCES})

SET(EXECUTABLE_OUTPUT_PATH "${PROJECT_SOURCE_DIR}/../../lib")

target_compile_definitions(${APP} PUBLIC "-DNOMINMAX")

target_include_directories(${APP} PUBLIC CachingUtil
                                         ../
                                         ./)

if (WIN32)
    # On Windows the import library is 'libappbuilder'.
    target_link_libraries(${PROJECT_NAME} PUBLIC libappbuilder)
    target_link_libraries(${APP} PRIVATE Shlwapi Shell32)
    set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} /MDd")
    set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} /MD /O2 /Ob2")
else()
    # On POSIX the shared library is 'appbuilder'.
    target_link_libraries(${APP} PRIVATE appbuilder pthread)
    if (NOT ANDROID)
        # shm_open / clock primitives live in librt on glibc.
        target_link_libraries(${APP} PRIVATE rt)
    endif()
    if (ANDROID)
        # ASharedMemory_create + __android_log_print.
        target_link_libraries(${APP} PRIVATE android log)
    endif()
endif()
# The installed ExecuTorch shared libraries reference additional kernel
# libraries without an embedded search path.  QAIAppSvc links through
# libappbuilder.so, so make those indirect dependencies visible both while
# linking the service and when running it from an SDK or package directory.
if (APPBUILDER_ENABLE_EXECUTORCH AND CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_options(${APP} PRIVATE "-Wl,-rpath-link,$ENV{EXECUTORCH_ROOT}/lib")
    # EXECUTORCH_ROOT is required by indirect dependencies as well.  DT_RPATH
    # remains transitive whereas DT_RUNPATH does not.
    target_link_options(${APP} PRIVATE "-Wl,--disable-new-dtags")
    set_target_properties(${APP} PROPERTIES
        BUILD_RPATH "$ENV{EXECUTORCH_ROOT}/lib;$ORIGIN/libs"
        INSTALL_RPATH "$ORIGIN/libs"
    )
endif()
