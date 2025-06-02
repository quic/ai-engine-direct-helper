import time
import sys
import os
import threading
import shutil
import re
import numpy as np
import gradio as gr
from gradio import ChatMessage
from gradio.data_classes import FileData

sys.path.append("python")
sys.path.append("genie\\python")

from ChainUtils import GenieLLM
from DocUtils import DocSummarize

import stable_diffusion_v2_1.stable_diffusion_v2_1 as stable_diffusion

###########################################################################

HOST="0.0.0.0"
PORT=8976
APP_PATH="genie\\python\\"

DOCS_MAX_SIZE = 4096 - 1024  # TODO, calculate this value.

FILE_TYPES = [".pdf", ".docx", ".pptx", ".txt", ".md", ".py", ".c", ".cpp", ".h", ".hpp" ]
FUNC_LIST = ["📐 解题答疑", "📚 文档总结", "🗛 AI 翻 译", "🌐 AI 搜 索", "✒️ 帮我写作", "🎨 图像生成", "🍸 定制功能", "✈️ 旅游规划"]

FILE_PATH = "files"

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    END = '\033[0m'  # 重置颜色

###########################################################################

llm = None
sumllm = None
_func_mode = 0
_question = None
_sys_prompt = ""

###########################################################################

css=""" 
body{
	display:flex;
}

::-webkit-scrollbar {
   width:18px;
   height:18px
}
::-webkit-scrollbar-thumb {
   border-radius:10px;
   background-color:rgba(55, 109, 151, .18);
   min-height:30px;
   border:5px solid transparent;
   background-clip:content-box
}
::-webkit-scrollbar-thumb:hover {
   background-color:rgba(55, 109, 151, .3)
}

footer{display:none !important}
"""

###########################################################################

def has_chinese(string):
    pattern = re.compile(r'[\u4e00-\u9fff]')
    match = pattern.search(string)
    return match is not None

def model_unload():
    global llm

    if llm:
        del(llm)
        llm = None

def model_change(value):
    global llm

    print()
    print(f"{Colors.GREEN}INFO:     loading model <<<", value, f">>>{Colors.END}")
    model_name = value
    model_unload() # unload old model if it is already loaded.

    time_start = time.time()
    llm = GenieLLM()
    llm.init(model_name=model_name)

    print(f"{Colors.GREEN}INFO:     model <<<", value, f">>> is ready!{Colors.END}")
    time_end = time.time()
    load_time = str(round(time_end - time_start, 2)) + " (s)"
    print(f"{Colors.GREEN}INFO:     model init time:", load_time, f"{Colors.END}")

def stop():
    if sumllm:
        sumllm.stop()
    else:
        llm.stop()

def chat(chatbot, max_length, temp, top_k, top_p):
    if len(_question) <= 0:
        time.sleep(0.01)
        yield chatbot, "", "", ""
        return chatbot, "", "", ""

    chatbot.append(ChatMessage(role="assistant", content=""))
    #chatbot.append(ChatMessage(role="assistant", content="", metadata={"title": "[Thinking]"}))

    answer = ""

    for chunk in llm.stream(_question):
        answer += chunk
        # print(chunk.text, end="", flush=True)
        if(len(answer) > 0):
            chatbot[-1].content = answer
            yield chatbot, "", "", ""

    chatbot[-1].content = answer

    time_to_first_token, prompt_speed, eval_speed, profile = llm.get_profile_str()
    chatbot.append(ChatMessage(role="assistant", content=profile))

    yield chatbot, time_to_first_token, prompt_speed, eval_speed

