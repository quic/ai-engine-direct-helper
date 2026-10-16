# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""AI Hub Device routes.

Exposes the Qualcomm AI Hub CLI operations as HTTP endpoints:

    GET  /api/aihub/config          — report whether a token is stored
    POST /api/aihub/configure       — save an API token
    GET  /api/aihub/devices         — list available target devices
    POST /api/aihub/jobs/compile    — submit a compile job (async)
    POST /api/aihub/jobs/profile    — submit a profile job (async)
    GET  /api/aihub/jobs/{job_id}/logs — SSE stream of job output

Long-running compile/profile jobs are executed as background subprocesses.
Their stdout/stderr is streamed line-by-line via SSE to the frontend.
"""

from __future__ import annotations

import asyncio
import configparser
import os
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, AsyncGenerator, Iterator
from apps.api._aihub_di import AiHubSite, AiHubSiteContext

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container
    from qai_hub.client import Client

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field



def _site_context(container: "Container", site: AiHubSite) -> AiHubSiteContext:
    return container.aihub.for_site(site)


def _hub_sdk(context: AiHubSiteContext) -> "Client":
    """Return a fresh client explicitly bound to one site."""
    from qai_hub.client import Client, ClientConfig

    metadata = context.metadata
    return Client(config=ClientConfig(api_url=metadata.api_url, web_url=metadata.web_url, api_token=context.config_manager.get_api_token() or "", verbose=False))


_AIHUB_AUTH_ERROR_MARKERS = (
    "http 401",
    "http 403",
    "status code 401",
    "status code 403",
    "401 unauthorized",
    "403 forbidden",
    "failed to authenticate",
    "authentication failed",
    "authorization failed",
    "bad or expired api token",
    "invalid api token",
    "api token is invalid",
    "missing api token",
    "no api token",
)


def _safe_aihub_list_error(resource: str, diagnostic: str) -> JSONResponse:
    """Map an upstream listing failure without exposing its raw diagnostic."""
    normalized = " ".join(diagnostic.casefold().split())
    authentication_failed = any(
        marker in normalized for marker in _AIHUB_AUTH_ERROR_MARKERS
    )
    if resource == "device":
        if authentication_failed:
            detail = "AI Hub authentication failed. Check the API token for the selected site."
            return JSONResponse({"detail": detail}, status_code=401)
        return JSONResponse(
            {"detail": "Could not load devices from AI Hub. Please try again."},
            status_code=502,
        )
    if authentication_failed:
        detail = "AI Hub model request failed: authentication required."
        return JSONResponse({"detail": detail}, status_code=401)
    detail = "AI Hub model request failed: upstream service unavailable."
    return JSONResponse({"detail": detail}, status_code=502)


def _list_models_response(context: AiHubSiteContext) -> JSONResponse:
    """List models without exposing SDK exception details."""
    try:
        models = _hub_sdk(context).get_models()
    except Exception as exc:
        return _safe_aihub_list_error("model", str(exc))
    return JSONResponse(
        {
            "models": [
                {
                    "model_id": model.model_id,
                    "name": getattr(model, "name", model.model_id),
                }
                for model in models
            ]
        }
    )


def _list_devices_response(context: AiHubSiteContext) -> JSONResponse:
    """List devices without exposing CLI stderr."""
    returncode, stdout, stderr = _run_hub_sync(context, ["list-devices"])
    if returncode != 0:
        return _safe_aihub_list_error("device", stderr)
    return JSONResponse({"devices": _parse_devices_text(stdout)})


# ─── Hub CLI helpers ──────────────────────────────────────────────────────────

@contextmanager
def _hub_env(context: AiHubSiteContext) -> Iterator[dict[str, str]]:
    """Yield a site-specific CLI environment with a private temporary INI."""
    parser = configparser.ConfigParser()
    parser["api"] = {
        "api_url": context.metadata.api_url,
        "web_url": context.metadata.web_url,
        "api_token": context.config_manager.get_api_token() or "",
        "verbose": "false",
    }
    fd, raw_path = tempfile.mkstemp(prefix=f"qai-hub-{context.config_manager.site.value}-", suffix=".ini")
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            parser.write(stream)
        env = os.environ.copy()
        env.pop("AIHUB_API_TOKEN", None)
        env.pop("AIHUB_CHINA_API_TOKEN", None)
        env["QAIHUB_CLIENT_INI"] = str(path)
        env["PYTHONUNBUFFERED"] = "1"
        yield env
    finally:
        path.unlink(missing_ok=True)


def _run_hub_sync(context: AiHubSiteContext, args: list[str]) -> tuple[int, str, str]:
    """Run a hub CLI command synchronously. Returns (returncode, stdout, stderr)."""
    try:
        with _hub_env(context) as env:
            result = subprocess.run(
                ["qai-hub"] + args,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return 1, "", "qai-hub CLI not found. Please install qai-hub: pip install qai-hub"
    except subprocess.TimeoutExpired:
        return 1, "", "qai-hub command timed out"
    except Exception as exc:
        return 1, "", str(exc)




def _run_compile_job_sync(context: AiHubSiteContext, job_id: str, args: list[str]) -> dict:
    """Run a compile job synchronously and return a result dict.

    This is for agent use where blocking is acceptable.
    Returns a dict with status, model_id, and output.
    """
    context.job_registry.create(job_id, {
        "status": "running",
        "output": [],
        "error": None,
        "model_id": None,
        "aihub_job_id": None,
    })

    cli_env = _hub_env(context)
    env = cli_env.__enter__()
    try:
        proc = subprocess.Popen(
            ["qai-hub"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )

        output_lines = []
        buffer = ""
        while True:
            char = proc.stdout.read(1)
            if not char:
                break

            if char in ('\r', '\n'):
                if buffer:
                    line = buffer.rstrip('\n')
                    output_lines.append(line)
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["output"].append(line)
                    buffer = ""
            else:
                buffer += char

        if buffer:
            line = buffer.rstrip('\n')
            output_lines.append(line)
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["output"].append(line)

        proc.wait()

        result = {
            "job_id": job_id,
            "status": "error" if proc.returncode != 0 else "done",
            "output": output_lines[-20:],
        }

        if proc.returncode == 0:
            full_output = "\n".join(output_lines)
            import re
            match = re.search(r"Scheduled compile job \((\w+)\)", full_output)
            if match:
                aihub_job_id = match.group(1)
                result["aihub_job_id"] = aihub_job_id

                try:
                    job = _hub_sdk(context).get_job(aihub_job_id)
                    model_id = job.model.model_id
                    result["model_id"] = model_id

                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["model_id"] = model_id
                        context.job_registry._jobs[job_id]["aihub_job_id"] = aihub_job_id
                        context.job_registry._jobs[job_id]["status"] = "done"
                except Exception as e:
                    result["error"] = f"Failed to get model ID: {str(e)}"
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["error"] = result["error"]
                        context.job_registry._jobs[job_id]["status"] = "done"
            else:
                with context.job_registry._lock:
                    context.job_registry._jobs[job_id]["status"] = "done"
        else:
            result["error"] = f"Compilation failed with exit code {proc.returncode}"
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "error"
                context.job_registry._jobs[job_id]["error"] = result["error"]

        return result

    except Exception as exc:
        result = {
            "job_id": job_id,
            "status": "error",
            "error": str(exc),
            "output": [],
        }
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["status"] = "error"
            context.job_registry._jobs[job_id]["error"] = str(exc)
        return result
    finally:
        cli_env.__exit__(None, None, None)
        context.job_registry.prune()


def _run_compile_job(context: AiHubSiteContext, job_id: str, args: list[str]) -> None:
    """Run a compile job and extract the model ID from the output."""
    context.job_registry.create(job_id, {
        "status": "running",
        "output": [],
        "error": None,
        "model_id": None,
        "aihub_job_id": None,
    })

    cli_env = _hub_env(context)
    env = cli_env.__enter__()
    try:
        proc = subprocess.Popen(
            ["qai-hub"] + args + ["--wait"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
        output_lines = []

        # Read output character by character to capture \r-based progress updates
        buffer = ""
        while True:
            char = proc.stdout.read(1)
            if not char:
                break

            if char == '\r':
                # Carriage return: this is a progress update line
                if buffer:
                    line = buffer.rstrip('\n')
                    output_lines.append(line)
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["output"].append(line)
                    buffer = ""
            elif char == '\n':
                # Newline: regular line
                if buffer:
                    line = buffer.rstrip('\n')
                    output_lines.append(line)
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["output"].append(line)
                    buffer = ""
            else:
                buffer += char

        # Handle any remaining buffer
        if buffer:
            line = buffer.rstrip('\n')
            output_lines.append(line)
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["output"].append(line)

        proc.wait()
        if proc.returncode == 0:
            # Extract AI Hub Job ID from output
            full_output = "\n".join(output_lines)
            import re
            match = re.search(r"Scheduled compile job \((\w+)\)", full_output)
            if match:
                aihub_job_id = match.group(1)

                # Get model ID from the job using qai_hub API
                try:
                    job = _hub_sdk(context).get_job(aihub_job_id)
                    model_id = job.model.model_id

                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["status"] = "done"
                        context.job_registry._jobs[job_id]["model_id"] = model_id
                        context.job_registry._jobs[job_id]["aihub_job_id"] = aihub_job_id
                except Exception as e:
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["status"] = "done"
                        context.job_registry._jobs[job_id]["error"] = f"Failed to get model ID: {str(e)}"
            else:
                with context.job_registry._lock:
                    context.job_registry._jobs[job_id]["status"] = "done"
        else:
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "error"
                context.job_registry._jobs[job_id]["error"] = f"qai-hub exited with code {proc.returncode}"

    except FileNotFoundError:
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["status"] = "error"
            context.job_registry._jobs[job_id]["output"].append(
                "qai-hub CLI not found. Please install: pip install qai-hub"
            )
            context.job_registry._jobs[job_id]["error"] = "qai-hub CLI not found"
    except Exception as exc:
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["status"] = "error"
            context.job_registry._jobs[job_id]["output"].append(str(exc))
            context.job_registry._jobs[job_id]["error"] = str(exc)
    finally:
        cli_env.__exit__(None, None, None)
        context.job_registry.prune()


def _run_profile_job_sdk(context: AiHubSiteContext, job_id: str, model_id: str, device_name: str) -> None:
    """Run a profile job using pure SDK (no CLI).

    This runs in a background thread and updates container.aihub.job_registry._jobs[job_id] with progress.
    """
    import time

    try:
        client = _hub_sdk(context)
        models = client.get_models()
        model_obj = next((m for m in models if m.model_id == model_id), None)

        if not model_obj:
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "error"
                context.job_registry._jobs[job_id]["error"] = f"Model {model_id} not found on AI Hub"
            return

        # Get Device object
        devices = client.get_devices()
        device_obj = next((d for d in devices if d.name == device_name), None)

        if not device_obj:
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "error"
                context.job_registry._jobs[job_id]["error"] = f"Device {device_name} not found"
            return

        # Submit job
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["output"].append("Submitting profile job...")

        job = client.submit_profile_job(model=model_obj, device=device_obj)
        aihub_job_id = job.job_id

        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["aihub_job_id"] = aihub_job_id
            context.job_registry._jobs[job_id]["output"].append(f"Job submitted: {aihub_job_id}")

        # Poll status
        last_code = None
        while True:
            status = job.get_status()
            code = status.code if hasattr(status, 'code') else str(status)

            # Update aihub_status field for API consumers
            if code != last_code:
                with context.job_registry._lock:
                    context.job_registry._jobs[job_id]["aihub_status"] = code
                    context.job_registry._jobs[job_id]["output"].append(f"Status: {code}")
                last_code = code

            # Check completion
            if hasattr(status, 'code'):
                if status.code == 'SUCCESS':
                    break
                elif status.code in ['FAILED', 'CANCELED']:
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["status"] = "error"
                        context.job_registry._jobs[job_id]["aihub_status"] = status.code
                        context.job_registry._jobs[job_id]["error"] = f"Job {status.code}"
                        context.job_registry._jobs[job_id]["output"].append(f"Job failed: {status.code}")
                    return

            time.sleep(10)  # Poll every 10 seconds

        # Get results
        profile = job.download_profile()

        # Extract metrics
        exec_summary = profile.get("execution_summary", {})
        latency_ns = exec_summary.get("estimated_inference_time", 0)
        latency_ms = latency_ns / 1_000_000 if latency_ns else 0
        throughput_fps = 1000 / latency_ms if latency_ms > 0 else 0

        # Store results
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["status"] = "done"
            context.job_registry._jobs[job_id]["profile_result"] = {
                "latency_ms": round(latency_ms, 2),
                "throughput_fps": round(throughput_fps, 2),
            }
            context.job_registry._jobs[job_id]["output"].append(
                f"Completed: latency={latency_ms:.2f}ms, throughput={throughput_fps:.1f} FPS"
            )

    except Exception as e:
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["status"] = "error"
            context.job_registry._jobs[job_id]["error"] = str(e)
    finally:
        context.job_registry.prune()


def _run_compile_and_profile_job_sdk(context: AiHubSiteContext, job_id: str, model_id: str, device_name: str) -> None:
    """Run compile + profile job using SDK.

    This runs in a background thread and updates container.aihub.job_registry._jobs[job_id] with progress.
    Combines compilation and profiling in a single job.
    """
    import time

    try:
        client = _hub_sdk(context)
        models = client.get_models()
        model_obj = next((m for m in models if m.model_id == model_id), None)

        if not model_obj:
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "error"
                context.job_registry._jobs[job_id]["error"] = f"Model {model_id} not found on AI Hub"
            return

        # Get Device object
        devices = client.get_devices()
        device_obj = next((d for d in devices if d.name == device_name), None)

        if not device_obj:
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "error"
                context.job_registry._jobs[job_id]["error"] = f"Device {device_name} not found"
            return

        # Submit compile + profile job
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["output"].append("Submitting compile and profile job...")

        compile_job, profile_job = client.submit_compile_and_profile_jobs(
            model=model_obj,
            device=device_obj,
        )

        compile_aihub_id = compile_job.job_id
        profile_aihub_id = profile_job.job_id if profile_job else None

        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["compile_job_id"] = compile_aihub_id
            if profile_aihub_id:
                context.job_registry._jobs[job_id]["profile_job_id"] = profile_aihub_id
            context.job_registry._jobs[job_id]["output"].append(f"Compile job: {compile_aihub_id}")
            if profile_aihub_id:
                context.job_registry._jobs[job_id]["output"].append(f"Profile job: {profile_aihub_id}")

        # Poll compile job status
        last_compile_status = None
        while True:
            status = compile_job.get_status()
            code = status.code if hasattr(status, 'code') else str(status)

            if code != last_compile_status:
                with context.job_registry._lock:
                    context.job_registry._jobs[job_id]["compile_status"] = code
                    context.job_registry._jobs[job_id]["output"].append(f"Compile status: {code}")
                last_compile_status = code

            # Check compile completion
            if hasattr(status, 'code'):
                if status.code == 'SUCCESS':
                    break
                elif status.code in ['FAILED', 'CANCELED']:
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["status"] = "error"
                        context.job_registry._jobs[job_id]["error"] = f"Compile job {status.code}"
                        context.job_registry._jobs[job_id]["output"].append(f"Compile failed: {status.code}")
                    return

            time.sleep(10)

        # Compile succeeded, store result
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["output"].append("Compilation completed successfully")

        # Wait for profile job if present
        if profile_job:
            last_profile_status = None
            while True:
                status = profile_job.get_status()
                code = status.code if hasattr(status, 'code') else str(status)

                if code != last_profile_status:
                    with context.job_registry._lock:
                        context.job_registry._jobs[job_id]["profile_status"] = code
                        context.job_registry._jobs[job_id]["output"].append(f"Profile status: {code}")
                    last_profile_status = code

                # Check profile completion
                if hasattr(status, 'code'):
                    if status.code == 'SUCCESS':
                        break
                    elif status.code in ['FAILED', 'CANCELED']:
                        with context.job_registry._lock:
                            context.job_registry._jobs[job_id]["status"] = "error"
                            context.job_registry._jobs[job_id]["error"] = f"Profile job {status.code}"
                            context.job_registry._jobs[job_id]["output"].append(f"Profile failed: {status.code}")
                        return

                time.sleep(10)

            # Get profile results
            profile = profile_job.download_profile()

            # Extract metrics
            exec_summary = profile.get("execution_summary", {})
            latency_ns = exec_summary.get("estimated_inference_time", 0)
            latency_ms = latency_ns / 1_000_000 if latency_ns else 0
            throughput_fps = 1000 / latency_ms if latency_ms > 0 else 0

            # Store results
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "done"
                context.job_registry._jobs[job_id]["profile_result"] = {
                    "latency_ms": round(latency_ms, 2),
                    "throughput_fps": round(throughput_fps, 2),
                }
                context.job_registry._jobs[job_id]["output"].append(
                    f"Profile completed: latency={latency_ms:.2f}ms, throughput={throughput_fps:.1f} FPS"
                )
        else:
            # No profile job, just compilation
            with context.job_registry._lock:
                context.job_registry._jobs[job_id]["status"] = "done"
                context.job_registry._jobs[job_id]["output"].append("Job completed (compile only)")

    except Exception as e:
        with context.job_registry._lock:
            context.job_registry._jobs[job_id]["status"] = "error"
            context.job_registry._jobs[job_id]["error"] = str(e)
            context.job_registry._jobs[job_id]["output"].append(f"Error: {str(e)}")
    finally:
        context.job_registry.prune()

def _onnx_has_confined_external_data(model_path: Path, allowed_root: Path) -> bool:
    """Inspect all external-data locations before any external bytes are loaded."""
    import onnx
    from onnx.external_data_helper import _get_all_tensors, uses_external_data

    metadata = onnx.load(str(model_path), load_external_data=False)
    found = False
    for tensor in _get_all_tensors(metadata):
        if not uses_external_data(tensor):
            continue
        found = True
        locations = [item.value for item in tensor.external_data if item.key == "location"]
        if len(locations) != 1 or not locations[0]:
            raise ValueError(f"ONNX tensor {tensor.name!r} has an invalid external-data location")
        location = locations[0]
        relative = Path(location)
        if relative.is_absolute() or PureWindowsPath(location).is_absolute():
            raise ValueError("ONNX external-data locations must be relative paths")
        if any(part == ".." for part in relative.parts):
            raise ValueError("ONNX external-data locations must not contain traversal")
        resolved = (model_path.parent / relative).resolve()
        try:
            resolved.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError("ONNX external data escapes the allowed data directory") from exc
    return found


# ─── Pydantic models ──────────────────────────────────────────────────────────

class ConfigureRequest(BaseModel):
    api_token: str = Field(min_length=1)
    site: AiHubSite = AiHubSite.GLOBAL


class JobRequest(BaseModel):
    model_name: str
    device: str
    site: AiHubSite = AiHubSite.GLOBAL


class UploadModelRequest(BaseModel):
    local_path: str
    name: str | None = None
    project: str | None = None
    site: AiHubSite = AiHubSite.GLOBAL
# ─── Router factory ───────────────────────────────────────────────────────────

def build_router(*, container: "Container") -> APIRouter:
    """Build and return the AI Hub Device router bound to the given DI container.

    No module-level state is captured; all state is managed through
    ``container.aihub`` services namespace.
    """
    router = APIRouter(prefix="/api/aihub", tags=["aihub"])

    @router.get("/config")
    async def get_config(site: AiHubSite = AiHubSite.GLOBAL) -> JSONResponse:
        """Return token presence without exposing any token-derived characters."""
        context = _site_context(container, site)
        return JSONResponse({"has_token": bool(context.config_manager.get_api_token())})

    @router.post("/configure")
    async def configure(req: ConfigureRequest) -> JSONResponse:
        context = _site_context(container, req.site)
        token = req.api_token.strip()
        if not token:
            return JSONResponse({"error": "API token must not be blank"}, status_code=422)
        config = context.config_manager.load()
        config["api_token"] = token
        context.config_manager.save(config)
        return JSONResponse({"ok": True})


    @router.get("/models")
    async def list_models(site: AiHubSite = AiHubSite.GLOBAL) -> JSONResponse:
        return _list_models_response(_site_context(container, site))

    @router.post("/upload")
    async def upload_to_aihub(req: UploadModelRequest) -> JSONResponse:
        """Upload a confined local model to the selected site."""
        context = _site_context(container, req.site)
        import re

        # Security: confine uploads to the application data directory only.
        # This prevents an authenticated user from uploading arbitrary server
        # files (e.g. /etc/passwd, private keys) to AI Hub.
        model_path = Path(req.local_path).resolve()
        allowed_root = container.data_paths.root.resolve()
        try:
            model_path.relative_to(allowed_root)
        except ValueError:
            return JSONResponse(
                {
                    "error": (
                        f"Path '{req.local_path}' is not within the allowed "
                        f"data directory. Upload the file via POST /api/uploads/model first."
                    )
                },
                status_code=400,
            )

        # Verify the resolved upload is a regular file, never a directory.
        if not model_path.is_file():
            return JSONResponse(
                {"error": f"Local file not found: {req.local_path}"},
                status_code=400,
            )

        if model_path.suffix.lower() == ".onnx":
            try:
                has_external_data = _onnx_has_confined_external_data(model_path, allowed_root)
            except ImportError:
                return JSONResponse(
                    {"error": "The 'onnx' package is required to inspect ONNX external data"},
                    status_code=400,
                )
            except (OSError, ValueError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

            if has_external_data:
                import onnx

                embedded_path = model_path.with_name(f".{model_path.stem}-{uuid.uuid4().hex}.onnx")
                try:
                    model = onnx.load(str(model_path))
                    onnx.save(model, str(embedded_path), save_as_external_data=False)
                    args = ["upload-model", "--model", str(embedded_path)]
                    if req.name:
                        args.extend(["--name", req.name])
                    if req.project:
                        args.extend(["--project", req.project])
                    returncode, stdout, stderr = _run_hub_sync(context, args)
                finally:
                    embedded_path.unlink(missing_ok=True)

                if returncode != 0:
                    return JSONResponse(
                        {"error": stderr or "Upload failed", "output": stdout, "note": "External data was embedded"},
                        status_code=500,
                    )
                match = re.search(r"model_id\s*:\s*(\S+)", stdout)
                if not match:
                    return JSONResponse(
                        {"error": "Failed to parse model_id from output", "output": stdout, "note": "External data was embedded"},
                        status_code=500,
                    )
                return JSONResponse({
                    "model_id": match.group(1),
                    "name": req.name,
                    "output": stdout,
                    "note": "External data was automatically embedded before upload",
                })
        # Build upload command for normal case (no external data)
        args = ["upload-model", "--model", str(model_path)]
        if req.name:
            args.extend(["--name", req.name])
        if req.project:
            args.extend(["--project", req.project])
        # Run upload synchronously
        returncode, stdout, stderr = _run_hub_sync(context, args)

        if returncode != 0:
            return JSONResponse(
                {"error": stderr or "Upload failed", "output": stdout},
                status_code=500,
            )

        # Parse model_id from output
        match = re.search(r"model_id\s*:\s*(\S+)", stdout)
        if not match:
            return JSONResponse(
                {"error": "Failed to parse model_id from output", "output": stdout},
                status_code=500,
            )

        model_id = match.group(1)

        return JSONResponse({
            "model_id": model_id,
            "name": req.name,
            "output": stdout,
        })


    @router.get("/jobs/{job_id}")
    async def get_job_status(job_id: str, site: AiHubSite = AiHubSite.GLOBAL) -> JSONResponse:
        """Get status from the selected site's registry."""
        context = _site_context(container, site)
        job = context.job_registry.get(job_id)

        if job is None:
            return JSONResponse(
                {"error": "Job not found"},
                status_code=404,
            )

        # Build response with all available information
        response = {
            "job_id": job_id,
            "status": job["status"],  # "running" | "done" | "error"
            "output": job.get("output", []),  # Execution logs
        }

        # Include error if present
        if job.get("error"):
            response["error"] = job["error"]

        # Include AI Hub job ID and status if available (for single jobs)
        if job.get("aihub_job_id"):
            response["aihub_job_id"] = job["aihub_job_id"]
        if job.get("aihub_status"):
            response["aihub_status"] = job["aihub_status"]

        # Include compile results (for compile jobs)
        if job.get("model_id"):
            response["model_id"] = job["model_id"]

        # Include profile results (for profile jobs)
        if job.get("profile_result"):
            response["profile_result"] = job["profile_result"]

        # Include compile-and-profile job information
        if job.get("compile_job_id"):
            response["compile_job_id"] = job["compile_job_id"]
        if job.get("profile_job_id"):
            response["profile_job_id"] = job["profile_job_id"]
        if job.get("compile_status"):
            response["compile_status"] = job["compile_status"]
        if job.get("profile_status"):
            response["profile_status"] = job["profile_status"]

        return JSONResponse(response)
    @router.post("/jobs/profile")
    async def submit_profile(req: JobRequest) -> JSONResponse:
        """Submit a model profiling job using the selected site."""
        context = _site_context(container, req.site)
        job_id = str(uuid.uuid4())
        context.job_registry.create(job_id, {
            "status": "running",
            "output": [],
            "error": None,
            "aihub_job_id": None,
            "profile_result": None,
        })

        # Run in background thread
        thread = threading.Thread(
            target=_run_profile_job_sdk,
            args=(context, job_id, req.model_name, req.device),
            daemon=True,
        )
        thread.start()

        return JSONResponse({
            "job_id": job_id,
            "status": "running",
        })

    @router.post("/jobs/compile-and-profile")
    async def submit_compile_and_profile(req: JobRequest) -> JSONResponse:
        """Submit a combined compile and profile job to the selected site."""
        context = _site_context(container, req.site)
        job_id = str(uuid.uuid4())
        context.job_registry.create(job_id, {
            "status": "running",
            "output": [],
            "error": None,
            "compile_job_id": None,
            "profile_job_id": None,
            "compile_status": None,
            "profile_status": None,
            "profile_result": None,
        })

        # Run in background thread
        thread = threading.Thread(
            target=_run_compile_and_profile_job_sdk,
            args=(context, job_id, req.model_name, req.device),
            daemon=True,
        )
        thread.start()

        return JSONResponse({
            "job_id": job_id,
            "status": "running",
        })


    @router.post("/compile")
    async def compile_sync(req: JobRequest) -> JSONResponse:
        """Synchronously compile a model on the selected site."""
        context = _site_context(container, req.site)
        job_id = str(uuid.uuid4())
        args = ["submit-compile-job", "--model", req.model_name, "--device", req.device, "--wait"]

        # Run synchronously (not in background thread)
        result = _run_compile_job_sync(context, job_id, args)
        return JSONResponse(result)
    @router.get("/devices")
    async def list_devices(site: AiHubSite = AiHubSite.GLOBAL) -> JSONResponse:
        return _list_devices_response(_site_context(container, site))


    @router.post("/jobs/compile")
    async def submit_compile(req: JobRequest) -> JSONResponse:
        """Submit a model compilation job to the selected site."""
        context = _site_context(container, req.site)
        job_id = str(uuid.uuid4())
        args = ["submit-compile-job", "--model", req.model_name, "--device", req.device]
        context.job_registry.create(job_id)
        thread = threading.Thread(
            target=_run_compile_job,
            args=(context, job_id, args),
            daemon=True,
        )
        thread.start()
        return JSONResponse({"job_id": job_id})

    @router.get("/jobs/{job_id}/logs")
    async def stream_job_logs(job_id: str, site: AiHubSite = AiHubSite.GLOBAL):
        context = _site_context(container, site)
        if context.job_registry.get(job_id) is None:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return StreamingResponse(
            _sse_generator(context, job_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_devices_text(text: str) -> list[dict]:
    """Parse table-format qai-hub list-devices output into device dicts."""
    devices = []
    lines = text.splitlines()

    # Skip header and separator lines (lines starting with + or |)
    for line in lines:
        line = line.strip()
        # Skip empty lines, table borders, and headers
        if not line or line.startswith("+") or line.startswith("|") and "Device" in line:
            continue

        # Parse table row: | Device | OS | Vendor | Type | Chipset | CLI Invocation |
        if line.startswith("|") and "|" in line[1:]:
            parts = [p.strip() for p in line.split("|")]
            # parts[0] is empty (before first |), parts[1] is Device, parts[2] is OS, etc.
            if len(parts) >= 7:
                device_name = parts[1]
                device_os = parts[2]
                chipset = parts[5]

                if device_name:  # Only add if device name is not empty
                    devices.append({
                        "name": device_name,
                        "os": device_os,
                        "chipset": chipset,
                    })

    return devices


async def _sse_generator(context: AiHubSiteContext, job_id: str) -> AsyncGenerator[str, None]:
    """Yield output from one site's job registry."""
    sent_lines = 0
    poll_interval = 0.3  # seconds
    while True:
        job = context.job_registry.get(job_id)

        if job is None:
            yield "event: error_event\ndata: Job not found\n\n"
            return

        # Send any new lines
        lines = job["output"]
        while sent_lines < len(lines):
            line = lines[sent_lines]
            # One ``data:`` field per physical line preserves valid SSE framing.
            payload = str(line).replace("\r", "").replace("\n", "\ndata: ")
            yield f"data: {payload}\n\n"
            sent_lines += 1

        status = job["status"]
        if status == "done":
            yield "event: done\ndata: done\n\n"
            return
        elif status == "error":
            error_msg = str(job.get("error", "Unknown error"))
            payload = error_msg.replace("\r", "").replace("\n", "\ndata: ")
            yield f"event: error_event\ndata: {payload}\n\n"
            return

        await asyncio.sleep(poll_interval)