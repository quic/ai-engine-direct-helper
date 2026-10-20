//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "task_memo_builder.h"
#include "tool_call_shape.h"
#include "tool_call_circuit_breaker_store.h"
#include <utils.h>
#include "log.h"
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <functional>
#include <cstring>
#include <cctype>
#include <chrono>
#include <mutex>
#include <unordered_map>

TaskMemoBuilder::TaskMemoBuilder(
    const PromptOptimizationConfig::TaskMemoConfig& config,
    const ModelInstanceConfig& instance_config,
    TaskMemoStore* store,
    InferFn infer_fn,
    IsAliveFn is_alive_fn)
    : config_(config)
    , instance_config_(instance_config)
    , store_(store)
    , infer_fn_(std::move(infer_fn))
    , is_alive_fn_(std::move(is_alive_fn))
{
}

// ── 会话指纹（技法对齐 GenieRoutingGateway::ComputeHistoryFingerprint/ComputeFirstMsgKey，
//    本地独立实现，不依赖 GenieRoutingGateway 模块，前缀 tmfp_/tmfmk_ 与 gateway 自身的
//    fmk_ 区分，避免日志排查时误认为同一个哈希空间）──────────────────────────────

std::string TaskMemoBuilder::ComputeFingerprint(const json& messages)
{
    if (!messages.is_array() || messages.empty())
        return "";

    std::vector<const json*> non_system;
    for (const auto& msg : messages) {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }
    if (non_system.size() <= 1)
        return "";

    uint64_t hash = 14695981039346656037ULL;
    const uint64_t prime = 1099511628211ULL;
    auto hash_string = [&](const std::string& s) {
        for (unsigned char c : s) { hash ^= c; hash *= prime; }
        hash ^= 0xFF; hash *= prime;
    };

    for (size_t i = 0; i < non_system.size() - 1; ++i) {
        const auto& msg = *non_system[i];
        hash_string(msg.value("role", ""));
        if (msg.contains("content")) {
            hash_string(msg["content"].is_string() ? msg["content"].get<std::string>() : msg["content"].dump());
        }
    }

    std::ostringstream oss;
    oss << "tmfp_" << std::hex << std::setfill('0') << std::setw(16) << hash;
    return oss.str();
}

std::string TaskMemoBuilder::ComputeFirstMsgKey(const json& messages)
{
    if (!messages.is_array() || messages.empty())
        return "";

    std::vector<const json*> non_system;
    for (const auto& msg : messages) {
        const std::string role = msg.value("role", "");
        if (role != "system" && role != "developer")
            non_system.push_back(&msg);
    }
    if (non_system.empty() || non_system[0]->value("role", "") != "user")
        return "";

    uint64_t h = 14695981039346656037ULL;
    const uint64_t p = 1099511628211ULL;
    auto hash_string = [&](const std::string& s) {
        for (unsigned char c : s) { h ^= c; h *= p; }
        h ^= 0xFF; h *= p;
    };
    hash_string("user");
    std::string content_str;
    if (non_system[0]->contains("content")) {
        content_str = (*non_system[0])["content"].is_string()
            ? (*non_system[0])["content"].get<std::string>()
            : (*non_system[0])["content"].dump();
    }
    hash_string(content_str);

    std::ostringstream oss;
    oss << "tmfmk_" << std::hex << std::setfill('0') << std::setw(16) << h;
    return oss.str();
}

// ── Lookup ────────────────────────────────────────────────────────────────────

std::optional<json> TaskMemoBuilder::Lookup(const json& raw_messages) const
{
    if (!config_.enabled || !store_)
        return std::nullopt;
    auto entry = store_->Lookup(ComputeFingerprint(raw_messages), ComputeFirstMsgKey(raw_messages));
    if (entry)
        SanitizeSedimentMarkers(*entry);
    return entry;
}

void TaskMemoBuilder::SanitizeSedimentMarkers(json& entry) const
{
    if (!entry.is_object())
        return;
    const auto& sed = config_.sediment;
    if (!sed.enabled) {
        entry.erase("_block_line_truncate");
    }
    if (!sed.enabled || !sed.registry.enabled) {
        entry.erase("_registry_render");
    }
    if (!sed.enabled || !sed.merge_paged_reads) {
        entry.erase("_read_merge");
        entry.erase("_read_page_lines");
    }
    if (!sed.enabled || sed.card.mode == "off" || sed.card.shadow_only) {
        entry.erase("_cards_render");
        entry.erase("_cards_before_memo");
    } else if (sed.card.placement != "before_memo") {
        entry.erase("_cards_before_memo");
    }
}

// ── BuildRuleLayer ────────────────────────────────────────────────────────────

namespace {
    constexpr size_t kMaxItemsPerField = 20;
    constexpr size_t kMaxHardRules = 12;
    constexpr size_t kMaxHardRulesPerFile = 6;
    constexpr size_t kMaxHardRuleChars = 220;
    constexpr size_t kMaxArtifacts = 8;
    constexpr size_t kMaxArtifactLinesPerResult = 3;
    constexpr size_t kMaxLastSuccessChars = 900;

    std::string FullToolCall(const std::string& assistant_content) {
        std::string visible = TaskMemoBuilder::StripThinkBlock(assistant_content);
        const size_t tc = visible.find("<tool_call>");
        if (tc == std::string::npos)
            return "";
        const size_t end = visible.find("</tool_call>", tc);
        std::string body = visible.substr(tc + 11, end == std::string::npos ? std::string::npos : end - tc - 11);
        try {
            json j = json::parse(body);
            return j.value("name", "") + " " + (j.contains("arguments") ? j["arguments"].dump() : std::string("{}"));
        } catch (...) {
            for (char& c : body)
                if (c == '\n' || c == '\r') c = ' ';
            return body;
        }
    }

