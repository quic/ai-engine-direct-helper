//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "audit_logger.h"
#include "log.h"
#include <nlohmann/json.hpp>
#include <algorithm>

using audit_json = nlohmann::ordered_json;

AuditLogger::AuditLogger(const RoutingConfig::MetricsConfig &metrics_config)
    : metrics_config_(metrics_config),
      last_summary_time_(std::chrono::steady_clock::now())
{
    int sample_size = std::max(1, metrics_config_.latency_sample_size);
    latency_samples_.resize(static_cast<size_t>(sample_size), 0);
}

std::string AuditLogger::VectorToJsonArray(const std::vector<std::string> &vec)
{
    audit_json arr = audit_json::array();
    for (const auto &s : vec)
        arr.push_back(s);
    return arr.dump();
}

void AuditLogger::Log(const AuditRecord &record)
{
    audit_json j;
    j["type"]                       = "audit";
    j["request_id"]                 = record.request_id;
    j["timestamp"]                  = record.timestamp;
    j["session_id"]                 = record.session_id;
    j["route_decision"]             = to_string(record.route_decision);
    j["sensitivity_level"]          = to_string(record.sensitivity_level);
    j["complexity_level"]           = to_string(record.complexity_level);
    j["desensitized"]               = record.desensitized;
    j["desensitize_strategies"]     = record.desensitize_strategies;
    j["desensitize_success"]        = record.desensitize_success;
    j["desensitize_failure_reason"] = record.desensitize_failure_reason;
    j["cloud_endpoint"]             = record.cloud_endpoint;
    j["latency_ms"]                 = record.latency_ms;
    j["http_status"]                = record.http_status;
    j["retry_count"]                = record.retry_count;
    j["hit_rule_ids"]               = record.hit_rule_ids;
    j["hit_categories"]             = record.hit_categories;
    j["route_reason"]               = record.route_reason;
    j["rule_engine_failed"]         = record.rule_engine_failed;
    j["tool_output_escalation"]     = record.tool_output_escalation;
    j["latency_warning"]            = record.latency_warning;
    j["oversized_field_warning"]    = record.oversized_field_warning;
    j["oversized_fields_count"]     = record.oversized_fields_count;
    j["keywords_dict_reloaded"]     = record.keywords_dict_reloaded;
    j["keywords_dict_rules_count"]  = record.keywords_dict_rules_count;
    j["local_output_overflow"]      = record.local_output_overflow;
    j["local_input_overflow"]       = record.local_input_overflow;
    j["sticky_route_hit"]           = record.sticky_route_hit;
    j["cloud_tier"]                 = record.cloud_tier;
    j["incremental_check_used"]     = record.incremental_check_used;
    j["incremental_new_messages"]   = record.incremental_new_messages;
    j["total_messages_count"]       = record.total_messages_count;
    if (!record.incremental_fallback_reason.empty()) {
        j["incremental_fallback_reason"] = record.incremental_fallback_reason;
    }
    if (record.cloud_total_tokens > 0 || record.cloud_prompt_tokens > 0 || record.cloud_completion_tokens > 0) {
        audit_json token_usage;
        token_usage["prompt_tokens"]     = record.cloud_prompt_tokens;
        token_usage["completion_tokens"] = record.cloud_completion_tokens;
        token_usage["total_tokens"]      = record.cloud_total_tokens;
        j["cloud_token_usage"] = token_usage;
    }

    My_Log{My_Log::Level::kInfo} << "[AUDIT] " << j.dump() << std::endl;

    RecordMetrics(record);
}

void AuditLogger::RecordMetrics(const AuditRecord &record)
{
    if (metrics_config_.summary_every_n_requests <= 0 &&
        metrics_config_.summary_every_seconds <= 0)
    {
        return;
    }

    std::lock_guard<std::mutex> lock(metrics_mutex_);

    total_requests_++;

    switch (record.route_decision) {
        case RouteTarget::LOCAL:
            route_local_++;
            break;
        case RouteTarget::ENTERPRISE_CLOUD:
            route_enterprise_cloud_++;
            route_cloud_++;
            break;
        case RouteTarget::PUBLIC_CLOUD:
            route_public_cloud_++;
            route_cloud_++;
            break;
        case RouteTarget::POLICY_ERROR:
            route_error_++;
            break;
        default: break;
    }

    switch (record.sensitivity_level) {
        case SensitivityLevel::S0: s0_count_++; break;
        case SensitivityLevel::S1: s1_count_++; break;
        case SensitivityLevel::S2: s2_count_++; break;
        default: break;
    }

    switch (record.complexity_level) {
        case ComplexityLevel::C0: c0_count_++; break;
        case ComplexityLevel::C1: c1_count_++; break;
        case ComplexityLevel::C2: c2_count_++; break;
        default: break;
    }

    if (record.desensitized) {
        if (record.desensitize_success) {
            desensitize_success_++;
        } else {
            desensitize_fail_++;
            std::string reason = record.desensitize_failure_reason;
            if (reason.size() > 64) reason = reason.substr(0, 64);
            if (!reason.empty()) {
                fail_reason_counts_[reason]++;
            }
        }
    }

    if (!latency_samples_.empty()) {
        latency_samples_[static_cast<size_t>(latency_sample_pos_)] = record.latency_ms;
        latency_sample_pos_ = (latency_sample_pos_ + 1) %
                              static_cast<int>(latency_samples_.size());
    }

    if (record.route_decision == RouteTarget::ENTERPRISE_CLOUD ||
        record.route_decision == RouteTarget::PUBLIC_CLOUD) {
        cloud_total_++;
        if (record.latency_warning) {
            cloud_timeout_count_++;
        }
        if (record.http_status >= 500) {
            cloud_5xx_count_++;
        }
        if (record.cloud_total_tokens > 0 || record.cloud_prompt_tokens > 0 || record.cloud_completion_tokens > 0) {
            cloud_prompt_tokens_total_     += record.cloud_prompt_tokens;
            cloud_completion_tokens_total_ += record.cloud_completion_tokens;
            cloud_tokens_total_            += record.cloud_total_tokens;
        }
    }

    if (record.sensitivity_level == SensitivityLevel::S2 &&
        record.route_decision == RouteTarget::POLICY_ERROR) {
        s2_block_count_++;
    }

    bool should_output = false;
    if (metrics_config_.summary_every_n_requests > 0 &&
        total_requests_ % metrics_config_.summary_every_n_requests == 0) {
        should_output = true;
    }

    if (!should_output && metrics_config_.summary_every_seconds > 0) {
        auto now = std::chrono::steady_clock::now();
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
            now - last_summary_time_).count();
        if (elapsed >= metrics_config_.summary_every_seconds) {
            should_output = true;
        }
    }

    if (should_output) {
        OutputMetricsSummary();
        last_summary_time_ = std::chrono::steady_clock::now();
    }
}

