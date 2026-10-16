//==============================================================================
//
// Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
//
// SPDX-License-Identifier: BSD-3-Clause
//
//==============================================================================

#pragma once

#ifndef _APPBUILDER_H
#define _APPBUILDER_H

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include <cstdint>
#include <numeric>
#include <unordered_map>
#include <mutex>
#include <algorithm>
#include <cctype>
#include <memory>
#include <cstring>
#include <stdexcept>

#include "LibAppBuilder.hpp"
#include "Lora.hpp"

#if defined(APPBUILDER_ENABLE_TFLITE) || defined(APPBUILDER_ENABLE_TFLITE_CPU)
#include "TFLiteInferenceEngine.hpp"
#endif
#if defined(APPBUILDER_ENABLE_EXECUTORCH)
#include "ExecuTorchInferenceEngine.hpp"
#endif

using namespace std;
namespace py = pybind11;

LibAppBuilder g_LibAppBuilder;

// -----------------------------------------------------------------------------
// Helper: map dtype string (from getInputDataType/getOutputDataType) to numpy dtype
// Returns false if the string is not a known/representable dtype ('out' unchanged).
//
// QNN quantized (fixed-point) types are exposed by dataTypeToString() as
// "ufpN"/"sfpN". In NATIVE mode their buffers hold the raw quantized integers,
// so they map to the unsigned/signed integer numpy dtype of the same width:
//   ufp8 -> uint8, ufp16 -> uint16, ufp32 -> uint32
//   sfp8 -> int8,  sfp16 -> int16,  sfp32 -> int32
// NOTE: "fp16" is the float16 alias, "ufp16"/"sfp16" are fixed-point 16-bit.
// 4-bit types (ufp4/sfp4) have no numpy equivalent and are reported as unknown.
// -----------------------------------------------------------------------------
static inline bool tryDtypeFromString(const std::string& dtypeStr, py::dtype& out) {
    std::string t = dtypeStr;
    for (auto& c : t) c = static_cast<char>(::tolower(c));

    if (t == "int8" || t == "sfp8") {
        out = py::dtype::of<int8_t>();
    } else if (t == "uint8" || t == "ufp8") {
        out = py::dtype::of<uint8_t>();
    } else if (t == "int16" || t == "sfp16") {
        out = py::dtype::of<int16_t>();
    } else if (t == "uint16" || t == "ufp16") {
        out = py::dtype::of<uint16_t>();
    } else if (t == "int32" || t == "sfp32") {
        out = py::dtype::of<int32_t>();
    } else if (t == "uint32" || t == "ufp32") {
        out = py::dtype::of<uint32_t>();
    } else if (t == "int64") {
        out = py::dtype::of<int64_t>();
    } else if (t == "uint64") {
        out = py::dtype::of<uint64_t>();
    } else if (t == "float16" || t == "fp16") {
        // NOTE:
        // Some pybind11 versions (including some builds with CPython 3.12) do NOT provide pybind11::half.
        // Use NumPy dtype string instead, which is stable across pybind11 versions.
        // This requires NumPy to be available at runtime (it already is for py::array usage).
        out = py::dtype("float16");
    } else if (t == "float32" || t == "fp32" || t == "float") {
        out = py::dtype::of<float>();
    } else if (t == "float64" || t == "fp64" || t == "double") {
        out = py::dtype::of<double>();
    } else if (t == "bool" || t == "bool_" || t == "bool8") {
        out = py::dtype::of<bool>();
    } else {
        return false;
    }
    return true;
}

static inline py::dtype dtypeFromString(const std::string& dtypeStr) {
    // Fallback: treat as raw bytes
    py::dtype dt = py::dtype::of<uint8_t>();
    tryDtypeFromString(dtypeStr, dt);
    return dt;
}

// ---------------------------------------------------------------------------------
// Helper: case-insensitive "float32 request" for input_data_type/output_data_type
// Accepts: "float", "float32", "fp32"
// ---------------------------------------------------------------------------------
static inline bool isFloat32Request(const std::string& s) {
    std::string t = s;
    for (auto& c : t) c = static_cast<char>(::tolower(c));
    return (t == "float" || t == "float32" || t == "fp32");
}

// ---------------------------------------------------------------------------------
// Helper: identify if a py::dtype is float32 (NumPy kind 'f' and itemsize == 4)
// Note: We avoid relying on dtype object identity; use kind/itemsize instead.
// ---------------------------------------------------------------------------------
static inline bool isNumpyFloat32Dtype(const py::dtype& dt) {
    try {
        // dt.kind is a 1-char string in NumPy, e.g. 'f' for floating
        std::string kindStr = py::str(dt.attr("kind"));
        char kind = kindStr.empty() ? '\0' : kindStr[0];
        py::ssize_t itemsize = dt.attr("itemsize").cast<py::ssize_t>();
        return (kind == 'f' && itemsize == 4);
    } catch (...) {
        // conservative fallback
        return false;
    }
}