    std::vector<std::string> ExtractArtifactLines(const std::string& content) {
        static const char* kVerbs[] = {"saved", "written", "created", "generated", "exported", "wrote", "已保存", "已生成"};
        std::vector<std::string> out;
        size_t pos = 0;
        while (pos < content.size() && out.size() < kMaxArtifactLinesPerResult) {
            size_t nl = content.find('\n', pos);
            if (nl == std::string::npos) nl = content.size();
            std::string line = content.substr(pos, nl - pos);
            pos = nl + 1;
            std::string lower = line;
            std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return (char) tolower(c); });
            bool verb = false;
            for (const char* v : kVerbs)
                if (lower.find(v) != std::string::npos) { verb = true; break; }
            const bool path = line.find(":\\") != std::string::npos || line.find(":/") != std::string::npos ||
                              line.find(" /") != std::string::npos;
            if (!verb || !path)
                continue;
            const size_t b = line.find_first_not_of(" \t\r");
            line = line.substr(b == std::string::npos ? 0 : b);
            while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
            out.push_back(safe_utf8_truncate(line, kMaxHardRuleChars, "..."));
        }
        return out;
    }

    bool IsHardRuleLine(const std::string& line) {
        static const char* kMarkers[] = {"NEVER", "MUST", "MANDATORY", "Do NOT", "DO NOT",
                                         "必须", "禁止", "不得", "严禁"};
        for (const char* k : kMarkers)
            if (line.find(k) != std::string::npos) return true;
        return false;
    }

    std::vector<std::string> ExtractHardRules(const std::string& content) {
        std::vector<std::string> out;
        size_t pos = 0;
        while (pos < content.size() && out.size() < kMaxHardRulesPerFile) {
            size_t nl = content.find('\n', pos);
            if (nl == std::string::npos) nl = content.size();
            std::string line = content.substr(pos, nl - pos);
            pos = nl + 1;
            const size_t b = line.find_first_not_of(" \t\r>-*#|");
            if (b == std::string::npos) continue;
            line = line.substr(b);
            while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
            if (line.size() < 8 || !IsHardRuleLine(line)) continue;
            out.push_back(safe_utf8_truncate(line, kMaxHardRuleChars, "..."));
        }
        return out;
    }

    void AppendCapped(json& field, const std::string& item, size_t cap) {
        if (!field.is_array())
            field = json::array();
        for (const auto& existing : field) {
            if (existing.is_string() && existing.get<std::string>() == item)
                return;
        }
        field.push_back(item);
        while (field.size() > cap)
            field.erase(field.begin());
    }

    bool HasNonEmptyErrorField(const std::string& content, const char* key) {
        size_t pos = 0;
        while ((pos = content.find(key, pos)) != std::string::npos) {
            size_t cursor = pos + strlen(key);
            while (cursor < content.size() && (content[cursor] == ' ' || content[cursor] == '\t'))
                cursor++;
            if (cursor < content.size() && content[cursor] == ':') {
                cursor++;
                while (cursor < content.size() && (content[cursor] == ' ' || content[cursor] == '\t'))
                    cursor++;
                static const char* kFalsy[] = { "null", "false", "0", "\"\"" };
                bool is_falsy = false;
                for (const char* f : kFalsy) {
                    size_t flen = strlen(f);
                    if (content.compare(cursor, flen, f) == 0)
                        is_falsy = true;
                }
                if (!is_falsy)
                    return true;
            }
            pos += strlen(key);
        }
        return false;
    }

    bool LooksLikeFailure(const std::string& content) {
        static const char* kSignals[] = {
            "Error:", "ERROR:", "Exception:", "Traceback (most recent call last)",
            "No such file or directory",
            "Permission denied", "command not found", "is not recognized as",
            "Failed to", "failed to", "Failed:",
        };
        for (const char* sig : kSignals) {
            if (content.find(sig) != std::string::npos)
                return true;
        }
        if (HasNonEmptyErrorField(content, "\"error\"") || HasNonEmptyErrorField(content, "\"is_error\""))
            return true;
        return false;
    }

    void CapObjectSize(json& obj, size_t cap) {
        if (!obj.is_object())
            return;
        while (obj.size() > cap)
            obj.erase(obj.begin());
    }

    bool IsReadLikeToolName(const std::string& name) {
        std::string lower = name;
        std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return (char)tolower(c); });
        // exec 类工具（即便名字含 "read"，如某些 shell 别名）不计入，由调用方对 exec 结果
        // 走正常的失败/成功摘要路径，不参与"已读文件索引"。
        return lower.find("read") != std::string::npos && lower.find("exec") == std::string::npos;
    }

    // 从承载 tool_call 的 assistant 消息文本里抽取 read 类工具调用的路径参数；兼容 Harmony
    // （to=functions.NAME ... {"path":"..."}）与 general 格式（<tool_call>{"name":"NAME",
    // "arguments":{"path":"..."}}</tool_call>）两种拼接方式。只用子串朴素扫描，不引入 <regex>，
    // 与本文件其余解析风格一致；误判的代价只是漏报/多报一次提示，不影响正确性。
    std::string ExtractReadToolPathImpl(const std::string& assistant_content) {
        std::string name;
        size_t search_from = 0;
        size_t name_pos = assistant_content.find("to=functions.");
        if (name_pos != std::string::npos) {
            size_t start = name_pos + strlen("to=functions.");
            size_t end = start;
            while (end < assistant_content.size() &&
                   (isalnum((unsigned char) assistant_content[end]) || assistant_content[end] == '_'))
                ++end;
            name = assistant_content.substr(start, end - start);
            search_from = end;
        } else {
            size_t np = assistant_content.find("\"name\"");
            if (np != std::string::npos) {
                size_t colon = assistant_content.find(':', np);
                size_t q1 = (colon == std::string::npos) ? std::string::npos : assistant_content.find('"', colon + 1);
                size_t q2 = (q1 == std::string::npos) ? std::string::npos : assistant_content.find('"', q1 + 1);
                if (q1 != std::string::npos && q2 != std::string::npos) {
                    name = assistant_content.substr(q1 + 1, q2 - q1 - 1);
                    search_from = q2;
                }
            }
        }
        if (name.empty() || !IsReadLikeToolName(name))
            return "";

        static const char* kPathKeys[] = { "\"path\"", "\"file\"", "\"file_path\"", "\"filename\"" };
        for (const char* key : kPathKeys) {
            size_t kp = assistant_content.find(key, search_from);
            if (kp == std::string::npos)
                continue;
            size_t colon = assistant_content.find(':', kp);
            size_t q1 = (colon == std::string::npos) ? std::string::npos : assistant_content.find('"', colon + 1);
            size_t q2 = (q1 == std::string::npos) ? std::string::npos : assistant_content.find('"', q1 + 1);
            if (q1 != std::string::npos && q2 != std::string::npos)
                return assistant_content.substr(q1 + 1, q2 - q1 - 1);
        }
        return "";
    }

    constexpr size_t kMaxCardSegments = 24;
    constexpr size_t kMaxCardHeadingBytes = 80;

    long long ArgsIntOfText(const std::string& text, const char* key, long long fallback) {
        const std::string pat = std::string("\"") + key + "\"";
        const size_t kp = text.find(pat);
        if (kp == std::string::npos)
            return fallback;
        const size_t colon = text.find(':', kp + pat.size());
        if (colon == std::string::npos)
            return fallback;
        size_t c = colon + 1;
        while (c < text.size() && (text[c] == ' ' || text[c] == '\t' || text[c] == '"'))
            ++c;
        if (c >= text.size() || !isdigit((unsigned char) text[c]))
            return fallback;
        long long v = 0;
        while (c < text.size() && isdigit((unsigned char) text[c]))
            v = v * 10 + (text[c++] - '0');
        return v;
    }

    long long ReadOffsetOfText(const std::string& text) {
        long long v = ArgsIntOfText(text, "offset", -1);
        if (v < 0)
            v = ArgsIntOfText(text, "start_line", -1);
        return v < 1 ? 1 : v;
    }

    std::string ShapeReadResourceOfText(const std::string& text) {
        return tool_shape::ReadResourceOfArgs(tool_shape::ParseArgsObjectFromText(text));
    }

    size_t CountLinesOf(const std::string& s) {
        if (s.empty())
            return 0;
        size_t n = 0;
        for (char c : s)
            if (c == '\n') ++n;
        if (s.back() != '\n')
            ++n;
        return n;
    }

    std::string RightTrimLine(const std::string& s) {
        std::string out = s;
        while (!out.empty() && (out.back() == '\r' || out.back() == ' ' || out.back() == '\t'))
            out.pop_back();
        return out;
    }

    bool HasEnumeratorPrefix(const std::string& body) {
        size_t c = 0;
        while (c < body.size() && isdigit((unsigned char) body[c]))
            ++c;
        if (c > 0 && c < body.size()) {
            if (body[c] == '.' || body[c] == ')' || body[c] == ':')
                return c + 1 >= body.size() || body[c + 1] == ' ' || body[c + 1] == '\t';
            if (body.compare(c, 3, "\xe3\x80\x81") == 0)
                return true;
        }
        static const char* kBullets[] = { "- ", "* ", "+ ", "\xe2\x80\xa2 ", "\xe3\x83\xbb" };
        for (const char* b : kBullets)
            if (body.rfind(b, 0) == 0)
                return true;
        return false;
    }

    bool LooksLikeCommandBody(const std::string& body, size_t indent) {
        if (body.rfind("$ ", 0) == 0 || body.rfind("> ", 0) == 0 || body.rfind("```", 0) == 0 ||
            body.rfind("~~~", 0) == 0)
            return true;
        if (body.find(" --") != std::string::npos)
            return true;
        if (indent >= 4 && (body.find('/') != std::string::npos || body.find('\\') != std::string::npos))
            return true;
        return false;
    }

    bool LooksLikeHeadingBody(const std::string& body, const std::string& next_line, size_t indent) {
        if (body.size() > kMaxCardHeadingBytes)
            return false;
        const char tail = body.back();
        if (tail == '.' || tail == ',' || tail == ';')
            return false;
        if (body.size() >= 3) {
            const std::string t = body.substr(body.size() - 3);
            if (t == "\xe3\x80\x82" || t == "\xef\xbc\x8c" || t == "\xef\xbc\x9b")
                return false;
        }
        const std::string next = RightTrimLine(next_line);
        if (next.empty())
            return true;
        const size_t next_indent = next.find_first_not_of(" \t");
        return next_indent != std::string::npos && next_indent > indent;
    }

    bool IsStructuralLine(const std::string& line, const std::string& next_line) {
        const std::string trimmed = RightTrimLine(line);
        const size_t indent = trimmed.find_first_not_of(" \t");
        if (indent == std::string::npos)
            return false;
        const std::string body = trimmed.substr(indent);
        if (body.size() < 2)
            return false;
        if (IsHardRuleLine(body) || HasEnumeratorPrefix(body) || LooksLikeCommandBody(body, indent))
            return true;
        return LooksLikeHeadingBody(body, next_line, indent);
    }

    struct CardPage {
        long long begin = 1;
        std::string content;
    };

    struct CardSource {
        std::string source;
        std::vector<CardPage> pages;
        size_t reads = 0;
        size_t max_result_bytes = 0;
    };

    void CollectReadPages(const json& raw_messages, std::vector<CardSource>& out) {
        if (!raw_messages.is_array())
            return;
        std::unordered_map<std::string, std::pair<std::string, long long>> pending;
        std::vector<std::pair<std::string, long long>> fifo;
        for (const auto& msg : raw_messages) {
            if (!msg.is_object())
                continue;
            const std::string role = msg.value("role", "");
            if (role == "assistant") {
                bool structured = false;
                if (msg.contains("tool_calls") && msg["tool_calls"].is_array()) {
                    for (const auto& tc : msg["tool_calls"]) {
                        if (!tc.is_object())
                            continue;
                        const json& fn = (tc.contains("function") && tc["function"].is_object()) ? tc["function"] : tc;
                        const json args = tool_shape::ParseArgsValue(
                            fn.contains("arguments") ? fn["arguments"] : json());
                        const std::string src = tool_shape::ReadResourceOfArgs(args);
                        if (src.empty())
                            continue;
                        structured = true;
                        const long long begin = tool_shape::ReadBeginOfArgs(args);
                        const std::string id = tc.value("id", "");
                        if (id.empty())
                            fifo.emplace_back(src, begin);
                        else
                            pending[id] = { src, begin };
                    }
                }
                if (!structured) {
                    const std::string text = get_json_value<std::string>(msg, "content", "");
                    const json args = tool_shape::ParseArgsObjectFromText(text);
                    const std::string src = tool_shape::ReadResourceOfArgs(args);
                    if (!src.empty())
                        fifo.emplace_back(src, tool_shape::ReadBeginOfArgs(args));
                }
            } else if (role == "tool") {
                std::pair<std::string, long long> key;
                const std::string id = msg.value("tool_call_id", "");
                if (!id.empty()) {
                    auto it = pending.find(id);
                    if (it == pending.end())
                        continue;
                    key = it->second;
                    pending.erase(it);
                } else if (!fifo.empty()) {
                    key = fifo.front();
                    fifo.erase(fifo.begin());
                } else {
                    continue;
                }
                const std::string content = get_json_value<std::string>(msg, "content", "");
                if (content.empty() || LooksLikeFailure(content))
                    continue;
                CardSource* slot = nullptr;
                for (auto& s : out) {
                    if (s.source == key.first) {
                        slot = &s;
                        break;
                    }
                }
                if (!slot) {
                    out.push_back(CardSource{ key.first, {}, 0, 0 });
                    slot = &out.back();
                }
                ++slot->reads;
                slot->max_result_bytes = std::max(slot->max_result_bytes, content.size());
                bool dup = false;
                for (const auto& p : slot->pages) {
                    if (p.begin == key.second) {
                        dup = true;
                        break;
                    }
                }
                if (!dup)
                    slot->pages.push_back(CardPage{ key.second, content });
            }
        }
    }

    constexpr size_t kMaxRegistryEntries = 10;
    constexpr size_t kMaxRegistrySignatureChars = 160;
    constexpr size_t kMaxRegistryTailChars = 180;

    std::string ReplaceInvalidUtf8(const std::string& s);

    std::string Fnv1aHex(const std::string& s) {
        uint64_t h = 14695981039346656037ULL;
        const uint64_t p = 1099511628211ULL;
        for (unsigned char c : s) { h ^= c; h *= p; }
        std::ostringstream oss;
        oss << std::hex << std::setfill('0') << std::setw(16) << h;
        return oss.str();
    }

    std::string NormalizeWrittenContent(const std::string& content) {
        std::string out;
        out.reserve(content.size() + 1);
        size_t pos = 0;
        while (pos <= content.size()) {
            const size_t nl = content.find('\n', pos);
            const size_t stop = (nl == std::string::npos) ? content.size() : nl;
            out += RightTrimLine(content.substr(pos, stop - pos));
            out += '\n';
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
        }
        return out;
    }

    std::string FoldWhitespace(const std::string& s) {
        std::string out;
        bool gap = false;
        for (char c : s) {
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n') {
                gap = !out.empty();
                continue;
            }
            if (gap) {
                out += ' ';
                gap = false;
            }
            out += c;
        }
        return out;
    }

    std::string PlaceholderDigits(const std::string& s) {
        std::string out;
        bool run = false;
        for (char c : s) {
            if (isdigit((unsigned char) c)) {
                if (!run) {
                    out += '#';
                    run = true;
                }
                continue;
            }
            run = false;
            out += c;
        }
        return out;
    }

    std::string PlaceholderPathPrefix(const std::string& token) {
        const size_t cut = token.find_last_of("/\\");
        if (cut == std::string::npos || cut + 1 >= token.size())
            return token;
        return "*/" + token.substr(cut + 1);
    }

    std::string NormalizeCommandPayload(const std::string& payload) {
        const std::string folded = FoldWhitespace(payload);
        std::string out;
        size_t pos = 0;
        while (pos <= folded.size()) {
            const size_t sp = folded.find(' ', pos);
            const size_t stop = (sp == std::string::npos) ? folded.size() : sp;
            const std::string token = folded.substr(pos, stop - pos);
            if (!token.empty()) {
                if (!out.empty())
                    out += ' ';
                out += PlaceholderDigits(PlaceholderPathPrefix(token));
            }
            if (sp == std::string::npos)
                break;
            pos = sp + 1;
        }
        return out;
    }

    std::string CommandSignatureOfArgs(const json& args) {
        const std::string payload = tool_shape::ExecutePayloadOfArgs(args);
        if (payload.empty())
            return std::string();
        std::string sig = NormalizeCommandPayload(payload);
        if (sig.empty())
            return std::string();
        const std::string ctx = tool_shape::ExecuteContextOfArgs(args, payload);
        if (!ctx.empty())
            sig += " @" + PlaceholderDigits(FoldWhitespace(ctx));
        return ReplaceInvalidUtf8(safe_utf8_truncate(sig, kMaxRegistrySignatureChars, "..."));
    }

    std::string ResultTailOf(const std::string& content, size_t tail_lines) {
        if (tail_lines == 0)
            return std::string();
        std::vector<std::string> lines;
        size_t pos = 0;
        while (pos <= content.size()) {
            const size_t nl = content.find('\n', pos);
            const size_t stop = (nl == std::string::npos) ? content.size() : nl;
            const std::string line = RightTrimLine(content.substr(pos, stop - pos));
            if (!line.empty())
                lines.push_back(line);
            if (nl == std::string::npos)
                break;
            pos = nl + 1;
        }
        std::string out;
        const size_t start = (lines.size() > tail_lines) ? lines.size() - tail_lines : 0;
        for (size_t i = start; i < lines.size(); ++i) {
            if (!out.empty())
                out += " | ";
            out += lines[i];
        }
        return ReplaceInvalidUtf8(safe_utf8_truncate(out, kMaxRegistryTailChars, "..."));
    }

    struct PendingCall {
        int kind = 0;
        std::string key;
        std::string payload;
    };

    bool ClassifyCall(const json& args, PendingCall& out) {
        if (!args.is_object() || args.empty())
            return false;
        const std::string write_res = tool_shape::WriteResourceOfArgs(args);
        if (!write_res.empty()) {
            out.kind = 2;
            out.key = write_res;
            out.payload = tool_shape::WritePayloadOfArgs(args);
            return !out.payload.empty();
        }
        const std::string read_res = tool_shape::ReadResourceOfArgs(args);
        if (!read_res.empty()) {
            out.kind = 1;
            out.key = read_res;
            return true;
        }
        if (!tool_shape::IsExecuteLikeArgs(args))
            return false;
        const std::string sig = CommandSignatureOfArgs(args);
        if (sig.empty())
            return false;
        out.kind = 3;
        out.key = sig;
        return true;
    }

    void CollectRegistryFacts(const json& raw_messages, size_t tail_lines, json& writes, json& commands,
                              std::unordered_map<std::string, bool>& resource_write_last) {
        writes = json::object();
        commands = json::object();
        if (!raw_messages.is_array())
            return;
        std::unordered_map<std::string, PendingCall> pending;
        std::vector<PendingCall> fifo;
        size_t call_seq = 0;
        for (const auto& msg : raw_messages) {
            if (!msg.is_object())
                continue;
            const std::string role = msg.value("role", "");
            if (role == "assistant") {
                bool structured = false;
                if (msg.contains("tool_calls") && msg["tool_calls"].is_array()) {
                    for (const auto& tc : msg["tool_calls"]) {
                        if (!tc.is_object())
                            continue;
                        const json& fn = (tc.contains("function") && tc["function"].is_object()) ? tc["function"] : tc;
                        const json args = tool_shape::ParseArgsValue(
                            fn.contains("arguments") ? fn["arguments"] : json());
                        PendingCall call;
                        if (!ClassifyCall(args, call))
                            continue;
                        structured = true;
                        const std::string id = tc.value("id", "");
                        if (id.empty())
                            fifo.push_back(call);
                        else
                            pending[id] = call;
                    }
                }
                if (!structured) {
                    const std::string text = get_json_value<std::string>(msg, "content", "");
                    PendingCall call;
                    if (ClassifyCall(tool_shape::ParseAnyArgsObjectFromText(text), call))
                        fifo.push_back(call);
                }
            } else if (role == "tool") {
                PendingCall call;
                const std::string id = msg.value("tool_call_id", "");
                if (!id.empty()) {
                    auto it = pending.find(id);
                    if (it == pending.end())
                        continue;
                    call = it->second;
                    pending.erase(it);
                } else if (!fifo.empty()) {
                    call = fifo.front();
                    fifo.erase(fifo.begin());
                } else {
                    continue;
                }
                ++call_seq;
                const std::string content = get_json_value<std::string>(msg, "content", "");
                const bool failed = content.empty() || LooksLikeFailure(content);
                if (call.kind == 1) {
                    if (!failed)
                        resource_write_last[call.key] = false;
                    continue;
                }
                if (call.kind == 2) {
                    if (failed)
                        continue;
                    json& dst = writes[call.key];
                    if (!dst.is_object())
                        dst = json::object();
                    dst["hash"] = Fnv1aHex(NormalizeWrittenContent(call.payload));
                    dst["lines"] = CountLinesOf(call.payload);
                    dst["seq"] = call_seq;
                    resource_write_last[call.key] = true;
                    continue;
                }
                json& slot = commands[call.key];
                if (!slot.is_object())
                    slot = json::object();
                slot["runs"] = slot.value("runs", (size_t) 0) + 1;
                slot["seq"] = call_seq;
                slot["ok"] = !failed;
                slot["tail"] = ResultTailOf(content, tail_lines);
                if (failed && !slot.contains("fail_seq")) {
                    slot["fail_seq"] = call_seq;
                    slot["fail_tail"] = slot["tail"];
                }
            }
        }
    }

    std::string RenderRegistryBlock(const json& reg) {
        if (!reg.is_object())
            return std::string();
        const json* written = (reg.contains("written") && reg["written"].is_object()) ? &reg["written"] : nullptr;
        const json* commands = (reg.contains("commands") && reg["commands"].is_object()) ? &reg["commands"] : nullptr;
        const bool has_written = written && !written->empty();
        const bool has_commands = commands && !commands->empty();
        if (!has_written && !has_commands)
            return std::string();
        std::ostringstream oss;
        oss << "Recorded facts from earlier tool calls (raw facts only, not a judgement about progress):\n";
        if (has_written) {
            for (auto it = written->begin(); it != written->end(); ++it) {
                if (!it.value().is_object())
                    continue;
                oss << "- wrote " << it.key() << " (" << it.value().value("lines", (size_t) 0)
                    << " line(s), content hash " << it.value().value("hash", std::string())
                    << ", last write at call #" << it.value().value("seq", (size_t) 0) << ")\n";
            }
        }
        if (has_commands) {
            for (auto it = commands->begin(); it != commands->end(); ++it) {
                if (!it.value().is_object())
                    continue;
                const json& c = it.value();
                const size_t seq = c.value("seq", (size_t) 0);
                const bool ok = c.value("ok", true);
                oss << "- ran `" << it.key() << "` " << c.value("runs", (size_t) 0)
                    << " time(s); last run at call #" << seq << " returned "
                    << (ok ? "no error signal" : "an error signal");
                const std::string tail = c.value("tail", std::string());
                if (!tail.empty())
                    oss << "; result tail: " << tail;
                oss << "\n";
                const size_t fail_seq = c.value("fail_seq", (size_t) 0);
                if (fail_seq != 0 && fail_seq != seq) {
                    oss << "- (an earlier run of the same signature at call #" << fail_seq
                        << " returned an error signal";
                    if (ok)
                        oss << ", superseded by the run at call #" << seq;
                    const std::string fail_tail = c.value("fail_tail", std::string());
                    if (!fail_tail.empty())
                        oss << "; result tail: " << fail_tail;
                    oss << ")\n";
                }
            }
        }
        return oss.str();
    }

    size_t DropReadFacts(json& entry, const std::string& resource) {
        size_t dropped = 0;
        bool first_field = true;
        for (const char* field : {"read_files", "_read_page_lines"}) {
            if (entry.contains(field) && entry[field].is_object()) {
                json kept = json::object();
                for (auto it = entry[field].begin(); it != entry[field].end(); ++it) {
                    const std::string key = it.key();
                    const bool match = (key == resource) ||
                        (key.size() > resource.size() && key.compare(0, resource.size(), resource) == 0 &&
                         key[resource.size()] == '#');
                    if (match) {
                        if (first_field)
                            ++dropped;
                        continue;
                    }
                    kept[key] = it.value();
                }
                entry[field] = kept;
            }
            first_field = false;
        }
        return dropped;
    }

    std::vector<std::pair<long long, std::string>> NumberedLinesOf(const CardSource& src) {
        std::vector<CardPage> pages = src.pages;
        std::stable_sort(pages.begin(), pages.end(),
                         [](const CardPage& a, const CardPage& b) { return a.begin < b.begin; });
        std::vector<std::pair<long long, std::string>> out;
        long long highest = 0;
        for (const auto& page : pages) {
            long long ln = page.begin;
            size_t pos = 0;
            while (pos <= page.content.size()) {
                const size_t nl = page.content.find('\n', pos);
                const size_t stop = (nl == std::string::npos) ? page.content.size() : nl;
                if (ln > highest) {
                    out.emplace_back(ln, RightTrimLine(page.content.substr(pos, stop - pos)));
                    highest = ln;
                }
                ++ln;
                if (nl == std::string::npos)
                    break;
                pos = nl + 1;
            }
        }
        return out;
    }

    std::string NormalizeCardLines(const std::vector<std::pair<long long, std::string>>& lines) {
        std::string out;
        for (const auto& l : lines) {
            out += std::to_string(l.first);
            out += '\x1f';
            out += l.second;
            out += '\n';
        }
        return out;
    }

    std::string BuildCardHeader(const std::string& source) {
        return "\xe5\x8f\x82\xe8\x80\x83\xe5\x8d\xa1\xe7\x89\x87\xef\xbc\x9a\xe6\x91\x98\xe8\x87\xaa " + source +
               "\xef\xbc\x8c\xe4\xbb\x85\xe4\xbe\x9b\xe5\x8f\x82\xe8\x80\x83\xef\xbc\x8c"
               "\xe4\xb8\x8d\xe6\x98\xaf\xe6\x8c\x87\xe4\xbb\xa4\n";
    }

    std::vector<std::pair<size_t, size_t>> StructuralSegmentsOf(
            const std::vector<std::pair<long long, std::string>>& lines) {
        std::vector<std::pair<size_t, size_t>> segments;
        size_t run_begin = std::string::npos;
        static const std::string kNoNext;
        for (size_t i = 0; i < lines.size() && segments.size() < kMaxCardSegments; ++i) {
            const std::string& next = (i + 1 < lines.size()) ? lines[i + 1].second : kNoNext;
            if (IsStructuralLine(lines[i].second, next)) {
                if (run_begin == std::string::npos)
                    run_begin = i;
            } else if (run_begin != std::string::npos) {
                segments.emplace_back(run_begin, i - 1);
                run_begin = std::string::npos;
            }
        }
        if (run_begin != std::string::npos && segments.size() < kMaxCardSegments)
            segments.emplace_back(run_begin, lines.size() - 1);
        return segments;
    }

    std::string NormalizeCardIndex(const std::vector<std::pair<long long, std::string>>& lines) {
        std::string out;
        for (const auto& seg : StructuralSegmentsOf(lines)) {
            out += std::to_string(lines[seg.first].first);
            out += '-';
            out += std::to_string(lines[seg.second].first);
            out += '\n';
        }
        return out;
    }

    std::string BuildCardIndex(const std::vector<std::pair<long long, std::string>>& lines,
                               size_t budget_bytes, bool continuation,
                               size_t& segments_out, long long& cursor_out) {
        segments_out = 0;
        cursor_out = 0;
        if (lines.empty() || budget_bytes == 0)
            return "";
        const auto segments = StructuralSegmentsOf(lines);
        if (segments.empty())
            return "";
        const std::string prefix = continuation
            ? "\xe7\xbb\xad\xef\xbc\x9a"
            : "\xe7\xbb\x93\xe6\x9e\x84\xe6\xae\xb5\xe6\x89\x80\xe5\x9c\xa8\xe8\xa1\x8c\xef\xbc\x9a";
        if (budget_bytes <= prefix.size() + 1)
            return "";
        std::string out = prefix;
        for (const auto& seg : segments) {
            std::ostringstream item;
            if (segments_out > 0)
                item << ", ";
            item << lines[seg.first].first << "-" << lines[seg.second].first;
            if (out.size() + item.str().size() + 1 > budget_bytes)
                break;
            out += item.str();
            ++segments_out;
            cursor_out = lines[seg.second].first;
        }
        if (segments_out == 0)
            return "";
        out += "\n";
        return out;
    }

    std::string BuildCardSegments(const std::vector<std::pair<long long, std::string>>& lines,
                                  size_t budget_bytes, size_t& segments_out, long long& cursor_out) {
        segments_out = 0;
        cursor_out = 0;
        if (lines.empty() || budget_bytes == 0)
            return "";
        const auto segments = StructuralSegmentsOf(lines);
        if (segments.empty())
            return "";

        std::string out;
        for (const auto& seg : segments) {
            std::ostringstream widest_head;
            widest_head << "[\xe7\xac\xac " << lines[seg.first].first << "-" << lines[seg.second].first
                        << " \xe8\xa1\x8c]\n";
            const size_t head_bytes = widest_head.str().size();
            if (out.size() + head_bytes >= budget_bytes)
                break;
            const size_t avail = budget_bytes - out.size() - head_bytes;
            size_t last = seg.first;
            size_t acc = 0;
            bool any = false;
            for (size_t i = seg.first; i <= seg.second; ++i) {
                const size_t need = lines[i].second.size() + 1;
                if (acc + need > avail)
                    break;
                acc += need;
                last = i;
                any = true;
            }
            if (!any)
                break;
            std::ostringstream chunk;
            chunk << "[\xe7\xac\xac " << lines[seg.first].first << "-" << lines[last].first
                  << " \xe8\xa1\x8c]\n";
            for (size_t i = seg.first; i <= last; ++i)
                chunk << lines[i].second << "\n";
            out += chunk.str();
            ++segments_out;
            cursor_out = lines[last].first;
            if (last < seg.second)
                break;
        }
        if (segments_out == 0)
            return "";
        return out;
    }

    std::string ReplaceInvalidUtf8(const std::string& s) {
        std::string out;
        out.reserve(s.size());
        const size_t n = s.size();
        size_t i = 0;
        while (i < n) {
            const unsigned char c = static_cast<unsigned char>(s[i]);
            size_t len = c < 0x80 ? 1 : (c >= 0xC2 && c <= 0xDF) ? 2 : (c >= 0xE0 && c <= 0xEF) ? 3 : (c >= 0xF0 && c <= 0xF4) ? 4 : 0;
            bool ok = len != 0 && i + len <= n;
            for (size_t k = 1; ok && k < len; ++k)
                ok = (static_cast<unsigned char>(s[i + k]) & 0xC0) == 0x80;
            if (ok) {
                out.append(s, i, len);
                i += len;
            } else {
                out += '?';
                ++i;
            }
        }
        return out;
    }

    std::string ExtractKeyFacts(const std::string& content, size_t max_len) {
        if (LooksLikeFailure(content)) {
            size_t nl = content.find('\n');
            std::string first_line = (nl == std::string::npos) ? content : content.substr(0, nl);
            return safe_utf8_truncate(first_line, max_len, "...");
        }

        std::vector<std::string> facts;
        try {
            json parsed = json::parse(content, nullptr, false);
            if (parsed.is_object()) {
                for (auto it = parsed.begin(); it != parsed.end(); ++it) {
                    if (facts.size() >= 4) break;
                    std::string v = it.value().is_string() ? it.value().get<std::string>() : it.value().dump();
                    facts.push_back(it.key() + "=" + v);
                }
            }
        } catch (...) {}

        if (facts.empty()) {
            static const char* kPathHints[] = { "path\":", "file\":", "C:\\", "/home/", "/root/", "/mnt/", "/usr/" };
            for (const char* hint : kPathHints) {
                size_t pos = content.find(hint);
                if (pos != std::string::npos) {
                    size_t start = utf8_align_start(content, (pos > 20) ? pos - 20 : 0);
                    facts.push_back(safe_utf8_truncate(content.substr(start), 80, ""));
                    break;
                }
            }
        }

        std::string out;
        if (!facts.empty()) {
            for (const auto& f : facts) {
                if (!out.empty()) out += "; ";
                out += f;
            }
        } else {
            out = content;
        }
        return safe_utf8_truncate(out, max_len, "...");
    }
}

