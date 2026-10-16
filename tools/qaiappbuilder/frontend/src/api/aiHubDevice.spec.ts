// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import {
  fetchAiHubConfig,
  fetchAiHubDevices,
  fetchAiHubModels,
  getJobStatus,
  saveAiHubConfig,
  streamJobLogs,
  submitCompileJob,
  submitProfileJob,
  uploadToAiHub,
} from "./aiHubDevice";
import { releaseAuthGate } from "./http";

beforeAll(() => {
  releaseAuthGate();
});

const fetchMock = vi.fn<typeof fetch>();
vi.stubGlobal("fetch", fetchMock);

afterEach(() => {
  fetchMock.mockReset();
});

describe("AI Hub Device API", () => {
  it("scopes every GET and POST operation to its captured site", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ has_token: false }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ models: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ devices: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "done", output: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ model_id: "m1", output: "ok" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ job_id: "j1" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ job_id: "j2" }), { status: 200 }));

    await fetchAiHubConfig("china");
    await fetchAiHubModels("china");
    await fetchAiHubDevices("china");
    await getJobStatus("china", "job/id");
    await saveAiHubConfig("china", "secret-token");
    await uploadToAiHub("china", { local_path: "/model.onnx" });
    await submitCompileJob("china", { model_name: "m1", device: "Device" });
    await submitProfileJob("china", { model_name: "m1", device: "Device" });

    expect(fetchMock.mock.calls.slice(0, 4).map(([input]) => String(input))).toEqual([
      "/api/aihub/config?site=china",
      "/api/aihub/models?site=china",
      "/api/aihub/devices?site=china",
      "/api/aihub/jobs/job%2Fid?site=china",
    ]);
    expect(fetchMock.mock.calls.slice(4).map(([, init]) => init?.body)).toEqual([
      JSON.stringify({ site: "china", api_token: "secret-token" }),
      JSON.stringify({ local_path: "/model.onnx", site: "china" }),
      JSON.stringify({ model_name: "m1", device: "Device", site: "china" }),
      JSON.stringify({ model_name: "m1", device: "Device", site: "china" }),
    ]);
  });


  it("distinguishes an upstream device error from a successful empty inventory", async () => {
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ devices: [] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        detail: "Could not load devices from AI Hub. Please try again.",
      }), { status: 502 }));

    await expect(fetchAiHubDevices("china")).resolves.toEqual([]);
    await expect(fetchAiHubDevices("china")).rejects.toThrow(
      "Could not load devices from AI Hub. Please try again.",
    );
  });
  it("closes a log stream once and dispatches named terminal events", () => {
    class FakeEventSource {
      static instance: FakeEventSource;
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onerror: (() => void) | null = null;
      readonly listeners: Record<string, (event: Event) => void> = {};
      readonly close = vi.fn();
      constructor(readonly url: string) {
        FakeEventSource.instance = this;
      }
      addEventListener(name: string, listener: EventListener): void {
        this.listeners[name] = listener;
      }
    }
    vi.stubGlobal("EventSource", FakeEventSource);
    const callbacks = { onLine: vi.fn(), onDone: vi.fn(), onError: vi.fn() };

    const stop = streamJobLogs("china", "job/id", callbacks);
    const source = FakeEventSource.instance;
    expect(source.url).toBe("/api/aihub/jobs/job%2Fid/logs?site=china");
    source.onmessage?.(new MessageEvent("message", { data: "working" }));
    source.listeners["done"]?.(new MessageEvent("done", { data: "done" }));
    source.onerror?.();
    stop();

    expect(callbacks.onLine).toHaveBeenCalledWith("working");
    expect(callbacks.onDone).toHaveBeenCalledOnce();
    expect(callbacks.onError).not.toHaveBeenCalled();
    expect(source.close).toHaveBeenCalledOnce();
  });
});