def predict(chatbot, max_length, temp, top_k, top_p):
    global _question
    global _sys_prompt
 

    if not llm or not llm.is_ready():
        gr.Warning("请先选择模型并等待模型加载完成！", duration=5)
        return reset_state()

    llm.set_params(str(max_length), str(temp), str(top_k), str(top_p))

    if FUNC_LIST[_func_mode] == "📐 解题答疑":
        for chunk in chat(chatbot, max_length, temp, top_k, top_p):
            yield chunk


    elif FUNC_LIST[_func_mode] == "🗛 AI 翻 译":
        if has_chinese(_question):
            _question = f"Translate the following content to English: \n{_question}\n\n"
        else:
            _question = f"将以下内容翻译成中文：\n{_question}\n\n"

        for chunk in chat(chatbot, max_length, temp, top_k, top_p):
            yield chunk

    elif FUNC_LIST[_func_mode] == "🍸 定制功能":
 
        _question = _sys_prompt + _question
       # _question = f"词源：\n{_question}\n\n"
        print("\nsys prompt:", _sys_prompt)
        print("\nquestion:",_question)
        user_prompt = _question

        for chunk in chat(chatbot, max_length, temp, top_k, top_p):
            yield chunk

    elif FUNC_LIST[_func_mode] == "📚 文档总结":
        if not os.path.exists(FILE_PATH):
            gr.Warning("请先上传文档！", duration=5)
            return reset_state()

        chain_type = "map_reduce"
        sumllm = DocSummarize(llm)
        # print("predict", _question)
        ret = sumllm.init(prompt=_question, chain_type=chain_type, file_path="files")
        if ret == False:
            gr.Warning("无法从此文档中解析中内容，文档中是否没有包含文本？请先上传包含有效文本的文档！", duration=5)
            return reset_state()

        for chunk in sumllm.summarize(chatbot, max_length, temp, top_k, top_p):
            yield chunk
        
        del(sumllm)
        sumllm = None

    elif FUNC_LIST[_func_mode] == "🎨 图像生成":
        image_data = {"path": ""}

        def callback(result):
            if ((None == result) or isinstance(result, str)):   # None == Image generates failed. 'str' == image_path: generated new image path.
                if (None == result):
                    result = "None"
                else:
                    print("Image saved to '" + result + "'")
                    image_data["path"] = result
                    print(image_data["path"])
            else:
                result = (result + 1) * 100
                result = int(result / user_step)
                result = str(result)
                print("step : " + result)

        user_prompt = _question

        if has_chinese(user_prompt):
            user_prompt = f"Translate the following content to English: \n{user_prompt}\n\n"
            chatbot.append(ChatMessage(role="assistant", content="翻译中..."))
            yield chatbot, "", "", ""
            user_prompt = llm.invoke(user_prompt)
            chatbot[-1].content = user_prompt
            yield chatbot, "", "", ""

        chatbot.append(ChatMessage(role="assistant", content="图片生成中..."))
        yield chatbot, "", "", ""

        stable_diffusion.model_initialize()

        user_negative_prompt = ""
        user_seed = np.random.randint(low=0, high=9999999999, size=None, dtype=np.int64)
        user_step = 20
        user_guidance_scale = 7.5

        stable_diffusion.setup_parameters(user_prompt, user_negative_prompt, user_seed, user_step, user_guidance_scale)
        stable_diffusion.model_execute(callback, "images", show_image = False)

        print(image_data["path"])
        chatbot[-1].content = gr.Image(image_data["path"])
        # chatbot.append(ChatMessage(role="assistant", content= gr.Image(image_data["path"])))
        yield chatbot, "", "", ""


# TODO. Handle media files.
def add_media(chatbot, chatmsg):
    global _question, _func_mode

    print()
    print(FUNC_LIST[_func_mode])

    if chatmsg["text"] is not None:
        _question = chatmsg["text"]
        # print(_question)

        if len(_question) > 0:
            chatbot.append(ChatMessage(role="user", content=_question))

        # handle files.
        if os.path.exists(FILE_PATH):
            shutil.rmtree(FILE_PATH)

        for file_path in chatmsg["files"]:
            if not os.path.exists(FILE_PATH):
                os.mkdir(FILE_PATH)

            file_name = os.path.basename(file_path)
            shutil.copy(file_path, FILE_PATH)
            file_path = FILE_PATH + "\\" + file_name
            #print(file_path)
            chatbot.append(ChatMessage(role="user", content="<small><a href='/gradio_api/file=" + file_path + "' target='_blank'>" + file_name + "</a></small>"))
            #chatbot.append(ChatMessage(role="user", content={"path": file_path, "alt_text": file_name}))
            #chatbot.append(ChatMessage(role="user", content=FileData(path=file_path, orig_name=file_name)))

    else:
        _question = ""

    return chatbot, gr.MultimodalTextbox(value=None, interactive=False, submit_btn=False, stop_btn=True)


def vote(data: gr.LikeData):
    # print(data.value, data.index)
    if data.liked:
        print("You upvoted this response: " + data.value[0] + " : " + str(data.index))
    else:
        print("You downvoted this response: " + data.value[0] + " : " + str(data.index))

def reset_state():
    return [], [], "", "", ""

###################

def update_text(value):
    global _sys_prompt
    _sys_prompt=value
    # print("input:", _sys_prompt) 
    with open("customprompt.txt", "w") as file:
        file.write(value)


    return value



