//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#include <cstdarg>
#include <cstdio>
#include <mutex>
#include <string>
#ifdef _WIN32
#include <windows.h>    // zw.
#endif
#include "QnnLog.h"

#ifdef __ANDROID__
#include <fstream>
#include <filesystem>
#endif

namespace qnn {
namespace log {
namespace utils {

void logStdoutCallback(const char* fmt, QnnLog_Level_t level, uint64_t timestamp, va_list argp);
void logCreateLock();

#ifdef __ANDROID__
// Android-specific file logging
void setLogFilePath(const std::string& logPath);
std::string getLogFilePath();
void logFileCallback(const char* fmt, QnnLog_Level_t level, uint64_t timestamp, va_list argp);
#endif

// Switch stdout logging to the cross-process named mutex. Call before spawning a
// QAIAppSvc child (it inherits stdout) and inside a QAIAppSvc process. Until then
// a process-local mutex is used (single-process case: no kernel round trip per line).
void logEnableCrossProcessLock();
// stdout was redirected to a log file (SetLogLevel(level, log_path)): flush every line.
void logSetStdoutRedirected();
}  // namespace utils
}  // namespace log
}  // namespace qnn
