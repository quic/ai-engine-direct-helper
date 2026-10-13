//==============================================================================
//
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#include "ExecuTorchInferenceEngine.hpp"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <sstream>
#include <stdexcept>
#include <unordered_map>
#include <utility>

#if defined(_WIN32)
#include <windows.h>
#else
#include <dlfcn.h>
#endif

#include <executorch/extension/tensor/tensor_ptr.h>
#include <executorch/runtime/core/evalue.h>
#include <executorch/runtime/core/tensor_shape_dynamism.h>

namespace qnn {
namespace tools {
namespace qnn_app {
namespace {

using executorch::aten::ScalarType;
using executorch::extension::TensorPtr;

std::string resultError(const executorch::runtime::Error error) {
  return "ExecuTorch runtime error " + std::to_string(static_cast<int>(error));
}

struct BackendLibraryCache {
  std::mutex mutex;
  std::unordered_map<std::string, void*> handles;
};

BackendLibraryCache& backendLibraryCache() {
  // Backend plugins register with process-wide ExecuTorch registries from
  // static initializers. Deliberately retain their loader handles until
  // process exit so releasing one context cannot unregister a plugin that a
  // later context will use.
  static auto* cache = new BackendLibraryCache();
  return *cache;
}

std::string normalizedBackendLibraryPath(const std::string& path) {
  std::error_code error;
  const auto normalized = std::filesystem::weakly_canonical(path, error);
  return error ? path : normalized.string();
}

void* loadBackendLibrary(const std::string& path) {
  if (path.empty()) {
    return nullptr;
  }
  const auto normalized_path = normalizedBackendLibraryPath(path);
  auto& cache = backendLibraryCache();
  std::lock_guard<std::mutex> lock(cache.mutex);
  const auto existing = cache.handles.find(normalized_path);
  if (existing != cache.handles.end()) {
    return existing->second;
  }

  void* handle = nullptr;
#if defined(_WIN32)
  handle = static_cast<void*>(LoadLibraryA(path.c_str()));
  if (handle == nullptr) {
    throw std::runtime_error("unable to load ExecuTorch backend library: " + path);
  }
#else
  handle = dlopen(path.c_str(), RTLD_NOW | RTLD_GLOBAL);
  if (handle == nullptr) {
    const char* error = dlerror();
    throw std::runtime_error(
        "unable to load ExecuTorch backend library '" + path + "': " +
        (error == nullptr ? "unknown loader error" : error));
  }
#endif
  cache.handles.emplace(normalized_path, handle);
  return handle;
}

void releaseBackendLibrary(void*& handle) noexcept {
  // The shared cache owns non-empty backend plugin handles for the process
  // lifetime. Contexts only relinquish their non-owning reference.
  handle = nullptr;
}

ScalarType scalarTypeFromName(const std::string& name) {
  if (name == "uint8") return ScalarType::Byte;
  if (name == "int8") return ScalarType::Char;
  if (name == "int16") return ScalarType::Short;
  if (name == "int32") return ScalarType::Int;
  if (name == "int64") return ScalarType::Long;
  if (name == "uint16") return ScalarType::UInt16;
  if (name == "uint32") return ScalarType::UInt32;
  if (name == "uint64") return ScalarType::UInt64;
  if (name == "float16") return ScalarType::Half;
  if (name == "float32") return ScalarType::Float;
  if (name == "float64") return ScalarType::Double;
  if (name == "bool") return ScalarType::Bool;
  throw std::invalid_argument("unsupported ExecuTorch tensor dtype: " + name);
}


}  // namespace

ExecuTorchInferenceEngine::ExecuTorchInferenceEngine(
    std::string model_path, std::string backend_lib_path)
    : m_model_path(std::move(model_path)),
      m_backend_lib_path(std::move(backend_lib_path)) {}

ExecuTorchInferenceEngine::~ExecuTorchInferenceEngine() noexcept { release(); }

std::string ExecuTorchInferenceEngine::modelError(
    const std::string& operation,
    const std::string& model_path,
    const std::string& detail) {
  return "ExecuTorch model '" + model_path + "' " + operation + " failed: " + detail;
}

void ExecuTorchInferenceEngine::initialize() {
  std::lock_guard<std::mutex> lock(m_inference_mutex);
  if (m_module != nullptr) {
    return;
  }
  if (m_model_path.empty()) {
    throw std::invalid_argument(modelError("load", m_model_path, "model path is empty"));
  }

  try {
    m_backend_handle = loadBackendLibrary(m_backend_lib_path);
    m_module = std::make_unique<executorch::extension::Module>(
        m_model_path, executorch::extension::Module::LoadMode::Mmap);
    const auto load_error = m_module->load();
    if (load_error != executorch::runtime::Error::Ok) {
      releaseLocked();
      throw std::runtime_error(modelError("load", m_model_path, resultError(load_error)));
    }
    const auto methods = m_module->method_names();
    if (!methods.ok() || methods.get().size() != 1 || methods.get().find("forward") == methods.get().end()) {
      releaseLocked();
      throw std::runtime_error(modelError("validation", m_model_path,
                                          "static program must contain only a forward method"));
    }
    const auto method_error = m_module->load_method("forward");
    if (method_error != executorch::runtime::Error::Ok) {
      releaseLocked();
      throw std::runtime_error(modelError("method setup", m_model_path,
                                          resultError(method_error)));
    }
    const auto metadata = m_module->method_meta("forward");
    if (!metadata.ok()) {
      releaseLocked();
      throw std::runtime_error(modelError("metadata", m_model_path,
                                          resultError(metadata.error())));
    }
    m_method_meta = metadata.get();
    cacheMetadataLocked();
    updateProviderModeLocked();
  } catch (...) {
    releaseLocked();
    throw;
  }
}

void ExecuTorchInferenceEngine::release() noexcept {
  std::lock_guard<std::mutex> lock(m_inference_mutex);
  releaseLocked();
}

void ExecuTorchInferenceEngine::releaseLocked() noexcept {
  m_method_meta.reset();
  m_module.reset();
  releaseBackendLibrary(m_backend_handle);
  m_input_shapes.clear();
  m_output_shapes.clear();
  m_input_types.clear();
  m_output_types.clear();
  m_input_names.clear();
  m_output_names.clear();
  m_input_nbytes.clear();
  m_output_nbytes.clear();
  m_provider_mode.clear();
}

void ExecuTorchInferenceEngine::validateInitialized(const char* operation) const {
  if (m_module == nullptr || !m_method_meta.has_value()) {
    throw std::runtime_error(modelError(operation, m_model_path,
                                        "context has not been initialized or was released"));
  }
}

void ExecuTorchInferenceEngine::validateGraphIndex(size_t graph_index) const {
  if (graph_index != 0) {
    throw std::out_of_range(modelError("graph selection", m_model_path,
                                       "graphIndex must be 0 for a single forward method"));
  }
}

std::string ExecuTorchInferenceEngine::scalarTypeName(ScalarType type) {
  switch (type) {
    case ScalarType::Byte: return "uint8";
    case ScalarType::Char: return "int8";
    case ScalarType::Short: return "int16";
    case ScalarType::Int: return "int32";
    case ScalarType::Long: return "int64";
    case ScalarType::UInt16: return "uint16";
    case ScalarType::UInt32: return "uint32";
    case ScalarType::UInt64: return "uint64";
    case ScalarType::Half: return "float16";
    case ScalarType::Float: return "float32";
    case ScalarType::Double: return "float64";
    case ScalarType::Bool: return "bool";
    default: return "unknown";
  }
}

void ExecuTorchInferenceEngine::cacheMetadataLocked() {
  m_input_shapes.clear();
  m_output_shapes.clear();
  m_input_types.clear();
  m_output_types.clear();
  m_input_names.clear();
  m_output_names.clear();
  m_input_nbytes.clear();
  m_output_nbytes.clear();
  const auto& meta = *m_method_meta;
  for (size_t i = 0; i < meta.num_inputs(); ++i) {
    const auto input_tag = meta.input_tag(i);
    if (!input_tag.ok() || input_tag.get() != executorch::runtime::Tag::Tensor) {
      throw std::runtime_error(modelError("metadata", m_model_path,
                                          "forward inputs must all be tensors"));
    }
    const auto info = meta.input_tensor_meta(i);
    if (!info.ok()) throw std::runtime_error(modelError("metadata", m_model_path, resultError(info.error())));
    m_input_shapes.push_back({});
    for (const auto size : info.get().sizes()) {
      if (size < 0) throw std::runtime_error(modelError("metadata", m_model_path, "dynamic dimensions are unsupported"));
      m_input_shapes.back().push_back(static_cast<size_t>(size));
    }
    const auto input_type = scalarTypeName(info.get().scalar_type());
    if (input_type == "unknown") {
      throw std::runtime_error(modelError("metadata", m_model_path,
                                          "unsupported input tensor dtype"));
    }
    m_input_types.push_back(input_type);
    m_input_names.emplace_back(info.get().name());
    m_input_nbytes.push_back(info.get().nbytes());
  }
  for (size_t i = 0; i < meta.num_outputs(); ++i) {
    const auto output_tag = meta.output_tag(i);
    if (!output_tag.ok() || output_tag.get() != executorch::runtime::Tag::Tensor) {
      throw std::runtime_error(modelError("metadata", m_model_path,
                                          "forward outputs must all be tensors"));
    }
    const auto info = meta.output_tensor_meta(i);
    if (!info.ok()) throw std::runtime_error(modelError("metadata", m_model_path, resultError(info.error())));
    m_output_shapes.push_back({});
    for (const auto size : info.get().sizes()) {
      if (size < 0) throw std::runtime_error(modelError("metadata", m_model_path, "dynamic dimensions are unsupported"));
      m_output_shapes.back().push_back(static_cast<size_t>(size));
    }
    const auto output_type = scalarTypeName(info.get().scalar_type());
    if (output_type == "unknown") {
      throw std::runtime_error(modelError("metadata", m_model_path,
                                          "unsupported output tensor dtype"));
    }
    m_output_types.push_back(output_type);
    m_output_names.emplace_back(info.get().name());
    m_output_nbytes.push_back(info.get().nbytes());
  }
}

void ExecuTorchInferenceEngine::updateProviderModeLocked() {
  m_provider_mode = "cpu";
  const auto& meta = *m_method_meta;
  for (size_t i = 0; i < meta.num_backends(); ++i) {
    const auto backend = meta.get_backend_name(i);
    if (!backend.ok() || backend.get() == nullptr) continue;
    std::string name = backend.get();
    std::transform(name.begin(), name.end(), name.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (name.find("qnn") != std::string::npos || name.find("qualcomm") != std::string::npos) {
      m_provider_mode = "qnn";
      return;
    }
    if (name.find("xnnpack") != std::string::npos) {
      m_provider_mode = "xnnpack";
    }
  }
}

std::vector<std::vector<uint8_t>> ExecuTorchInferenceEngine::inference(
    const std::vector<const uint8_t*>& input_buffers,
    const std::vector<size_t>& input_sizes,
    size_t graph_index) {
  std::lock_guard<std::mutex> lock(m_inference_mutex);
  validateInitialized("inference");
  validateGraphIndex(graph_index);
  if (input_buffers.size() != m_input_shapes.size() || input_sizes.size() != m_input_shapes.size()) {
    throw std::invalid_argument(modelError("inference", m_model_path, "input tensor count does not match the model"));
  }

  std::vector<TensorPtr> tensors;
  std::vector<executorch::runtime::EValue> values;
  tensors.reserve(input_buffers.size());
  values.reserve(input_buffers.size());
  for (size_t i = 0; i < input_buffers.size(); ++i) {
    const size_t expected = m_input_nbytes[i];
    if (input_buffers[i] == nullptr || input_sizes[i] != expected) {
      std::ostringstream detail;
      detail << "input " << i << " byte size is " << input_sizes[i] << ", expected " << expected;
      throw std::invalid_argument(modelError("inference", m_model_path, detail.str()));
    }
    std::vector<executorch::aten::SizesType> sizes;
    for (size_t dim : m_input_shapes[i]) sizes.push_back(static_cast<executorch::aten::SizesType>(dim));
    const auto type = scalarTypeFromName(m_input_types[i]);
    tensors.push_back(executorch::extension::make_tensor_ptr(
        std::move(sizes), const_cast<uint8_t*>(input_buffers[i]), type,
        executorch::aten::Device(executorch::aten::DeviceType::CPU),
        executorch::runtime::TensorShapeDynamism::STATIC));
    if (!tensors.back()) throw std::runtime_error(modelError("inference", m_model_path, "input tensor allocation failed"));
    values.emplace_back(*tensors.back());
  }

  const auto result = m_module->execute("forward", values);
  if (!result.ok()) throw std::runtime_error(modelError("inference", m_model_path, resultError(result.error())));
  if (result.get().size() != m_output_nbytes.size()) {
    throw std::runtime_error(modelError("inference", m_model_path,
                                       "forward returned an unexpected number of outputs"));
  }
  std::vector<std::vector<uint8_t>> outputs;
  outputs.reserve(result.get().size());
  for (size_t i = 0; i < result.get().size(); ++i) {
    const auto& value = result.get()[i];
    if (value.tag != executorch::runtime::Tag::Tensor) {
      throw std::runtime_error(modelError("inference", m_model_path, "forward returned a non-tensor value"));
    }
    const auto& tensor = value.toTensor();
    const auto* data = static_cast<const uint8_t*>(tensor.const_data_ptr());
    if (data == nullptr) throw std::runtime_error(modelError("inference", m_model_path, "output tensor storage is unavailable"));
    if (tensor.nbytes() != m_output_nbytes[i]) {
      throw std::runtime_error(modelError("inference", m_model_path,
                                         "forward returned an output with unexpected byte size"));
    }
    outputs.emplace_back(data, data + tensor.nbytes());
  }
  return outputs;
}

std::vector<std::vector<size_t>> ExecuTorchInferenceEngine::getInputShapes(size_t graph_index) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("input shape query"); validateGraphIndex(graph_index); return m_input_shapes;
}
std::vector<std::vector<size_t>> ExecuTorchInferenceEngine::getOutputShapes(size_t graph_index) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("output shape query"); validateGraphIndex(graph_index); return m_output_shapes;
}
std::vector<std::string> ExecuTorchInferenceEngine::getInputDataType(size_t graph_index) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("input dtype query"); validateGraphIndex(graph_index); return m_input_types;
}
std::vector<std::string> ExecuTorchInferenceEngine::getOutputDataType(size_t graph_index) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("output dtype query"); validateGraphIndex(graph_index); return m_output_types;
}
std::vector<std::string> ExecuTorchInferenceEngine::getInputName(size_t graph_index) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("input name query"); validateGraphIndex(graph_index); return m_input_names;
}
std::vector<std::string> ExecuTorchInferenceEngine::getOutputName(size_t graph_index) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("output name query"); validateGraphIndex(graph_index); return m_output_names;
}
std::string ExecuTorchInferenceEngine::getGraphName(size_t graph_index) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("graph name query"); validateGraphIndex(graph_index); return m_model_path;
}
uint64_t ExecuTorchInferenceEngine::getProfilingEvent(uint32_t) const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("profiling query"); return 0;
}
std::string ExecuTorchInferenceEngine::getProviderMode() const {
  std::lock_guard<std::mutex> lock(m_inference_mutex); validateInitialized("provider query"); return m_provider_mode;
}

}  // namespace qnn_app
}  // namespace tools
}  // namespace qnn