std::string TaskMemoBuilder::StripThinkBlock(const std::string& content)
{
    const size_t close = content.rfind("</think>");
    if (close != std::string::npos)
        return content.substr(close + 8);
    const size_t open = content.find("<think>");
    return open == std::string::npos ? content : content.substr(0, open);
}

namespace {
    const char kPinPrefix[] = "[reasoning note: ";
    std::mutex g_pin_mu;
    std::unordered_map<std::string, std::pair<std::string, std::chrono::steady_clock::time_point>> g_pins;
}

void TaskMemoBuilder::PinThinkConclusion(const std::string& session_key, const std::string& call_signature,
                                         const std::string& conclusion)
{
    if (session_key.empty() || call_signature.empty() || conclusion.empty())
        return;
    std::lock_guard<std::mutex> lk(g_pin_mu);
    const auto now = std::chrono::steady_clock::now();
    if (g_pins.size() > 4096) {
        for (auto it = g_pins.begin(); it != g_pins.end();)
            it = (now - it->second.second > std::chrono::hours(2)) ? g_pins.erase(it) : std::next(it);
        if (g_pins.size() > 4096)
            g_pins.clear();
    }
    g_pins[session_key + "\x1f" + call_signature] = {conclusion, now};
}

std::string TaskMemoBuilder::LookupPinnedConclusion(const std::string& session_key, const std::string& call_signature)
{
    if (session_key.empty() || call_signature.empty())
        return "";
    std::lock_guard<std::mutex> lk(g_pin_mu);
    auto it = g_pins.find(session_key + "\x1f" + call_signature);
    return it == g_pins.end() ? "" : std::string(kPinPrefix) + it->second.first + "]\n";
}

