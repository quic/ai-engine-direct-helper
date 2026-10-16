if (NOT KLEIDIAI_SRC_DIR OR NOT EXISTS "${KLEIDIAI_SRC_DIR}/kai")
    message(FATAL_ERROR "kleidiai_windows_clang.cmake: KLEIDIAI_SRC_DIR is not a KleidiAI source tree: ${KLEIDIAI_SRC_DIR}")
endif ()

set(_win_block
"    #if defined(_WIN32)
        #define KAI_ASM_GLOBAL(name) .global name
        #define KAI_ASM_FUNCTION_TYPE(name)
        #define KAI_ASM_FUNCTION_LABEL(name) name:
        #define KAI_ASM_FUNCTION_END(name)
        #define KAI_ASM_FUNCTION_USE(name) name
    #elif defined(__APPLE__)")

file(GLOB_RECURSE _asm_files "${KLEIDIAI_SRC_DIR}/kai/*.S")
set(_patched 0)
foreach (_f ${_asm_files})
    file(READ "${_f}" _txt)
    set(_orig "${_txt}")
    string(REGEX REPLACE "#if defined\\(_MSC_VER\\)([\r\n])" "#if defined(_MSC_VER) && !defined(__clang__)\\1" _txt "${_txt}")
    if (NOT _txt MATCHES "#if defined\\(_WIN32\\)")
        string(REPLACE "    #if defined(__APPLE__)" "${_win_block}" _txt "${_txt}")
    endif ()
    if (NOT _txt STREQUAL _orig)
        file(WRITE "${_f}" "${_txt}")
        math(EXPR _patched "${_patched} + 1")
    endif ()
endforeach ()
message(STATUS "kleidiai_windows_clang: patched ${_patched} assembly files")
