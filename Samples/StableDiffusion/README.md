# StableDiffusion Sample Code

## Introduction
This is sample code for using QAI AppBuilder to load Stable Diffusion 1.5 QNN models, run the inference and free the resource. 

## Python Environment
Please setup the Python environment according to the guide below:<br>
https://github.com/quic/ai-engine-direct-helper/blob/main/docs/user_guide.md<br>
Make sure to use the right QAI AppBuilder version according to the QNN SDK version which the models were generated.

## Stable Diffusion QNN models
You need to generate Stable Diffusion QNN models according to the guide below before you running it with this sample code:
https://docs.qualcomm.com/bundle/publicresource/topics/80-64748-1/introduction.html

After the models are ready, please copy them to the following path:
```
c:\ai-app\SD_1.5\models\sd_v1.5\stable_diffusion_v1_5_quantized-textencoder_quantized.bin
c:\ai-app\SD_1.5\models\sd_v1.5\stable_diffusion_v1_5_quantized-unet_quantized.bin
c:\ai-app\SD_1.5\models\sd_v1.5\stable_diffusion_v1_5_quantized-vaedecoder_quantized.bin
```

## time-embedding
In this sample code, we need to use 'time-embedding' data. The below code can be used to generate the 'time-embedding' data:
```
import os
import torch
import numpy as np
from diffusers.models.embeddings import get_timestep_embedding
from diffusers import UNet2DConditionModel
from diffusers import DPMSolverMultistepScheduler

user_step = 20
time_embeddings = UNet2DConditionModel.from_pretrained('runwayml/stable-diffusion-v1-5', subfolder='unet', cache_dir='./cache').time_embedding
scheduler = DPMSolverMultistepScheduler(num_train_timesteps=1000, beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear")

def get_timestep(step):
    return np.int32(scheduler.timesteps.numpy()[step])

def get_time_embedding(timestep):
    timestep = torch.tensor([timestep])
    t_emb = get_timestep_embedding(timestep, 320, True, 0)
    emb = time_embeddings(t_emb).detach().numpy()
    return emb

def gen_time_embedding():
    scheduler.set_timesteps(user_step)
    
    time_emb_path = ".\\models\\time-embedding_v1.5\\" + str(user_step) + "\\"
    os.mkdir(time_emb_path)
    for step in range(user_step):
        file_path = time_emb_path + str(step) + ".raw"
        timestep = get_timestep(step)
        time_embedding = get_time_embedding(timestep)
        time_embedding.tofile(file_path)

# Only needs to executed once for generating time enbedding data to app folder.
# Modify 'user_step' to '20', '30', '50' to generate 'time_embedding' for steps - '20', '30', '50'.

user_step = 20
gen_time_embedding()

user_step = 30
gen_time_embedding()

user_step = 50
gen_time_embedding()
```

After generated the 'time-embedding' data, please copy them to the following path:
```
c:\ai-app\SD_1.5\models\time-embedding\20
c:\ai-app\SD_1.5\models\time-embedding\30
c:\ai-app\SD_1.5\models\time-embedding\50
```

## CLIP ViT-L/14 model
In this sample code, we need CLIP ViT-L/14 as text encoder. You can download the file below from 'https://huggingface.co/openai/clip-vit-large-patch14/tree/main' and save them to foldet 'clip-vit-large-patch14'. 
Rename the files to below:
```
merges.txt
special_tokens_map.json
tokenizer_config.json
vocab.json
```

After downloaded the model, please copy them to the following path:
```
c:\ai-app\SD_1.5\models\clip-vit-large-patch14
```

## Run the sample code
Download the sample code from the following link: <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/Samples/StableDiffusion/StableDiffusion.py

After downloaded the sample code, please copy them to the following path:
```
c:\ai-app\SD_1.5\
```

Run the sample code:
```
python StableDiffusion.py
```

## Output
The output image will be saved to the following path:
```
c:\ai-app\SD_1.5\images\
```

## Reference
You need to setup the QAI AppBuilder environment before you run the sample code. Below is the guide on how to setup the QAI AppBuilder environment:<br>
https://github.com/quic/ai-engine-direct-helper/blob/main/README.md <br>
https://github.com/quic/ai-engine-direct-helper/blob/main/Docs/User_Guide.md
