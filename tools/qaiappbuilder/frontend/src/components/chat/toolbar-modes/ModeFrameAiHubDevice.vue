<!--
  Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
  SPDX-License-Identifier: BSD-3-Clause
-->

<script setup lang="ts">
/**
 * ModeFrameAiHubDevice — chat-input sub-toolbar for `aihub-device` mode.
 *
 * Provides five sub-panels for interacting with Qualcomm AI Hub:
 *   1. Configure — set the AI Hub API token
 *   2. Devices   — list and select available target devices
 *   3. Models    — list and select available models
 *   4. Compile   — submit a model compilation job with live log streaming
 *   5. Profile   — submit a model profiling job with live log streaming
 * Long-running operations (compile / profile) stream their output via SSE
 * from the backend, which executes the `hub` CLI subprocess and forwards
 * stdout/stderr line-by-line.
 */
import { ref, computed, onMounted, onBeforeUnmount, useId } from "vue";
import { useI18n } from "vue-i18n";
import {
  fetchAiHubModels,
  type AiHubModel,
  fetchAiHubConfig,
  saveAiHubConfig,
  fetchAiHubDevices,
  submitCompileJob,
  submitProfileJob,
  streamJobLogs,
  type AiHubDevice,
  type AiHubSite,
} from "@/api/aiHubDevice";
import { useToast } from "@/composables/useToast";

const { t } = useI18n();
const toast = useToast();

const emit = defineEmits<{
  exit: [];
}>();

// ── Active sub-panel ─────────────────────────────────────────────────────────
type Panel = "configure" | "devices" | "models" | "compile" | "profile" | null;
const activePanel = ref<Panel>(null);  // Start with all panels closed
const inputIdPrefix = useId();
const tokenInputId = `${inputIdPrefix}-token`;
const compileModelInputId = `${inputIdPrefix}-compile-model`;
const compileDeviceInputId = `${inputIdPrefix}-compile-device`;
const profileModelInputId = `${inputIdPrefix}-profile-model`;
const profileDeviceInputId = `${inputIdPrefix}-profile-device`;
const compileDeviceListId = `${inputIdPrefix}-compile-devices`;
const profileDeviceListId = `${inputIdPrefix}-profile-devices`;
const selectedSite = ref<AiHubSite>("global");
const siteSwitchDisabled = computed(() => compileRunning.value || profileRunning.value);
const configureUrl = computed(() => selectedSite.value === "china"
  ? "https://aihub.qualcomm.cn"
  : "https://aihub.qualcomm.com");
let _siteGeneration = 0;


// ── Configure panel ──────────────────────────────────────────────────────────
const apiToken = ref("");
const configLoading = ref(false);
const configSaving = ref(false);
const configLoaded = ref(false);
const hasToken = ref(false);

async function loadConfig(): Promise<void> {
  if (configLoaded.value || configLoading.value) return;
  const site = selectedSite.value;
  const siteGeneration = _siteGeneration;
  configLoading.value = true;
  try {
    const cfg = await fetchAiHubConfig(site);
    if (_disposed || siteGeneration !== _siteGeneration) return;
    hasToken.value = cfg.has_token;
    // The backend deliberately returns only presence/masked data. Never put it
    // in the editable field, where it could accidentally be saved.
    apiToken.value = "";
    configLoaded.value = true;
  } catch {
    // Configuration presence is best-effort.
  } finally {
    if (!_disposed && siteGeneration === _siteGeneration) configLoading.value = false;
  }
}
async function saveConfig(): Promise<void> {
  if (configSaving.value) return;
  const token = apiToken.value.trim();
  if (token === "") {
    toast.error(t("aiHubDevice.configure.tokenRequired"));
    return;
  }
  const site = selectedSite.value;
  const siteGeneration = _siteGeneration;
  configSaving.value = true;
  try {
    await saveAiHubConfig(site, token);
    if (_disposed || siteGeneration !== _siteGeneration) return;
    apiToken.value = "";
    hasToken.value = true;
    toast.success(t("aiHubDevice.configure.saved"));
    activePanel.value = "devices";
    void loadDevices();
  } catch (e) {
    if (_disposed || siteGeneration !== _siteGeneration) return;
    const errMsg = e instanceof Error ? e.message : String(e);
    console.error("[ModeFrameAiHubDevice] saveConfig failed:", e);
    toast.error(t("aiHubDevice.configure.saveFailed") + " " + errMsg);
  } finally {
    if (!_disposed && siteGeneration === _siteGeneration) configSaving.value = false;
  }
}
function onConfigureClick(): void {
  // Toggle: if already open, close it; otherwise open it
  activePanel.value = activePanel.value === "configure" ? null : "configure";
  void loadConfig();
}

