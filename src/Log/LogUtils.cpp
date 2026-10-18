//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
// 
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "LogUtils.hpp"

#include <atomic>
#include <cstdlib>
#include <cstring>

#ifdef __ANDROID__
#include <chrono>
#include <iomanip>
#include <sstream>

namespace {
    struct FileLogger {
        std::string log_path_;
        std::ofstream out_;
        std::string tmp_path_;
        const uint32_t kNewOffset_ = 4 * 1024 * 1024;
        const uint32_t kMaxSize_ = 6 * 1024 * 1024;
        std::mutex file_mutex_;
        int write_count_ = 0;
        
        void RotateFile(const std::string& from, const std::string& dst, uint32_t offset) {
            std::ifstream in(from, std::ios::binary);
            std::ofstream out(dst, std::ios::binary | std::ios::trunc);
            in.seekg(offset, std::ios::beg);
            out << in.rdbuf();
            in.close();
            out.close();
        }
    };
    
    FileLogger g_fileLogger;
    bool g_useFileLogging = false;
}

std::string getTimeString() {
    auto now = std::chrono::system_clock::now();
    std::time_t t = std::chrono::system_clock::to_time_t(now);
    auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;
    std::tm tm = *std::localtime(&t);
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d %H:%M:%S");
    oss << '.' << std::setfill('0') << std::setw(3) << milliseconds.count();
    return oss.str();
}
#endif

namespace {
// Process-local lock: always taken, serialises the threads of this process.
std::mutex sg_logLocalMutex;
#ifdef _WIN32
// Named (cross-process) lock: only taken once this process has spawned a QAIAppSvc
// child or is a QAIAppSvc itself, because those processes share one stdout/console.
std::atomic<HANDLE> sg_logSharedMutex{nullptr};
std::once_flag sg_logSharedOnce;
#endif
// stdout is a file opened by SetLogLevel(level, log_path): the CRT fully buffers it, so
// without a flush per line INFO lines would only reach log_out.txt when the buffer fills
// and would be lost on os._exit or a native crash. Per-line flush matches the release.
std::atomic<bool> sg_stdoutRedirected{false};
}  // namespace

void qnn::log::utils::logSetStdoutRedirected() {
    sg_stdoutRedirected.store(true, std::memory_order_relaxed);
}

void qnn::log::utils::logCreateLock() {
    // Kept for API compatibility. The named mutex is created lazily by
    // logEnableCrossProcessLock(); single-process use needs no kernel object.
}

void qnn::log::utils::logEnableCrossProcessLock() {
#ifdef _WIN32
    std::call_once(sg_logSharedOnce, [] {
        HANDLE h = OpenMutexA(MUTEX_ALL_ACCESS, FALSE, "logStdoutCallbackSharedMutex");
        if (!h) {
            h = CreateMutexA(NULL, FALSE, "logStdoutCallbackSharedMutex");
        }
        // Publish under the local lock so no line is printed half under one lock regime.
        std::lock_guard<std::mutex> lock(sg_logLocalMutex);
        // Drain lines buffered under the single-process policy first, so they reach the
        // shared stdout before any line of the child that is about to be spawned.
        fflush(stdout);
        sg_logSharedMutex.store(h, std::memory_order_release);
    });
#endif
}

#ifdef _WIN32
extern std::string g_ProcName;
#endif

#ifdef __ANDROID__
void qnn::log::utils::setLogFilePath(const std::string& logPath) {
    if (logPath.empty()) {
        g_useFileLogging = false;
        return;
    }
    
    std::lock_guard<std::mutex> lock(g_fileLogger.file_mutex_);
    
    g_fileLogger.log_path_ = logPath;
    g_fileLogger.tmp_path_ = std::filesystem::path{logPath}.parent_path().append("tmp.log").generic_string();
    
    if (g_fileLogger.out_.is_open()) {
        g_fileLogger.out_.close();
    }
    
    g_fileLogger.out_.open(g_fileLogger.log_path_, std::ios::app | std::ios::binary);
    
    if (g_fileLogger.out_.is_open()) {
        g_useFileLogging = true;
        // Write initialization message
        g_fileLogger.out_ << "\n=== Log initialized at " << getTimeString() << " ===\n";
        g_fileLogger.out_.flush();
    } else {
        g_useFileLogging = false;
    }
}

