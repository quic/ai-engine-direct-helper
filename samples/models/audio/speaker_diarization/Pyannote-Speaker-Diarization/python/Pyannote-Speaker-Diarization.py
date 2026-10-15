# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#
# Pyannote-Speaker-Diarization (pyannote/speaker-diarization-3.1) on
# Windows on Snapdragon (WoS) -- the two neural networks run on the HTP (NPU)
# through qai_appbuilder (QNN):
#
#   segmentation.bin : waveforms (32, 1, 80000) -> powerset logits (32, 293, 7)
#   embedding.bin    : fbank     (32, 498, 80)  -> speaker embedding (32, 256)
#
# Everything else (chunking, fbank, powerset decoding, speaker counting,
# agglomerative clustering, reconstruction, binarization) is re-implemented
# here with numpy / scipy only, following
#   qai_hub_models/models/pyannote_speaker_diarization/{app,model,dataset}.py
#   (v0.64.0) and pyannote.audio 3.3.2.
# Neither `pyannote.audio` nor `torchaudio` (no ARM64-Windows wheel) nor
# `einops` / HuggingFace access is required.
#
# Models: pre-compiled HTP context binaries (QAIRT 2.50) published by
# AI Hub, downloaded with install.download_url() on first run and copied to
# ../models/:
#   Snapdragon X Elite  (8380) -> HTP v73
#   Snapdragon X2 Elite (8480) -> HTP v81
#
# The QNN/HTP runtime libraries are the ones bundled in the qai_appbuilder wheel;
# no SDK lookup or library copying is done here.
#
# CLI options:
#   --input_audio_path <path>   Input audio (default: 30 s AMI demo clip, downloaded)
#   --chipset <name>            Override detected device: x_elite | x2_elite
#   --num_speakers N            Exact number of speakers
#   --min_speakers N            Minimum number of speakers
#   --max_speakers N            Maximum number of speakers
#   --output_rttm <path>        Also write the result as an RTTM file
# ---------------------------------------------------------------------

import sys
import os
import time
import json
import math
import shutil
import zipfile
import argparse
import platform
import itertools
from pathlib import Path

sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "shared", "python"))

import install

import numpy as np
import soundfile as sf
import soxr
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import cdist

from qai_appbuilder import (
    QNNContext,
    Runtime,
    LogLevel,
    ProfilingLevel,
    PerfProfile,
    QNNConfig,
)

# ─────────────────────────────────────────────────────────────────────────────
# Model metadata
# ─────────────────────────────────────────────────────────────────────────────

MODEL_NAME = "Pyannote-Speaker-Diarization"
MODEL_ID = "pyannote_speaker_diarization"

_URL_BASE = (
    "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
    "qai-hub-models/models/pyannote_speaker_diarization/releases/v0.64.0/"
)
MODEL_URLS = {
    # Snapdragon X Elite (8380)
    "snapdragon_x_elite": _URL_BASE + "pyannote_speaker_diarization-voice_ai-mixed_with_float-qualcomm_snapdragon_x_elite.zip",
    # Snapdragon X2 Elite (8480)
    "snapdragon_x2_elite": _URL_BASE + "pyannote_speaker_diarization-voice_ai-mixed_with_float-qualcomm_snapdragon_x2_elite.zip",
}
CHIPSET_ALIASES = {
    "x_elite": "snapdragon_x_elite", "snapdragon_x_elite": "snapdragon_x_elite", "8380": "snapdragon_x_elite",
    "x2_elite": "snapdragon_x2_elite", "snapdragon_x2_elite": "snapdragon_x2_elite", "8480": "snapdragon_x2_elite",
}
MODEL_FILES = ("segmentation.bin", "embedding.bin", "metadata.json")

# AMI demo audio (same clip as the AI Hub demo: IS1009a, 55 s - 85 s)
AMI_WAV_URL = ("https://qaihub-public-assets.s3.us-west-2.amazonaws.com/"
               "qai-hub-models/datasets/ami_diarization/v1/IS1009a.Mix-Headset.wav")

# Pipeline constants (qai_hub_models ... model.py / pyannote speaker-diarization-3.1 config)
SAMPLE_RATE = 16000
SEG_DURATION = 5.0
SEG_STEP = SEG_DURATION * 0.2                     # SEG_STEP_RATIO = 0.2 -> 1 s
SEG_WINDOW = round(SEG_DURATION * SAMPLE_RATE)    # 80000
SEG_STEP_SAMPLES = round(SEG_STEP * SAMPLE_RATE)  # 16000
BATCH_SIZE = 32                                   # fixed batch of both compiled models
SEG_NUM_FRAMES = 293
NUM_LOCAL_SPEAKERS = 3
POWERSET_MAX_SET = 2
NUM_MELS = 80
EMB_NUM_FRAMES = 498
EMB_DIM = 256
# min #samples the WeSpeaker ResNet34 can embed = one 25 ms fbank window (400 samples)
EMB_MIN_NUM_SAMPLES = 400