// ── Models panel ─────────────────────────────────────────────────────────────
const models = ref<AiHubModel[]>([]);
const modelsLoading = ref(false);
const modelsError = ref("");

async function loadModels(): Promise<void> {
  if (modelsLoading.value) return;
  const site = selectedSite.value;
  const siteGeneration = _siteGeneration;
  modelsLoading.value = true;
  modelsError.value = "";
  try {
    const result = await fetchAiHubModels(site);
    if (_disposed || siteGeneration !== _siteGeneration) return;
    models.value = result;
  } catch (e) {
    if (_disposed || siteGeneration !== _siteGeneration) return;
    modelsError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (!_disposed && siteGeneration === _siteGeneration) modelsLoading.value = false;
  }
}
function onModelsClick(): void {
  activePanel.value = activePanel.value === "models" ? null : "models";
  if (activePanel.value === "models") {
    void loadModels();
  }
}

// ── Devices panel ────────────────────────────────────────────────────────────
const devices = ref<AiHubDevice[]>([]);
const devicesLoading = ref(false);
const devicesError = ref("");

async function loadDevices(): Promise<void> {
  if (devicesLoading.value) return;
  const site = selectedSite.value;
  const siteGeneration = _siteGeneration;
  devicesLoading.value = true;
  devicesError.value = "";
  try {
    const result = await fetchAiHubDevices(site);
    if (_disposed || siteGeneration !== _siteGeneration) return;
    devices.value = result;
  } catch (e) {
    if (_disposed || siteGeneration !== _siteGeneration) return;
    devicesError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (!_disposed && siteGeneration === _siteGeneration) devicesLoading.value = false;
  }
}
function onDevicesClick(): void {
  // Toggle: if already open, close it; otherwise open it
  activePanel.value = activePanel.value === "devices" ? null : "devices";
  if (activePanel.value === "devices") {
    void loadDevices();
  }
}
function onCompileClick(): void {
  // Toggle: if already open, close it; otherwise open it
  activePanel.value = activePanel.value === "compile" ? null : "compile";
}

function onProfileClick(): void {
  // Toggle: if already open, close it; otherwise open it
  activePanel.value = activePanel.value === "profile" ? null : "profile";
}

// ── Device selection ─────────────────────────────────────────────────────────
function selectDevice(deviceName: string): void {
  selectedDevice.value = deviceName;
}

// ── Model selection ──────────────────────────────────────────────────────────
function selectModel(modelId: string): void {
  modelPath.value = modelId;
  // Switch to compile panel after selecting a model
  activePanel.value = "compile";
}
// ── Compile / Profile shared state ───────────────────────────────────────────
const modelPath = ref("");
const selectedDevice = ref("");

// Compile state
const compileLogs = ref<string[]>([]);
const compileRunning = ref(false);
const compileError = ref("");
let _stopCompileStream: (() => void) | null = null;

// Profile state
const profileLogs = ref<string[]>([]);
const profileRunning = ref(false);
const profileError = ref("");
let _stopProfileStream: (() => void) | null = null;
let _compileGeneration = 0;
let _profileGeneration = 0;
let _disposed = false;

function isCurrentCompile(generation: number, siteGeneration: number): boolean {
  return !_disposed && generation === _compileGeneration && siteGeneration === _siteGeneration;
}

function isCurrentProfile(generation: number, siteGeneration: number): boolean {
  return !_disposed && generation === _profileGeneration && siteGeneration === _siteGeneration;
}