std::string qnn::log::utils::getLogFilePath() {
    std::lock_guard<std::mutex> lock(g_fileLogger.file_mutex_);
    return g_fileLogger.log_path_;
}

void qnn::log::utils::logFileCallback(const char* fmt,
                                       QnnLog_Level_t level,
                                       uint64_t timestamp,
                                       va_list argp) {
    if (!g_useFileLogging || !g_fileLogger.out_.is_open()) {
        return;
    }
    
    const char* levelStr = "";
    switch (level) {
        case QNN_LOG_LEVEL_ERROR:
            levelStr = " [E] ";
            break;
        case QNN_LOG_LEVEL_WARN:
            levelStr = " [W] ";
            break;
        case QNN_LOG_LEVEL_INFO:
            levelStr = " [I] ";
            break;
        case QNN_LOG_LEVEL_DEBUG:
            levelStr = " [D] ";
            break;
        case QNN_LOG_LEVEL_VERBOSE:
            levelStr = " [V] ";
            break;
        default:
            levelStr = " [U] ";
            break;
    }
    
    std::lock_guard<std::mutex> lock(g_fileLogger.file_mutex_);
    
    // Write log entry
    g_fileLogger.out_ << levelStr;
    
    char buffer[2048];
    vsnprintf(buffer, sizeof(buffer), fmt, argp);
    g_fileLogger.out_ << buffer;
    
    if (buffer[strlen(buffer) - 1] != '\n') {
        g_fileLogger.out_ << '\n';
    }
    
    g_fileLogger.out_.flush();
    
    // Check if rotation is needed
    g_fileLogger.write_count_++;
    if (g_fileLogger.write_count_ >= 20 && 
        g_fileLogger.out_.tellp() >= g_fileLogger.kMaxSize_) {
        g_fileLogger.out_.close();
        
        g_fileLogger.RotateFile(g_fileLogger.log_path_,
                               g_fileLogger.tmp_path_,
                               g_fileLogger.kNewOffset_);
        
        g_fileLogger.RotateFile(g_fileLogger.tmp_path_,
                               g_fileLogger.log_path_,
                               0);
        
        g_fileLogger.out_.open(g_fileLogger.log_path_, std::ios::app | std::ios::binary);
        g_fileLogger.write_count_ = 0;
    }
}
#endif

// Builds "<header><message>\n" into buf (heap-allocated if it does not fit; caller
// frees when the result != buf). Always ends with exactly one '\n' unless the message
// already ends with one. va_list is consumed via va_copy so argp stays usable.
static char* formatLogLine(char* buf, size_t cap, size_t* outLen, const char* fmt, va_list argp,
                           const char* hdrFmt, ...) {
    va_list hdrArgs;
    va_start(hdrArgs, hdrFmt);
    char hdr[160];
    int h = vsnprintf(hdr, sizeof(hdr), hdrFmt, hdrArgs);
    va_end(hdrArgs);
    if (h < 0) h = 0;
    if ((size_t)h >= sizeof(hdr)) h = (int)sizeof(hdr) - 1;

    // Format straight into the stack buffer; only a message that does not fit is
    // formatted a second time into a heap buffer of the exact size.
    char* out = buf;
    size_t room = cap;
    memcpy(out, hdr, (size_t)h);
    va_list a1;
    va_copy(a1, argp);
    int w = vsnprintf(out + h, room - (size_t)h, fmt, a1);
    va_end(a1);
    if (w >= 0 && (size_t)h + (size_t)w + 2 > cap) {
        const size_t need = (size_t)h + (size_t)w + 2;  // + '\n' + '\0'
        char* big = static_cast<char*>(malloc(need));
        if (big) {
            memcpy(big, hdr, (size_t)h);
            va_list a2;
            va_copy(a2, argp);
            w = vsnprintf(big + h, need - (size_t)h, fmt, a2);
            va_end(a2);
            out = big;
            room = need;
        }
    }
    size_t len = (size_t)h + (w < 0 ? 0 : (size_t)w);
    if (len > room - 2) len = room - 2;  // truncated into the stack buffer (malloc failed)
    if (len == 0 || out[len - 1] != '\n') out[len++] = '\n';
    out[len] = '\0';
    *outLen = len;
    return out;
}