# Receptive field of the segmentation-3.0 SincNet (stride 10, k251 -> pool3 -> k5 -> pool3 -> k5 -> pool3)
RF_DURATION = 991 / SAMPLE_RATE      # 0.0619375 s
RF_STEP = 270 / SAMPLE_RATE          # 0.016875 s

# Clustering hyper-parameters (pyannote/speaker-diarization-3.1 config.yaml)
CLUSTER_METHOD = "centroid"
CLUSTER_MIN_SIZE = 12
CLUSTER_THRESHOLD = 0.7045654963945799
MIN_DURATION_OFF = 0.0

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

execution_ws = Path(os.path.dirname(os.path.abspath(__file__)))
model_dir = execution_ws / ".." / "models"
assets_dir = execution_ws / ".." / "assets"
demo_wav_path = assets_dir / "demo_audio.wav"

# ─────────────────────────────────────────────────────────────────────────────
# Global state
# ─────────────────────────────────────────────────────────────────────────────

segmentation = None
embedding = None


class _Net(QNNContext):
    def Inference(self, input_data):
        return super().Inference([input_data])[0]


# ─────────────────────────────────────────────────────────────────────────────
# Platform / device
# ─────────────────────────────────────────────────────────────────────────────

def _is_wos():
    """True on a Windows-on-Snapdragon host.

    An x64 Python running under emulation reports platform.machine() == 'AMD64', so ask Windows for
    the *native* machine of the host (IsWow64Process2) and fall back to the environment variables.
    """
    if platform.system().lower() != "windows":
        return False
    if platform.machine().lower() in ("arm64", "aarch64"):
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ushort),
                                        ctypes.POINTER(ctypes.c_ushort)]
        k32.IsWow64Process2.restype = wintypes.BOOL
        process_machine, native_machine = ctypes.c_ushort(), ctypes.c_ushort()
        if k32.IsWow64Process2(k32.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)):
            return native_machine.value == 0xAA64            # IMAGE_FILE_MACHINE_ARM64
    except Exception:
        pass
    return "ARM" in (os.environ.get("PROCESSOR_IDENTIFIER", "") + os.environ.get("PROCESSOR_ARCHITEW6432", "")).upper()


def _device_model(chipset=None):
    if chipset:
        key = str(chipset).lower()
        if key not in CHIPSET_ALIASES:
            print(f"[ERROR] Unknown --chipset '{chipset}'. Use one of: x_elite, x2_elite.")
            sys.exit(1)
        return CHIPSET_ALIASES[key]
    return install.detect_device_model()


# ─────────────────────────────────────────────────────────────────────────────
# Model download (install.download_url)
# ─────────────────────────────────────────────────────────────────────────────

def _model_matches_device(device):
    """True if models/ contains all files and metadata.json belongs to `device`."""
    if not all((model_dir / f).is_file() for f in MODEL_FILES):
        return False
    try:
        meta = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
        name = meta.get("chipset_attributes", {}).get("name", "")
        return name.replace("-", "_").replace("qualcomm_", "") == device
    except Exception:
        return True   # cannot verify -> trust the files