function resetCompileJob(): void {
  compileLogs.value = [];
  compileRunning.value = false;
  compileError.value = "";
  if (_stopCompileStream !== null) {
    _stopCompileStream();
    _stopCompileStream = null;
  }
}

function resetProfileJob(): void {
  profileLogs.value = [];
  profileRunning.value = false;
  profileError.value = "";
  if (_stopProfileStream !== null) {
    _stopProfileStream();
    _stopProfileStream = null;
  }
}

async function runCompileJob(): Promise<void> {
  if (compileRunning.value) return;
  const model = modelPath.value.trim();
  const device = selectedDevice.value.trim();
  if (!model) {
    toast.error(t("aiHubDevice.job.modelRequired"));
    return;
  }
  if (!device) {
    toast.error(t("aiHubDevice.job.deviceRequired"));
    return;
  }

  resetCompileJob();
  compileRunning.value = true;
  compileLogs.value = [t("aiHubDevice.job.starting")];
  const generation = ++_compileGeneration;
  const site = selectedSite.value;
  const siteGeneration = _siteGeneration;
  const workbenchUrl = site === "china"
    ? "https://workbench.aihub.qualcomm.cn"
    : "https://workbench.aihub.qualcomm.com";

  try {
    const result = await submitCompileJob(site, { model_name: model, device });
    if (!isCurrentCompile(generation, siteGeneration)) return;
    const jobId = result.job_id;
    compileLogs.value.push(t("aiHubDevice.job.jobId", { id: jobId }));

    _stopCompileStream = streamJobLogs(site, jobId, {
      onLine: (line) => {
        if (!isCurrentCompile(generation, siteGeneration)) return;
        const urlMatch = line.match(new RegExp(`(${workbenchUrl.replaceAll(".", "\\.")}\\/[^\\s]+)`));
        if (urlMatch) {
          compileLogs.value.push(`${t("aiHubDevice.job.viewResults")}${urlMatch[1]}`);
          compileLogs.value.push(t("aiHubDevice.job.waiting"));
          return;
        }
        compileLogs.value.push(line);
      },
      onDone: () => {
        if (!isCurrentCompile(generation, siteGeneration)) return;
        compileRunning.value = false;
        compileLogs.value.push(t("aiHubDevice.job.done"));
        _stopCompileStream = null;
      },
      onError: (err) => {
        if (!isCurrentCompile(generation, siteGeneration)) return;
        compileRunning.value = false;
        compileError.value = err;
        compileLogs.value.push(t("aiHubDevice.job.error", { msg: err }));
        _stopCompileStream = null;
      },
    });
  } catch (e) {
    if (!isCurrentCompile(generation, siteGeneration)) return;
    compileRunning.value = false;
    compileError.value = e instanceof Error ? e.message : String(e);
    compileLogs.value.push(t("aiHubDevice.job.submitFailed", { msg: compileError.value }));
  }
}