void qnn::log::utils::logStdoutCallback(const char* fmt,
                                        QnnLog_Level_t level,
                                        uint64_t timestamp,
                                        va_list argp) {
  const char* levelStr = "";
  switch (level) {
    case QNN_LOG_LEVEL_ERROR:
      levelStr = " ERROR ";
      break;
    case QNN_LOG_LEVEL_WARN:
      levelStr = "WARNING";
      break;
    case QNN_LOG_LEVEL_INFO:
      levelStr = "  INFO ";
      break;
    case QNN_LOG_LEVEL_DEBUG:
      levelStr = " DEBUG ";
      break;
    case QNN_LOG_LEVEL_VERBOSE:
      levelStr = "VERBOSE";
      break;
    case QNN_LOG_LEVEL_MAX:
      levelStr = "UNKNOWN";
      break;
  }

  double ms = (double)timestamp / 1000000.0;
  // To avoid interleaved messages
#ifdef _WIN32
  {    // zw: enhance the log print.
    std::lock_guard<std::mutex> localLock(sg_logLocalMutex);
    HANDLE shared = sg_logSharedMutex.load(std::memory_order_acquire);
    bool owned = true;
    if (shared) {
        DWORD dwWaitResult = WaitForSingleObject(shared, INFINITE);
        // WAIT_ABANDONED: a previous process died while holding the named mutex.
        // We now own it; proceed with logging rather than silently dropping output.
        owned = (WAIT_OBJECT_0 == dwWaitResult || WAIT_ABANDONED == dwWaitResult);
    }
    if (owned) {
        char stackBuf[1024];
        size_t len = 0;
        char* line = formatLogLine(stackBuf, sizeof(stackBuf), &len, fmt, argp,
                                   "%8.1fms [%s][%d][%-7s] ", ms, g_ProcName.c_str(),
                                   (int)GetCurrentProcessId(), levelStr);
        // One fwrite per record: with unbuffered stdout (python -u / PYTHONUNBUFFERED=1)
        // each stdio call is a separate WriteFile, so 3 calls per line cost 3 syscalls.
        fwrite(line, 1, len, stdout);
        if (line != stackBuf) free(line);
        // Flush policy:
        //  - ERROR/WARN: always, so a native crash or os._exit does not lose the lines that
        //    explain it (no WARN line is printed per inference, so this is cheap).
        //  - cross-process mode (shared mutex): always, so a buffered line of one process
        //    cannot be split by another process's output on the shared stdout.
        //  - stdout redirected to log_out.txt (SetLogLevel with log_path): always, so the
        //    file is complete on os._exit / native crash, as in the release.
        //  - INFO/DEBUG/VERBOSE in a single process on a console/pipe: left to stdio
        //    buffering; those levels print per-call lines, and a flush per line is one
        //    extra WriteFile each.
        if (shared || level == QNN_LOG_LEVEL_ERROR || level == QNN_LOG_LEVEL_WARN ||
            sg_stdoutRedirected.load(std::memory_order_relaxed)) {
            fflush(stdout);
        }
        if (shared) {
            ReleaseMutex(shared);
        }
    }
  }
#else
  {
    std::lock_guard<std::mutex> lock(sg_logLocalMutex);
    char stackBuf[1024];
    size_t len = 0;
    char* line = formatLogLine(stackBuf, sizeof(stackBuf), &len, fmt, argp,
                               "%8.1fms [%-7s] ", ms, levelStr);
    fwrite(line, 1, len, stdout);
    if (line != stackBuf) free(line);
    // Same policy as Windows; there is no cross-process stdout lock on this path.
    if (level == QNN_LOG_LEVEL_ERROR || level == QNN_LOG_LEVEL_WARN) {
        fflush(stdout);
    }
  }
#endif
}