def _download_models(device):
    """Download the model zip for `device`, unzip it and copy the files to ../models/."""
    model_dir.mkdir(parents=True, exist_ok=True)
    url = MODEL_URLS[device]
    zip_path = model_dir / os.path.basename(url)
    extract_dir = model_dir / "_extract_tmp"

    print(f"[INFO] Downloading {MODEL_NAME} models for {device}:\n  {url}")
    ok = install.download_url(url, str(zip_path))
    if not ok or not zip_path.is_file():
        print(f"[ERROR] Failed to download {url}")
        sys.exit(1)

    try:
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(extract_dir))
    except Exception as e:
        print(f"[ERROR] Failed to extract {zip_path}: {e}")
        zip_path.unlink(missing_ok=True)
        sys.exit(1)

    copied = set()
    for root, _dirs, files in os.walk(str(extract_dir)):
        for fname in files:
            if fname in MODEL_FILES:
                shutil.copy2(os.path.join(root, fname), str(model_dir / fname))
                copied.add(fname)
                print(f"[INFO] Copied {fname} -> {model_dir / fname}")

    shutil.rmtree(str(extract_dir), ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    missing = set(MODEL_FILES) - copied
    if missing:
        print(f"[ERROR] Files missing in the downloaded package: {sorted(missing)}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Init / Release
# ─────────────────────────────────────────────────────────────────────────────

def Init(chipset=None):
    """Download the models (if needed) and load segmentation + embedding on the HTP."""
    global segmentation, embedding

    if not _is_wos():
        print("[ERROR] The AI Hub context binaries of this model run on Windows on Snapdragon (HTP) only.")
        sys.exit(1)

    device = _device_model(chipset)
    print(f"[INFO] Device: {device}")

    if not _model_matches_device(device):
        _download_models(device)
    else:
        print(f"[INFO] Models found in {model_dir.resolve()}")

    print("[INFO] Runtime: HTP")
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    segmentation = _Net("segmentation", str(model_dir / "segmentation.bin"))
    embedding = _Net("embedding", str(model_dir / "embedding.bin"))

    if not segmentation.getInputShapes() or not embedding.getInputShapes():
        print("[ERROR] Failed to load the context binaries. They were built with QAIRT 2.50, so the\n"
              "        QNN libs bundled in qai_appbuilder must be >= 2.50:  pip install -U qai_appbuilder")
        sys.exit(1)


def Release():
    global segmentation, embedding
    del segmentation
    del embedding
    segmentation = None
    embedding = None


# ─────────────────────────────────────────────────────────────────────────────
# Audio helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_audiofile(path):
    """Read audio, downmix to mono and resample to 16 kHz -> float32 [num_samples]."""
    path = Path(path)
    if not path.is_file():
        print(f"[ERROR] Input audio not found: {path.resolve()}")
        sys.exit(1)
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)   # [T, C]
    if audio.shape[0] == 0:
        print(f"[ERROR] Input audio contains no samples (file size {path.stat().st_size} bytes): {path.resolve()}")
        sys.exit(1)
    audio = audio.mean(axis=1)                                         # downmix
    if sr != SAMPLE_RATE:
        audio = soxr.resample(audio, sr, SAMPLE_RATE).astype(np.float32)
    return np.ascontiguousarray(audio, dtype=np.float32)


def _valid_wav(path, min_seconds=1.0):
    try:
        info = sf.info(str(path))
        return info.frames >= min_seconds * info.samplerate
    except Exception:
        return False


def _ensure_demo_audio():
    """30 s clip (55 s - 85 s) of AMI IS1009a, the same as the AI Hub demo."""
    clip_seconds = 85 - 55
    if demo_wav_path.is_file():
        if _valid_wav(demo_wav_path, clip_seconds - 1):
            return str(demo_wav_path.resolve())
        print(f"[WARN] Demo audio is empty or truncated, regenerating: {demo_wav_path.resolve()}")
        demo_wav_path.unlink()

    assets_dir.mkdir(parents=True, exist_ok=True)
    full = assets_dir / "IS1009a.Mix-Headset.wav"
    # download_url() compares the local size with the server's and re-downloads incomplete files.
    print(f"[INFO] Fetching demo audio:\n  {AMI_WAV_URL}")
    if not install.download_url(AMI_WAV_URL, str(full)):
        sys.exit(1)

    audio = load_audiofile(full)
    clip = audio[55 * SAMPLE_RATE: 85 * SAMPLE_RATE]
    if clip.shape[0] < clip_seconds * SAMPLE_RATE:
        print(f"[ERROR] {full.resolve()} is only {audio.shape[0] / SAMPLE_RATE:.1f} s long (incomplete download?). "
              f"Delete it and run again.")
        sys.exit(1)
    sf.write(str(demo_wav_path), clip, SAMPLE_RATE, subtype="PCM_16")
    full.unlink(missing_ok=True)
    return str(demo_wav_path.resolve())


# ─────────────────────────────────────────────────────────────────────────────
# Kaldi fbank (replaces torchaudio.compliance.kaldi.fbank, WeSpeaker settings)
# ─────────────────────────────────────────────────────────────────────────────

_FBANK_EPS = float(np.finfo(np.float32).eps)
_FBANK_CACHE = {}


def _mel_scale(freq):
    return 1127.0 * np.log(1.0 + freq / 700.0)


def _mel_banks(num_bins=NUM_MELS, padded_window=512, sample_freq=SAMPLE_RATE, low_freq=20.0, high_freq=0.0):
    num_fft_bins = padded_window // 2
    nyquist = 0.5 * sample_freq
    if high_freq <= 0.0:
        high_freq += nyquist
    fft_bin_width = sample_freq / padded_window
    mel_low, mel_high = _mel_scale(low_freq), _mel_scale(high_freq)
    delta = (mel_high - mel_low) / (num_bins + 1)

    b = np.arange(num_bins, dtype=np.float64)[:, None]
    left = mel_low + b * delta
    center = mel_low + (b + 1.0) * delta
    right = mel_low + (b + 2.0) * delta
    mel = _mel_scale(fft_bin_width * np.arange(num_fft_bins, dtype=np.float64))[None, :]

    up = (mel - left) / (center - left)
    down = (right - mel) / (right - center)
    bins = np.maximum(0.0, np.minimum(up, down))
    bins = np.pad(bins, ((0, 0), (0, 1)))               # nyquist column = 0
    return bins.astype(np.float32)                      # [num_bins, padded_window/2 + 1]


def kaldi_fbank(waveform):
    """waveform: float32 [N] (already scaled by 2**15) -> log-mel fbank [frames, 80].

    Same settings as pyannote's WeSpeaker ResNet: 25 ms hamming window, 10 ms shift,
    dither 0, snip_edges, pre-emphasis 0.97, remove DC offset, 512-pt FFT, 20 Hz - Nyquist.
    """
    win_size, win_shift, padded = 400, 160, 512
    if "w" not in _FBANK_CACHE:
        n = np.arange(win_size, dtype=np.float64)
        _FBANK_CACHE["w"] = (0.54 - 0.46 * np.cos(2 * np.pi * n / (win_size - 1))).astype(np.float32)
        _FBANK_CACHE["mel"] = _mel_banks()
    window, mel = _FBANK_CACHE["w"], _FBANK_CACHE["mel"]

    frames = np.lib.stride_tricks.sliding_window_view(waveform, win_size)[::win_shift].astype(np.float32)
    frames = frames - frames.mean(axis=1, keepdims=True)                       # remove DC offset
    prev = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)             # replicate pad
    frames = (frames - 0.97 * prev) * window                                   # pre-emphasis + window
    frames = np.pad(frames, ((0, 0), (0, padded - win_size)))
    power = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32) ** 2
    return np.log(np.maximum(power @ mel.T, _FBANK_EPS)).astype(np.float32)