std::string TaskMemoBuilder::ExtractThinkConclusion(const std::string& content)
{
    if (content.rfind(kPinPrefix, 0) == 0) {
        const size_t end = content.find("]\n");
        return end == std::string::npos ? "" : content.substr(sizeof(kPinPrefix) - 1, end - (sizeof(kPinPrefix) - 1));
    }
    size_t open = content.find("<think>");
    size_t close = content.find("</think>", open == std::string::npos ? 0 : open);
    if (open == std::string::npos && close == std::string::npos)
        return "";
    const size_t body_begin = open == std::string::npos ? 0 : open + 7;
    std::string body = content.substr(body_begin, close == std::string::npos ? std::string::npos : close - body_begin);
    static const std::string kForced = "Considering the time constraints";
    const size_t forced = body.rfind(kForced);
    if (forced != std::string::npos)
        body.resize(forced);
    while (!body.empty() && std::isspace(static_cast<unsigned char>(body.back())))
        body.pop_back();
    if (body.empty())
        return "";
    size_t start = body.size() > 240 ? body.size() - 240 : 0;
    if (start > 0) {
        const size_t nl = body.find('\n', start);
        const size_t dot = body.find(". ", start);
        const size_t cut = std::min(nl, dot == std::string::npos ? std::string::npos : dot + 1);
        if (cut != std::string::npos && cut + 1 < body.size())
            start = cut + 1;
    }
    while (start < body.size() && (static_cast<unsigned char>(body[start]) & 0xC0) == 0x80)
        ++start;
    std::string out = body.substr(start);
    for (char& c : out)
        if (c == '\n' || c == '\r') c = ' ';
    return out;
}

std::string TaskMemoBuilder::ExtractToolCallSummary(const std::string& content)
{
    std::string visible = StripThinkBlock(content);
    if (visible.rfind(kPinPrefix, 0) == 0) {
        const size_t end = visible.find("]\n");
        if (end != std::string::npos)
            visible = visible.substr(end + 2);
    }
    const size_t tc = visible.find("<tool_call>");
    if (tc == std::string::npos)
        return "";
    size_t end = visible.find("</tool_call>", tc);
    std::string body = visible.substr(tc + 11, end == std::string::npos ? std::string::npos : end - tc - 11);
    try {
        json j = json::parse(body);
        std::string out = j.value("name", "");
        if (j.contains("arguments")) {
            const auto& a = j["arguments"];
            if (a.is_object() && a.size() == 1 && a.begin()->is_string())
                out += " " + a.begin()->get<std::string>();
            else
                out += " " + a.dump();
        }
        return safe_utf8_truncate(out, 400, "...");
    } catch (...) {
        for (char& c : body)
            if (c == '\n' || c == '\r') c = ' ';
        return safe_utf8_truncate(body, 400, "...");
    }
}

std::string TaskMemoBuilder::ExtractReadToolPath(const std::string& assistant_content)
{
    return ExtractReadToolPathImpl(assistant_content);
}

bool TaskMemoBuilder::LooksLikeToolFailure(const std::string& content)
{
    return LooksLikeFailure(content);
}