// ---------------------------------------------------------------------------
// Helper: product of dims (for output element count)
// ---------------------------------------------------------------------------
static inline size_t productDims(const std::vector<size_t>& dims) {
    if (dims.empty()) return 0;
    size_t p = 1;
    for (size_t d : dims) {
        // guard overflow is optional; keep simple
        p *= d;
    }
    return p;
}

// ---------------------------------------------------------------------------
// Helper: infer actual numpy dtype from (outputSizeBytes, outputShape, modelReportedDtype, floatOutMode)
// - For output_data_type == float (FLOAT_ONLY), QnnInferenceEngine converts non-float32 outputs to
//   float32 and reports outputSize as floatBytes, while getOutputDataType() still returns the model
//   native dtype.
// - For output_data_type == native (NATIVE_ONLY), outputs are the raw tensor bytes, so the
//   model-reported dtype is authoritative (e.g. ufp16 -> uint16, NOT float16).
// - We use bytesPerElem = outputSizeBytes / expectedElems to detect the real buffer element width,
//   and only trust the model-reported dtype when its itemsize matches that width.
// ---------------------------------------------------------------------------
static inline py::dtype inferOutputNumpyDtype(size_t outputSizeBytes,
                                             const std::vector<size_t>& outShape,
                                             const std::string& modelReportedDtypeStr,
                                             bool floatOutMode) {
    // fallback to model-reported dtype
    py::dtype fallback = py::dtype::of<uint8_t>();
    const bool modelDtypeKnown = tryDtypeFromString(modelReportedDtypeStr, fallback);

    const size_t expectedElems = productDims(outShape);
    if (expectedElems == 0) {
        return fallback;
    }
    if (outputSizeBytes == 0) {
        return fallback;
    }
    if ((outputSizeBytes % expectedElems) != 0) {
        // size mismatch; keep old behavior
        return fallback;
    }

    const size_t bytesPerElem = outputSizeBytes / expectedElems;

    // FLOAT_ONLY: every output buffer is float32 (converted, or float32 natively).
    if (floatOutMode && bytesPerElem == 4) {
        return py::dtype::of<float>();
    }

    // Model-reported dtype matches the buffer element width: it is the real buffer dtype.
    // This keeps float16 for float16 tensors and gives uint16/int16 for ufp16/uint16/sfp16/int16,
    // int32/uint32 for int32/uint32/sfp32/ufp32, int8 for sfp8/int8, bool for bool_, etc.
    if (modelDtypeKnown && static_cast<size_t>(fallback.itemsize()) == bytesPerElem) {
        return fallback;
    }

    // Unknown or mismatching model dtype: expose the raw bits with a neutral dtype of the same width.
    // NOTE: do NOT expose 2-byte buffers as float16 here; reinterpreting quantized integers as
    // IEEE half produces NaN/inf/negative values (e.g. argmax over logits returns garbage).
    if (bytesPerElem == 4) {
        return py::dtype::of<float>();   // legacy behavior for 4-byte buffers
    }
    if (bytesPerElem == 2) {
        return py::dtype::of<uint16_t>();
    }
    if (bytesPerElem == 1) {
        return py::dtype::of<uint8_t>();
    }
    // otherwise fallback
    return fallback;
}