def compute_fbank(waveform):
    """float [1, N] or [N] waveform in [-1, 1] -> centered fbank [frames, 80]."""
    wav = np.asarray(waveform, dtype=np.float32).reshape(-1) * np.float32(1 << 15)
    feats = kaldi_fbank(wav)
    return feats - feats.mean(axis=0, keepdims=True)


def _nearest_resize(x, size):
    """torch.nn.functional.interpolate(mode='nearest') along the last axis."""
    in_size = x.shape[-1]
    scale = np.float32(in_size / size)
    idx = np.minimum(np.floor(np.arange(size, dtype=np.float32) * scale).astype(np.int64), in_size - 1)
    return x[..., idx]


def apply_speaker_masks(fbank_chunk, masks, clean_masks, min_num_frames):
    """Per speaker slot: mask -> resize to fbank frames -> cyclic-tile to the full fbank length.

    Silent slots return the full chunk (as in dataset._apply_speaker_masks).
    """
    T = fbank_chunk.shape[0]
    out = []
    for spk in range(masks.shape[1]):
        mask, clean = masks[:, spk], clean_masks[:, spk]
        used = clean if np.sum(clean) > min_num_frames else mask
        active = _nearest_resize(used, T) > 0.5
        n_active = int(active.sum())
        if n_active == 0:
            out.append(fbank_chunk)
            continue
        out.append(fbank_chunk[active][np.arange(T) % n_active])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Powerset decoding (pyannote.audio.utils.powerset.Powerset)
# ─────────────────────────────────────────────────────────────────────────────

def _powerset_mapping(num_classes=NUM_LOCAL_SPEAKERS, max_set_size=POWERSET_MAX_SET):
    rows = []
    for size in range(0, max_set_size + 1):
        for comb in itertools.combinations(range(num_classes), size):
            row = np.zeros(num_classes, dtype=np.float32)
            row[list(comb)] = 1.0
            rows.append(row)
    return np.stack(rows)       # [7, 3]


POWERSET_MAPPING = _powerset_mapping()


def powerset_to_multilabel(powerset):
    """[B, F, 7] -> hard multilabel [B, F, 3]."""
    return POWERSET_MAPPING[np.argmax(powerset, axis=-1)]


# ─────────────────────────────────────────────────────────────────────────────
# Sliding-window aggregation (pyannote.audio Inference.aggregate)
# ─────────────────────────────────────────────────────────────────────────────

def _closest_frame(t, frame_duration, frame_step, start=0.0):
    return int(np.rint((t - start - 0.5 * frame_duration) / frame_step))


