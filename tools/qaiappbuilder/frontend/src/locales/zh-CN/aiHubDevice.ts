// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

const aiHubDevice = {
  site: {
    label: "AI Hub 站点",
    global: "全球 AI Hub",
    china: "中国 AI Hub",
  },
  tab: {
    configure: "配置",
    devices: "设备",
    models: "模型",
    compile: "编译",
    profile: "性能分析",
  },
  tooltip: {
    configure: "配置 Qualcomm AI Hub 的访问凭据和运行参数。完成配置后即可使用设备、编译和性能分析功能。",
    devices: "查看并选择 AI Hub 提供的目标设备。所选设备将用于模型编译和性能分析。",
    models: "查看并选择 AI Hub 中可用的模型。所选模型可用于后续编译或性能分析。",
    compile: "针对指定设备编译并优化模型，生成可部署的目标产物。",
    profile: "在指定设备上运行模型并分析性能，查看延迟、吞吐量等指标。",
  },
  configure: {
    title: "配置 AI Hub 客户端",
    loading: "加载配置中…",
    tokenLabel: "API Token",
    tokenPlaceholder: "输入您的 Qualcomm AI Hub API Token",
    tokenConfigured: "已配置 API Token。仅在需要替换时输入新的 Token。",
    tokenRequired: "请输入新的 API Token 后再保存。",
    tokenHintPrefix: "在",
    tokenHintLink: "https://aihub.qualcomm.com",
    tokenHintSuffix: "— 账户 → API Token 获取您的 Token。",
    save: "保存",
    saving: "保存中…",
    saved: "API Token 已保存。",
    saveFailed: "保存 API Token 失败。",
  },
  compile: {
    title: "编译",
    run: "运行编译",
  },
  models: {
    title: "可用模型",
    loading: "加载模型中…",
    empty: "未找到模型。请确认已配置 API Token。",
    colModelId: "模型 ID",
    colName: "名称",
  },
  profile: {
    title: "性能分析",
    run: "运行性能分析",
  },
  devices: {
    title: "可用设备",
    loading: "加载设备中…",
    empty: "未找到设备。请确认已配置 API Token。",
    refresh: "刷新",
    colName: "设备",
    colOs: "操作系统",
    colChipset: "芯片组",
  },
  job: {
    modelLabel: "模型名称或路径",
    modelPlaceholder: "mm5lrg54q（模型: ResNet50）或 /path/to/model.onnx",
    deviceLabel: "目标设备",
    devicePlaceholder: "例如：Samsung Galaxy S24 (Snapdragon 8 Gen 3)",
    modelRequired: "请输入模型名称或路径。",
    deviceRequired: "请选择目标设备。",
    starting: "正在启动任务…",
    jobId: "任务 ID：{id}",
    running: "运行中…",
    done: "任务已完成。",
    error: "任务错误：{msg}",
    submitFailed: "提交任务失败：{msg}",
    waiting: "任务已提交到队列，正在等待设备空闲。这可能需要几分钟，请访问上方链接查看实时进度。",
    viewResults: "查看结果：",
  },
};

export default aiHubDevice;