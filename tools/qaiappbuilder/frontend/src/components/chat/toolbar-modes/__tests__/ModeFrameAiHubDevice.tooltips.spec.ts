// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { flushPromises, mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { beforeEach, describe, expect, it, vi } from "vitest";

import enAiHubDevice from "@/locales/en/aiHubDevice";
import zhCnAiHubDevice from "@/locales/zh-CN/aiHubDevice";
import zhTwAiHubDevice from "@/locales/zh-TW/aiHubDevice";
import enIndex from "@/locales/en/index";
import zhCnIndex from "@/locales/zh-CN/index";
import zhTwIndex from "@/locales/zh-TW/index";
import * as aiHubApi from "@/api/aiHubDevice";

vi.mock("@/api/aiHubDevice", () => ({
  fetchAiHubConfig: vi.fn().mockResolvedValue({ has_token: false }),
  saveAiHubConfig: vi.fn(),
  fetchAiHubDevices: vi.fn().mockResolvedValue([]),
  fetchAiHubModels: vi.fn().mockResolvedValue([]),
  submitCompileJob: vi.fn(),
  submitProfileJob: vi.fn(),
  streamJobLogs: vi.fn(),
}));

vi.mock("@/composables/useToast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));

import ModeFrameAiHubDevice from "@/components/chat/toolbar-modes/ModeFrameAiHubDevice.vue";

const buttons = ["configure", "devices", "models", "compile", "profile"] as const;
const expectedTitles = {
  en: {
    configure: "Configure Qualcomm AI Hub access credentials and runtime settings. Once configured, you can use device, compilation, and profiling features.",
    devices: "View and select target devices available through AI Hub. The selected device will be used for model compilation and profiling.",
    models: "View and select models available on AI Hub. The selected model can then be compiled or profiled.",
    compile: "Compile and optimize a model for a specified device to generate a deployable target artifact.",
    profile: "Run a model on a specified device and analyze performance metrics such as latency and throughput.",
  },
  "zh-CN": {
    configure: "配置 Qualcomm AI Hub 的访问凭据和运行参数。完成配置后即可使用设备、编译和性能分析功能。",
    devices: "查看并选择 AI Hub 提供的目标设备。所选设备将用于模型编译和性能分析。",
    models: "查看并选择 AI Hub 中可用的模型。所选模型可用于后续编译或性能分析。",
    compile: "针对指定设备编译并优化模型，生成可部署的目标产物。",
    profile: "在指定设备上运行模型并分析性能，查看延迟、吞吐量等指标。",
  },
  "zh-TW": {
    configure: "設定 Qualcomm AI Hub 的存取憑證和執行參數。完成設定後即可使用裝置、編譯和效能分析功能。",
    devices: "檢視並選擇 AI Hub 提供的目標裝置。所選裝置將用於模型編譯和效能分析。",
    models: "檢視並選擇 AI Hub 中可用的模型。所選模型可用於後續編譯或效能分析。",
    compile: "針對指定裝置編譯並最佳化模型，產生可部署的目標產物。",
    profile: "在指定裝置上執行模型並分析效能，檢視延遲、吞吐量等指標。",
  },
} as const;

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ModeFrameAiHubDevice tooltips", () => {
  it("uses all five active-locale tooltip messages as native titles", async () => {
    const i18n = createI18n({
      legacy: false,
      locale: "en",
      fallbackLocale: "en",
      messages: {
        en: { aiHubDevice: enAiHubDevice, index: enIndex },
        "zh-CN": { aiHubDevice: zhCnAiHubDevice, index: zhCnIndex },
        "zh-TW": { aiHubDevice: zhTwAiHubDevice, index: zhTwIndex },
      },
    });
    const wrapper = mount(ModeFrameAiHubDevice, {
      global: {
        plugins: [i18n],
        mocks: { $t: (key: string) => key },
      },
    });

    for (const locale of ["en", "zh-CN", "zh-TW"] as const) {
      i18n.global.locale.value = locale;
      await wrapper.vm.$nextTick();

      for (const button of buttons) {
        expect(wrapper.get(`[data-testid="ahd-tab-${button}"]`).attributes("title"))
          .toBe(expectedTitles[locale][button]);
      }
    }
  });

  it("localizes the accessible site selector and uses the selected site's configure link", async () => {
    const cases = [
      ["en", enAiHubDevice, "AI Hub site", "Global AI Hub", "China AI Hub"],
      ["zh-CN", zhCnAiHubDevice, "AI Hub 站点", "全球 AI Hub", "中国 AI Hub"],
      ["zh-TW", zhTwAiHubDevice, "AI Hub 站點", "全球 AI Hub", "中國 AI Hub"],
    ] as const;

    for (const [locale, messages, siteLabel, globalLabel, chinaLabel] of cases) {
      const i18n = createI18n({
        legacy: false,
        locale,
        messages: { [locale]: { aiHubDevice: messages, index: { aiHubDevice: "AI Hub Device" } } },
      });
      const wrapper = mount(ModeFrameAiHubDevice, { global: { plugins: [i18n] } });
      await flushPromises();
      const select = wrapper.get('[data-testid="ahd-site-select"]');
      const label = wrapper.get(`label[for="${select.attributes("id")}"]`);
      expect(select.attributes("id")).toBe("ahd-site-select");
      expect(select.element.tagName).toBe("SELECT");
      expect(label.text()).toBe(siteLabel);
      expect(select.findAll("option").map(option => option.text())).toEqual([globalLabel, chinaLabel]);
      expect((select.element as HTMLSelectElement).value).toBe("global");
      await wrapper.get('[data-testid="ahd-tab-configure"]').trigger("click");
      expect(wrapper.get(".ahd-link").attributes("href")).toBe("https://aihub.qualcomm.com");
      await select.setValue("china");
      await flushPromises();
      await wrapper.get('[data-testid="ahd-tab-configure"]').trigger("click");
      expect(wrapper.get(".ahd-link").attributes("href")).toBe("https://aihub.qualcomm.cn");
      wrapper.unmount();
    }
  });

  it("reloads configuration and clears selected data when the site changes", async () => {
    vi.mocked(aiHubApi.fetchAiHubModels).mockResolvedValueOnce([
      { model_id: "global-model", name: "Global Model" },
    ]);
    const i18n = createI18n({
      legacy: false,
      locale: "en",
      messages: { en: { aiHubDevice: enAiHubDevice, index: { aiHubDevice: "AI Hub Device" } } },
    });
    const wrapper = mount(ModeFrameAiHubDevice, { global: { plugins: [i18n] } });
    await flushPromises();
    expect(aiHubApi.fetchAiHubConfig).toHaveBeenCalledWith("global");
    await wrapper.get('[data-testid="ahd-tab-models"]').trigger("click");
    await flushPromises();
    await wrapper.get('[data-testid="ahd-models-table"] tbody button').trigger("click");
    expect((wrapper.get('[data-testid="ahd-compile-model"]').element as HTMLInputElement).value)
      .toBe("global-model");

    await wrapper.get('[data-testid="ahd-site-select"]').setValue("china");
    await flushPromises();
    expect(aiHubApi.fetchAiHubConfig).toHaveBeenLastCalledWith("china");
    await wrapper.get('[data-testid="ahd-tab-compile"]').trigger("click");
    expect((wrapper.get('[data-testid="ahd-compile-model"]').element as HTMLInputElement).value)
      .toBe("");
  });

  it("ignores a late response from the previously selected site", async () => {
    const globalModels = Promise.withResolvers<Array<{ model_id: string; name: string }>>();
    vi.mocked(aiHubApi.fetchAiHubModels)
      .mockReturnValueOnce(globalModels.promise)
      .mockResolvedValueOnce([{ model_id: "china-model", name: "China Model" }]);
    const i18n = createI18n({
      legacy: false,
      locale: "en",
      messages: { en: { aiHubDevice: enAiHubDevice, index: { aiHubDevice: "AI Hub Device" } } },
    });
    const wrapper = mount(ModeFrameAiHubDevice, { global: { plugins: [i18n] } });
    await flushPromises();
    await wrapper.get('[data-testid="ahd-tab-models"]').trigger("click");
    await wrapper.get('[data-testid="ahd-site-select"]').setValue("china");
    await flushPromises();
    await wrapper.get('[data-testid="ahd-tab-models"]').trigger("click");
    await flushPromises();
    globalModels.resolve([{ model_id: "global-late", name: "Late Global" }]);
    await flushPromises();

    expect(wrapper.get('[data-testid="ahd-models-table"]').text()).toContain("china-model");
    expect(wrapper.get('[data-testid="ahd-models-table"]').text()).not.toContain("global-late");
  });

  it("loads models and carries a selected model into compilation", async () => {
    vi.mocked(aiHubApi.fetchAiHubModels).mockResolvedValueOnce([
      { model_id: "model-123", name: "ResNet" },
    ]);
    const i18n = createI18n({
      legacy: false,
      locale: "en",
      fallbackLocale: "en",
      messages: { en: { aiHubDevice: enAiHubDevice, index: { aiHubDevice: "AI Hub Device" } } },
    });
    const wrapper = mount(ModeFrameAiHubDevice, {
      global: { plugins: [i18n] },
    });

    await wrapper.get('[data-testid="ahd-tab-models"]').trigger("click");
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="ahd-models-table"]').exists()).toBe(true);
    });
    await wrapper.get('[data-testid="ahd-models-table"] tbody tr').trigger("click");

    expect(wrapper.find('[data-testid="ahd-panel-models"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="ahd-panel-compile"]').isVisible()).toBe(true);
    expect((wrapper.get('[data-testid="ahd-compile-model"]').element as HTMLInputElement).value)
      .toBe("model-123");
  });


  it("shows device failures in the error UI and reserves empty text for successful empty inventory", async () => {
    vi.mocked(aiHubApi.fetchAiHubDevices)
      .mockRejectedValueOnce(new Error("Could not load devices from AI Hub. Please try again."))
      .mockResolvedValueOnce([]);
    const i18n = createI18n({
      legacy: false,
      locale: "en",
      messages: { en: { aiHubDevice: enAiHubDevice, index: { aiHubDevice: "AI Hub Device" } } },
    });
    const wrapper = mount(ModeFrameAiHubDevice, { global: { plugins: [i18n] } });

    await wrapper.get('[data-testid="ahd-tab-devices"]').trigger("click");
    await flushPromises();
    expect(wrapper.get(".ahd-error").text()).toBe(
      "Could not load devices from AI Hub. Please try again.",
    );
    expect(wrapper.find(".ahd-empty").exists()).toBe(false);

    await wrapper.get('[data-testid="ahd-refresh-devices"]').trigger("click");
    await flushPromises();
    expect(wrapper.find(".ahd-error").exists()).toBe(false);
    expect(wrapper.get(".ahd-empty").text()).toBe(
      "No devices found. Make sure your API token is configured.",
    );
  });
  it("uses native buttons for keyboard-accessible device and model selection", async () => {
    vi.mocked(aiHubApi.fetchAiHubDevices).mockResolvedValueOnce([
      { name: "Test Device", os: "Android", chipset: "Test Chip" },
    ]);
    vi.mocked(aiHubApi.fetchAiHubModels).mockResolvedValueOnce([
      { model_id: "model-keyboard", name: "Keyboard Model" },
    ]);
    const i18n = createI18n({
      legacy: false,
      locale: "en",
      messages: { en: { aiHubDevice: enAiHubDevice, index: { aiHubDevice: "AI Hub Device" } } },
    });
    const wrapper = mount(ModeFrameAiHubDevice, { global: { plugins: [i18n] } });

    await wrapper.get('[data-testid="ahd-tab-devices"]').trigger("click");
    await flushPromises();
    const deviceControl = wrapper.get('[data-testid="ahd-devices-table"] tbody button');
    expect(deviceControl.element.tagName).toBe("BUTTON");
    await deviceControl.trigger("click");
    expect(wrapper.get('[data-testid="ahd-devices-table"] tbody tr').classes())
      .toContain("ahd-device-row--selected");

    await wrapper.get('[data-testid="ahd-tab-models"]').trigger("click");
    await flushPromises();
    const modelControl = wrapper.get('[data-testid="ahd-models-table"] tbody button');
    expect(modelControl.element.tagName).toBe("BUTTON");
    await modelControl.trigger("click");
    expect((wrapper.get('[data-testid="ahd-compile-model"]').element as HTMLInputElement).value)
      .toBe("model-keyboard");
  });

  it("associates visible labels with token, compile, and profile inputs", async () => {
    const i18n = createI18n({
      legacy: false,
      locale: "en",
      messages: { en: { aiHubDevice: enAiHubDevice, index: { aiHubDevice: "AI Hub Device" } } },
    });
    const wrapper = mount(ModeFrameAiHubDevice, { global: { plugins: [i18n] } });

    for (const panel of ["configure", "compile", "profile"] as const) {
      await wrapper.get(`[data-testid="ahd-tab-${panel}"]`).trigger("click");
      await flushPromises();
      for (const label of wrapper.findAll(`[data-testid="ahd-panel-${panel}"] label`)) {
        const inputId = label.attributes("for");
        expect(inputId).toBeTruthy();
        expect(wrapper.get(`[id="${inputId}"]`).element.tagName).toBe("INPUT");
      }
    }
  });

  it.each([
    ["compile", "exit"],
    ["compile", "unmount"],
    ["profile", "exit"],
    ["profile", "unmount"],
  ] as const)(
    "does not open a %s job stream when submission resolves after %s",
    async (job, action) => {
      const submission = Promise.withResolvers<{ job_id: string }>();
      const submit = job === "compile"
        ? vi.mocked(aiHubApi.submitCompileJob)
        : vi.mocked(aiHubApi.submitProfileJob);
      submit.mockReturnValueOnce(submission.promise);
      const i18n = createI18n({
        legacy: false,
        locale: "en",
        messages: { en: { aiHubDevice: enAiHubDevice, index: { aiHubDevice: "AI Hub Device" } } },
      });
      const wrapper = mount(ModeFrameAiHubDevice, { global: { plugins: [i18n] } });
      await wrapper.get(`[data-testid="ahd-tab-${job}"]`).trigger("click");
      await wrapper.get(`[data-testid="ahd-${job}-model"]`).setValue("model-1");
      await wrapper.get(`[data-testid="ahd-${job}-device"]`).setValue("device-1");
      await wrapper.get(`[data-testid="ahd-${job}-run"]`).trigger("click");
      expect(wrapper.get('[data-testid="ahd-site-select"]').attributes("disabled")).toBeDefined();

      if (action === "exit") {
        await wrapper.get('[data-testid="mode-frame-exit"]').trigger("click");
      } else {
        wrapper.unmount();
      }
      submission.resolve({ job_id: "late-job" });
      await flushPromises();

      expect(aiHubApi.streamJobLogs).not.toHaveBeenCalled();
    },
  );
});