def aggregate(scores, chunk_step=SEG_STEP, chunk_duration=SEG_DURATION,
              frame_duration=RF_DURATION, frame_step=RF_STEP,
              missing=np.nan, skip_average=False, epsilon=1e-12):
    """scores [num_chunks, num_frames, num_classes] -> [total_frames, num_classes] (no hamming/warm-up)."""
    num_chunks, num_frames_per_chunk, num_classes = scores.shape
    total = _closest_frame(chunk_duration + (num_chunks - 1) * chunk_step + 0.5 * frame_duration,
                           frame_duration, frame_step) + 1

    agg = np.zeros((total, num_classes), dtype=np.float32)
    cnt = np.zeros((total, num_classes), dtype=np.float32)
    amask = np.zeros((total, num_classes), dtype=np.float32)

    for c in range(num_chunks):
        score = scores[c]
        mask = 1.0 - np.isnan(score)
        score = np.nan_to_num(score, nan=0.0)
        s = _closest_frame(c * chunk_step + 0.5 * frame_duration, frame_duration, frame_step)
        sl = slice(s, s + num_frames_per_chunk)
        agg[sl] += score * mask
        cnt[sl] += mask
        amask[sl] = np.maximum(amask[sl], mask)

    avg = agg if skip_average else agg / np.maximum(cnt, epsilon)
    avg[amask == 0.0] = missing
    return avg


def speaker_count(binary_segmentations):
    """Instantaneous #speakers per output frame -> uint8 [total_frames]."""
    summed = np.sum(binary_segmentations, axis=-1, keepdims=True)
    count = aggregate(summed, missing=0.0, skip_average=False)
    return np.rint(count[:, 0]).astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
# Clustering (pyannote.audio.pipelines.clustering.AgglomerativeClustering, centroid linkage)
# ─────────────────────────────────────────────────────────────────────────────

def _set_num_clusters(num_embeddings, num_clusters=None, min_clusters=None, max_clusters=None):
    min_clusters = num_clusters or min_clusters or 1
    min_clusters = max(1, min(num_embeddings, min_clusters))
    max_clusters = num_clusters or max_clusters or num_embeddings
    max_clusters = max(1, min(num_embeddings, max_clusters))
    if min_clusters > max_clusters:
        raise ValueError(f"min_clusters ({min_clusters}) must be <= max_clusters ({max_clusters}).")
    if min_clusters == max_clusters:
        num_clusters = min_clusters
    return num_clusters, min_clusters, max_clusters


def _agglomerative_cluster(embeddings, min_clusters, max_clusters, num_clusters=None,
                           method=CLUSTER_METHOD, threshold=CLUSTER_THRESHOLD,
                           min_cluster_size=CLUSTER_MIN_SIZE, metric="cosine"):
    num_embeddings, _ = embeddings.shape
    min_cluster_size = min(min_cluster_size, max(1, round(0.1 * num_embeddings)))

    if num_embeddings == 1:
        return np.zeros((1,), dtype=np.uint8)

    if metric == "cosine" and method in ("centroid", "median", "ward"):
        embeddings = embeddings.copy()
        with np.errstate(divide="ignore", invalid="ignore"):
            embeddings /= np.linalg.norm(embeddings, axis=-1, keepdims=True)
        dendrogram = linkage(embeddings, method=method, metric="euclidean")
    else:
        dendrogram = linkage(embeddings, method=method, metric=metric)

    clusters = fcluster(dendrogram, threshold, criterion="distance") - 1

    cluster_unique, cluster_counts = np.unique(clusters, return_counts=True)
    large_clusters = cluster_unique[cluster_counts >= min_cluster_size]
    num_large_clusters = len(large_clusters)

    if num_large_clusters < min_clusters:
        num_clusters = min_clusters
    elif num_large_clusters > max_clusters:
        num_clusters = max_clusters

    if num_clusters is not None and num_large_clusters != num_clusters:
        _dendrogram = np.copy(dendrogram)
        _dendrogram[:, 2] = np.arange(num_embeddings - 1)

        best_iteration = num_embeddings - 1
        best_num_large_clusters = 1

        for iteration in np.argsort(np.abs(dendrogram[:, 2] - threshold)):
            if _dendrogram[iteration, 3] < min_cluster_size:
                continue
            clusters = fcluster(_dendrogram, iteration, criterion="distance") - 1
            cluster_unique, cluster_counts = np.unique(clusters, return_counts=True)
            large_clusters = cluster_unique[cluster_counts >= min_cluster_size]
            num_large_clusters = len(large_clusters)
            if abs(num_large_clusters - num_clusters) < abs(best_num_large_clusters - num_clusters):
                best_iteration = iteration
                best_num_large_clusters = num_large_clusters
            if num_large_clusters == num_clusters:
                break

        if best_num_large_clusters != num_clusters:
            clusters = fcluster(_dendrogram, best_iteration, criterion="distance") - 1
            cluster_unique, cluster_counts = np.unique(clusters, return_counts=True)
            large_clusters = cluster_unique[cluster_counts >= min_cluster_size]
            num_large_clusters = len(large_clusters)
            print(f"[WARN] Found only {num_large_clusters} clusters. "
                  f"Using a smaller `min_cluster_size` than {min_cluster_size} might help.")

    if num_large_clusters == 0:
        clusters[:] = 0
        return clusters

    small_clusters = cluster_unique[cluster_counts < min_cluster_size]
    if len(small_clusters) == 0:
        return clusters

    large_centroids = np.vstack([np.mean(embeddings[clusters == k], axis=0) for k in large_clusters])
    small_centroids = np.vstack([np.mean(embeddings[clusters == k], axis=0) for k in small_clusters])
    d = cdist(large_centroids, small_centroids, metric=metric)
    for small_k, large_k in enumerate(np.argmin(d, axis=0)):
        clusters[clusters == small_clusters[small_k]] = large_clusters[large_k]

    _, clusters = np.unique(clusters, return_inverse=True)
    return clusters