// ---------------------------------------------------------------------------
// Helper: validate one NATIVE-mode input array against the model input tensor metadata.
// In NATIVE mode the input bytes are copied into the QNN tensor as-is (no conversion), and the copy
// length is the tensor's own byte length (IOTensor::populateInputTensor). So:
// - byte size mismatch -> std::invalid_argument (Python ValueError). Otherwise QNN would read past
//   the end of a too-small array or silently ignore part of a too-large one (e.g. float32 data
//   passed to a uint16 tensor).
// - same byte size but different dtype (e.g. a float16 array for a 'ufp16' tensor) -> RuntimeWarning.
//   The bytes are still passed through unchanged, for backward compatibility with existing code that
//   carries raw quantized values via .view(np.float16).
// Validation is skipped when metadata is unavailable/incomplete or the model dtype has no numpy
// equivalent (e.g. 4-bit types); the engine then behaves exactly as before.
// ---------------------------------------------------------------------------
static inline void validateNativeInput(const std::string& model_name, size_t inputIdx,
                                       const py::array& arr,
                                       const std::vector<std::string>& expectedTypes,
                                       const std::vector<std::vector<size_t>>& expectedShapes) {
    if (inputIdx >= expectedTypes.size() || expectedTypes.size() != expectedShapes.size()) {
        return;
    }
    py::dtype expected = py::dtype::of<uint8_t>();
    if (!tryDtypeFromString(expectedTypes[inputIdx], expected)) {
        return;
    }
    const size_t expectedElems = productDims(expectedShapes[inputIdx]);
    if (expectedElems == 0) {
        return;
    }

    const std::string actualDtypeStr   = py::str(arr.dtype()).cast<std::string>();
    const std::string expectedDtypeStr = py::str(expected).cast<std::string>();
    const size_t expectedBytes = expectedElems * static_cast<size_t>(expected.itemsize());
    const size_t actualBytes   = static_cast<size_t>(arr.nbytes());

    if (actualBytes != expectedBytes) {
        throw std::invalid_argument(
            "NATIVE input " + std::to_string(inputIdx) + " of model '" + model_name + "' has " +
            std::to_string(actualBytes) + " bytes (dtype " + actualDtypeStr + ", " +
            std::to_string(static_cast<size_t>(arr.size())) + " elements), but model tensor type '" +
            expectedTypes[inputIdx] + "' with " + std::to_string(expectedElems) + " elements expects " +
            std::to_string(expectedBytes) + " bytes (numpy dtype " + expectedDtypeStr + "). "
            "In NATIVE mode inputs are passed to QNN as raw bytes; convert/quantize the data to the "
            "model dtype first (see getInputDataType()/getInputShapes()).");
    }

    if (!arr.dtype().equal(expected)) {
        const std::string msg =
            "NATIVE input " + std::to_string(inputIdx) + " of model '" + model_name + "' has dtype " +
            actualDtypeStr + ", but model tensor type is '" + expectedTypes[inputIdx] +
            "' (numpy dtype " + expectedDtypeStr + "). The bytes are passed to QNN unchanged; make sure "
            "the array holds raw " + expectedDtypeStr + " values, e.g. arr.view(np." + expectedDtypeStr +
            ") for reinterpretation, not real-valued data.";
        if (PyErr_WarnEx(PyExc_RuntimeWarning, msg.c_str(), 1) != 0) {
            throw py::error_already_set();   // warnings configured as errors
        }
    }
}

// ---------------------------------------------------------------------------
// Helper: NATIVE input metadata for the multi-process (proc) path.
// getInputDataType/getInputShapes(model, proc) are IPC round-trips to the Svc process, so cache the
// result per (model_name, proc_name). Static per-model metadata, safe to cache for the context
// lifetime; an entry is dropped when that model/proc pair is destroyed (see destroy_P).
// Called with the GIL held (same as the existing proc getters); the GIL serializes cache access.
// Returned by value so callers never hold a reference into the map.
// ---------------------------------------------------------------------------
struct NativeInputMeta {
    std::vector<std::string> dtypes;
    std::vector<std::vector<size_t>> shapes;
};

static std::unordered_map<std::string, NativeInputMeta> g_nativeInputMetaCache;

static inline std::string nativeInputMetaKey(const std::string& model_name, const std::string& proc_name) {
    return model_name + '\x1f' + proc_name;
}

static inline NativeInputMeta getNativeInputMeta_P(const std::string& model_name, const std::string& proc_name) {
    const std::string key = nativeInputMetaKey(model_name, proc_name);
    auto it = g_nativeInputMetaCache.find(key);
    if (it != g_nativeInputMetaCache.end()) {
        return it->second;
    }
    NativeInputMeta meta;
    meta.dtypes = g_LibAppBuilder.getInputDataType(model_name, proc_name);
    meta.shapes = g_LibAppBuilder.getInputShapes(model_name, proc_name);
    if (!meta.dtypes.empty() && meta.dtypes.size() == meta.shapes.size()) {
        g_nativeInputMetaCache[key] = meta;
    }
    // Incomplete metadata is not cached; validateNativeInput() then skips validation.
    return meta;
}

static inline void dropNativeInputMeta_P(const std::string& model_name, const std::string& proc_name) {
    g_nativeInputMetaCache.erase(nativeInputMetaKey(model_name, proc_name));
}

ModelInfo_t getModelInfo_P(std::string model_name, std::string proc_name,
                           std::string input, size_t graphIndex = 0) {

    ModelInfo_t output = g_LibAppBuilder.getModelInfo(model_name, proc_name, input);
    return output;
}


/*
    QNN_LOG_LEVEL_ERROR = 1,
    QNN_LOG_LEVEL_WARN = 2,
    QNN_LOG_LEVEL_INFO = 3,
    QNN_LOG_LEVEL_VERBOSE = 4,
    QNN_LOG_LEVEL_DEBUG = 5,
*/
int set_log_level(int32_t log_level, std::string log_path = "None") {
    return SetLogLevel(log_level, log_path);
}

/*
    OFF = 0,
    BASIC = 1,
    DETAILED = 2,
    INVALID = 3,
*/
int set_profiling_level(int32_t log_level) {
    return SetProfilingLevel(log_level);
}

int set_perf_profile(const std::string& perf_profile) {
    py::gil_scoped_release release;
    return SetPerfProfileGlobal(perf_profile);
}

