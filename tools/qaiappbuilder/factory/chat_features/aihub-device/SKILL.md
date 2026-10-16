---
name: aihub-device
description: Use when compiling and profiling models on AI Hub cloud without requiring local devices. Submit models to AI Hub for cloud-based compilation and performance analysis on target hardware. Ideal when you don't have physical devices available. For downloading pre-compiled AI Hub packages, use model-hub. For local compilation with physical devices, use model-builder.
tags: aihub, compilation, profiling, on-device, qualcomm, model-optimization, upload, cloud-profiling, compile model, profile model, upload model, compile on cloud, profile on device, cloud compilation, performance profiling, target device, Snapdragon, Dragonwing, model_id, submit profile job, submit compile job, compile and profile, cloud compile, cloud profile, upload my model, profile my model, upload a model, profile a model
homepage: "https://aihub.qualcomm.com"
---

# AI Hub Device

> **Core Value Proposition**: Compile and profile models on AI Hub cloud when you **don't have physical devices available**. Get real device performance metrics without hardware.

> **How to use this SKILL:**
> ① Pass the **Pre-Flight Self-Check** below (MANDATORY gate).
> ② Determine model source: **AI Hub** vs **Local File**.
> ③ Follow the appropriate workflow below **using qai_hub SDK via exec tool**.
> ④ Monitor job progress; report results to user.
>
> ✅ **Use qai_hub SDK via exec tool**
> - Use `exec` tool to run Python code with `qai_hub` SDK
> - No HTTP API or CSRF token complications
> - Direct access to all AI Hub functionality

---

## 🎯 When to Use This Skill vs Alternatives

| Scenario | Use This Skill |
|----------|---------------|
| Want to know model performance on Snapdragon, but **no device available** | ✅ `aihub-device` |
| Have a custom model, want to profile on multiple devices without buying them | ✅ `aihub-device` |
| Need to compile ONNX for specific hardware, but no access to that hardware | ✅ `aihub-device` |
| Already have AI Hub pre-compiled package, just want to run inference | ❌ Use `model-hub` |
| Have physical device, want local compilation + profiling | ❌ Use `model-builder` |
| Converting/quantizing custom ONNX/PyTorch locally | ❌ Use `model-builder` |

---

## ✋ Pre-Flight Self-Check (MANDATORY GATE)

| # | Must Verify | How |
|---|-------------|-----|
| 1 | AI Hub API token configured? | `exec("import qai_hub; print(qai_hub.Client())")` |
| 2 | Model source identified? | User provides: model name (AI Hub) OR file path (local) |
| 3 | Target device confirmed? | See device list below |
| 4 | User intent clear? | "compile" vs "profile" vs "upload" |
| 5 | **No local device required?** | If user has device → suggest `model-builder` |

---

## SDK Initialization

Initialize the client and create device object before any operation:

```python
import qai_hub

# Initialize client
client = qai_hub.Client()

# Create device object (REQUIRED for all jobs)
device = qai_hub.Device("Samsung Galaxy S23")
```

⚠️ **CRITICAL**: Always use `Device` object!
- ❌ `device="Samsung Galaxy S23"` - Will fail
- ✅ `device=qai_hub.Device("Samsung Galaxy S23")` - Correct

---

## Common Devices

| Device Name | Chipset | Use Case |
|-------------|---------|----------|
| Samsung Galaxy S23 | Snapdragon 8 Gen 2 | Latest flagship |
| Google Pixel 7 | Tensor G2 | Common test device |
| Dragonwing IQ-9075 EVK | Snapdragon 8 Gen 1 | Development board |

To list all available devices:
```python
devices = client.get_devices()
for d in devices:
    print(f"{d.name}: {d.os}")
```

---

## Workflow A: Compile AI Hub Model

**Use when**: User has an AI Hub model name and wants to compile it for a target device.

### Steps

```python
import qai_hub

# Initialize
client = qai_hub.Client()

# Get model from AI Hub
model_id = "mnj8v9rxm"  # User provides this
model = client.get_model(model_id)

# Create device object
device = qai_hub.Device("Samsung Galaxy S23")

# Submit compile job
compile_job = client.submit_compile_job(
    model=model,  # Use Model object
    device=device,  # Must be Device object
)

# Wait for completion
compile_job.wait()

# Get compiled model
compiled_model = compile_job.get_target_model()

# Report to user
print(f"✅ Compilation completed")
print(f"- **Compiled Model ID**: {compiled_model.model_id}")
print(f"- **Model Page**: {compiled_model.url}")
```

---

## Workflow B: Upload and Compile Local Model

**Use when**: User has a local model file and wants to compile it.

### Steps

