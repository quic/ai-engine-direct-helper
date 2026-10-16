// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

/**
 * AI Hub Device API client.
 *
 * Covers the routes exposed by the backend `aihub` module:
 *
 *   GET  /api/aihub/config          → { has_token: boolean }
 *   POST /api/aihub/configure       → { ok: true }
 *   GET  /api/aihub/models          → AiHubModel[]
 *   GET  /api/aihub/devices         → AiHubDevice[]
 *   POST /api/aihub/jobs/compile    → { job_id: string }
 *   POST /api/aihub/jobs/profile    → { job_id: string }
 *   GET  /api/aihub/jobs/{id}/logs  → SSE stream (text/event-stream)
 *
 * Long-running compile/profile jobs stream their output via SSE.
 * `streamJobLogs` returns a stop function; call it to abort the stream.
 */

import { ApiError } from "./errors";
import { apiJson, buildApiUrl } from "./http";

// ─── Types ────────────────────────────────────────────────────────────────────
export type AiHubSite = "global" | "china";


/** A model from AI Hub. */
export interface AiHubModel {
  model_id: string;
  name: string;
}

/** A single AI Hub target device. */
export interface AiHubDevice {
  /** Device display name (e.g. "Samsung Galaxy S24 (Snapdragon 8 Gen 3)"). */
  name: string;
  /** Operating system (e.g. "Android"). */
  os?: string | null;
  /** Chipset identifier (e.g. "Snapdragon 8 Gen 3"). */
  chipset?: string | null;
}

/** Presence-only response from GET /api/aihub/config. */
export interface AiHubConfig {
  has_token: boolean;
}

/** Request body for POST /api/aihub/jobs/compile or /profile. */
export interface AiHubJobRequest {
  /** Model name (AI Hub model id) or local file path. */
  model_name: string;
  /** Target device name. */
  device: string;
}

/** Response from POST /api/aihub/jobs/compile or /profile. */
export interface AiHubJobResult {
  /** Unique job identifier for polling logs. */
  job_id: string;
}

/** Request body for POST /api/aihub/upload. */
export interface AiHubUploadRequest {
  local_path: string;
  name?: string;
  project?: string;
}

/** Response from POST /api/aihub/upload. */
export interface AiHubUploadResult {
  model_id: string;
  name?: string;
  output: string;
}

/** Profile result returned from a successful profile job. */
export interface AiHubProfileResult {
  latency_ms: number;  // Inference latency in milliseconds
  throughput_fps: number;  // Throughput in frames per second (batch=1)
}

/** Response from POST /api/aihub/compile (synchronous). */
export interface AiHubCompileResult {
  job_id: string;
  status: "running" | "done" | "error";
  model_id?: string;
  aihub_job_id?: string;
  error?: string;
  output: string[];
}

/** Response from GET /api/aihub/jobs/{id}. */
export interface AiHubJobStatus {
  job_id: string;
  status: "running" | "done" | "error";
  model_id?: string;
  aihub_job_id?: string;
  profile_result?: AiHubProfileResult;
  error?: string;
  output: string[];
}

export interface JobLogCallbacks {
  /** Called for each line of output. */
  onLine: (line: string) => void;
  /** Called when the job completes successfully. */
  onDone: () => void;
  /** Called when the job fails or the stream errors. */
  onError: (message: string) => void;
}
// ─── API functions ────────────────────────────────────────────────────────────

/**
 * Fetch the current AI Hub client configuration (API token).
 * `GET /api/aihub/config`
 */
export async function fetchAiHubConfig(site: AiHubSite): Promise<AiHubConfig> {
  return apiJson<AiHubConfig>("GET", "/api/aihub/config", undefined, { query: { site } });
}

/**
 * Save the AI Hub API token.
 * `POST /api/aihub/configure`
 */
export async function saveAiHubConfig(site: AiHubSite, apiToken: string): Promise<void> {
  await apiJson<void>("POST", "/api/aihub/configure", { site, api_token: apiToken });
}

/**
 * List available AI Hub models.
 * `GET /api/aihub/models`
 */