int rel_perf_profile() {
    py::gil_scoped_release release;
    return RelPerfProfileGlobal();
}

int initialize(const std::string& model_name,
               const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path,
               bool async, const std::string& input_data_type, const std::string& output_data_type) {
    py::gil_scoped_release release;
    return g_LibAppBuilder.ModelInitialize(model_name, model_path, backend_lib_path, system_lib_path, async, input_data_type, output_data_type);
}

int initialize_P(const std::string& model_name, const std::string& proc_name,
                 const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path,
                 bool async, const std::string& input_data_type, const std::string& output_data_type) {
    py::gil_scoped_release release;
    return g_LibAppBuilder.ModelInitialize(model_name, proc_name, model_path, backend_lib_path, system_lib_path, async, input_data_type, output_data_type);
}

int destroy(std::string model_name) {
    py::gil_scoped_release release;
    return g_LibAppBuilder.ModelDestroy(model_name);
}

int destroy_P(std::string model_name, std::string proc_name) {
    dropNativeInputMeta_P(model_name, proc_name);
    py::gil_scoped_release release;
    return g_LibAppBuilder.ModelDestroy(model_name, proc_name);
}

std::vector<py::array> inference(std::string model_name, const std::vector<py::array>& input,
                                 std::string perf_profile, size_t graphIndex = 0,
                                 const std::string& input_data_type="float", const std::string& output_data_type="float") {
    std::vector<uint8_t*> inputBuffers;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    // Keep temporary converted/contiguous arrays alive during ModelInference
    std::vector<py::array> keepAlive;
    const bool floatMode = isFloat32Request(input_data_type);
    const bool floatOutMode = isFloat32Request(output_data_type);

    //QNN_INF("inference input vector length: %d\n", input.size());

    // NATIVE mode: fetch model input metadata (cached in QnnInferenceEngine) to validate input dtype/size.
    std::vector<std::string> inDtypes;
    std::vector<std::vector<size_t>> inShapes;
    if (!floatMode) {
        inDtypes = g_LibAppBuilder.getInputDataType(model_name, graphIndex);
        inShapes = g_LibAppBuilder.getInputShapes(model_name, graphIndex);
    }

    for (auto i = 0; i < input.size(); i++) {
        if (floatMode) {
            // Backward compatible: force-cast to float32 contiguous like old py::array_t<float>
            py::array_t<float, py::array::c_style | py::array::forcecast> farr(input[i]);
            py::buffer_info buf = farr.request();
            keepAlive.push_back(py::array(farr));  // keep alive
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
        } else {
            // Native mode: preserve dtype, but ensure contiguous C-style memory.
            py::array carr = py::array::ensure(input[i], py::array::c_style);
            if (!carr) {
                throw std::runtime_error("Failed to ensure contiguous input array for model: " + model_name);
            }
            validateNativeInput(model_name, static_cast<size_t>(i), carr, inDtypes, inShapes);
            py::buffer_info buf = carr.request();
            keepAlive.push_back(carr);
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
        }
    }

    // issue#97: release the GIL during the blocking HTP/NPU inference call so
    // other Python threads can run concurrently. All arguments are raw C++
    // pointers/vectors extracted above; no py:: objects are accessed until the
    // output construction below.
    bool inference_ok;
    {
        py::gil_scoped_release release;
        inference_ok = g_LibAppBuilder.ModelInference(model_name, inputBuffers, outputBuffers, outputSize, perf_profile, graphIndex);
    }
    if (!inference_ok) {
        return {};
    }

    //QNN_INF("inference::inference output vector length: %d\n", outputBuffers.size());

    // dtype list like: ['float16', 'float16', ...]
    std::vector<std::string> outDtypes = g_LibAppBuilder.getOutputDataType(model_name, graphIndex);
    // output shapes for robust element count & float/native dtype inference
    std::vector<std::vector<size_t>> outShapes = g_LibAppBuilder.getOutputShapes(model_name, graphIndex);

    std::vector<py::array> output;

    //start_time();
    for (auto i = 0; i < outputBuffers.size(); i++) {
        std::string dtypeStr = (i < outDtypes.size()) ? outDtypes[i] : std::string("uint8");
        const std::vector<size_t> shape = (i < outShapes.size()) ? outShapes[i] : std::vector<size_t>{};

        // infer real dtype from outputSize & outputShape to support FLOAT_ONLY outputs
        py::dtype dt = inferOutputNumpyDtype((i < outputSize.size()) ? outputSize[i] : 0, shape, dtypeStr, floatOutMode);

        // element count: prefer shape product (stable for both native & float)
        size_t elemCount = 0;
        if (!shape.empty()) {
            elemCount = productDims(shape);
        } else {
            // fallback to old method if shape unavailable
            const size_t itemBytes = static_cast<size_t>(dt.itemsize());
            if (itemBytes > 0 && i < outputSize.size() && (outputSize[i] % itemBytes) == 0) {
                elemCount = outputSize[i] / itemBytes;
            } else if (i < outputSize.size()) {
                dt = py::dtype::of<uint8_t>();
                elemCount = outputSize[i];
            }
        }

        // https://github.com/pybind/pybind11/issues/1042#issuecomment-325941022
        // Avoid memory copy for saving time. 'py::capsule' for freeing the memory.
        py::capsule free_data(outputBuffers[i], [](void* f) {free(f);});
        py::array result(dt,
                        { static_cast<py::ssize_t>(elemCount) },
                        { static_cast<py::ssize_t>(dt.itemsize()) },
                        outputBuffers[i],
                        free_data);

        // If user requests float output, cast to float32 before returning.
        // IMPORTANT: do NOT reinterpret the raw buffer as float32 (size may not match).
        // We first create 'result' using the inferred real dtype, then cast (copy) if needed.
        if (floatOutMode && !isNumpyFloat32Dtype(dt)) {
            py::array_t<float, py::array::c_style | py::array::forcecast> farr(result);
            output.push_back(py::array(farr));
        } else {
            output.push_back(result);
        }
    }
    //print_time("convert Data To ArrayV");

    return output;
}

