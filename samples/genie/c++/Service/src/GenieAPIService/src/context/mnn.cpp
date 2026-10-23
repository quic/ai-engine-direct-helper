//==============================================================================
//
// Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "mnn.h"

#include <llm/llm.hpp>
#include <MNN/AutoTime.hpp>
#include <MNN/expr/ExecutorScope.hpp>

#include <iomanip>
#include <mutex>
#include <sstream>

#include "log.h"
#include "utils.h"
#include "watermark_provider_host.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/sysinfo.h>
#endif

static constexpr double kMnnMemoryEstimateFactor = 1.3;
static constexpr uint64_t kMnnMemoryEstimateMarginBytes = 2ULL * 1024 * 1024 * 1024;
static constexpr uint64_t kMnnLowMemoryAbortThresholdBytes = 1ULL * 1024 * 1024 * 1024;
static constexpr int kMnnLowMemoryCheckIntervalTokens = 16;

using namespace MNN::Transformer;

class MNNContext::Impl
{
public:
    std::unique_ptr<Llm> m_llm;
};

// Utf8StreamProcessor now lives in common/utils.h (shared with the GGUF backend, which needs
// the exact same per-token UTF-8 boundary buffering).

// https://github.com/alibaba/MNN/blob/master/apps/Android/MnnLlmChat/app/src/main/cpp/llm_stream_buffer.hpp
class LlmStreamBuffer : public std::streambuf
{
public:
    using CallBack = std::function<void(const char *str, size_t len)>;;

    explicit LlmStreamBuffer(CallBack callback) :
            m_callback(std::move(callback))
    {}

protected:
    std::streamsize xsputn(const char *s, std::streamsize n)

    override
    {
        if (m_callback)
        {
            m_callback(s, n);
        }
        return n;
    }

private:
    CallBack m_callback = nullptr;
};

uint64_t MNNContext::EstimateMnnMemoryRequirement(const std::string &model_path)
{
    uint64_t total_bytes = 0;
    std::vector<std::string> files;
    if (File::MatchFileInDir(model_path, ".mnn", &files))
    {
        for (const auto &file: files)
        {
            total_bytes += File::get_file_size(file, std::ios::binary);
        }
    }
    return static_cast<uint64_t>(static_cast<double>(total_bytes) * kMnnMemoryEstimateFactor) + kMnnMemoryEstimateMarginBytes;
}

uint64_t MNNContext::GetAvailablePhysicalMemoryBytes()
{
#ifdef _WIN32
    MEMORYSTATUSEX statex;
    statex.dwLength = sizeof(statex);
    if (GlobalMemoryStatusEx(&statex))
    {
        return statex.ullAvailPhys;
    }
#else
    struct sysinfo info{};
    if (sysinfo(&info) == 0)
    {
        return static_cast<uint64_t>(info.freeram) * info.mem_unit;
    }
#endif
    return UINT64_MAX;
}

MNNContext::MNNContext(const ModelInstanceConfig &config) :
        ContextBase(config)
{
    impl_ = new Impl{};
    MNN::BackendConfig backendConfig;
    auto executor = MNN::Express::Executor::newExecutor(MNN_FORWARD_CPU, backendConfig, 1);
    MNN::Express::ExecutorScope s(executor);
    std::string config_path = config.i_model_config_.get_config_path();
    impl_->m_llm = std::unique_ptr<Llm>(Llm::createLLM(config_path.c_str()));

    bool result = impl_->m_llm->load();
    if (!result)
    {
        throw std::runtime_error("MNNContext::MNNContext Load model failed.\n");
    }

    // One-time honest log: MNN/CPU does not support token-level watermarking.
    static std::once_flag s_mnn_wm_log_once;
    std::call_once(s_mnn_wm_log_once, []()
    {
        if (WatermarkProviderHost::Instance().HasTokenHook())
        {
            My_Log{My_Log::Level::kWarning}
                << "[Watermark] MNN/CPU backend does not support token-level watermarking. "
                << "Watermark plugin is loaded but cannot operate at token level on this backend.\n";
        }
    });
}

MNNContext::~MNNContext()
{
    impl_->m_llm = nullptr;
    delete impl_;
    My_Log{} << "MNNContext::~MNNContext() Done。\n";
}

