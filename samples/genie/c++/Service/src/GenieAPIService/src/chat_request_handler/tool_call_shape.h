//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#ifndef TOOL_CALL_SHAPE_H
#define TOOL_CALL_SHAPE_H

#include <nlohmann/json.hpp>
#include <algorithm>
#include <limits>
#include <string>

using json = nlohmann::ordered_json;

namespace tool_shape {

constexpr size_t kResourceValueMaxBytes = 1024;
constexpr size_t kWritePayloadMinBytes = 24;
constexpr size_t kArgsTextCandidates = 32;

inline json ParseArgsValue(const json& arguments)
{
    if (arguments.is_null()) return json();
    json a = arguments;
    if (a.is_string()) {
        try { a = json::parse(a.get<std::string>()); } catch (...) { return json(); }
    }
    return a.is_object() ? a : json();
}

inline json ParseToolCallArgs(const json& tool_call)
{
    if (!tool_call.is_object()) return json();
    const json& fn = (tool_call.contains("function") && tool_call["function"].is_object())
        ? tool_call["function"] : tool_call;
    if (!fn.is_object() || !fn.contains("arguments")) return json();
    return ParseArgsValue(fn["arguments"]);
}

inline bool IsResourceLikeValue(const std::string& v)
{
    if (v.empty() || v.size() > kResourceValueMaxBytes) return false;
    if (v.find_first_of(" \t\r\n") != std::string::npos) return false;
    return v.find('/') != std::string::npos || v.find('\\') != std::string::npos;
}

inline std::string ResourceArgKey(const json& args)
{
    if (!args.is_object()) return std::string();
    for (auto it = args.begin(); it != args.end(); ++it) {
        if (!it.value().is_string()) continue;
        if (IsResourceLikeValue(it.value().get<std::string>())) return it.key();
    }
    return std::string();
}

inline void ScanStringPayload(const json& v, size_t& max_len, size_t& nonempty)
{
    if (v.is_string()) {
        const size_t n = v.get<std::string>().size();
        if (n == 0) return;
        ++nonempty;
        if (n > max_len) max_len = n;
        return;
    }
    if (!v.is_array() && !v.is_object()) return;
    for (auto it = v.begin(); it != v.end(); ++it) ScanStringPayload(it.value(), max_len, nonempty);
}

inline bool IsWriteLikeArgs(const json& args, const std::string& resource_key)
{
    if (!args.is_object()) return false;
    size_t max_len = 0;
    size_t nonempty = 0;
    for (auto it = args.begin(); it != args.end(); ++it) {
        if (it.key() == resource_key) continue;
        ScanStringPayload(it.value(), max_len, nonempty);
    }
    return max_len >= kWritePayloadMinBytes || nonempty >= 2;
}

inline bool IsWriteLikeCall(const json& assistant_msg)
{
    if (!assistant_msg.is_object() || !assistant_msg.contains("tool_calls") ||
        !assistant_msg["tool_calls"].is_array()) return false;
    for (const auto& tc : assistant_msg["tool_calls"]) {
        const json args = ParseToolCallArgs(tc);
        const std::string resource_key = ResourceArgKey(args);
        if (resource_key.empty()) continue;
        if (IsWriteLikeArgs(args, resource_key)) return true;
    }
    return false;
}

inline std::string ReadResourceOfArgs(const json& args)
{
    const std::string resource_key = ResourceArgKey(args);
    if (resource_key.empty() || IsWriteLikeArgs(args, resource_key)) return std::string();
    return args.at(resource_key).get<std::string>();
}

inline void ScanLongestString(const json& v, std::string& best)
{
    if (v.is_string()) {
        const std::string s = v.get<std::string>();
        if (s.size() > best.size()) best = s;
        return;
    }
    if (!v.is_array() && !v.is_object()) return;
    for (auto it = v.begin(); it != v.end(); ++it) ScanLongestString(it.value(), best);
}

inline std::string WriteResourceOfArgs(const json& args)
{
    const std::string resource_key = ResourceArgKey(args);
    if (resource_key.empty() || !IsWriteLikeArgs(args, resource_key)) return std::string();
    return args.at(resource_key).get<std::string>();
}

inline std::string WritePayloadOfArgs(const json& args)
{
    const std::string resource_key = ResourceArgKey(args);
    std::string best;
    if (resource_key.empty() || !args.is_object()) return best;
    for (auto it = args.begin(); it != args.end(); ++it) {
        if (it.key() == resource_key) continue;
        ScanLongestString(it.value(), best);
    }
    return best;
}

inline bool IsExecuteLikeArgs(const json& args)
{
    return args.is_object() && !args.empty() && ResourceArgKey(args).empty();
}

inline std::string ExecutePayloadOfArgs(const json& args)
{
    std::string best;
    if (args.is_object()) ScanLongestString(args, best);
    return best;
}

inline std::string ExecuteContextOfArgs(const json& args, const std::string& payload)
{
    std::string out;
    if (!args.is_object()) return out;
    bool payload_skipped = false;
    for (auto it = args.begin(); it != args.end(); ++it) {
        if (!it.value().is_string()) continue;
        const std::string v = it.value().get<std::string>();
        if (v.empty()) continue;
        if (!payload_skipped && v == payload) {
            payload_skipped = true;
            continue;
        }
        if (!out.empty()) out += '\x1f';
        out += v;
    }
    return out;
}

inline long long ArgsInt(const json& args, const char* key, long long fallback)
{
    if (!args.is_object() || !args.contains(key)) return fallback;
    const json& v = args.at(key);
    if (v.is_number_integer()) return v.get<long long>();
    if (v.is_number_float()) return static_cast<long long>(v.get<double>());
    if (v.is_string()) {
        try { return std::stoll(v.get<std::string>()); } catch (...) { return fallback; }
    }
    return fallback;
}

constexpr const char* kReadSpanBeginKeys[] = {"offset", "start_line", "line_start", "startLine"};
constexpr const char* kReadSpanEndKeys[] = {"end_line", "line_end", "endLine"};
constexpr const char* kReadSpanLengthKeys[] = {"limit", "lines"};
constexpr const char* kReadSpanOpaqueKeys[] = {"line_range", "range"};

template <size_t N>
inline bool ArgsHaveAnyKey(const json& args, const char* const (&keys)[N])
{
    if (!args.is_object()) return false;
    for (size_t i = 0; i < N; ++i)
        if (args.contains(keys[i]) && !args.at(keys[i]).is_null()) return true;
    return false;
}

template <size_t N>
inline long long ArgsIntAnyKey(const json& args, const char* const (&keys)[N], long long fallback)
{
    for (size_t i = 0; i < N; ++i) {
        const long long v = ArgsInt(args, keys[i], std::numeric_limits<long long>::min());
        if (v != std::numeric_limits<long long>::min()) return v;
    }
    return fallback;
}

inline bool ArgsHaveReadSpan(const json& args)
{
    return ArgsHaveAnyKey(args, kReadSpanBeginKeys) || ArgsHaveAnyKey(args, kReadSpanEndKeys) ||
           ArgsHaveAnyKey(args, kReadSpanLengthKeys) || ArgsHaveAnyKey(args, kReadSpanOpaqueKeys);
}

inline void ReadSpanOfArgs(const json& args, long long& span_begin, long long& span_end)
{
    span_begin = std::max<long long>(1, ArgsIntAnyKey(args, kReadSpanBeginKeys, 1));
    const long long length = ArgsIntAnyKey(args, kReadSpanLengthKeys, -1);
    const long long end_line = ArgsIntAnyKey(args, kReadSpanEndKeys, -1);
    if (length > 0) span_end = span_begin + length - 1;
    else if (end_line >= span_begin) span_end = end_line;
    else if (!ArgsHaveAnyKey(args, kReadSpanBeginKeys) && ArgsHaveReadSpan(args)) span_end = span_begin - 1;
    else span_end = std::numeric_limits<long long>::max();
}

inline long long ReadBeginOfArgs(const json& args)
{
    long long begin = 1;
    long long end = 0;
    ReadSpanOfArgs(args, begin, end);
    return begin;
}

inline size_t BalancedObjectEnd(const std::string& text, size_t open_pos)
{
    size_t depth = 0;
    bool in_string = false;
    bool escaped = false;
    for (size_t i = open_pos; i < text.size(); ++i) {
        const char c = text[i];
        if (in_string) {
            if (escaped) escaped = false;
            else if (c == '\\') escaped = true;
            else if (c == '"') in_string = false;
            continue;
        }
        if (c == '"') in_string = true;
        else if (c == '{') ++depth;
        else if (c == '}') {
            if (--depth == 0) return i + 1;
        }
    }
    return std::string::npos;
}

inline json ParseArgsObjectFromText(const std::string& text)
{
    size_t from = 0;
    size_t tried = 0;
    while (tried < kArgsTextCandidates) {
        const size_t open_pos = text.find('{', from);
        if (open_pos == std::string::npos) break;
        ++tried;
        from = open_pos + 1;
        const size_t end = BalancedObjectEnd(text, open_pos);
        if (end == std::string::npos) break;
        json parsed;
        try { parsed = json::parse(text.substr(open_pos, end - open_pos)); } catch (...) { continue; }
        if (!parsed.is_object()) continue;
        json args = parsed.contains("arguments") ? ParseArgsValue(parsed["arguments"]) : parsed;
        if (args.is_object() && !ResourceArgKey(args).empty()) return args;
    }
    return json();
}

inline json ParseAnyArgsObjectFromText(const std::string& text)
{
    size_t from = 0;
    size_t tried = 0;
    while (tried < kArgsTextCandidates) {
        const size_t open_pos = text.find('{', from);
        if (open_pos == std::string::npos) break;
        ++tried;
        from = open_pos + 1;
        const size_t end = BalancedObjectEnd(text, open_pos);
        if (end == std::string::npos) break;
        json parsed;
        try { parsed = json::parse(text.substr(open_pos, end - open_pos)); } catch (...) { continue; }
        if (!parsed.is_object()) continue;
        json args = parsed.contains("arguments") ? ParseArgsValue(parsed["arguments"]) : parsed;
        if (args.is_object() && !args.empty()) return args;
    }
    return json();
}

}  // namespace tool_shape

#endif // TOOL_CALL_SHAPE_H