std::vector<py::array> inference_P(std::string model_name, std::string proc_name, std::string share_memory_name,
                                   const std::vector<py::array>& input, std::string perf_profile, size_t graphIndex = 0,
                                   const std::string& input_data_type="float", const std::string& output_data_type="float") {
    std::vector<uint8_t*> inputBuffers;
    std::vector<size_t> inputSize;
    std::vector<uint8_t*> outputBuffers;
    std::vector<size_t> outputSize;

    // Keep temporary converted/contiguous arrays alive during ModelInference
    std::vector<py::array> keepAlive;
    const bool floatMode = isFloat32Request(input_data_type);
    const bool floatOutMode = isFloat32Request(output_data_type);

    // NATIVE mode: model input metadata (cached per model/proc) to validate input dtype/size.
    NativeInputMeta inMeta;
    if (!floatMode) {
        inMeta = getNativeInputMeta_P(model_name, proc_name);
    }

    for (auto i = 0; i < input.size(); i++) {
        if (floatMode) {
            py::array_t<float, py::array::c_style | py::array::forcecast> farr(input[i]);
            py::buffer_info buf = farr.request();
            keepAlive.push_back(py::array(farr));
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
            // float32 bytes
            size_t bytes = static_cast<size_t>(buf.size) * static_cast<size_t>(buf.itemsize);
            inputSize.push_back(bytes);
        } else {
            py::array carr = py::array::ensure(input[i], py::array::c_style);
            if (!carr) {
                throw std::runtime_error("Failed to ensure contiguous input array for model: " + model_name);
            }
            validateNativeInput(model_name, static_cast<size_t>(i), carr, inMeta.dtypes, inMeta.shapes);
            py::buffer_info buf = carr.request();
            keepAlive.push_back(carr);
            inputBuffers.push_back(reinterpret_cast<uint8_t*>(buf.ptr));
            // native bytes
            size_t bytes = static_cast<size_t>(buf.size) * static_cast<size_t>(buf.itemsize);
            inputSize.push_back(bytes);
        }
    }

    // issue#97: release the GIL during the blocking IPC call to the service
    // process so other Python threads can run concurrently.
    bool success;
    {
        py::gil_scoped_release release;
        success = g_LibAppBuilder.ModelInference(model_name, proc_name, share_memory_name, inputBuffers, inputSize, outputBuffers, outputSize, perf_profile, graphIndex);
    }
    if (!success) {
        QNN_ERR("ModelInference failed for model: %s, proc: %s", model_name.c_str(), proc_name.c_str());
        return {};
    }

    //QNN_INF("inference_P::inference output vector length: %d\n", outputBuffers.size());

    // dtype list like: ['float16', 'float16', ...]
    std::vector<std::string> outDtypes = g_LibAppBuilder.getOutputDataType(model_name, proc_name);
    std::vector<std::vector<size_t>> outShapes = g_LibAppBuilder.getOutputShapes(model_name, proc_name);

    std::vector<py::array> output;

    //start_time();
    for (auto i = 0; i < outputBuffers.size(); i++) {
        std::string dtypeStr = (i < outDtypes.size()) ? outDtypes[i] : std::string("uint8");
        const std::vector<size_t> shape = (i < outShapes.size()) ? outShapes[i] : std::vector<size_t>{};

        py::dtype dt = inferOutputNumpyDtype((i < outputSize.size()) ? outputSize[i] : 0, shape, dtypeStr, floatOutMode);

        size_t elemCount = 0;
        if (!shape.empty()) {
            elemCount = productDims(shape);
        } else {
            const size_t itemBytes = static_cast<size_t>(dt.itemsize());
            if (itemBytes > 0 && i < outputSize.size() && (outputSize[i] % itemBytes) == 0) {
                elemCount = outputSize[i] / itemBytes;
            } else if (i < outputSize.size()) {
                dt = py::dtype::of<uint8_t>();
                elemCount = outputSize[i];
            }
        }

        // https://github.com/pybind/pybind11/issues/1042#issuecomment-325941022
        // Avoid memory copy for saving time. 'py::capsule' for freeing the memory.
        py::capsule free_data(outputBuffers[i], [](void* f) {});  // Not free this memory since it's share memory.
        py::array result(dt,
                        { static_cast<py::ssize_t>(elemCount) },
                        { static_cast<py::ssize_t>(dt.itemsize()) },
                        outputBuffers[i],
                        free_data);

        // If user requests float output, cast to float32 before returning.
        // For shared memory outputs, this will create a float32 copy (shared memory remains untouched).
        if (floatOutMode && !isNumpyFloat32Dtype(dt)) {
            py::array_t<float, py::array::c_style | py::array::forcecast> farr(result);
            output.push_back(py::array(farr));
        } else {
            output.push_back(result);
        }
    }
    //print_time("convert Data To ArrayV");

    return output;
}