bool MNNContext::Query(const ModelInput &model_input, const Callback& callback,
                       PrefillHeartbeatCallback /*prefill_heartbeat*/)
{
    // 注意：MNN 后端目前不支持 prefill 阶段心跳（MNN 推理接口不提供批次级回调）。
    // prefill_heartbeat 参数保留以满足接口兼容性，暂不使用。
    auto &prompt = model_input.text_;
    {
        const char *mnn_label = (model_input.agent_type_ == "main") ? "MAINAGENT_INFERENCE" : "SUBAGENT_INFERENCE";
        My_Log{} << "\n[Prompt] [" << mnn_label << "][" << model_config_.get_model_name() << "]:\n"
                 << prompt << "\n------------\n\n"
                 << "[Response] [" << mnn_label << "][" << model_config_.get_model_name() << "]:\n";
    }

    m_stop = false;
    stopped_by_output_limit_ = false;
    impl_->m_llm->reset();

    std::vector<int> input_ids = impl_->m_llm->tokenizer_encode(prompt);

    Utf8StreamProcessor processor([&](std::string &utf8Char)
                                  {
                                      if (!callback(utf8Char))
                                      {
                                          m_stop = true;
                                      }
                                  });

    LlmStreamBuffer stream_buffer{[&processor](const char *str, size_t len)
                                  {
                                      processor.processStream(str, len);
                                  }};

    std::ostream output_stream(&stream_buffer);
    impl_->m_llm->response(input_ids, &output_stream, nullptr, 0);

    int n_generated = 0;
    while (!impl_->m_llm->stoped() && !m_stop)
    {
        impl_->m_llm->generate(1);
        n_generated++;
        if (max_length_ > 0 && n_generated >= max_length_)
        {
            My_Log{My_Log::Level::kWarning}
                << "[MNN] Generated " << n_generated
                << " tokens, reached max output limit " << max_length_
                << ". Stopping generation." << std::endl;
            stopped_by_output_limit_ = true;
            m_stop = true;
        }
        else if (n_generated % kMnnLowMemoryCheckIntervalTokens == 0 &&
                 GetAvailablePhysicalMemoryBytes() < kMnnLowMemoryAbortThresholdBytes)
        {
            My_Log{My_Log::Level::kError}
                << "[MNN] Available physical memory dropped below "
                << kMnnLowMemoryAbortThresholdBytes << " bytes after generating "
                << n_generated << " tokens. Aborting generation early to avoid an OS-level OOM kill."
                << std::endl;
            m_stop = true;
        }
    }

    m_stop = false;

    output_stream.flush();

    return true;
}

bool MNNContext::Stop()
{
    m_stop = true;
    return true;
}

size_t MNNContext::TokenLength(const std::string &text)
{
    if (!impl_ || !impl_->m_llm)
    {
        return text.size();
    }
    try
    {
        std::vector<int> tokens = impl_->m_llm->tokenizer_encode(text);
        return tokens.size();
    }
    catch (const std::exception &e)
    {
        My_Log{My_Log::Level::kWarning}
            << "[MNN] TokenLength failed: " << e.what()
            << ", falling back to text.size()" << std::endl;
        return text.size();
    }
}

json MNNContext::HandleProfile()
{
    // 从 LlmContext 中读取性能数据（LlmContext 由 MNN LLM 在推理过程中填充）
    // LlmContext 字段说明（来自 llm.hpp）：
    //   prompt_len  : prompt token 数
    //   gen_seq_len : 生成 token 数
    //   prefill_us  : prefill 阶段耗时（微秒）
    //   decode_us   : decode 阶段耗时（微秒）
    const LlmContext *ctx = impl_->m_llm->getContext();
    if (!ctx)
    {
        return {};
    }

    json result;
    std::ostringstream oss;

    double prefill_s = static_cast<double>(ctx->prefill_us) / 1000000.0;
    oss << std::fixed << std::setprecision(2) << prefill_s;
    result["time_to_first_token"] = oss.str();

    oss.str("");
    double decode_s = static_cast<double>(ctx->decode_us) / 1000000.0;
    oss << std::fixed << std::setprecision(2) << decode_s;
    result["token_generation_time"] = oss.str();

    oss.str("");
    double prefill_rate = (prefill_s > 0.0 && ctx->prompt_len > 0)
                          ? static_cast<double>(ctx->prompt_len) / prefill_s
                          : 0.0;
    oss << std::fixed << std::setprecision(2) << prefill_rate;
    result["prompt_processing_rate"] = oss.str();

    oss.str("");
    double decode_rate = (decode_s > 0.0 && ctx->gen_seq_len > 0)
                         ? static_cast<double>(ctx->gen_seq_len) / decode_s
                         : 0.0;
    oss << std::fixed << std::setprecision(2) << decode_rate;
    result["token_generation_rate"] = oss.str();

    result["num_prompt_tokens"] = ctx->prompt_len;
    result["num_generated_tokens"] = ctx->gen_seq_len;

    return result;
}