async function runProfileJob(): Promise<void> {
  if (profileRunning.value) return;
  const model = modelPath.value.trim();
  const device = selectedDevice.value.trim();
  if (!model) {
    toast.error(t("aiHubDevice.job.modelRequired"));
    return;
  }
  if (!device) {
    toast.error(t("aiHubDevice.job.deviceRequired"));
    return;
  }

  resetProfileJob();
  profileRunning.value = true;
  profileLogs.value = [t("aiHubDevice.job.starting")];
  const generation = ++_profileGeneration;
  const site = selectedSite.value;
  const siteGeneration = _siteGeneration;
  const workbenchUrl = site === "china"
    ? "https://workbench.aihub.qualcomm.cn"
    : "https://workbench.aihub.qualcomm.com";

  try {
    const result = await submitProfileJob(site, { model_name: model, device });
    if (!isCurrentProfile(generation, siteGeneration)) return;
    const jobId = result.job_id;
    profileLogs.value.push(t("aiHubDevice.job.jobId", { id: jobId }));

    _stopProfileStream = streamJobLogs(site, jobId, {
      onLine: (line) => {
        if (!isCurrentProfile(generation, siteGeneration)) return;
        const urlMatch = line.match(new RegExp(`(${workbenchUrl.replaceAll(".", "\\.")}\\/[^\\s]+)`));
        if (urlMatch) {
          profileLogs.value.push(`${t("aiHubDevice.job.viewResults")}${urlMatch[1]}`);
          profileLogs.value.push(t("aiHubDevice.job.waiting"));
          return;
        }
        profileLogs.value.push(line);
      },
      onDone: () => {
        if (!isCurrentProfile(generation, siteGeneration)) return;
        profileRunning.value = false;
        profileLogs.value.push(t("aiHubDevice.job.done"));
        _stopProfileStream = null;
      },
      onError: (err) => {
        if (!isCurrentProfile(generation, siteGeneration)) return;
        profileRunning.value = false;
        profileError.value = err;
        profileLogs.value.push(t("aiHubDevice.job.error", { msg: err }));
        _stopProfileStream = null;
      },
    });
  } catch (e) {
    if (!isCurrentProfile(generation, siteGeneration)) return;
    profileRunning.value = false;
    profileError.value = e instanceof Error ? e.message : String(e);
    profileLogs.value.push(t("aiHubDevice.job.submitFailed", { msg: profileError.value }));
  }
}
// ── Device selector options ───────────────────────────────────────────────────

const deviceOptions = computed<string[]>(() =>
  devices.value.map((d) => d.name),
);

// ── Exit ─────────────────────────────────────────────────────────────────────
function invalidateJobs(): void {
  ++_compileGeneration;
  ++_profileGeneration;
  if (_stopCompileStream !== null) {
    _stopCompileStream();
    _stopCompileStream = null;
  }
  if (_stopProfileStream !== null) {
    _stopProfileStream();
    _stopProfileStream = null;
  }
}

function switchSite(site: AiHubSite): void {
  if (site === selectedSite.value || siteSwitchDisabled.value) return;
  ++_siteGeneration;
  invalidateJobs();
  selectedSite.value = site;
  activePanel.value = null;
  apiToken.value = "";
  configLoaded.value = false;
  configLoading.value = false;
  configSaving.value = false;
  hasToken.value = false;
  models.value = [];
  modelsLoading.value = false;
  modelsError.value = "";
  devices.value = [];
  devicesLoading.value = false;
  devicesError.value = "";
  modelPath.value = "";
  selectedDevice.value = "";
  resetCompileJob();
  resetProfileJob();
  void loadConfig();
}

function onSiteChange(event: Event): void {
  switchSite((event.target as HTMLSelectElement).value as AiHubSite);
}

function onExit(): void {
  invalidateJobs();
  emit("exit");
}
// Check token configuration on mount.
onMounted(() => {
  void loadConfig();
});
onBeforeUnmount(() => {
  _disposed = true;
  invalidateJobs();
});
</script>