json TaskMemoBuilder::BuildRuleLayer(const json& prev, const std::vector<GenieChatMessage>& dropped_messages) const
{
    json entry = prev.is_object() ? prev : json::object();
    if (!entry.contains("current_plan") || !entry["current_plan"].is_string())
        entry["current_plan"] = "";
    if (!entry.contains("next_actions") || !entry["next_actions"].is_array())
        entry["next_actions"] = json::array();
    if (!entry.contains("open_questions") || !entry["open_questions"].is_array())
        entry["open_questions"] = json::array();
    if (!entry.contains("tool_state") || !entry["tool_state"].is_object())
        entry["tool_state"] = json::object();

    if (!entry.contains("read_files") || !entry["read_files"].is_object())
        entry["read_files"] = json::object();

    size_t tool_seq = entry["tool_state"].size();
    std::string last_assistant_content;
    for (const auto& m : dropped_messages) {
        std::string preview = ReplaceInvalidUtf8(ExtractKeyFacts(m.content, config_.rule_layer_preview_chars));
        if (m.role == "user") {
            AppendCapped(entry["facts_constraints"], "用户提及: " + preview, kMaxItemsPerField);
            size_t todo_pos = m.content.find("TODO");
            if (todo_pos == std::string::npos)
                todo_pos = m.content.find("todo");
            if (todo_pos != std::string::npos)
                AppendCapped(entry["open_questions"], "待办: " + ReplaceInvalidUtf8(safe_utf8_truncate(m.content.substr(todo_pos), 160, "")), kMaxItemsPerField);
        } else if (m.role == "assistant") {
            const std::string conclusion = TaskMemoBuilder::ExtractThinkConclusion(m.content);
            if (!conclusion.empty())
                AppendCapped(entry["facts_constraints"], "思考结论: " + ReplaceInvalidUtf8(conclusion), kMaxItemsPerField);
            last_assistant_content = m.content;
        } else if (m.role == "tool") {
            bool failed = LooksLikeFailure(m.content);
            entry["tool_state"]["tool_result_" + std::to_string(tool_seq++)] =
                (failed ? "失败: " : "成功: ") + preview;
            CapObjectSize(entry["tool_state"], kMaxItemsPerField);
            const std::string call = ExtractToolCallSummary(last_assistant_content);
            AppendCapped(entry["completed"], (failed ? "失败: " : "成功: ") +
                (call.empty() ? std::string() : ReplaceInvalidUtf8(safe_utf8_truncate(call, 200, "...")) + " -> ") + preview, kMaxItemsPerField);
            if (!failed && !call.empty()) {
                const std::string tool = call.substr(0, call.find(' '));
                if (!entry.contains("last_success") || !entry["last_success"].is_object())
                    entry["last_success"] = json::object();
                entry["last_success"][tool] = ReplaceInvalidUtf8(safe_utf8_truncate(FullToolCall(last_assistant_content), kMaxLastSuccessChars, "..."));
                CapObjectSize(entry["last_success"], 6);
            }
            const std::string tool_name = call.substr(0, call.find(' '));
            const std::string shape_read_path = config_.sediment.enabled
                ? ShapeReadResourceOfText(last_assistant_content) : std::string();
            const bool reads_content = config_.sediment.enabled
                ? !shape_read_path.empty()
                : (IsReadLikeToolName(tool_name) || tool_name.find("fetch") != std::string::npos);
            if (!failed && !call.empty() && !reads_content)
                for (const auto& a : ExtractArtifactLines(m.content))
                    AppendCapped(entry["artifacts"], ReplaceInvalidUtf8(a), kMaxArtifacts);

            // 已读文件索引：被驱逐的 read 类工具结果不再丢弃全部痕迹，而是在 Task Memo 里
            // 留一条「路径 + 关键事实摘要」，供模型在原始内容已不在上下文时判断是否要重读。
            if (!failed) {
                std::string read_path = config_.sediment.enabled
                    ? shape_read_path : ExtractReadToolPathImpl(last_assistant_content);
                if (!read_path.empty()) {
                    if (config_.sediment.enabled && config_.sediment.merge_paged_reads) {
                        const long long begin = ReadOffsetOfText(last_assistant_content);
                        const long long end = begin + (long long) CountLinesOf(m.content) - 1;
                        const std::string page_key = read_path + "#" + std::to_string(begin);
                        entry["read_files"][page_key] = preview;
                        if (!entry.contains("_read_page_lines") || !entry["_read_page_lines"].is_object())
                            entry["_read_page_lines"] = json::object();
                        entry["_read_page_lines"][page_key] = end;
                        entry["_read_merge"] = true;
                        CapObjectSize(entry["_read_page_lines"], kMaxItemsPerField);
                    } else {
                        entry["read_files"][read_path] = preview;
                    }
                    CapObjectSize(entry["read_files"], kMaxItemsPerField);
                    if (!entry.contains("hard_rules") || !entry["hard_rules"].is_array())
                        entry["hard_rules"] = json::array();
                    if (!entry.contains("_hard_rule_files") || !entry["_hard_rule_files"].is_object())
                        entry["_hard_rule_files"] = json::object();
                    if (!entry["_hard_rule_files"].contains(read_path)) {
                        entry["_hard_rule_files"][read_path] = true;
                        for (const auto& rule : ExtractHardRules(m.content)) {
                            if (entry["hard_rules"].size() >= kMaxHardRules) break;
                            const std::string item = ReplaceInvalidUtf8(rule);
                            bool dup = false;
                            for (const auto& r : entry["hard_rules"])
                                if (r.is_string() && r.get<std::string>() == item) { dup = true; break; }
                            if (!dup) entry["hard_rules"].push_back(item);
                        }
                    }
                }
            }
        }
    }

    size_t folded_before = entry.value("_folded_count", (size_t)0);
    size_t folded = folded_before + dropped_messages.size();
    entry["_folded_count"] = folded;
    entry["source_range"] = "messages[0:" + std::to_string(folded) + ")";

    // 轻量分页目录：每次 Update() 调用（即一次驱逐批次）分配一个递增 page_id，只记
    // {page_id, source_range, 一行确定性类别统计 gist}，不含任何原始消息文本——与
    // 现有安全脱敏边界保持一致。环形缓冲超过 page_directory.max_entries 时 FIFO
    // 淘汰最旧条目（不是按 page_id 淘汰，是按数组顺序，天然等价于最旧）。
    {
        size_t tool_failed = 0;
        for (const auto& m : dropped_messages) {
            if (m.role == "tool" && LooksLikeFailure(m.content))
                ++tool_failed;
        }
        size_t page_id = entry.value("_next_page_id", (size_t) 1);
        entry["_next_page_id"] = page_id + 1;

        json page = json::object();
        page["page_id"] = page_id;
        page["source_range"] = "messages[" + std::to_string(folded_before) + ":" + std::to_string(folded) + ")";
        std::ostringstream gist;
        gist << dropped_messages.size() << " 条消息";
        if (tool_failed > 0)
            gist << "，含 " << tool_failed << " 条工具失败";
        page["gist"] = gist.str();

        if (!entry.contains("pages") || !entry["pages"].is_array())
            entry["pages"] = json::array();
        entry["pages"].push_back(page);
        while (entry["pages"].size() > config_.page_directory.max_entries)
            entry["pages"].erase(entry["pages"].begin());
    }

    std::string concat;
    for (const auto& m : dropped_messages)
        concat += m.role + "|" + m.content;
    entry["content_hash"] = std::to_string(std::hash<std::string>{}(concat));
    entry["confidence"] = 0.6;
    return entry;
}

// ── UpdateCards（参考资料卡片：确定性抽取，只在驱逐时刻重写）───────────────────