int64_t AuditLogger::CalcPercentile(std::vector<int64_t> samples, double percentile)
{
    samples.erase(std::remove(samples.begin(), samples.end(), 0LL), samples.end());
    if (samples.empty()) return 0;

    std::sort(samples.begin(), samples.end());
    size_t idx = static_cast<size_t>(percentile / 100.0 * static_cast<double>(samples.size()));
    if (idx >= samples.size()) idx = samples.size() - 1;
    return samples[idx];
}

void AuditLogger::OutputMetricsSummary()
{
    int64_t p50 = CalcPercentile(latency_samples_, 50.0);
    int64_t p95 = CalcPercentile(latency_samples_, 95.0);

    double cloud_timeout_rate = 0.0;
    double cloud_5xx_rate = 0.0;
    if (cloud_total_ > 0) {
        cloud_timeout_rate = static_cast<double>(cloud_timeout_count_) /
                             static_cast<double>(cloud_total_);
        cloud_5xx_rate = static_cast<double>(cloud_5xx_count_) /
                         static_cast<double>(cloud_total_);
    }

    double s2_block_rate = 0.0;
    if (total_requests_ > 0) {
        s2_block_rate = static_cast<double>(s2_block_count_) /
                        static_cast<double>(total_requests_);
    }

    int topn = std::max(1, metrics_config_.fail_reason_topn);
    std::vector<std::pair<std::string, int>> fail_reasons_sorted(
        fail_reason_counts_.begin(), fail_reason_counts_.end());
    std::sort(fail_reasons_sorted.begin(), fail_reasons_sorted.end(),
              [](const auto &a, const auto &b) { return a.second > b.second; });

    audit_json fail_topn_arr = audit_json::array();
    for (int i = 0; i < topn && i < static_cast<int>(fail_reasons_sorted.size()); ++i) {
        audit_json item;
        item["reason"] = fail_reasons_sorted[i].first;
        item["count"]  = fail_reasons_sorted[i].second;
        fail_topn_arr.push_back(item);
    }

    audit_json summary;
    summary["type"]                = "metrics_summary";
    summary["total_requests"]      = total_requests_;

    audit_json route_dist;
    route_dist["local"]  = route_local_;
    route_dist["cloud"]  = route_cloud_;
    route_dist["error"]  = route_error_;
    summary["route_distribution"] = route_dist;

    audit_json sens_dist;
    sens_dist["S0"] = s0_count_;
    sens_dist["S1"] = s1_count_;
    sens_dist["S2"] = s2_count_;
    summary["sensitivity_distribution"] = sens_dist;

    audit_json comp_dist;
    comp_dist["C0"] = c0_count_;
    comp_dist["C1"] = c1_count_;
    comp_dist["C2"] = c2_count_;
    summary["complexity_distribution"] = comp_dist;

    audit_json desens;
    desens["success"]       = desensitize_success_;
    desens["fail"]          = desensitize_fail_;
    desens["fail_topn"]     = fail_topn_arr;
    summary["desensitization"] = desens;

    audit_json latency;
    latency["p50_ms"] = p50;
    latency["p95_ms"] = p95;
    summary["latency"] = latency;

    audit_json cloud;
    cloud["total"]        = cloud_total_;
    cloud["timeout_rate"] = cloud_timeout_rate;
    cloud["5xx_rate"]     = cloud_5xx_rate;
    audit_json cloud_tokens;
    cloud_tokens["prompt_tokens_total"]     = cloud_prompt_tokens_total_;
    cloud_tokens["completion_tokens_total"] = cloud_completion_tokens_total_;
    cloud_tokens["total_tokens_total"]      = cloud_tokens_total_;
    if (cloud_total_ > 0 && cloud_tokens_total_ > 0) {
        cloud_tokens["avg_tokens_per_request"] =
            static_cast<double>(cloud_tokens_total_) / static_cast<double>(cloud_total_);
    } else {
        cloud_tokens["avg_tokens_per_request"] = 0.0;
    }
    cloud["token_usage"] = cloud_tokens;
    summary["cloud"] = cloud;

    summary["s2_block_rate"] = s2_block_rate;
    summary["s2_block_count"] = s2_block_count_;

    My_Log{My_Log::Level::kInfo} << "[METRICS] " << summary.dump() << std::endl;
}
