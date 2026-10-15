# Pyannote Speaker Diarization — "Who Spoke When" on Snapdragon NPU

## Overview

**Pyannote-Speaker-Diarization** runs the `pyannote/speaker-diarization-3.1` pipeline on the
Snapdragon NPU (HTP) via QAI AppBuilder. Given an audio recording with multiple speakers, it
outputs a list of `(start, end, speaker_label)` segments — i.e. who was speaking, and when.

- **Task**: Speaker Diarization
- **Input**: WAV/audio file (any sample rate, auto-resampled to 16 kHz; any duration)
- **Output**: List of `(start_sec, end_sec, SPEAKER_xx)` segments, optionally written as RTTM
- **Platform**: Windows on Snapdragon (WoS) only, Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [pyannote_speaker_diarization](https://aihub.qualcomm.com/compute/models/pyannote_speaker_diarization)

## Model Architecture

Two QNN context binaries run on the NPU; everything else (chunking, fbank, powerset decoding,
speaker counting, clustering, reconstruction, binarization) is re-implemented with NumPy / SciPy
only — no `pyannote.audio`, `torchaudio` or `einops` required.

| Component | Model File | Description |
| --------- | ---------- | ----------- |
| Segmentation | `segmentation.bin` | waveform chunk `(32, 1, 80000)` → powerset logits `(32, 293, 7)` (local speaker activity) |
| Embedding | `embedding.bin` | fbank `(32, 498, 80)` → WeSpeaker ResNet34 speaker embedding `(32, 256)` |

Both binaries use a fixed batch size of 32; inputs are padded/batched accordingly.

## Requirements

```
pip install soundfile soxr scipy
```

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model pyannote_speaker_diarization
```

With a custom audio file:
```bash
python run_inference.py --model pyannote_speaker_diarization --args "--input_audio_path path\to\audio.wav"
```

Or run directly:
```bash
python models\audio\speaker_diarization\Pyannote-Speaker-Diarization\python\Pyannote-Speaker-Diarization.py
python models\audio\speaker_diarization\Pyannote-Speaker-Diarization\python\Pyannote-Speaker-Diarization.py --input_audio_path path\to\audio.wav --output_rttm out.rttm
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--input_audio_path` | 30 s AMI demo clip (auto-downloaded) | Path to the input audio file |
| `--chipset` | Auto-detected | Override detected device: `x_elite` (8380) \| `x2_elite` (8480) |
| `--num_speakers` | None | Exact number of speakers (overrides min/max) |
| `--min_speakers` | None | Minimum number of speakers |
| `--max_speakers` | None | Maximum number of speakers |
| `--output_rttm` | None | Also write the result as an RTTM file |

## Model Download

Pre-compiled HTP context binaries (QAIRT 2.50) are downloaded automatically on first run from
AI Hub and copied to `models/`:
- Snapdragon X Elite (8380) → HTP v73
- Snapdragon X2 Elite (8480) → HTP v81

If no `--input_audio_path` is given, a 30 s demo clip (AMI `IS1009a`, 55 s–85 s — the same clip
used by the AI Hub demo) is downloaded to `assets/demo_audio.wav` on first run.

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Audio file | Any sample rate (auto-resampled to 16 kHz), any duration, downmixed to mono |
| **Output** | List of tuples | `(start_sec, end_sec, 'SPEAKER_xx')`, optionally saved as RTTM |

## Pipeline Details

```
Audio file
    ↓ Load, downmix to mono, resample to 16 kHz
    ↓ Sliding 5 s windows, 1 s step (last window zero-padded)
Waveform chunks [N, 1, 80000]
    ↓ Segmentation model (NPU, batch 32)
Powerset logits [N, 293, 7] → multilabel [N, 293, 3] (local speaker activity)
    ↓ Speaker counting (instantaneous active-speaker count per frame)
    ↓ Per chunk, per local speaker: mask fbank with that speaker's activity
Fbank [498, 80] (Kaldi/WeSpeaker settings, computed on CPU)
    ↓ Embedding model (NPU, batch 32)
Speaker embeddings [N, 3, 256]
    ↓ Agglomerative clustering (centroid linkage, cosine distance)
Hard cluster assignment per local speaker
    ↓ Reconstruction (cluster-wise max, aggregate across overlapping chunks)
    ↓ Binarization (onset = offset = 0.5)
Segments (start, end, SPEAKER_xx)
```

## Example Output

```
SOC_ID: None
[INFO] Detected platform: wos
[INFO] Device: snapdragon_x_elite
[INFO] Runtime: HTP
[INFO] Input audio: ...\assets\demo_audio.wav
[INFO] Audio: 30.0 s @ 16000 Hz
[INFO] Segmentation: 26 chunks in 42.1 ms
[INFO] Embedding: 78 vectors in 55.3 ms
Diarization result:
[ 00:00:00.497 --> 00:00:04.258] SPEAKER_00
[ 00:00:04.713 --> 00:00:09.825] SPEAKER_01
[ 00:00:10.112 --> 00:00:14.980] SPEAKER_00
...
[INFO] 7 segments, 2 speaker(s): SPEAKER_00, SPEAKER_01
[INFO] Total diarization time: 1.23 s
```

## Notes

- Clustering hyper-parameters (centroid linkage, threshold ≈ 0.7046, min cluster size 12) follow
  `pyannote/speaker-diarization-3.1`'s `config.yaml`; results closely match the reference
  pipeline but are not bit-exact.
- `--num_speakers` fixes the exact speaker count; `--min_speakers`/`--max_speakers` instead bound
  the automatically-determined count.
- This model requires Windows on Snapdragon (HTP); it does not fall back to CPU/DLC on x86 Windows.