<template>
  <div
    class="rit-left ahd-frame"
    data-testid="mode-frame-aihub-device"
  >
    <!-- Mode badge / exit button -->
    <button
      type="button"
      class="rit-mode-badge"
      data-testid="mode-frame-exit"
      @click="onExit"
    >
      <!-- AI Hub Device chip icon -->
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <rect x="7" y="7" width="10" height="10" rx="1.5" />
        <path d="M9 7V4M12 7V4M15 7V4" />
        <path d="M9 17v3M12 17v3M15 17v3" />
        <path d="M7 9H4M7 12H4M7 15H4" />
        <path d="M17 9h3M17 12h3M17 15h3" />
      </svg>
      <span>{{ t("index.aiHubDevice") }}</span>
      <span class="rit-close">✕</span>
    </button>

    <span class="rit-sep"></span>
    <div class="ahd-site-selector">
      <label class="ahd-site-label" for="ahd-site-select">
        {{ t("aiHubDevice.site.label") }}
      </label>
      <select
        id="ahd-site-select"
        class="config-input ahd-site-select"
        :value="selectedSite"
        :disabled="siteSwitchDisabled"
        data-testid="ahd-site-select"
        @change="onSiteChange"
      >
        <option value="global">{{ t("aiHubDevice.site.global") }}</option>
        <option value="china">{{ t("aiHubDevice.site.china") }}</option>
      </select>
    </div>

    <span class="rit-sep"></span>

    <!-- Sub-panel tab buttons -->
    <!-- Configure -->
    <button
      type="button"
      class="rit-btn"
      :class="{ 'rit-model-upload--active': activePanel === 'configure' }"
      data-testid="ahd-tab-configure"
      :title="t('aiHubDevice.tooltip.configure')"
      @click="onConfigureClick"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68 1.65 1.65 0 0 0 10 3.17V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
      <span>{{ t("aiHubDevice.tab.configure") }}</span>
    </button>

    <span class="rit-sep"></span>

    <!-- Devices -->
    <button
      type="button"
      class="rit-btn"
      :class="{ 'rit-model-upload--active': activePanel === 'devices' }"
      data-testid="ahd-tab-devices"
      :title="t('aiHubDevice.tooltip.devices')"
      @click="onDevicesClick"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="7" y="7" width="10" height="10" rx="1.5" />
        <path d="M9 7V4M12 7V4M15 7V4M9 17v3M12 17v3M15 17v3M7 9H4M7 12H4M7 15H4M17 9h3M17 12h3M17 15h3" />
      </svg>
      <span>{{ t("aiHubDevice.tab.devices") }}</span>
    </button>

    <span class="rit-sep"></span>

    <!-- Models -->
    <button
      type="button"
      class="rit-btn"
      :class="{ 'rit-model-upload--active': activePanel === 'models' }"
      data-testid="ahd-tab-models"
      :title="t('aiHubDevice.tooltip.models')"
      @click="onModelsClick"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
        <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
        <line x1="12" y1="22.08" x2="12" y2="12" />
      </svg>
      <span>{{ t("aiHubDevice.tab.models") }}</span>
    </button>

    <span class="rit-sep"></span>

    <!-- Compile -->
    <button
      type="button"
      class="rit-btn"
      :class="{ 'rit-model-upload--active': activePanel === 'compile' }"
      data-testid="ahd-tab-compile"
      :title="t('aiHubDevice.tooltip.compile')"
      @click="onCompileClick"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
      </svg>
      <span>{{ t("aiHubDevice.tab.compile") }}</span>
    </button>

    <span class="rit-sep"></span>

    <!-- Profile -->
    <button
      type="button"
      class="rit-btn"
      :class="{ 'rit-model-upload--active': activePanel === 'profile' }"
      data-testid="ahd-tab-profile"
      :title="t('aiHubDevice.tooltip.profile')"
      @click="onProfileClick"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" />
      </svg>
      <span>{{ t("aiHubDevice.tab.profile") }}</span>
    </button>

    <!-- ── Sub-panels ──────────────────────────────────────────────────────── -->

    <!-- Configure panel -->
    <div
      v-if="activePanel === 'configure'"
      class="rit-submenu ahd-panel"
      data-testid="ahd-panel-configure"
    >
      <div class="rit-submenu-header">{{ t("aiHubDevice.configure.title") }}</div>
      <div v-if="configLoading" class="ahd-loading">{{ t("aiHubDevice.configure.loading") }}</div>
      <div v-else class="ahd-configure-body">
        <label class="ahd-label" :for="tokenInputId">{{ t("aiHubDevice.configure.tokenLabel") }}</label>
        <input
          v-model="apiToken"
          :id="tokenInputId"
          type="password"
          class="ahd-input"
          :placeholder="t('aiHubDevice.configure.tokenPlaceholder')"
          data-testid="ahd-token-input"
          @keyup.enter="saveConfig"
        />
        <div v-if="hasToken" class="ahd-configure-hint">
          {{ t("aiHubDevice.configure.tokenConfigured") }}
        </div>
        <div class="ahd-configure-hint">
          {{ t("aiHubDevice.configure.tokenHintPrefix") }}
          <a
            :href="configureUrl"
            target="_blank"
            rel="noopener noreferrer"
            class="ahd-link"
          >
            {{ configureUrl }}
          </a>
          {{ t("aiHubDevice.configure.tokenHintSuffix") }}
        </div>
        <button
          type="button"
          class="rit-btn ahd-save-btn"
          :disabled="configSaving || apiToken.trim() === ''"
          data-testid="ahd-save-btn"
          @click="saveConfig"
        >
          {{ configSaving ? t("aiHubDevice.configure.saving") : t("aiHubDevice.configure.save") }}
        </button>
      </div>
    </div>

    <!-- Devices panel -->
    <div
      v-if="activePanel === 'devices'"
      class="rit-submenu ahd-panel ahd-panel--wide"
      data-testid="ahd-panel-devices"
    >
      <div class="ahd-panel-header-row">
        <span class="rit-submenu-header">{{ t("aiHubDevice.devices.title") }}</span>
        <button
          type="button"
          class="rit-btn ahd-refresh-btn"
          :disabled="devicesLoading"
          data-testid="ahd-refresh-devices"
          @click="loadDevices"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" />
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
          </svg>
          {{ t("aiHubDevice.devices.refresh") }}
        </button>
      </div>
      <div v-if="devicesLoading" class="ahd-loading">{{ t("aiHubDevice.devices.loading") }}</div>
      <div v-else-if="devicesError" class="ahd-error">{{ devicesError }}</div>
      <div v-else-if="devices.length === 0" class="ahd-empty">{{ t("aiHubDevice.devices.empty") }}</div>
      <div v-else class="ahd-table-container">
        <div class="ahd-table-header">
          <table class="ahd-table">
            <thead>
              <tr>
                <th>{{ t("aiHubDevice.devices.colName") }}</th>
                <th>{{ t("aiHubDevice.devices.colOs") }}</th>
                <th>{{ t("aiHubDevice.devices.colChipset") }}</th>
              </tr>
            </thead>
          </table>
        </div>
        <div class="ahd-table-body">
          <table class="ahd-table" data-testid="ahd-devices-table">
            <tbody>
              <tr
                v-for="d in devices"
                :key="d.name"
                class="ahd-device-row"
                :class="{ 'ahd-device-row--selected': selectedDevice === d.name }"
                @click="selectDevice(d.name)"
              >
                <td>
                  <button
                    type="button"
                    class="ahd-table-select"
                    @click.stop="selectDevice(d.name)"
                  >
                    {{ d.name }}
                  </button>
                </td>
                <td>{{ d.os }}</td>
                <td>{{ d.chipset }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Models panel -->
    <div
      v-if="activePanel === 'models'"
      class="rit-submenu ahd-panel ahd-panel--wide"
      data-testid="ahd-panel-models"
    >
      <div class="rit-submenu-header">{{ t("aiHubDevice.models.title") }}</div>
      <div v-if="modelsLoading" class="ahd-loading">
        {{ t("aiHubDevice.models.loading") }}
      </div>
      <div v-else-if="modelsError" class="ahd-error">
        {{ modelsError }}
      </div>
      <div v-else-if="models.length === 0" class="ahd-empty">
        {{ t("aiHubDevice.models.empty") }}
      </div>
      <div v-else class="ahd-table-container">
        <div class="ahd-table-header">
          <table class="ahd-table">
            <thead>
              <tr>
                <th>{{ t("aiHubDevice.models.colModelId") }}</th>
                <th>{{ t("aiHubDevice.models.colName") }}</th>
              </tr>
            </thead>
          </table>
        </div>
        <div class="ahd-table-body">
          <table class="ahd-table" data-testid="ahd-models-table">
            <tbody>
              <tr
                v-for="m in models"
                :key="m.model_id"
                class="ahd-model-row"
                @click="selectModel(m.model_id)"
              >
                <td>
                  <button
                    type="button"
                    class="ahd-table-select"
                    @click.stop="selectModel(m.model_id)"
                  >
                    {{ m.model_id }}
                  </button>
                </td>
                <td>{{ m.name }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Compile panel -->
    <div
      v-if="activePanel === 'compile'"
      class="rit-submenu ahd-panel ahd-panel--wide"
      data-testid="ahd-panel-compile"
    >
      <div class="rit-submenu-header">{{ t("aiHubDevice.compile.title") }}</div>
      <div class="ahd-job-form">
        <label class="ahd-label" :for="compileModelInputId">{{ t("aiHubDevice.job.modelLabel") }}</label>
        <input
          v-model="modelPath"
          :id="compileModelInputId"
          type="text"
          class="ahd-input"
          :placeholder="t('aiHubDevice.job.modelPlaceholder')"
          data-testid="ahd-compile-model"
        />
        <label class="ahd-label" :for="compileDeviceInputId">{{ t("aiHubDevice.job.deviceLabel") }}</label>
        <input
          v-model="selectedDevice"
          :id="compileDeviceInputId"
          type="text"
          class="ahd-input"
          :placeholder="t('aiHubDevice.job.devicePlaceholder')"
          :list="compileDeviceListId"
          data-testid="ahd-compile-device"
        />
        <datalist :id="compileDeviceListId">
          <option v-for="d in deviceOptions" :key="d" :value="d" />
        </datalist>
        <button
          type="button"
          class="rit-btn ahd-run-btn"
          :disabled="compileRunning"
          data-testid="ahd-compile-run"
          @click="runCompileJob"
        >
          {{ compileRunning ? t("aiHubDevice.job.running") : t("aiHubDevice.compile.run") }}
        </button>
      </div>
      <div v-if="compileLogs.length > 0" class="ahd-log-area" data-testid="ahd-compile-logs">
        <div
          v-for="(line, i) in compileLogs"
          :key="i"
          class="ahd-log-line"
          :class="{ 'ahd-log-line--error': line.startsWith('[error]') || line.startsWith('[Error]') }"
        >{{ line }}</div>
      </div>
    </div>

    <!-- Profile panel -->
    <div
      v-if="activePanel === 'profile'"
      class="rit-submenu ahd-panel ahd-panel--wide"
      data-testid="ahd-panel-profile"
    >
      <div class="rit-submenu-header">{{ t("aiHubDevice.profile.title") }}</div>
      <div class="ahd-job-form">
        <label class="ahd-label" :for="profileModelInputId">{{ t("aiHubDevice.job.modelLabel") }}</label>
        <input
          v-model="modelPath"
          :id="profileModelInputId"
          type="text"
          class="ahd-input"
          :placeholder="t('aiHubDevice.job.modelPlaceholder')"
          data-testid="ahd-profile-model"
        />
        <label class="ahd-label" :for="profileDeviceInputId">{{ t("aiHubDevice.job.deviceLabel") }}</label>
        <input
          v-model="selectedDevice"
          :id="profileDeviceInputId"
          type="text"
          class="ahd-input"
          :placeholder="t('aiHubDevice.job.devicePlaceholder')"
          :list="profileDeviceListId"
          data-testid="ahd-profile-device"
        />
        <datalist :id="profileDeviceListId">
          <option v-for="d in deviceOptions" :key="d" :value="d" />
        </datalist>
        <button
          type="button"
          class="rit-btn ahd-run-btn"
          :disabled="profileRunning"
          data-testid="ahd-profile-run"
          @click="runProfileJob"
        >
          {{ profileRunning ? t("aiHubDevice.job.running") : t("aiHubDevice.profile.run") }}
        </button>
      </div>
      <div v-if="profileLogs.length > 0" class="ahd-log-area" data-testid="ahd-profile-logs">
        <div
          v-for="(line, i) in profileLogs"
          :key="i"
          class="ahd-log-line"
          :class="{ 'ahd-log-line--error': line.startsWith('[error]') || line.startsWith('[Error]') }"
        >{{ line }}</div>
      </div>
    </div>

    <!-- Dropdown overlay: click outside to close current panel -->
    <div
      v-if="activePanel !== null"
      class="dropdown-overlay"
      @click="activePanel = null"
    ></div>
  </div>
</template>

<style scoped>
.ahd-frame {
  position: relative;
  flex-wrap: wrap;
}

.ahd-site-selector {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
}

.ahd-site-label {
  color: var(--text-secondary);
  font-size: var(--text-xs, 11px);
  white-space: nowrap;
}

.ahd-site-select {
  width: auto;
  padding: 4px 24px 4px 7px;
  font-size: var(--text-xs, 11px);
  white-space: nowrap;
}

/* Panel container */
.ahd-panel {
  min-width: 320px;
  max-height: 420px;
  overflow-y: auto;
}

.ahd-panel--wide {
  min-width: 480px;
}

/* Header row with refresh button */
.ahd-panel-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px 4px;
}

.ahd-panel-header-row .rit-submenu-header {
  padding: 0;
}

.ahd-refresh-btn {
  font-size: var(--text-xs, 11px);
  gap: 4px;
}

/* Configure body */
.ahd-configure-body {
  padding: 8px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* Fixed table header and scrollable body */
.ahd-table-container {
  display: flex;
  flex-direction: column;
  max-height: 320px;
}

.ahd-table-header {
  flex-shrink: 0;
  overflow: hidden;
  border-bottom: 1px solid var(--border-color, #e5e7eb);
  background: var(--bg-secondary, #f9fafb);
}
.ahd-table-select {
  width: 100%;
  padding: 0;
  color: inherit;
  font: inherit;
  text-align: left;
  background: transparent;
  border: 0;
  cursor: pointer;
}

.ahd-table-select:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.ahd-table-body {
  flex: 1;
  overflow-y: auto;
}

/* Device row selection */
.ahd-device-row {
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.ahd-device-row:hover {
  background-color: rgba(59, 130, 246, 0.1);
}

.ahd-device-row--selected {
  background-color: rgba(59, 130, 246, 0.2);
}

.ahd-device-row--selected:hover {
  background-color: rgba(59, 130, 246, 0.3);
}



.ahd-label {
  font-size: var(--text-xs, 11px);
  color: var(--text-secondary);
  margin-bottom: 2px;
}

.ahd-input {
  width: 100%;
  padding: 5px 8px;
  font-size: var(--text-sm);
  color: var(--text-primary, inherit);
  background: var(--bg-primary, transparent);
  border: 1px solid var(--border, #3a3a42);
  border-radius: 6px;
  box-sizing: border-box;
}

.ahd-configure-hint {
  font-size: var(--text-xs, 11px);
  color: var(--text-secondary);
  line-height: 1.4;
}

.ahd-save-btn,
.ahd-run-btn {
  align-self: flex-start;
  margin-top: 4px;
}

/* Job form */
.ahd-job-form {
  padding: 8px 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

/* Devices table */
.ahd-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-xs, 11px);
  margin: 4px 0 8px;
}

.ahd-table th,
.ahd-table td {
  padding: 5px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border, #3a3a42);
}

.ahd-table th {
  color: var(--text-secondary);
  font-weight: 600;
  background: var(--bg-secondary, transparent);
}

.ahd-table tr:last-child td {
  border-bottom: none;
}

/* Log area */
.ahd-log-area {
  margin: 6px 12px 10px;
  padding: 8px 10px;
  background: var(--bg-code, #0d0d12);
  border: 1px solid var(--border, #3a3a42);
  border-radius: 6px;
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  line-height: 1.5;
  max-height: 200px;
  overflow-y: auto;
  color: var(--text-primary);
}

.ahd-log-line {
  white-space: pre-wrap;
  word-break: break-all;
}

.ahd-log-line--error {
  color: var(--color-error, #f87171);
}

/* States */
.ahd-loading,
.ahd-empty {
  padding: 10px 12px;
  font-size: var(--text-xs, 11px);
  color: var(--text-secondary);
}

/* Link styling for AI Hub hint */
.ahd-link {
  color: var(--color-primary);
  text-decoration: underline;
  cursor: pointer;
}

.ahd-link:hover {
  text-decoration: none;
}

.ahd-link:focus {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.ahd-error {
  padding: 10px 12px;
  font-size: var(--text-xs, 11px);
  color: var(--color-error, #f87171);
}
</style>