bool TaskMemoBuilder::UpdateCards(json& entry, const json& raw_messages) const
{
    const auto& card_cfg = config_.sediment.card;
    if (!config_.sediment.enabled || card_cfg.mode == "off")
        return true;

    std::vector<CardSource> sources;
    CollectReadPages(raw_messages, sources);
    if (sources.empty())
        return true;

    const size_t context_budget = (size_t) std::max(0, instance_config_.get_context_size());
    double bytes_per_token = entry.value("_card_bytes_per_token", 0.0);
    if (bytes_per_token <= 0.0) {
        std::string probe;
        for (const auto& s : sources)
            for (const auto& p : s.pages)
                probe += p.content;
        const auto& fid = instance_config_.i_model_config_.GetPromptOptimizationConfig().fidelity;
        bytes_per_token = EstimateCjkAwareBytesPerToken(probe, fid.cjk_bytes_per_token, fid.ascii_bytes_per_token);
        entry["_card_bytes_per_token"] = bytes_per_token;
    }
    size_t budget = entry.value("_card_budget_bytes", (size_t) 0);
    if (budget == 0) {
        budget = ResolveSedimentBudget(card_cfg.max_ratio, card_cfg.max_bytes, context_budget, bytes_per_token);
        if (budget == 0)
            return true;
        entry["_card_budget_bytes"] = budget;
    }
    const size_t trigger_bytes = ResolveSedimentThreshold(card_cfg.trigger_result_ratio,
                                                           context_budget, bytes_per_token);

    if (!entry.contains("cards") || !entry["cards"].is_array())
        entry["cards"] = json::array();
    entry["_cards_render"] = !card_cfg.shadow_only;
    entry["_cards_before_memo"] = (card_cfg.placement == "before_memo");

    size_t used = CardBytesOf(entry);
    bool stable = true;

    std::vector<const CardSource*> triggered;
    for (const auto& src : sources) {
        if (src.reads > 1 || src.max_result_bytes >= trigger_bytes)
            triggered.push_back(&src);
    }
    size_t slots = triggered.size();
    for (const auto& c : entry["cards"]) {
        if (!c.is_object())
            continue;
        const std::string s = c.value("source", std::string());
        bool present = false;
        for (const auto* t : triggered) {
            if (t->source == s) {
                present = true;
                break;
            }
        }
        if (!present)
            ++slots;
    }
    const size_t share = (slots > 0) ? budget / slots : 0;

    auto reserved_for_others = [&](const std::string& self) -> size_t {
        size_t reserved = 0;
        for (const auto& c : entry["cards"]) {
            if (!c.is_object())
                continue;
            const std::string s = c.value("source", std::string());
            if (s == self)
                continue;
            const size_t owned = c.value("text", std::string()).size();
            if (share > owned)
                reserved += share - owned;
        }
        for (const auto* t : triggered) {
            if (t->source == self)
                continue;
            bool has_card = false;
            for (const auto& c : entry["cards"]) {
                if (c.is_object() && c.value("source", std::string()) == t->source) {
                    has_card = true;
                    break;
                }
            }
            if (!has_card)
                reserved += share;
        }
        return reserved;
    };

    auto rebuild = [&](const std::string& source,
                       const std::vector<std::pair<long long, std::string>>& lines,
                       size_t avail, json& out_card) -> bool {
        const std::string header = BuildCardHeader(source);
        if (avail <= header.size())
            return false;
        size_t segments = 0;
        long long cursor = 0;
        std::string kind = "extract";
        std::string body;
        if (card_cfg.mode != "index_only")
            body = BuildCardSegments(lines, avail - header.size(), segments, cursor);
        if (segments == 0) {
            kind = "index";
            body = BuildCardIndex(lines, avail - header.size(), false, segments, cursor);
        }
        if (segments == 0)
            return false;
        size_t raw_bytes = 0;
        for (const auto& l : lines)
            raw_bytes += l.second.size() + 1;
        const std::string text = header + body;
        if ((double) text.size() > (double) raw_bytes * card_cfg.min_compression_ratio)
            return false;
        std::vector<std::pair<long long, std::string>> covered;
        for (const auto& l : lines) {
            if (l.first <= cursor)
                covered.push_back(l);
        }
        out_card = json::object();
        out_card["source"] = source;
        out_card["kind"] = kind;
        out_card["line_begin"] = lines.front().first;
        out_card["line_end"] = cursor;
        out_card["line_cursor"] = cursor;
        out_card["covered_hash"] = std::to_string(std::hash<std::string>{}(
            kind == "index" ? NormalizeCardIndex(covered) : NormalizeCardLines(covered)));
        out_card["segments"] = segments;
        out_card["text"] = ReplaceInvalidUtf8(text);
        return true;
    };

    for (const auto* srcp : triggered) {
        const CardSource& src = *srcp;
        const auto lines = NumberedLinesOf(src);
        if (lines.empty())
            continue;
        size_t index = entry["cards"].size();
        for (size_t i = 0; i < entry["cards"].size(); ++i) {
            if (entry["cards"][i].is_object()
                    && entry["cards"][i].value("source", std::string()) == src.source) {
                index = i;
                break;
            }
        }
        const size_t global_left = (budget > used) ? budget - used : 0;
        bool all_sources_carded = true;
        for (const auto* t : triggered) {
            bool has_card = false;
            for (const auto& c : entry["cards"]) {
                if (c.is_object() && c.value("source", std::string()) == t->source) {
                    has_card = true;
                    break;
                }
            }
            if (!has_card) {
                all_sources_carded = false;
                break;
            }
        }
        const size_t reserved = reserved_for_others(src.source);
        const size_t extra = (all_sources_carded && global_left > reserved) ? global_left - reserved : 0;
        if (index >= entry["cards"].size()) {
            json card;
            if (rebuild(src.source, lines, std::min(share + extra, global_left), card)) {
                used += card["text"].get<std::string>().size();
                entry["cards"].push_back(card);
            }
            continue;
        }
        json& existing = entry["cards"][index];
        const std::string kind = existing.value("kind", std::string("extract"));
        const long long cursor = existing.value("line_cursor", (long long) 0);
        std::vector<std::pair<long long, std::string>> covered, fresh;
        for (const auto& l : lines)
            (l.first <= cursor ? covered : fresh).push_back(l);
        const std::string covered_hash = std::to_string(std::hash<std::string>{}(
            kind == "index" ? NormalizeCardIndex(covered) : NormalizeCardLines(covered)));
        const size_t own_bytes = existing.value("text", std::string()).size();
        if (covered_hash != existing.value("covered_hash", std::string())) {
            json card;
            const size_t room = (budget + own_bytes > used) ? budget + own_bytes - used : 0;
            if (rebuild(src.source, lines, std::min(std::max(share, own_bytes), room), card)) {
                used = used + card["text"].get<std::string>().size() - own_bytes;
                existing = card;
                stable = false;
            }
            continue;
        }
        if (fresh.empty() || index + 1 != entry["cards"].size())
            continue;
        const size_t avail = std::min(((share > own_bytes) ? share - own_bytes : 0) + extra, global_left);
        size_t segments = 0;
        long long new_cursor = 0;
        const std::string body = (kind == "index")
            ? BuildCardIndex(fresh, avail, true, segments, new_cursor)
            : BuildCardSegments(fresh, avail, segments, new_cursor);
        if (segments == 0)
            continue;
        std::vector<std::pair<long long, std::string>> new_covered;
        for (const auto& l : lines) {
            if (l.first <= new_cursor)
                new_covered.push_back(l);
        }
        existing["text"] = existing.value("text", std::string()) + ReplaceInvalidUtf8(body);
        existing["line_end"] = new_cursor;
        existing["line_cursor"] = new_cursor;
        existing["covered_hash"] = std::to_string(std::hash<std::string>{}(
            kind == "index" ? NormalizeCardIndex(new_covered) : NormalizeCardLines(new_covered)));
        existing["segments"] = existing.value("segments", (size_t) 0) + segments;
        used += body.size();
    }

    My_Log{My_Log::Level::kInfo}
        << "[TaskMemo] Cards: sources=" << sources.size()
        << ", triggered=" << triggered.size()
        << ", cards=" << entry["cards"].size()
        << ", bytes=" << CardBytesOf(entry)
        << ", budget=" << budget
        << ", share=" << share
        << ", trigger_bytes=" << trigger_bytes
        << ", stable=" << (stable ? 1 : 0)
        << ", shadow_only=" << (card_cfg.shadow_only ? 1 : 0) << std::endl;
    return stable;
}

size_t TaskMemoBuilder::CardBytesOf(const json& entry)
{
    if (!entry.is_object() || !entry.contains("cards") || !entry["cards"].is_array())
        return 0;
    size_t total = 0;
    for (const auto& c : entry["cards"]) {
        if (c.is_object() && c.contains("text") && c["text"].is_string())
            total += c["text"].get<std::string>().size();
    }
    return total;
}

// ── UpdateRegistry（产物与命令登记表：只列事实，沿用驱逐时刻改写）─────────────

size_t TaskMemoBuilder::RegistryBytesOf(const json& entry)
{
    if (!entry.is_object() || !entry.value("_registry_render", false) || !entry.contains("registry"))
        return 0;
    return RenderRegistryBlock(entry["registry"]).size();
}

size_t TaskMemoBuilder::UpdateRegistry(json& entry, const json& raw_messages) const
{
    const auto& reg_cfg = config_.sediment.registry;
    if (!config_.sediment.enabled || !reg_cfg.enabled) {
        entry.erase("_registry_render");
        return 0;
    }

    json writes = json::object();
    json commands = json::object();
    std::unordered_map<std::string, bool> resource_write_last;
    CollectRegistryFacts(raw_messages, reg_cfg.command_tail_lines, writes, commands, resource_write_last);

    json reg = json::object();
    reg["written"] = writes;
    reg["commands"] = commands;
    CapObjectSize(reg["written"], kMaxRegistryEntries);
    CapObjectSize(reg["commands"], kMaxRegistryEntries);

    size_t invalidated_reads = 0;
    for (const auto& kv : resource_write_last) {
        if (kv.second)
            invalidated_reads += DropReadFacts(entry, kv.first);
    }

    size_t budget = entry.value("_registry_budget_bytes", (size_t) 0);
    if (budget == 0) {
        const auto& fid = instance_config_.i_model_config_.GetPromptOptimizationConfig().fidelity;
        double bytes_per_token = entry.value("_card_bytes_per_token", 0.0);
        if (bytes_per_token <= 0.0) {
            bytes_per_token = EstimateCjkAwareBytesPerToken(RenderRegistryBlock(reg),
                                                             fid.cjk_bytes_per_token, fid.ascii_bytes_per_token);
        }
        budget = ResolveSedimentBudget(reg_cfg.max_ratio, reg_cfg.max_bytes,
                                       (size_t) std::max(0, instance_config_.get_context_size()), bytes_per_token);
        if (budget == 0) {
            entry.erase("_registry_render");
            return 0;
        }
        entry["_registry_budget_bytes"] = budget;
    }

    size_t bytes = RenderRegistryBlock(reg).size();
    size_t evicted = 0;
    while (bytes > budget) {
        const char* victim_field = nullptr;
        std::string victim_key;
        size_t victim_seq = 0;
        for (const char* field : {"written", "commands"}) {
            for (auto it = reg[field].begin(); it != reg[field].end(); ++it) {
                const size_t seq = it.value().is_object() ? it.value().value("seq", (size_t) 0) : 0;
                if (victim_field == nullptr || seq < victim_seq) {
                    victim_field = field;
                    victim_key = it.key();
                    victim_seq = seq;
                }
            }
        }
        if (victim_field == nullptr)
            break;
        reg[victim_field].erase(victim_key);
        bytes = RenderRegistryBlock(reg).size();
        ++evicted;
    }

    entry["registry"] = reg;
    entry["_registry_render"] = true;

    My_Log{My_Log::Level::kInfo}
        << "[TaskMemo] Registry: written=" << reg["written"].size()
        << ", commands=" << reg["commands"].size()
        << ", bytes=" << bytes
        << ", budget=" << budget
        << ", evicted=" << evicted
        << ", invalidated_reads=" << invalidated_reads << std::endl;
    return bytes;
}

// ── ExtractGoalAnchor（原始目标锚点鲁棒抓取）───────────────────────────────────

std::pair<std::string, bool> TaskMemoBuilder::ExtractGoalAnchor(const json& raw_messages) const
{
    if (!raw_messages.is_array() || raw_messages.empty())
        return {"", false};

    std::vector<std::string> candidates;
    for (const auto& msg : raw_messages) {
        if (msg.value("role", "") != "user")
            continue;
        // 用 get_json_value<std::string>() 而非裸 dump()：content 既可能是纯字符串，
        // 也可能是 OpenAI 多段 content-parts 数组，该函数对两种格式都能安全提取纯文本。
        candidates.push_back(get_json_value<std::string>(msg, "content", ""));
        if (candidates.size() >= config_.goal_scan_window)
            break;
    }
    if (candidates.empty())
        return {"", false};

    for (const auto& c : candidates) {
        if (c.size() >= config_.min_goal_signal_chars)
            return {safe_utf8_truncate(c, config_.goal_anchor_preview_chars, ""), true};
    }
    // 全部不达标（如首几条均为"你好"一类寒暄）：仍需确定性兜底，取第一条并标记低置信度，
    // 不能返回空锚点——这是"不遗忘目标"硬保证的前提，哪怕信号很弱也要有锚点可渲染。
    return {safe_utf8_truncate(candidates[0], config_.goal_anchor_preview_chars, ""), false};
}

// ── ShouldTriggerModelLayer ───────────────────────────────────────────────────

bool TaskMemoBuilder::ShouldTriggerModelLayer(const std::vector<GenieChatMessage>& dropped_messages,
                                               bool has_prev, double prev_confidence) const
{
    if (!config_.model_layer_enabled || !infer_fn_)
        return false;
    if (!has_prev)
        return true;
    if (prev_confidence < config_.low_confidence_threshold)
        return true;

    size_t max_tool_run = 0, cur_run = 0;
    bool has_error = false;
    for (const auto& m : dropped_messages) {
        if (m.role == "tool") { ++cur_run; max_tool_run = std::max(max_tool_run, cur_run); }
        else cur_run = 0;
        if (LooksLikeFailure(m.content))
            has_error = true;
    }
    return max_tool_run >= config_.long_tool_chain_threshold || has_error;
}

// ── BuildModelPrompt（沿用 LongTextSummarizer::BuildMapPrompt 的 Harmony/General 双格式约定）──

