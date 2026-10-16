// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

const aiHubDevice = {
  site: {
    label: "AI Hub 站點",
    global: "全球 AI Hub",
    china: "中國 AI Hub",
  },
  tab: {
    configure: "設定",
    devices: "裝置",
    models: "模型",
    compile: "編譯",
    profile: "效能分析",
  },
  tooltip: {
    configure: "設定 Qualcomm AI Hub 的存取憑證和執行參數。完成設定後即可使用裝置、編譯和效能分析功能。",
    devices: "檢視並選擇 AI Hub 提供的目標裝置。所選裝置將用於模型編譯和效能分析。",
    models: "檢視並選擇 AI Hub 中可用的模型。所選模型可用於後續編譯或效能分析。",
    compile: "針對指定裝置編譯並最佳化模型，產生可部署的目標產物。",
    profile: "在指定裝置上執行模型並分析效能，檢視延遲、吞吐量等指標。",
  },
  configure: {
    title: "設定 AI Hub 用戶端",
    loading: "載入設定中…",
    tokenLabel: "API Token",
    tokenPlaceholder: "輸入您的 Qualcomm AI Hub API Token",
    tokenConfigured: "已設定 API Token。僅在需要替換時輸入新的 Token。",
    tokenRequired: "請輸入新的 API Token 後再儲存。",
    tokenHintPrefix: "在",
    tokenHintLink: "https://aihub.qualcomm.com",
    tokenHintSuffix: "— 帳戶 → API Token 取得您的 Token。",
    save: "儲存",
    saving: "儲存中…",
    saved: "API Token 已儲存。",
    saveFailed: "儲存 API Token 失敗。",
  },
  compile: {
    title: "編譯",
    run: "執行編譯",
  },
  models: {
    title: "可用模型",
    loading: "載入模型中…",
    empty: "未找到模型。請確認已設定 API Token。",
    colModelId: "模型 ID",
    colName: "名稱",
  },
  profile: {
    title: "效能分析",
    run: "執行效能分析",
  },

  devices: {
    title: "可用裝置",
    loading: "載入裝置中…",
    empty: "未找到裝置。請確認已設定 API Token。",
    refresh: "重新整理",
    colName: "裝置",
    colOs: "作業系統",
    colChipset: "晶片組",
  },
  job: {
    modelLabel: "模型名稱或路徑",
    modelPlaceholder: "mm5lrg54q（模型: ResNet50）或 /path/to/model.onnx",
    deviceLabel: "目標裝置",
    devicePlaceholder: "例如：Samsung Galaxy S24 (Snapdragon 8 Gen 3)",
    modelRequired: "請輸入模型名稱或路徑。",
    deviceRequired: "請選擇目標裝置。",
    starting: "正在啟動任務…",
    jobId: "任務 ID：{id}",
    running: "執行中…",
    done: "任務已完成。",
    error: "任務錯誤：{msg}",
    waiting: "任務已提交到佇列，正在等待裝置空閒。這可能需要幾分鐘，請訪問上方連結查看即時進度。",
    viewResults: "查看結果：",
    submitFailed: "提交任務失敗：{msg}",
  },
};

export default aiHubDevice;