def main():
    global _sys_prompt
    file_name="customprompt.txt"

    model_root = APP_PATH + "models"
    model_list = [f for f in os.listdir(model_root) if os.path.isdir(os.path.join(model_root, f))]
    model_list.insert(0, "")

    with gr.Blocks(css=css, theme='davehornik/Tealy', fill_width=True, fill_height=True) as demo:
        demo.title = "Genie App"

        gr.set_static_paths(paths=["resources/", "files/"])
        gr.HTML("""<div align="center"><div style="width:500px"><font size="6" style="color:rgb(42, 42, 234);">Genie App</font></div></div>""")
        # gr.HTML("""<div align="center"><div style="width:500px"><img style="display: inline" src="/gradio_api/file=resources/icon.png"> <font size="6" style="color:rgb(42, 42, 234);"> Genie App</font>&nbsp;&nbsp;</div></div>""")

        with gr.Tab("Settings") as tab:
            with gr.Row():
                with gr.Column(scale=2):
                    with gr.Group():                     
                        model_select = gr.Dropdown(choices=model_list, value="", label="models", interactive=True)
                        max_length = gr.Slider(1, 4096, value=4096, step=1.0, label="max length", interactive=True)
                        temp = gr.Slider(0.01, 1, value=0.8, step=0.01, label="temperature", interactive=True)
                        top_k = gr.Slider(1, 100, value=40, step=1, label="Top K", interactive=True)
                        top_p = gr.Slider(0, 1, value=0.95, step=0.01, label="Top P", interactive=True)
                    with gr.Group():
                        f_latency = gr.Textbox(label="First Latency", visible=True)
                        p_speed = gr.Textbox(label="Prompt Speed", visible=True)
                        e_speed = gr.Textbox(label="Eval Speed", visible=True)
                        cust_prompt = gr.Textbox(label="Customer Prompt", value="分析单词的词源:", visible=True, interactive=True)

                with gr.Column(scale=8):
                    chatbot = gr.Chatbot(scale=9, type='messages', show_copy_button=True, group_consecutive_messages=True, height="52vh",)
                    chatbot.like(vote, None, None)

                    chatmsg = gr.MultimodalTextbox(scale=1, interactive=True, file_count="multiple", placeholder="Enter message or upload file...", show_label=True, autofocus=True,
                                                   max_plain_text_length=3000, sources=[],      # sources=["upload", "microphone"],
                                                   file_types=FILE_TYPES, label=FUNC_LIST[_func_mode])

                    with gr.Row():
                        # ["📐 解题答疑", "📚 文档总结", "🗛 AI 翻 译", "🌐 AI 搜 索", "✒️ 帮我写作", "🎨 图像生成", "定制功能", "✈️ 旅游规划"]
                        func_1_btn = gr.Button(FUNC_LIST[0], elem_classes="button_cls")
                        func_2_btn = gr.Button(FUNC_LIST[1], elem_classes="button_cls")
                        func_3_btn = gr.Button(FUNC_LIST[2], elem_classes="button_cls")
                        #func_4_btn = gr.Button(FUNC_LIST[3], elem_classes="button_cls")
                        #func_5_btn = gr.Button(FUNC_LIST[4], elem_classes="button_cls")
                        func_6_btn = gr.Button(FUNC_LIST[5], elem_classes="button_cls")
                        func_7_btn = gr.Button(FUNC_LIST[6], elem_classes="button_cls")
                        #func_8_btn = gr.Button(FUNC_LIST[7], elem_classes="button_cls")

                    gr.Examples(["分析单词的词源", "分析源代码，给出逐行注释", "帮我检查一下如下英语语法，如果有误，帮我修正：\n"], chatmsg, label="快捷输入")

        model_select.change(model_change, inputs=model_select)

        chat_run = chatmsg.submit(add_media, [chatbot, chatmsg], [chatbot, chatmsg])
        chat_run = chat_run.then(predict, [chatbot, max_length, temp, top_k, top_p], [chatbot, f_latency, p_speed, e_speed], show_progress=False)
        chat_run.then(lambda: gr.MultimodalTextbox(interactive=True, submit_btn=True, stop_btn=False), None, [chatmsg])

        chatmsg.stop(fn=stop)
 

        if not os.path.exists(file_name):
            with open(file_name, "w") as file:
                file.write("")  # Create an empty file



        with open(file_name, "r") as file:
            cust_prompt.value = file.read()

        cust_prompt.change(update_text, inputs=cust_prompt, outputs=None)
        _sys_prompt = cust_prompt.value

        def func_change(func_mode):
            global _func_mode
            global _sys_prompt

            _sys_prompt = cust_prompt.value
            print("\nchange:sys prompt:", _sys_prompt)

            _func_mode = func_mode
            func_name = FUNC_LIST[func_mode]
            if func_name == "📚 文档总结":
                return gr.MultimodalTextbox(label=func_name, sources=["upload"])
            else:
                return gr.MultimodalTextbox(label=func_name, sources=[])

        func_1_btn.click(lambda: func_change(0), None, [chatmsg])
        func_2_btn.click(lambda: func_change(1), None, [chatmsg])
        func_3_btn.click(lambda: func_change(2), None, [chatmsg])
        #func_4_btn.click(lambda: func_change(3), None, [chatmsg])
        #func_5_btn.click(lambda: func_change(4), None, [chatmsg])
        func_6_btn.click(lambda: func_change(5), None, [chatmsg])
        func_7_btn.click(lambda: func_change(6), None, [chatmsg])
        #func_8_btn.click(lambda: func_change(7), None, [chatmsg])

    demo.queue().launch(server_name=HOST, share=False, inbrowser=True, server_port=PORT)

if __name__ == '__main__':
    
    main()
