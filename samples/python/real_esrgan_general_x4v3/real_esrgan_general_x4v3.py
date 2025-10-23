# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import cv2
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray

from qai_appbuilder import (QNNContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################

MODEL_ID = "mn0j68p8m"
MODEL_NAME = "real_esrgan_general_x4v3"
MODEL_HELP_URL = "https://github.com/quic/ai-engine-direct-helper/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 512

####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

####################################################################

image_buffer = None
realesrgan = None

def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.Resize(IMAGE_SIZE),      # bgr image
                                    transforms.CenterCrop(IMAGE_SIZE),
                                    transforms.PILToTensor()])
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.float() / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)

# RealESRGan class which inherited from the class QNNContext.
class RealESRGan(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)[0]        
        return output_data

def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init():
    global realesrgan

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(str(qnn_dir), Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)

    # Instance for RealESRGan objects.
    realesrgan = RealESRGan("realesrgan", str(model_path))

def Inference(input_image_path, output_image_path):
    global image_buffer

    # Read and preprocess the image.
    image = Image.open(input_image_path)
    image = preprocess_PIL_image(image).numpy()
    image = np.transpose(image, (1, 2, 0))  # CHW -> HWC

    # Burst the HTP.
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)

    # Run the inference.
    output_image = realesrgan.Inference([image])

    # Reset the HTP.
    PerfProfile.RelPerfProfileGlobal()

    # show & save the result
    output_image = torch.from_numpy(output_image)
    output_image = output_image.reshape(IMAGE_SIZE * 4, IMAGE_SIZE * 4, 3)
    output_image = torch.unsqueeze(output_image, 0)
    output_image = [torch_tensor_to_PIL_image(img) for img in output_image]
    image_buffer = output_image[0]
    image_buffer.save(output_image_path)
    image_buffer.show()

def Release():
    global realesrgan

    # Release the resources.
    del(realesrgan)


Init()

Inference(execution_ws / "input.jpg", execution_ws / "output.jpg")

Release()