bool ApplyBinaryUpdate(const std::vector<LoraAdapter>& lora_adapters);

int create_memory(std::string share_memory_name, size_t share_memory_size) {
    return g_LibAppBuilder.CreateShareMemory(share_memory_name, share_memory_size);
}

int delete_memory(std::string share_memory_name) {
    return g_LibAppBuilder.DeleteShareMemory(share_memory_name);
}

class ShareMemory {
public:
    std::string m_share_memory_name;
    size_t m_share_memory_size = 0;

    ShareMemory(const std::string& share_memory_name, const size_t share_memory_size);
    ~ShareMemory();

};

#if defined(APPBUILDER_ENABLE_TFLITE) || defined(APPBUILDER_ENABLE_TFLITE_CPU)
class TFLiteQnnContext {
public:
    TFLiteQnnContext(const std::string& model_name, const std::string& model_path,
                     const std::string& backend_lib_path = "",
                     bool use_qnn_delegate = true, int num_threads = 1)
        : m_model_name(model_name),
          m_engine(std::make_unique<qnn::tools::qnn_app::TFLiteInferenceEngine>(model_path, backend_lib_path, use_qnn_delegate, num_threads)) {
        m_engine->initialize();
    }

    ~TFLiteQnnContext() { release(); }

    std::vector<py::array> Inference(const std::vector<py::array>& input, size_t graph_index = 0) {
        const auto expected_types = m_engine->getInputDataType(graph_index);
        std::vector<const uint8_t*> buffers;
        std::vector<size_t> sizes;
        std::vector<py::array> keep_alive;
        buffers.reserve(input.size());
        sizes.reserve(input.size());
        for (size_t i = 0; i < input.size(); ++i) {
            if (i >= expected_types.size()) {
                throw std::invalid_argument("TFLite inference received more inputs than the model declares");
            }
            py::array array = py::array::ensure(input[i], py::array::c_style);
            if (!array) {
                throw std::invalid_argument("TFLite inference input is not a contiguous NumPy array");
            }
            const py::dtype expected_dtype = dtypeFromString(expected_types[i]);
            if (!array.dtype().equal(expected_dtype)) {
                throw std::invalid_argument(
                    "TFLite inference input " + std::to_string(i) + " has dtype " +
                    py::str(array.dtype()).cast<std::string>() + "; expected " + expected_types[i]);
            }
            py::buffer_info info = array.request();
            keep_alive.push_back(array);
            buffers.push_back(static_cast<const uint8_t*>(info.ptr));
            sizes.push_back(static_cast<size_t>(info.size) * static_cast<size_t>(info.itemsize));
        }
        std::vector<std::vector<uint8_t>> raw_outputs;
        {
            py::gil_scoped_release release_gil;
            raw_outputs = m_engine->inference(buffers, sizes, graph_index);
        }
        std::vector<py::array> outputs;
        const auto shapes = m_engine->getOutputShapes(graph_index);
        const auto types = m_engine->getOutputDataType(graph_index);
        for (size_t i = 0; i < raw_outputs.size(); ++i) {
            const py::dtype dtype = dtypeFromString(i < types.size() ? types[i] : "uint8");
            std::vector<py::ssize_t> shape;
            if (i < shapes.size() && !shapes[i].empty()) {
                for (const auto dim : shapes[i]) shape.push_back(static_cast<py::ssize_t>(dim));
            } else {
                shape.push_back(static_cast<py::ssize_t>(raw_outputs[i].size() / static_cast<size_t>(dtype.itemsize())));
            }
            py::array output(dtype, shape);
            std::memcpy(output.mutable_data(), raw_outputs[i].data(), raw_outputs[i].size());
            outputs.push_back(std::move(output));
        }
        return outputs;
    }

