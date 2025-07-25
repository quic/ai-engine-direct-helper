<br>

<div align="center">
  <h3>Run the model locally on NPU, deploy the AI-Hub model quickly.</h3>
  <p><i> SIMPLE | EASY | FAST </i></p>
</div>
<br>

## Disclaimer
This software is provided “as is,” without any express or implied warranties. The authors and contributors shall not be held liable for any damages arising from its use. The code may be incomplete or insufficiently tested. Users are solely responsible for evaluating its suitability and assume all associated risks. <br>
Note: Contributions are welcome. Please ensure thorough testing before deploying in critical systems.

## Introduction 
This guide helps developers setup Python environment for using QAI AppBuilder to run sample code on Windows on Snapdragon (WoS) platforms.

## Setting Up QAI AppBuilder Python Environment
You can also run the batch file from [QAI AppBuilder Launcher](../../tools/launcher/) to setup the environment automatically.

### Step 1: Install Dependencies
Refer to [python.md](../../docs/python.md) on how to setup x64 version Python environment.

### Step 2: Install basic Python dependencies
Run below commands in Windows terminal:
```
pip install qai_hub_models==0.30.2 huggingface_hub==0.33.1 Pillow==10.4.0 numpy==1.26.4 torch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1 transformers==4.46.3 diffusers==0.32.2 tqdm==4.67.1 scikit-image==0.25.2 pygame==2.6.1 ftfy==6.3.1 av==15.0.0 resampy==0.4.3 soundfile==0.13.1 easyocr==1.7.2 samplerate==0.2.1 whisper==1.1.10 audio2numpy==0.1.2 ultralytics==8.0.193 opencv-python-headless==4.11.0.86 opencv-python==4.10.0.84 openai-whisper==20250625 openai-clip==1.0.1 resampy==0.4.3
```

### Step 3: Run Model
Run below commands in Windows terminal:
```
cd ai-engine-direct-helper\samples
python <Python script for running model> <Parameter of Python script>
```
Where `<Python script for running model>` is the Python script you want to run. For example, if you want to run `stable_diffusion_v2_1`, you can run below command:
```
cd ai-engine-direct-helper\samples
python python\stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "spectacular view of northern lights from Alaska"
```

### Support Automatically Setting Up Model List:

|  Model   | Command  |
|  ----  | :---- |
| stable_diffusion_v2_1 | python python\stable_diffusion_v2_1\stable_diffusion_v2_1.py --prompt "the prompt string ..." |
| stable_diffusion_v1_5 | python python\stable_diffusion_v1_5\stable_diffusion_v1_5.py --prompt "the prompt string ..." |
| real_esrgan_x4plus  | python python\real_esrgan_x4plus\real_esrgan_x4plus.py |
| real_esrgan_general_x4v3  | python python\real_esrgan_general_x4v3\real_esrgan_general_x4v3.py |
| quicksrnetmedium  | python python\quicksrnetmedium\quicksrnetmedium.py |
| inception_v3  | python python\inception_v3\inception_v3.py |
| beit  | python python\beit\beit.py |
| googlenet  | python python\googlenet\googlenet.py |
| resnet_3d  | python python\resnet_3d\resnet_3d.py |
| facemap_3dmm  | python python\facemap_3dmm\facemap_3dmm.py |
| yamnet  | python python\yamnet\yamnet.py |
| yolov8_det  | python python\yolov8_det\yolov8_det.py |
| unet_segmentation  | python python\unet_segmentation\unet_segmentation.py |
| openpose  | python python\openpose\openpose.py |
| lama_dilated  | python python\lama_dilated\lama_dilated.py |
| aotgan  | python python\aotgan\aotgan.py |
| depth_anything  | python python\depth_anything\depth_anything.py |
| mediapipe_hand  | python python\mediapipe_hand\mediapipe_hand.py |
| nomic_embed_text  | python python\nomic_embed_text\nomic_embed_text.py |
| easy_ocr  | python python python\easy_ocr\easy_ocr.py |
| face_attrib_net  | python python python\face_attrib_net\face_attrib_net.py |
| openai_clip  | python python\openai_clip\openai_clip.py --text "mountain" |
| whisper_base_en  | python python\whisper_base_en\whisper_base_en.py |
| whisper_tiny_en  | python python\whisper_tiny_en\whisper_tiny_en.py |
| | |

### Prepare Stable Diffusion models manually
Stable Diffusion models are big, you can also download these models manually if automatically downloading functions don't work well.<br>
Download Stable Diffusion models from following AI-Hub website and save them to path: 'samples\python\stable_diffusion_v1_5\models' & 'samples\python\stable_diffusion_v2_1\models' manually.<br>
For other models, the sample python script will download them automatically.

There're 3 models for each Stable Diffusion need to be downloaded: TextEncoderQuantizable, UnetQuantizable, VaeDecoderQuantizable. <br>
Make sure to select the right model when download them:<br>
1. Choose runtime: *Qualcomm® AI Engine Direct*<br>
2. Choose device: *Snapdragon® X Elite*<br>

Do *not* rename the model names, just download and copy them to the 'models' folder. <br>

Models AI-Hub links:<br>
[stable_diffusion_v1_5](https://aihub.qualcomm.com/compute/models/stable_diffusion_v1_5)<br>
[stable_diffusion_v2_1](https://aihub.qualcomm.com/compute/models/stable_diffusion_v2_1)<br>