```python
import qai_hub

# Initialize
client = qai_hub.Client()

# Upload local model
local_path = "/home/user/my_model.onnx"  # User provides this
model = client.upload_model(local_path, name="my_model")

# Create device object
device = qai_hub.Device("Samsung Galaxy S23")

# Compile for target device
compile_job = client.submit_compile_job(
    model=model,
    device=device,
)

# Wait for completion
compile_job.wait()

# Get compiled model
compiled_model = compile_job.get_target_model()

# Report
print(f"✅ Local model uploaded and compiled")
print(f"- **Original File**: {local_path}")
print(f"- **Compiled Model ID**: {compiled_model.model_id}")
print(f"- **Model Page**: {compiled_model.url}")
```

---

## Workflow C: Profile Model Performance

**Use when**: User wants detailed performance metrics for a compiled model on a target device.

### Prerequisites
- Model must be **compiled for the target device** first (Workflows A or B)
- Profiling requires a **compiled model**, not source model

### Steps

```python
import qai_hub
import time

# Initialize
client = qai_hub.Client()

# Get compiled model (from previous compile job)
# compiled_model = compile_job.get_target_model()
# Or fetch from AI Hub:
# compiled_model = client.get_model("compiled_model_id")

# Create device object
device = qai_hub.Device("Samsung Galaxy S23")

# Submit profile job
profile_job = client.submit_profile_job(
    model=compiled_model,  # ⚠️ MUST use Model object, NOT model_id string
    device=device,
)

# Monitor progress (poll every 20-30s)
while True:
    status = profile_job.get_status()
    print(f"Status: {status.code}")

    if status.code in ["COMPLETED", "SUCCESS", "FAILED"]:
        break

    time.sleep(30)

# Get performance data
profile = profile_job.download_profile()
exec_summary = profile["execution_summary"]

# Extract and report all key metrics
print(f"\n✅ Profiling completed")

# Inference Performance
inference_us = exec_summary["estimated_inference_time"]
inference_ms = inference_us / 1000
print(f"\n## Inference Performance")
print(f"- **Average Inference Latency**: {inference_ms:.2f} ms ({inference_us} μs)")

# Calculate throughput
throughput_fps = 1000 / inference_ms
print(f"- **Throughput**: {throughput_fps:.1f} FPS")

# Inference Stability
all_inference_times = exec_summary.get("all_inference_times", [])
if all_inference_times:
    min_time = min(all_inference_times) / 1000
    max_time = max(all_inference_times) / 1000
    avg_time = sum(all_inference_times) / len(all_inference_times) / 1000
    print(f"- **Latency Range**: {min_time:.2f} - {max_time:.2f} ms")
    print(f"- **Average Latency**: {avg_time:.2f} ms")
    print(f"- **Number of Runs**: {len(all_inference_times)}")

# Memory Usage
inference_peak_mem = exec_summary["estimated_inference_peak_memory"]
inference_peak_mb = inference_peak_mem / (1024 * 1024)
print(f"\n## Memory Usage")
print(f"- **Inference Peak Memory**: {inference_peak_mb:.2f} MB")

# Load Performance
first_load_us = exec_summary["first_load_time"]
warm_load_us = exec_summary["warm_load_time"]
first_load_ms = first_load_us / 1000
warm_load_ms = warm_load_us / 1000
print(f"\n## Load Performance")
print(f"- **First Load Time**: {first_load_ms:.2f} ms")
print(f"- **Warm Load Time**: {warm_load_ms:.2f} ms")

# Memory Usage
first_load_mem = exec_summary["first_load_peak_memory"]
warm_load_mem = exec_summary["warm_load_peak_memory"]
first_load_mb = first_load_mem / (1024 * 1024)
warm_load_mb = warm_load_mem / (1024 * 1024)
print(f"- **First Load Peak Memory**: {first_load_mb:.2f} MB")
print(f"- **Warm Load Peak Memory**: {warm_load_mb:.2f} MB")

# Model and Device Info
print(f"\n## Job Info")
print(f"- **Target Device**: {device.name}")
print(f"- **Compiled Model ID**: {compiled_model.model_id}")
print(f"- **Model Page**: {compiled_model.url}")
print(f"- **Job Page**: {profile_job.url}")
```

### ⚠️ Critical: Always Poll Until Complete

- DO NOT stop polling until terminal state (COMPLETED, SUCCESS, or FAILED)
- Device provisioning takes 1-2 minutes
- Performance measurement takes 10-30 minutes
- Continue checking every 20-30 seconds

---

## Workflow D: Upload + Compile + Profile (Combined)

**Use when**: User wants to upload a local model and immediately profile it.

### Steps