    std::vector<std::vector<size_t>> getInputShapes(size_t graph_index = 0) const { return m_engine->getInputShapes(graph_index); }
    std::vector<std::vector<size_t>> getOutputShapes(size_t graph_index = 0) const { return m_engine->getOutputShapes(graph_index); }
    std::vector<std::string> getInputDataType(size_t graph_index = 0) const { return m_engine->getInputDataType(graph_index); }
    std::vector<std::string> getOutputDataType(size_t graph_index = 0) const { return m_engine->getOutputDataType(graph_index); }
    std::vector<std::string> getInputName(size_t graph_index = 0) const { return m_engine->getInputName(graph_index); }
    std::vector<std::string> getOutputName(size_t graph_index = 0) const { return m_engine->getOutputName(graph_index); }
    std::string getGraphName(size_t graph_index = 0) const { return m_engine->getGraphName(graph_index); }
    uint64_t getProfilingEvent(uint32_t event_type) const { return m_engine->getProfilingEvent(event_type); }
    std::string getProviderMode() const { return m_engine->getProviderMode(); }
    void release() noexcept { if (m_engine) m_engine->release(); }

private:
    std::string m_model_name;
    std::unique_ptr<qnn::tools::qnn_app::TFLiteInferenceEngine> m_engine;
};
#endif

#ifdef APPBUILDER_ENABLE_TFLITE_CPU
class TFLiteCpuContext : public TFLiteQnnContext {
public:
    TFLiteCpuContext(const std::string& model_name, const std::string& model_path,
                    int num_threads = 1)
        : TFLiteQnnContext(model_name, model_path, "", false, num_threads) {}
};
#endif

#if defined(APPBUILDER_ENABLE_EXECUTORCH)
class ExecuTorchContext {
public:
    ExecuTorchContext(const std::string& model_name, const std::string& model_path,
                      const std::string& backend_lib_path = "")
        : m_model_name(model_name),
          m_engine(std::make_unique<qnn::tools::qnn_app::ExecuTorchInferenceEngine>(model_path, backend_lib_path)) {
        m_engine->initialize();
    }

    ~ExecuTorchContext() { release(); }

    std::vector<py::array> Inference(const std::vector<py::array>& input, size_t graph_index = 0) {
        const auto expected_types = m_engine->getInputDataType(graph_index);
        if (input.size() != expected_types.size()) {
            throw std::invalid_argument("ExecuTorch inference input count does not match the model");
        }
        std::vector<const uint8_t*> buffers;
        std::vector<size_t> sizes;
        std::vector<py::array> keep_alive;
        for (size_t i = 0; i < input.size(); ++i) {
            py::array array = py::array::ensure(input[i], py::array::c_style);
            if (!array) throw std::invalid_argument("ExecuTorch inference input is not a contiguous NumPy array");
            const py::dtype expected_dtype = dtypeFromString(expected_types[i]);
            if (!array.dtype().equal(expected_dtype)) {
                throw std::invalid_argument("ExecuTorch inference input " + std::to_string(i) + " has dtype " +
                                            py::str(array.dtype()).cast<std::string>() + "; expected " + expected_types[i]);
            }
            py::buffer_info info = array.request();
            keep_alive.push_back(array);
            buffers.push_back(static_cast<const uint8_t*>(info.ptr));
            sizes.push_back(static_cast<size_t>(info.size) * static_cast<size_t>(info.itemsize));
        }
        std::vector<std::vector<uint8_t>> raw_outputs;
        {
            py::gil_scoped_release release_gil;
            raw_outputs = m_engine->inference(buffers, sizes, graph_index);
        }
        const auto shapes = m_engine->getOutputShapes(graph_index);
        const auto types = m_engine->getOutputDataType(graph_index);
        std::vector<py::array> outputs;
        for (size_t i = 0; i < raw_outputs.size(); ++i) {
            const py::dtype dtype = dtypeFromString(i < types.size() ? types[i] : "uint8");
            std::vector<py::ssize_t> shape;
            if (i < shapes.size()) {
                for (const auto dim : shapes[i]) shape.push_back(static_cast<py::ssize_t>(dim));
            } else {
                shape.push_back(static_cast<py::ssize_t>(raw_outputs[i].size() / static_cast<size_t>(dtype.itemsize())));
            }
            py::array output(dtype, shape);
            if (raw_outputs[i].size() != static_cast<size_t>(output.nbytes())) {
                throw std::runtime_error("ExecuTorch output byte size does not match static tensor metadata");
            }
            std::memcpy(output.mutable_data(), raw_outputs[i].data(), raw_outputs[i].size());
            outputs.push_back(std::move(output));
        }
        return outputs;
    }

