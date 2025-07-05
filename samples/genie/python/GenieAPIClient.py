# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# python GenieAPIClient.py --prompt "How to fish?"
# python GenieAPIClient.py --prompt "How to fish?" --stream

import argparse
from openai import OpenAI

HOST="localhost"
PORT="8910"

parser = argparse.ArgumentParser()
parser.add_argument("--stream", action="store_true")
parser.add_argument("--prompt", default="你好", type=str)
parser.add_argument("--model", default="IBM-Granite-v3.1-8B", type=str)
args = parser.parse_args()

client = OpenAI(base_url="http://" + HOST + ":" + PORT + "/v1", api_key="123")

# model_lst = client.models.list()
# print(model_lst)

messages = [{"role": "system", "content": "You are a math teacher who teaches algebra."}, {"role": "user", "content": args.prompt}]
extra_body = {"size": 4096, "seed": 146, "temp": 1.5, "top_k": 13, "top_p": 0.6, "penalty_last_n": 64, "penalty_repeat": 1.3}

model_name = args.model

if args.stream:
    response = client.chat.completions.create(model=model_name, stream=True, messages=messages, extra_body=extra_body)

    for chunk in response:
        # print(chunk)
        content = chunk.choices[0].delta.content
        if content is not None:
            print(content, end="", flush=True)
else:
    response = client.chat.completions.create(model=model_name, messages=messages, extra_body=extra_body)
    # print(response)
    print(response.choices[0].message.content)