std::string TaskMemoBuilder::BuildModelPrompt(const json& prev, const json& rule_layer_result) const
{
    std::ostringstream task;
    task << "Maintain a structured task memo for a long-running session. Update it using the "
         << "rule-extracted facts below. Output ONLY one JSON object with exactly these fields: "
         << "completed (string array), current_plan (string), next_actions (string array), "
         << "facts_constraints (string array), open_questions (string array), tool_state (object), "
         << "confidence (number 0.0-1.0), goal_restatement (string, OPTIONAL: a one-sentence "
         << "restatement of the user's original goal, only if it can be confidently inferred from "
         << "the context; omit this field entirely if unclear, do not guess). "
         << "No prose, no markdown fences, JSON only.\n\n"
         << "Previous memo: " << (prev.is_object() ? prev.dump() : std::string("{}")) << "\n\n"
         << "Rule-extracted facts: " << rule_layer_result.dump();

    std::string task_str = task.str();
    bool is_harmony = (instance_config_.get_prompt_type() == PromptType::Harmony);

    if (is_harmony) {
        std::string prompt;
        prompt += "<|start|>system<|message|>You are a task-memo maintenance assistant.<|end|>";
        prompt += "<|start|>user<|message|>" + task_str + "<|end|>";
        prompt += "<|start|>assistant<|channel|>final<|message|>";
        return prompt;
    }

    const auto& j = instance_config_.get_prompt_template();
    if (j.is_object() && j.contains("system") && j["system"].is_string()
            && j.contains("user") && j["user"].is_string()
            && j.contains("start") && j["start"].is_string()) {
        std::string system_instruction = "You are a task-memo maintenance assistant.";
        if (instance_config_.is_thinking_model())
            system_instruction += "/no_think";
        return str_replace(j["system"].get<std::string>(), "string", system_instruction)
             + str_replace(j["user"].get<std::string>(), "string", task_str)
             + j["start"].get<std::string>();
    }
    return task_str;
}

// ── TryParseModelOutput ───────────────────────────────────────────────────────

bool TaskMemoBuilder::TryParseModelOutput(const std::string& raw, json& out) const
{
    size_t start = raw.find('{');
    size_t end = raw.rfind('}');
    if (start == std::string::npos || end == std::string::npos || end <= start)
        return false;
    try {
        out = json::parse(raw.substr(start, end - start + 1));
        return out.is_object();
    } catch (...) {
        return false;
    }
}

// ── Update ────────────────────────────────────────────────────────────────────

TaskMemoBuilder::UpdateResult TaskMemoBuilder::Update(const json& raw_messages,
                                                       const std::vector<GenieChatMessage>& dropped_messages)
{
    UpdateResult res;
    size_t min_trigger = std::max<size_t>(1, config_.min_dropped_for_trigger);
    if (!config_.enabled || !store_ || dropped_messages.size() < min_trigger)
        return res;

    std::string fingerprint = ComputeFingerprint(raw_messages);
    std::string first_msg_key = ComputeFirstMsgKey(raw_messages);
    std::optional<json> prev = store_->Lookup(fingerprint, first_msg_key);
    bool has_prev = prev.has_value();
    double prev_confidence = has_prev ? prev->value("confidence", 0.0) : 0.0;
    size_t prev_refresh = has_prev ? prev->value("refresh_count", (size_t)0) : 0;

    size_t already_folded = has_prev ? prev->value("_folded_count", (size_t)0) : 0;
    if (already_folded > dropped_messages.size())
        already_folded = 0;
    if (has_prev && already_folded == dropped_messages.size()) {
        res.active = true;
        res.confidence = prev_confidence;
        res.refresh_count = prev_refresh;
        res.pages_total = (prev->contains("pages") && (*prev)["pages"].is_array()) ? (*prev)["pages"].size() : 0;
        res.card_bytes = CardBytesOf(*prev);
        res.registry_bytes = RegistryBytesOf(*prev);
        res.compact_render = RenderCompact(*prev);
        return res;
    }
    const std::vector<GenieChatMessage> new_dropped(dropped_messages.begin() + (long long) already_folded,
                                                    dropped_messages.end());

    json entry = BuildRuleLayer(has_prev ? *prev : json::object(), new_dropped);
    if (config_.sediment.enabled)
        entry["_block_line_truncate"] = true;
    const bool cards_stable = UpdateCards(entry, raw_messages);
    const size_t registry_bytes = UpdateRegistry(entry, raw_messages);

    // 原始目标锚点：只在尚未捕获过时抓取一次；一旦捕获，后续轮次通过 entry/prev 透传，
    // 永不被规则层或模型层的后续更新覆盖（BuildRuleLayer 已经把 prev 的既有字段原样
    // 带入 entry，这里只需补齐"从未捕获过"的情形）。
    bool goal_already_captured = entry.contains("original_goal_raw")
        && entry["original_goal_raw"].is_string()
        && !entry["original_goal_raw"].get<std::string>().empty();
    if (!goal_already_captured) {
        auto anchor = ExtractGoalAnchor(raw_messages);
        entry["original_goal_raw"] = anchor.first;
        entry["original_goal_confidence"] = anchor.second ? std::string("high") : std::string("low");
    }

    if (ShouldTriggerModelLayer(new_dropped, has_prev, prev_confidence)
            && (!is_alive_fn_ || is_alive_fn_())) {
        std::string prompt = BuildModelPrompt(has_prev ? *prev : json::object(), entry);
        std::string raw_output = infer_fn_(prompt);
        json model_entry;
        if (!raw_output.empty() && TryParseModelOutput(raw_output, model_entry)) {
            model_entry["source_range"] = entry["source_range"];
            model_entry["content_hash"] = entry["content_hash"];
            model_entry["_folded_count"] = entry["_folded_count"];
            // pages/_next_page_id 是规则层维护的轻量分页目录，模型层不感知、不生成，
            // 必须显式透传，否则模型层触发时这批 Update() 新增的分页记录会被整体丢失。
            model_entry["pages"] = entry["pages"];
            model_entry["_next_page_id"] = entry["_next_page_id"];
            // read_files（已读文件索引）同理：模型层不感知、不生成，必须显式透传。
            model_entry["read_files"] = entry["read_files"];
            for (const char* k : {"hard_rules", "_hard_rule_files", "artifacts", "last_success",
                                   "cards", "_cards_render", "_cards_before_memo", "_card_budget_bytes", "_card_bytes_per_token",
                                   "_read_merge", "_read_page_lines",
                                   "registry", "_registry_render", "_registry_budget_bytes", "_block_line_truncate"})
                if (entry.contains(k))
                    model_entry[k] = entry[k];
            // original_goal_raw/confidence 权威兜底，从 entry 显式透传，不允许模型层输出覆盖。
            model_entry["original_goal_raw"] = entry["original_goal_raw"];
            model_entry["original_goal_confidence"] = entry["original_goal_confidence"];
            // goal_restatement 是模型层旁路产出的辅助字段，写入 original_goal_refined 并标注
            // "可能不准确"；模型本轮未给出时，沿用 entry（即 prev）里已有的上一次旁路结果。
            if (model_entry.contains("goal_restatement") && model_entry["goal_restatement"].is_string()
                    && !model_entry["goal_restatement"].get<std::string>().empty()) {
                model_entry["original_goal_refined"] =
                    safe_utf8_truncate(model_entry["goal_restatement"].get<std::string>(),
                                        config_.goal_anchor_preview_chars, "");
            } else if (entry.contains("original_goal_refined")) {
                model_entry["original_goal_refined"] = entry["original_goal_refined"];
            }
            model_entry.erase("goal_restatement");
            model_entry["confidence"] = std::min(1.0, std::max(0.0, model_entry.value("confidence", 0.75)));
            entry = model_entry;
        }
    }

    // 连续失败不轻易放弃：与 ToolCallCircuitBreakerStore 打通，只读查询（不调用
    // RecordLayer3Trigger/RecordSuccess，保持该 store 计数语义的唯一写入方仍是
    // response_dispatcher.cpp）。key 计算方式与该 store 自身注释里记录的约定一致：
    // session_key 用 ComputeFirstMsgKey（已算好的 first_msg_key），model_name 用
    // instance_config_.get_model_name()，与 response_dispatcher.cpp 查到的是同一条记录。
    std::string cb_key = ToolCallCircuitBreakerStore::MakeKey(first_msg_key, instance_config_.get_model_name());
    entry["failure_streak"] = ToolCallCircuitBreakerStore::GetInstance().GetConsecutiveCount(cb_key);
    // Render() 是 static 方法读不到 config_，阈值随 entry 一起落盘，保持 Render() 签名
    // 不变（纯函数于 entry），也让每条已存储的备忘录自描述当时生效的阈值是多少。
    entry["failure_streak_warn_threshold"] = config_.failure_streak_warn_threshold;

    const auto &redundant_cfg = instance_config_.i_model_config_.GetToolCallRepairConfig().redundant_call_guard;
    if (redundant_cfg.enabled) {
        std::string rc_key = ToolCallRepetitionStore::MakeKey(first_msg_key, instance_config_.get_model_name());
        entry["redundant_tool_call_count"] = ToolCallRepetitionStore::GetInstance().GetRepeatCount(rc_key);
        entry["redundant_tool_call_threshold"] = redundant_cfg.repeat_threshold;
    } else {
        entry["redundant_tool_call_count"] = 0;
        entry["redundant_tool_call_threshold"] = 0;
    }

    entry["refresh_count"] = prev_refresh + 1;
    store_->Put(fingerprint, first_msg_key, entry);

    res.active = true;
    res.confidence = entry.value("confidence", 0.0);
    res.refresh_count = entry.value("refresh_count", (size_t)0);
    res.pages_total = (entry.contains("pages") && entry["pages"].is_array()) ? entry["pages"].size() : 0;
    res.card_bytes = CardBytesOf(entry);
    res.card_stable = cards_stable;
    res.registry_bytes = registry_bytes;
    res.compact_render = RenderCompact(entry);
    return res;
}

// ── Render / RenderCompact ────────────────────────────────────────────────────

std::string TaskMemoBuilder::RenderCards(const json& entry)
{
    if (!entry.is_object() || !entry.value("_cards_render", false) || !entry.contains("cards")
            || !entry["cards"].is_array())
        return "";
    std::string out;
    for (const auto& c : entry["cards"]) {
        if (c.is_object() && c.contains("text") && c["text"].is_string())
            out += c["text"].get<std::string>();
    }
    return out;
}