    std::vector<std::vector<size_t>> getInputShapes(size_t graph_index = 0) const { return m_engine->getInputShapes(graph_index); }
    std::vector<std::vector<size_t>> getOutputShapes(size_t graph_index = 0) const { return m_engine->getOutputShapes(graph_index); }
    std::vector<std::string> getInputDataType(size_t graph_index = 0) const { return m_engine->getInputDataType(graph_index); }
    std::vector<std::string> getOutputDataType(size_t graph_index = 0) const { return m_engine->getOutputDataType(graph_index); }
    std::vector<std::string> getInputName(size_t graph_index = 0) const { return m_engine->getInputName(graph_index); }
    std::vector<std::string> getOutputName(size_t graph_index = 0) const { return m_engine->getOutputName(graph_index); }
    std::string getGraphName(size_t graph_index = 0) const { return m_engine->getGraphName(graph_index); }
    uint64_t getProfilingEvent(uint32_t event_type) const { return m_engine->getProfilingEvent(event_type); }
    std::string getProviderMode() const { return m_engine->getProviderMode(); }
    void release() noexcept { if (m_engine) m_engine->release(); }

private:
    std::string m_model_name;
    std::unique_ptr<qnn::tools::qnn_app::ExecuTorchInferenceEngine> m_engine;
};
#endif

class QNNContext {
public:
    std::string m_model_name;
    std::string m_proc_name;
    std::vector<LoraAdapter> m_lora_adapters;

    QNNContext(const std::string& model_name, const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path,
               bool async = false, const std::string& input_data_type="float", const std::string& output_data_type="float", uint32_t deviceID=0, std::string coreIdsStr="", const std::vector<std::string>& enable_graphs={});

    QNNContext(const std::string& model_name, const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path, const std::vector<LoraAdapter>& lora_adapters,
               bool async = false, const std::string& input_data_type="float", const std::string& output_data_type="float", uint32_t deviceID=0, std::string coreIdsStr="", const std::vector<std::string>& enable_graphs={});

    QNNContext(const std::string& model_name, const std::string& proc_name, const std::string& model_path, const std::string& backend_lib_path, const std::string& system_lib_path,
               bool async = false, const std::string& input_data_type="float", const std::string& output_data_type="float", uint32_t deviceID=0, std::string coreIdsStr="");

    std::vector<py::array> Inference(const std::vector<py::array>& input, const std::string& perf_profile = "default", size_t graphIndex = 0, const std::string& input_data_type="float", const std::string& output_data_type="float");
    std::vector<py::array> Inference(const ShareMemory& share_memory, const std::vector<py::array>& input, const std::string& perf_profile = "default", size_t graphIndex = 0, const std::string& input_data_type="float", const std::string& output_data_type="float");

    // Async cross-process inference: returns a request_id string immediately.
    std::string InferenceAsync(const ShareMemory& share_memory, const std::vector<py::array>& input, const std::string& perf_profile = "default", size_t graphIndex = 0, const std::string& input_data_type="float", const std::string& output_data_type="float");

    // Wait for an async inference started by InferenceAsync() and return outputs.
    std::vector<py::array> InferenceWait(const std::string& request_id, const ShareMemory& share_memory, const std::string& output_data_type="float");

    bool ApplyBinaryUpdate(const std::vector<LoraAdapter>& lora_adapters);

    // issue#24
    std::vector<std::vector<size_t>> getInputShapes(size_t graphIdx);
    std::vector<std::string>  getInputDataType(size_t graphIdx);
    std::vector<std::string>  getOutputDataType(size_t graphIdx);
    std::vector<std::vector<size_t>> getOutputShapes(size_t graphIdx);
    std::string getGraphName(size_t graphIdx);
    std::vector<std::string>  getInputName(size_t graphIdx);
    std::vector<std::string>  getOutputName(size_t graphIdx);

    std::vector<std::vector<size_t>> getInputShapes(const std::string& proc_name);
    std::vector<std::string>  getInputDataType(const std::string& proc_name);
    std::vector<std::string>  getOutputDataType(const std::string& proc_name);
    std::vector<std::vector<size_t>> getOutputShapes(const std::string& proc_name);
    std::string getGraphName(const std::string& proc_name);
    std::vector<std::string>  getInputName(const std::string& proc_name);
    std::vector<std::string>  getOutputName(const std::string& proc_name);
    uint64_t getProfilingEvent(uint32_t eventType);

    typedef struct ModelInfo {
        std::vector<std::vector<size_t>> inputShapes;
        std::vector<std::string>  inputDataType;
        std::vector<std::vector<size_t>> onputShapes;
        std::vector<std::string> onputDataType;
        std::string graphName;
    } ModelInfo_t;
    ~QNNContext();
};

#endif