def cluster_embeddings(embeddings, segmentations, num_clusters=None, min_clusters=None, max_clusters=None):
    """embeddings [C, S, D], segmentations [C, F, S] -> hard_clusters [C, S] (int)."""
    active = np.sum(segmentations, axis=1) > 0
    valid = ~np.any(np.isnan(embeddings), axis=2)
    chunk_idx, speaker_idx = np.where(active * valid)
    train = embeddings[chunk_idx, speaker_idx]
    num_embeddings = train.shape[0]

    num_clusters, min_clusters, max_clusters = _set_num_clusters(
        num_embeddings, num_clusters, min_clusters, max_clusters)

    num_chunks, num_speakers, _ = embeddings.shape
    if max_clusters < 2:
        return np.zeros((num_chunks, num_speakers), dtype=np.int8)

    train_clusters = _agglomerative_cluster(train, min_clusters, max_clusters, num_clusters=num_clusters)

    # assign every (chunk, speaker) embedding to the closest centroid
    n_clusters = int(np.max(train_clusters)) + 1
    centroids = np.vstack([np.mean(train[train_clusters == k], axis=0) for k in range(n_clusters)])
    dist = cdist(embeddings.reshape(num_chunks * num_speakers, -1), centroids, metric="cosine")
    soft = 2 - dist.reshape(num_chunks, num_speakers, n_clusters)
    soft = np.nan_to_num(soft, nan=-np.inf) if np.isnan(soft).any() else soft
    return np.argmax(soft, axis=2)


# ─────────────────────────────────────────────────────────────────────────────
# Reconstruction & annotation (pyannote SpeakerDiarizationMixin)
# ─────────────────────────────────────────────────────────────────────────────

def reconstruct(segmentations, hard_clusters, count):
    """Cluster-wise max over local speakers -> aggregate -> keep top-`count` speakers per frame."""
    num_chunks, num_frames, _ = segmentations.shape
    num_clusters = int(np.max(hard_clusters)) + 1
    clustered = np.full((num_chunks, num_frames, num_clusters), np.nan, dtype=np.float32)

    for c in range(num_chunks):
        cluster = hard_clusters[c]
        for k in np.unique(cluster):
            if k == -2:
                continue
            clustered[c, :, k] = np.max(segmentations[c][:, cluster == k], axis=1)

    activations = aggregate(clustered, missing=0.0, skip_average=True)       # [total_frames, num_clusters]

    total = min(activations.shape[0], count.shape[0])
    activations, count = activations[:total], count[:total]

    max_count = int(np.max(count))
    if activations.shape[1] < max_count:
        activations = np.pad(activations, ((0, 0), (0, max_count - activations.shape[1])))

    # keep the top-`count[t]` most active clusters of every frame
    rank = np.argsort(np.argsort(-activations, axis=-1), axis=-1)
    return (rank < count[:, None]).astype(np.float32)