std::string TaskMemoBuilder::Render(const json& entry, size_t max_chars)
{
    if (!entry.is_object())
        return "";

    auto render_list = [&](const char* label, const char* key) -> std::string {
        if (!entry.contains(key) || !entry[key].is_array() || entry[key].empty())
            return "";
        std::ostringstream oss;
        oss << label << ":\n";
        for (const auto& item : entry[key]) {
            if (item.is_string())
                oss << "- " << item.get<std::string>() << "\n";
        }
        return oss.str();
    };

    std::string header = "## Task Memo\n";

    // 目标锚点块：最高优先级，不参与下面的"整块跳过"循环；预算不足时只截断该块文本
    // 本身（复用 safe_utf8_truncate），绝不整块跳过——这是"不遗忘目标"的硬保证。
    // 连续失败持久化提示行同属这一不可整块丢弃的段落，与目标锚点块拼在一起。
    std::string goal_block;
    std::string goal_raw = entry.value("original_goal_raw", std::string(""));
    if (!goal_raw.empty()) {
        std::ostringstream oss;
        oss << "Original goal: " << goal_raw;
        if (entry.value("original_goal_confidence", std::string("high")) == "low")
            oss << " (low confidence: no strong signal detected in the opening messages)";
        oss << "\n";
        std::string goal_refined = entry.value("original_goal_refined", std::string(""));
        if (!goal_refined.empty()) {
            oss << "Model-restated goal (may be inaccurate; the raw goal above is authoritative): "
                << goal_refined << "\n";
        }
        goal_block = oss.str();
    }

    int failure_streak = entry.value("failure_streak", 0);
    int failure_streak_warn_threshold = entry.value("failure_streak_warn_threshold", 0);
    if (failure_streak_warn_threshold > 0 && failure_streak >= failure_streak_warn_threshold) {
        std::ostringstream oss;
        oss << "Note: " << failure_streak << " consecutive tool-call failures detected. "
            << "Try an alternative approach instead of giving up on the original goal above.\n";
        goal_block += oss.str();
    }

    int redundant_tool_call_count = entry.value("redundant_tool_call_count", 0);
    int redundant_tool_call_threshold = entry.value("redundant_tool_call_threshold", 0);
    if (redundant_tool_call_threshold > 0 && redundant_tool_call_count >= redundant_tool_call_threshold) {
        goal_block += TaskMemoBuilder::RenderRedundantToolCallNote(redundant_tool_call_count);
    }

    std::string plan = entry.value("current_plan", std::string(""));
    if (!plan.empty())
        header += "Current plan: " + plan + "\n";

    std::string tool_state_block;
    if (entry.contains("tool_state") && entry["tool_state"].is_object() && !entry["tool_state"].empty()) {
        std::ostringstream oss;
        oss << "Tool state:\n";
        for (auto it = entry["tool_state"].begin(); it != entry["tool_state"].end(); ++it) {
            oss << "- " << it.key() << ": "
                << (it.value().is_string() ? it.value().get<std::string>() : it.value().dump()) << "\n";
        }
        tool_state_block = oss.str();
    }

    // 已读文件索引：被驱逐的 read 类工具结果不再丢弃全部痕迹，模型重复 read 同一路径前
    // 可以先看这里判断是否真的需要重读；仅记路径与关键事实摘要，不含原始文件内容。
    std::string read_files_block;
    if (entry.contains("read_files") && entry["read_files"].is_object() && !entry["read_files"].empty()) {
        std::ostringstream oss;
        oss << "Files already read whose content has left the context (summary only; re-read a "
               "file only if you need details not in its summary, preferably a specific line range):\n";
        if (entry.value("_read_merge", false)) {
            std::vector<std::string> order;
            std::unordered_map<std::string, std::vector<std::pair<long long, std::string>>> spans;
            std::unordered_map<std::string, std::pair<long long, std::string>> first_preview;
            for (auto it = entry["read_files"].begin(); it != entry["read_files"].end(); ++it) {
                const std::string key = it.key();
                const size_t sep = key.rfind('#');
                const std::string path = (sep == std::string::npos) ? key : key.substr(0, sep);
                long long begin = 1;
                if (sep != std::string::npos) {
                    try {
                        begin = std::stoll(key.substr(sep + 1));
                    } catch (...) {
                        begin = 1;
                    }
                }
                long long end = begin;
                if (entry.contains("_read_page_lines") && entry["_read_page_lines"].is_object()
                        && entry["_read_page_lines"].contains(key)
                        && entry["_read_page_lines"][key].is_number()) {
                    end = entry["_read_page_lines"][key].get<long long>();
                }
                if (end < begin)
                    end = begin;
                std::ostringstream span;
                span << begin << "-" << end;
                if (spans.find(path) == spans.end())
                    order.push_back(path);
                spans[path].emplace_back(begin, span.str());
                const std::string preview = it.value().is_string() ? it.value().get<std::string>()
                                                                   : it.value().dump();
                auto fp = first_preview.find(path);
                if (fp == first_preview.end() || begin < fp->second.first)
                    first_preview[path] = { begin, preview };
            }
            for (const auto& path : order) {
                auto& pages = spans[path];
                std::stable_sort(pages.begin(), pages.end(),
                                 [](const std::pair<long long, std::string>& a,
                                    const std::pair<long long, std::string>& b) { return a.first < b.first; });
                oss << "- " << path << " (lines ";
                for (size_t i = 0; i < pages.size(); ++i)
                    oss << (i ? ", " : "") << pages[i].second;
                oss << "): " << first_preview[path].second << "\n";
            }
        } else {
            for (auto it = entry["read_files"].begin(); it != entry["read_files"].end(); ++it) {
                oss << "- " << it.key() << ": "
                    << (it.value().is_string() ? it.value().get<std::string>() : it.value().dump()) << "\n";
            }
        }
        read_files_block = oss.str();
    }

    std::string cards_block;
    if (!entry.value("_cards_before_memo", false))
        cards_block = RenderCards(entry);

    const bool registry_render = entry.value("_registry_render", false);
    std::string registry_block;
    if (registry_render && entry.contains("registry"))
        registry_block = RenderRegistryBlock(entry["registry"]);

    std::string last_success_block;
    if (!registry_render && entry.contains("last_success") && entry["last_success"].is_object()
            && !entry["last_success"].empty()) {
        std::ostringstream oss;
        oss << "Last successful call per tool (reuse instead of re-deriving the syntax):\n";
        for (auto it = entry["last_success"].begin(); it != entry["last_success"].end(); ++it)
            if (it.value().is_string())
                oss << "- " << it.value().get<std::string>() << "\n";
        last_success_block = oss.str();
    }

    // 轻量分页目录：最低优先级，预算放不下时整块跳过。只渲染 page_id/source_range/
    // gist 三个字段，不含任何原始消息文本——与 BuildRuleLayer() 落盘时的安全边界一致。
    std::string page_directory_block;
    if (entry.contains("pages") && entry["pages"].is_array() && !entry["pages"].empty()) {
        std::ostringstream oss;
        oss << "Evicted message pages (oldest first, no original text retained):\n";
        for (const auto& page : entry["pages"]) {
            if (!page.is_object())
                continue;
            oss << "- page " << page.value("page_id", (size_t) 0) << " ("
                << page.value("source_range", std::string("")) << "): "
                << page.value("gist", std::string("")) << "\n";
        }
        page_directory_block = oss.str();
    }

    // 由高到低优先级排列；预算不足时优先按条目（整行）截断列表型块，连标题加一条都放不下
    // 才整块跳过。cards 是逐字摘录、按行切会把「摘自 X 第 a-b 行」的段落腰斩，故不可截断。
    const std::string blocks[] = {
        render_list("Rules from documents you read (still in force)", "hard_rules"),
        cards_block,
        registry_block,
        registry_render ? std::string()
            : render_list("Artifacts already produced (confirmed by tool output; do not redo these steps)", "artifacts"),
        last_success_block,
        render_list("Completed", "completed"),
        render_list("Next actions", "next_actions"),
        render_list("Facts / constraints", "facts_constraints"),
        render_list("Open questions", "open_questions"),
        read_files_block,
        tool_state_block,
        page_directory_block,
    };
    static const char* kBlockNames[] = {
        "hard_rules", "cards", "registry", "artifacts", "last_success", "completed", "next_actions",
        "facts_constraints", "open_questions", "read_files", "tool_state", "page_directory",
    };
    const bool merged_reads = entry.value("_read_merge", false);
    const bool list_truncatable = entry.value("_block_line_truncate", false);
    const bool kBlockLineTruncatable[] = {
        list_truncatable, false, list_truncatable, list_truncatable, list_truncatable, list_truncatable,
        list_truncatable, list_truncatable, list_truncatable, merged_reads || list_truncatable,
        list_truncatable, list_truncatable,
    };

    std::string out = header;
    if (!goal_block.empty()) {
        if (max_chars > 0 && out.size() + goal_block.size() > max_chars) {
            // 预算不足：只截断目标锚点块文本本身，绝不整块丢弃。
            size_t remaining = (max_chars > out.size()) ? (max_chars - out.size()) : 0;
            out += safe_utf8_truncate(goal_block, remaining, "");
        } else {
            out += goal_block;
        }
    }
    size_t block_index = 0;
    for (const auto& block : blocks) {
        const size_t slot = block_index++;
        const char* block_name = kBlockNames[slot];
        if (block.empty())
            continue;
        if (max_chars > 0 && out.size() + block.size() > max_chars) {
            std::string kept;
            if (kBlockLineTruncatable[slot]) {
                const size_t remaining = max_chars - out.size();
                const size_t head_end = block.find('\n');
                size_t take = 0;
                size_t pos = 0;
                while (true) {
                    const size_t nl = block.find('\n', pos);
                    if (nl == std::string::npos || nl + 1 > remaining)
                        break;
                    take = nl + 1;
                    pos = nl + 1;
                }
                if (head_end != std::string::npos && take > head_end + 1)
                    kept = block.substr(0, take);
            }
            if (!kept.empty()) {
                My_Log{My_Log::Level::kWarning}
                    << "[TaskMemo] Render block truncated: name=" << block_name
                    << ", block_chars=" << block.size()
                    << ", kept_chars=" << kept.size()
                    << ", used_chars=" << out.size()
                    << ", max_chars=" << max_chars << std::endl;
                out += kept;
                continue;
            }
            My_Log{My_Log::Level::kWarning}
                << "[TaskMemo] Render block skipped: name=" << block_name
                << ", block_chars=" << block.size()
                << ", used_chars=" << out.size()
                << ", max_chars=" << max_chars << std::endl;
            continue;
        }
        out += block;
    }
    if (max_chars > 0 && out.size() > max_chars)
        out = safe_utf8_truncate(out, max_chars, "");
    return out;
}

std::string TaskMemoBuilder::RenderRedundantToolCallNote(int repeat_count)
{
    std::ostringstream oss;
    oss << "Note: you have called the same tool with the same arguments " << repeat_count
        << " time(s) in a row. Its result is already in the conversation (or summarized in this memo); "
           "repeating it again is unlikely to give new information. Use that result or try a different approach.\n";
    return oss.str();
}

std::string TaskMemoBuilder::RenderCompact(const json& entry)
{
    if (!entry.is_object())
        return "";

    size_t completed_n = (entry.contains("completed") && entry["completed"].is_array())
        ? entry["completed"].size() : 0;
    std::string plan = entry.value("current_plan", std::string(""));
    std::string next;
    if (entry.contains("next_actions") && entry["next_actions"].is_array() && !entry["next_actions"].empty()
            && entry["next_actions"][0].is_string()) {
        next = entry["next_actions"][0].get<std::string>();
    }

    std::ostringstream oss;
    oss << "[Task memo: " << completed_n << " step(s) completed";
    std::string goal_raw = entry.value("original_goal_raw", std::string(""));
    if (!goal_raw.empty())
        oss << "; goal: " << safe_utf8_truncate(goal_raw, 80, "...");
    if (!plan.empty())
        oss << "; plan: " << plan;
    if (!next.empty())
        oss << "; next: " << next;
    oss << "]\n";
    return oss.str();
}