```python
import qai_hub
import time

# Initialize
client = qai_hub.Client()

# Upload local model
local_path = "/home/user/my_model.onnx"
model = client.upload_model(local_path, name="my_model")

# Create device object
device = qai_hub.Device("Samsung Galaxy S23")

# Compile for target device
compile_job = client.submit_compile_job(
    model=model,
    device=device,
)
compile_job.wait()

# Get compiled model
compiled_model = compile_job.get_target_model()

# Submit profile job
profile_job = client.submit_profile_job(
    model=compiled_model,  # Use Model object
    device=device,
)

# Monitor progress
while True:
    status = profile_job.get_status()
    if status.code in ["COMPLETED", "SUCCESS", "FAILED"]:
        break
    time.sleep(30)

# Get performance data and report
profile = profile_job.download_profile()
exec_summary = profile["execution_summary"]

inference_us = exec_summary["estimated_inference_time"]
inference_ms = inference_us / 1000
throughput_fps = 1000 / inference_ms

print(f"✅ Upload, compile, and profile completed")
print(f"- **Inference Latency**: {inference_ms:.2f} ms")
print(f"- **Throughput**: {throughput_fps:.1f} FPS")
print(f"- **Model Page**: {compiled_model.url}")
```

---

## ⚠️ Critical: External Data Files (.data) for ONNX

Before uploading ONNX models, check for external data files:

1. **Detection**: If model file < 1MB and a `.data` file exists in same directory → external weights
2. **Embedding Required**: `upload_model()` only uploads the .onnx file, NOT the .data file
3. **Solution**: Embed external data before upload:

```python
import onnx

model = onnx.load(model_path)
onnx.save(model, output_path, save_as_external_data=False)
```

---

## ⚠️ Important: SDK Parameter Requirements

### Device Parameter
- ❌ `device="Samsung Galaxy S23"` - String will fail
- ✅ `device=qai_hub.Device("Samsung Galaxy S23")` - Must be Device object

### Model Parameter

**For compile jobs**:
- ✅ `model=model_object` - Recommended
- ✅ `model=model_id_string` - Also works for AI Hub models

**For profile jobs**:
- ✅ `model=compiled_model_object` - MUST use Model object
- ❌ `model=model_id_string` - Will fail with "Unsupported model type"

### Best Practice
```python
# Always get Model object first
model = client.get_model(model_id)  # or client.upload_model(path)
device = qai_hub.Device("Samsung Galaxy S23")

# Then use these objects in all API calls
compile_job = client.submit_compile_job(model=model, device=device)
profile_job = client.submit_profile_job(model=compiled_model, device=device)
```

---

## Performance Metrics Reference

### Available Metrics from Profile

| Metric | Description | Unit | How to Access |
|--------|-------------|------|---------------|
| **Inference Latency** | Average inference time | μs | `exec_summary["estimated_inference_time"]` |
| **Throughput** | Frames per second | FPS | `1000 / inference_ms` |
| **Latency Range** | Min-max inference times | ms | `exec_summary["all_inference_times"]` |
| **Inference Peak Memory** | Peak memory during inference | bytes | `exec_summary["estimated_inference_peak_memory"]` |
| **First Load Time** | First model load time | μs | `exec_summary["first_load_time"]` |
| **Warm Load Time** | Warm model load time | μs | `exec_summary["warm_load_time"]` |
| **Load Peak Memory** | Peak memory during load | bytes | `exec_summary["first_load_peak_memory"]` |

### Unit Conversions
```python
# Time: μs → ms
time_ms = time_us / 1000

# Memory: bytes → MB
memory_mb = memory_bytes / (1024 * 1024)

# Throughput: ms → FPS
fps = 1000 / time_ms
```

---

## Common Errors

### Error: "Unsupported model type"
- **For compile**: Model format must be supported (ONNX, TFLite, etc.)
- **For profile**: Must use Model object, not model_id string
- **Solution**: Use `client.get_model()` or `compile_job.get_target_model()`

### Error: "Device not supported"
- Use `client.get_devices()` to see available devices
- Check device name spelling (must match exactly)

### Error: Type error on device parameter
- **Cause**: Using string instead of Device object
- **Solution**: Always create Device object first

---

## API Token Configuration

AI Hub API token must be configured before using SDK:

**Method 1**: Environment variable
```bash
export AIHUB_API_TOKEN="your_token_here"
```

**Method 2**: Configure command
```bash
qai-hub configure
```

**Get token from**: https://aihub.qualcomm.com/account/

---

## Summary

- ✅ Use `qai_hub` SDK via `exec` tool for all operations
- ✅ **Always create Device object** for compile/profile jobs
- ✅ **Always use Model object** for profile jobs
- ✅ Always compile before profiling
- ✅ Poll jobs until terminal state
- ✅ Report comprehensive performance metrics
- ✅ Provide Model Page and Job Page links
- ✅ Check for external data files (.data) before uploading ONNX