export async function fetchAiHubModels(site: AiHubSite): Promise<AiHubModel[]> {
  const result = await apiJson<{ models: AiHubModel[] }>("GET", "/api/aihub/models", undefined, { query: { site } });
  return result.models ?? [];
}

/**
 * Submit a synchronous compilation job (blocks until completion).
 * `POST /api/aihub/compile`
 *
 * Use this for agent/script workflows where blocking is acceptable.
 * Returns the compiled model_id on success.
 */
export async function compileSync(site: AiHubSite, req: AiHubJobRequest): Promise<AiHubCompileResult> {
  return apiJson<AiHubCompileResult>("POST", "/api/aihub/compile", { ...req, site });
}

/**
 * Get the status and result of a compile or profile job.
 * `GET /api/aihub/jobs/{jobId}`
 *
 * Use this to poll for completion after submitting an async job.
 */
export async function getJobStatus(site: AiHubSite, jobId: string): Promise<AiHubJobStatus> {
  return apiJson<AiHubJobStatus>("GET", `/api/aihub/jobs/${encodeURIComponent(jobId)}`, undefined, { query: { site } });
}

/**
 * List available AI Hub target devices.
 * `GET /api/aihub/devices`
 */
export async function fetchAiHubDevices(site: AiHubSite): Promise<AiHubDevice[]> {
  try {
    const result = await apiJson<{ devices: AiHubDevice[] }>("GET", "/api/aihub/devices", undefined, { query: { site } });
    return result.devices ?? [];
  } catch (error) {
    if (error instanceof ApiError) {
      const body = error.details?.body;
      if (typeof body === "object" && body !== null && "detail" in body) {
        const detail = body.detail;
        if (typeof detail === "string") throw new Error(detail);
      }
    }
    throw error;
  }
}

/**
 * Upload a local model to AI Hub and get a model_id.
 * `POST /api/aihub/upload`
 *
 * Use this after uploading a file via POST /api/uploads/model.
 * Returns an AI Hub model_id that can be used for compilation.
 */
export async function uploadToAiHub(site: AiHubSite, req: AiHubUploadRequest): Promise<AiHubUploadResult> {
  return apiJson<AiHubUploadResult>("POST", "/api/aihub/upload", { ...req, site });
}

/**
 * Submit a model compilation job.
 * `POST /api/aihub/jobs/compile`
 */
export async function submitCompileJob(site: AiHubSite, req: AiHubJobRequest): Promise<AiHubJobResult> {
  return apiJson<AiHubJobResult>("POST", "/api/aihub/jobs/compile", { ...req, site });
}

/**
 * Submit a model profiling job.
 * `POST /api/aihub/jobs/profile`
 */
export async function submitProfileJob(site: AiHubSite, req: AiHubJobRequest): Promise<AiHubJobResult> {
  return apiJson<AiHubJobResult>("POST", "/api/aihub/jobs/profile", { ...req, site });
}

/**
 * Stream the logs of a running AI Hub job via SSE.
 * `GET /api/aihub/jobs/{jobId}/logs`
 *
 * Returns a stop function. Call it to abort the stream.
 * The stream emits:
 *   - `data: <line>` for each output line
 *   - `event: done` when the job completes
 *   - `event: error_event` with `data: <message>` on failure
 */
export function streamJobLogs(site: AiHubSite, jobId: string, callbacks: JobLogCallbacks): () => void {
  const url = buildApiUrl(
    `/api/aihub/jobs/${encodeURIComponent(jobId)}/logs`,
    { site },
  );
  const es = new EventSource(url);
  let settled = false;

  const close = (): void => {
    if (settled) return;
    settled = true;
    es.close();
  };

  es.onmessage = (ev) => {
    if (!settled && ev.data !== "") callbacks.onLine(ev.data);
  };

  es.addEventListener("done", () => {
    if (settled) return;
    close();
    callbacks.onDone();
  });

  es.addEventListener("error_event", (ev) => {
    if (settled) return;
    const message = ev instanceof MessageEvent ? ev.data : "";
    close();
    callbacks.onError(typeof message === "string" && message !== "" ? message : "Unknown error");
  });

  es.onerror = () => {
    if (settled) return;
    close();
    callbacks.onError("Stream connection error");
  };

  return close;
}