def to_annotation(discrete, min_duration_off=MIN_DURATION_OFF):
    """Binarize (onset = offset = 0.5) -> sorted list of (start, end, speaker_label)."""
    num_frames, num_speakers = discrete.shape
    ts = RF_DURATION / 2 + np.arange(num_frames) * RF_STEP            # frame centres
    segs = []
    for k in range(num_speakers):
        active = discrete[:, k] > 0.5
        change = np.diff(active.astype(np.int8))
        starts = ts[1:][change == 1]
        ends = ts[1:][change == -1]
        if active[0]:
            starts = np.concatenate([[ts[0]], starts])
        if active[-1]:
            ends = np.concatenate([ends, [ts[-1]]])
        segs += [(float(st), float(en), k) for st, en in zip(starts, ends)]

    if min_duration_off > 0.0:   # fill same-speaker gaps shorter than min_duration_off
        merged = []
        for seg in sorted(segs, key=lambda s: (s[2], s[0])):
            if merged and merged[-1][2] == seg[2] and seg[0] - merged[-1][1] <= min_duration_off:
                merged[-1] = (merged[-1][0], max(merged[-1][1], seg[1]), seg[2])
            else:
                merged.append(seg)
        segs = merged
    return sorted(segs, key=lambda s: (s[0], s[1], s[2]))


# ─────────────────────────────────────────────────────────────────────────────
# Inference
# ─────────────────────────────────────────────────────────────────────────────

def _chunk_audio(waveform):
    """Sliding 5 s windows (1 s step); last partial window zero-padded. -> [N, 80000]"""
    num_samples = waveform.shape[0]
    if num_samples > SEG_WINDOW:
        chunks = np.lib.stride_tricks.sliding_window_view(waveform, SEG_WINDOW)[::SEG_STEP_SAMPLES]
        num_chunks = chunks.shape[0]
    else:
        chunks = np.empty((0, SEG_WINDOW), dtype=np.float32)
        num_chunks = 0

    has_last = (num_chunks == 0) or ((num_samples - SEG_WINDOW) % SEG_STEP_SAMPLES > 0)
    if has_last:
        last = waveform[num_chunks * SEG_STEP_SAMPLES:]
        last = np.pad(last, (0, SEG_WINDOW - last.shape[0]))
        chunks = np.concatenate([chunks, last[None]], axis=0)
    return np.ascontiguousarray(chunks, dtype=np.float32)


def run_segmentation(chunks):
    """chunks [N, 80000] -> hard multilabel segmentation [N, 293, 3] (NPU, fixed batch 32)."""
    n = chunks.shape[0]
    pad = (-n) % BATCH_SIZE
    if pad:
        chunks = np.concatenate([chunks, np.zeros((pad, SEG_WINDOW), dtype=np.float32)], axis=0)

    outs = []
    for i in range(0, chunks.shape[0], BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE].reshape(BATCH_SIZE, 1, SEG_WINDOW)
        outs.append(powerset_to_multilabel(segmentation.Inference(batch)))
    return np.concatenate(outs, axis=0)[:n]


def run_embedding(fbank_iter):
    """Iterable of [498, 80] fbank -> embeddings [N, 256] (NPU, fixed batch 32)."""
    results = []
    batch = []

    def flush():
        actual = len(batch)
        x = np.stack(batch).astype(np.float32)
        if actual < BATCH_SIZE:
            x = np.concatenate([x, np.zeros((BATCH_SIZE - actual, EMB_NUM_FRAMES, NUM_MELS), dtype=np.float32)], axis=0)
        results.append(np.array(embedding.Inference(x))[:actual])
        batch.clear()

    for fb in fbank_iter:
        batch.append(fb)
        if len(batch) == BATCH_SIZE:
            flush()
    if batch:
        flush()
    return np.concatenate(results, axis=0) if results else np.zeros((0, EMB_DIM), dtype=np.float32)


