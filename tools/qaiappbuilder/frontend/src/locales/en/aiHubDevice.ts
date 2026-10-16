// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

const aiHubDevice = {
  site: {
    label: "AI Hub site",
    global: "Global AI Hub",
    china: "China AI Hub",
  },
  tab: {
    configure: "Configure",
    devices: "Devices",
    models: "Models",
    compile: "Compile",
    profile: "Profile",
  },
  tooltip: {
    configure: "Configure Qualcomm AI Hub access credentials and runtime settings. Once configured, you can use device, compilation, and profiling features.",
    devices: "View and select target devices available through AI Hub. The selected device will be used for model compilation and profiling.",
    models: "View and select models available on AI Hub. The selected model can then be compiled or profiled.",
    compile: "Compile and optimize a model for a specified device to generate a deployable target artifact.",
    profile: "Run a model on a specified device and analyze performance metrics such as latency and throughput.",
  },
  configure: {
    title: "Configure AI Hub Client",
    loading: "Loading configuration…",
    tokenLabel: "API Token",
    tokenPlaceholder: "Enter your Qualcomm AI Hub API token",
    tokenConfigured: "An API token is already configured. Enter a new token only to replace it.",
    tokenRequired: "Enter a new API token before saving.",
    tokenHintPrefix: "Get your API token from",
    tokenHintLink: "https://aihub.qualcomm.com",
    tokenHintSuffix: "— Account → API Token.",
    save: "Save",
    saving: "Saving…",
    saved: "API token saved.",
    saveFailed: "Failed to save API token.",
  },
  compile: {
    title: "Compile",
    run: "Run Compile",
  },
  models: {
    title: "Available Models",
    loading: "Loading models…",
    empty: "No models found. Make sure your API token is configured.",
    colModelId: "Model ID",
    colName: "Name",
  },
  profile: {
    title: "Profile",
    run: "Run Profile",
  },
  devices: {
    title: "Available Devices",
    loading: "Loading devices…",
    empty: "No devices found. Make sure your API token is configured.",
    refresh: "Refresh",
    colName: "Device",
    colOs: "OS",
    colChipset: "Chipset",
  },
  job: {
    modelLabel: "Model Name or Path",
    modelPlaceholder: "mm5lrg54q (Model: ResNet50) or /path/to/model.onnx",
    deviceLabel: "Target Device",
    devicePlaceholder: "e.g. Samsung Galaxy S24 (Snapdragon 8 Gen 3)",
    modelRequired: "Please enter a model name or path.",
    deviceRequired: "Please select a target device.",
    starting: "Starting job…",
    jobId: "Job ID: {id}",
    running: "Running…",
    done: "Job completed.",
    error: "Job error: {msg}",
    submitFailed: "Failed to submit job: {msg}",
    waiting: "Job submitted to queue, waiting for device availability. This may take several minutes. Visit the link above for real-time progress.",
    viewResults: "View results: ",
  },
};

export default aiHubDevice;