def Inference(input_audio_path, num_speakers=None, min_speakers=None, max_speakers=None):
    """Run speaker diarization. Returns a list of (start_sec, end_sec, 'SPEAKER_xx')."""
    input_audio_path = Path(input_audio_path)
    print(f"[INFO] Input audio: {input_audio_path.resolve()}")
    waveform = load_audiofile(input_audio_path)
    print(f"[INFO] Audio: {waveform.shape[0] / SAMPLE_RATE:.1f} s @ {SAMPLE_RATE} Hz")

    # speaker-count constraints
    min_spk = num_speakers or min_speakers or 1
    max_spk = num_speakers or max_speakers or np.inf
    if min_spk > max_spk:
        raise ValueError(f"min_speakers ({min_spk}) must be <= max_speakers ({max_spk}).")
    if min_spk == max_spk:
        num_speakers = min_spk

    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        # 1. segmentation (NPU)
        t0 = time.perf_counter()
        chunks = _chunk_audio(waveform)
        seg = run_segmentation(chunks)                       # [C, 293, 3]
        t_seg = time.perf_counter() - t0
        num_chunks, num_frames, _ = seg.shape
        print(f"[INFO] Segmentation: {num_chunks} chunks in {t_seg * 1000:.1f} ms")

        # 2. speaker counting
        count = speaker_count(seg)
        if int(np.max(count)) == 0:
            print("[INFO] No speech detected.")
            return []

        # 3. embeddings (fbank on CPU, ResNet on NPU)
        t0 = time.perf_counter()
        min_num_frames = math.ceil(num_frames * EMB_MIN_NUM_SAMPLES / SEG_WINDOW)
        clean_frames = 1.0 * (np.sum(seg, axis=2, keepdims=True) < 2)
        clean_seg = seg * clean_frames

        def _iter_fbank():
            for c in range(num_chunks):
                fb = compute_fbank(chunks[c])                # [498, 80]
                yield from apply_speaker_masks(fb, seg[c].astype(np.float32),
                                               clean_seg[c].astype(np.float32), min_num_frames)

        emb = run_embedding(_iter_fbank())
        embeddings = emb.reshape(num_chunks, NUM_LOCAL_SPEAKERS, -1)
        t_emb = time.perf_counter() - t0
        print(f"[INFO] Embedding: {emb.shape[0]} vectors in {t_emb * 1000:.1f} ms")
    finally:
        PerfProfile.RelPerfProfileGlobal()

    # 4. clustering
    hard_clusters = cluster_embeddings(embeddings, seg, num_clusters=num_speakers,
                                       min_clusters=min_speakers, max_clusters=max_speakers)
    num_different = int(np.max(hard_clusters)) + 1
    if num_different < min_spk or num_different > max_spk:
        print(f"[WARN] Detected {num_different} speakers, outside [{min_spk}, {max_spk}].")

    # 5. reconstruction -> annotation
    count = np.minimum(count, max_spk).astype(np.int8)
    hard_clusters = hard_clusters.copy()
    hard_clusters[np.sum(seg, axis=1) == 0] = -2             # inactive local speakers
    discrete = reconstruct(seg, hard_clusters, count)
    segments = to_annotation(discrete)

    labels = {k: f"SPEAKER_{i:02d}" for i, k in enumerate(sorted({s[2] for s in segments}))}
    return [(s, e, labels[k]) for s, e, k in segments]


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def post_process(result, rttm_path=None, uri="audio"):
    print("Diarization result:")
    for s, e, spk in result:
        print(f"[ {_fmt(s)} --> {_fmt(e)}] {spk}")
    speakers = sorted({r[2] for r in result})
    print(f"[INFO] {len(result)} segments, {len(speakers)} speaker(s): {', '.join(speakers)}")
    if rttm_path:
        with open(rttm_path, "w", encoding="utf-8") as f:
            for s, e, spk in result:
                f.write(f"SPEAKER {uri} 1 {s:.3f} {e - s:.3f} <NA> <NA> {spk} <NA> <NA>\n")
        print(f"[INFO] RTTM written to {rttm_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Debug helpers
# ─────────────────────────────────────────────────────────────────────────────

def print_model_info():
    for name, ctx in (("segmentation", segmentation), ("embedding", embedding)):
        print(f"[DEBUG] {name:<12} input  : {ctx.getInputName()} {ctx.getInputShapes()} {ctx.getInputDataType()}")
        print(f"[DEBUG] {name:<12} output : {ctx.getOutputName()} {ctx.getOutputShapes()} {ctx.getOutputDataType()}")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(input=None, chipset=None, num_speakers=None, min_speakers=None, max_speakers=None, output_rttm=None):
    if input is None:
        input = _ensure_demo_audio()

    Init(chipset=chipset)
    print_model_info()

    t0 = time.perf_counter()
    result = Inference(input, num_speakers=num_speakers, min_speakers=min_speakers, max_speakers=max_speakers)
    print(f"[INFO] Total diarization time: {time.perf_counter() - t0:.2f} s")

    post_process(result, output_rttm, uri=Path(input).stem)
    Release()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            f"{MODEL_NAME} speaker diarization on the Snapdragon HTP (NPU) via qai_appbuilder\n"
            f"Models: AI Hub context binaries, auto-downloaded to ../models/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input_audio_path", default=None,
                        help="Path to the input audio file (default: 30 s AMI demo clip, auto-downloaded)")
    parser.add_argument("--chipset", default=None, metavar="NAME",
                        help="Override detected device: x_elite (8380) | x2_elite (8480)")
    parser.add_argument("--num_speakers", type=int, default=None, help="Exact number of speakers")
    parser.add_argument("--min_speakers", type=int, default=None, help="Minimum number of speakers")
    parser.add_argument("--max_speakers", type=int, default=None, help="Maximum number of speakers")
    parser.add_argument("--output_rttm", default=None, help="Write the diarization to this RTTM file")
    args = parser.parse_args()

    main(
        input=args.input_audio_path,
        chipset=args.chipset,
        num_speakers=args.num_speakers,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        output_rttm=args.output_rttm,
    )
