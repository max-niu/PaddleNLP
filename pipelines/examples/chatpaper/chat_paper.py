# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import os
import re

import arxiv
import erniebot as eb
import gradio as gr
from utils import (
    _apply_token,
    load_all_json_path,
    merge_summary,
    pdf2image,
    retrieval,
    summarize_abstract,
    tackle_history,
    translate_part,
)

parser = argparse.ArgumentParser()
parser.add_argument("--api_type", type=str, default="qianfan")
parser.add_argument("--api_key", type=str, default="", help="The API Key.")
parser.add_argument("--secret_key", type=str, default="", help="The secret key.")
parser.add_argument("--bos_ak", type=str, default="", help="The Access Token for uploading files to bos")
parser.add_argument("--bos_sk", type=str, default="", help="The Secret Token for uploading files to bos")
parser.add_argument(
    "--top_p",
    type=float,
    default=0.7,
    help="The range is between 0 and 1.The smaller the parameter, the more stable the generated result. When it is 0, randomness is minimized",
)
parser.add_argument(
    "--temperature", type=float, default=0.95, help="The smaller the parameter, the more stable the generated result"
)
parser.add_argument("--max_length", type=int, default=1024, help="Maximum number of generated tokens")
parser.add_argument("--ernie_model", type=str, default="ernie-bot-3.5", help="Model type")
parser.add_argument("--system_prompt", type=str, default="你是我的AI助理。", help="System settings for dialogue models")
parser.add_argument("--es_host", type=str, default="", help="the host of es")
parser.add_argument("--es_port", type=int, default=8309, help="the port of es")
parser.add_argument("--es_username", type=str, default="", help="the username of es")
parser.add_argument("--es_password", type=str, default="", help="the password of es")
parser.add_argument("--es_index_abstract", type=str, default="", help="the index of abstracts")
parser.add_argument("--es_index_full_text", type=str, default="", help="the index of all papers")
parser.add_argument("--es_chunk_size", type=int, default=500, help="the size of chunk in es")
parser.add_argument("--es_thread_count", type=int, default=30, help="the thread count in es")
parser.add_argument("--es_queue_size", type=int, default=30, help="the size of queue in es")
parser.add_argument("--retriever_batch_size", type=int, default=16, help="the batch size of retriever ")
parser.add_argument("--retriever_api_key", type=str, default="", help="the api key of retriever")
parser.add_argument("--retriever_secret_key", type=str, default="", help="the secret key of retriever")
parser.add_argument(
    "--retriever_embed_title", type=bool, default=False, help="whether use embedding title in retriever"
)
parser.add_argument("--retriever_threshold", type=float, default=0.95, help="the threshold of retriever")
parser.add_argument("--txt_file", type=str, default="", help="the path of a txt file which includes all papers path")
parser.add_argument("--max_token", type=int, default=11200, help=" the max number of tokens of LLM")
parser.add_argument("--translation_chunk_size", type=int, default=300, help="the chunk size of translation")
parser.add_argument("--translation_cycle_num", type=int, default=3, help="fault tolerance times")
parser.add_argument(
    "--translation_max_token", type=int, default=500, help="the max number of tokens of translation segment"
)
args = parser.parse_args()
PROMPT_SYSTEM = """
你现在需要一步步执行下面的操作
你需要先完成关键句抽取任务，从背景信息中抽取与输入问题相关的关键句，
并输出信关键句抽取任务的结果,关键句需要按照1,2,3,...序号编号，然后你需要基于抽取的内容完成问答任务,回答输出问题。
请记住你输出的格式是一个json格式的字符串。
json有两个key值，一个是"关键句抽取任务的结果",对应的value是关键句需要按照1,2,3,...序号编号后的结果，第二个是"问答任务的结果",对应的value是问答任务的结果。
输出格式如下:
```
json{'关键句抽取任务的结果':'1.关键句1 \n2.关键句1\n...','问答任务的结果':''}
```
"""
PROMPT_RETRIVER = """
现在我给你背景信息和问题：
背景信息：{documents}
输入问题：{query}
根据背景信息，来完成关键句抽取任务和问答任务。
请记住你需要先执行关键句抽取任务，再执行问答任务。你的输出格式需要是一个json格式的字符串。
"""
PROMPT_RETRIVER_MUL = """
根据背景信息，简洁和专业的来回答问题。如果无法从中得到答案，
请说 “根据已知信息无法回答该问题”，不允许在答案中添加编造成分，答案请使用中文。
背景信息：{documents}
问题：{query}
"""
PROMPT_PROBLEM = """
给你一篇论文的标题和关键词，请你给出一些用户可能针对这篇论文进行问答的问题，问题的数量不要超过3个。
论文的标题：{title}
论文的关键词：{key_words}
问题："""
all_json_id = load_all_json_path(args.txt_file)
eb.api_type = args.api_type
access_token = _apply_token(args.api_key, args.secret_key)
eb.access_token = access_token
model = "ernie-bot-3.5" if args.ernie_model is None or args.ernie_model.strip() == "" else args.ernie_model


def retrieval_papers(history=[]):
    """
    Retrieve papers
    """
    query = history.pop()[0]
    query = query.strip().replace("<br>", "\n")
    context = tackle_history(history)
    if query:
        if len(history) == 1:
            paper_id_list = []
            context.append({"role": "user", "content": query})
            prediction = retrieval(
                query=query,
                es_host=args.es_host,
                es_port=args.es_port,
                es_username=args.es_username,
                es_password=args.es_password,
                es_index=args.es_index_abstract,
                es_chunk_size=args.es_chunk_size,
                es_thread_count=args.es_thread_count,
                es_queue_size=args.es_queue_size,
                retriever_batch_size=args.retriever_batch_size,
                retriever_api_key=args.retriever_api_key,
                retriever_secret_key=args.retriever_secret_key,
                retriever_embed_title=args.retriever_embed_title,
                retriever_topk=30,
                rank_topk=3,
            )
            documents = prediction["documents"]
            all_content = ""
            papers_absatract = []
            for i in range(len(documents)):
                if documents[i].meta["id"] not in paper_id_list:
                    paper_id_list.append(documents[i].meta["id"])
                    key_words = documents[i].meta.get("key_words", "")
                    title = documents[i].meta.get("title", "")
                    abstract = documents[i].meta.get("abstracts", "")
                    abstract = summarize_abstract(
                        abstract,
                        api_key=args.api_key,
                        secret_key=args.secret_key,
                        chunk_size=500,
                        max_token=args.max_token,
                    )
                    papers_absatract.append({"content": abstract, "meta": {}})
                    paper_content = (
                        "**" + str(len(paper_id_list)) + "." + title + "**" + "\n" + key_words + "\n" + abstract
                    )
                    all_content += paper_content + "\n\n"
            history.append(["下面请基于这几篇论文进行问答，单篇文档问答请使用单篇问答精读翻译", ",".join(paper_id_list)])
            confine_summary = merge_summary(papers_absatract, api_key=args.api_key, secret_key=args.secret_key)
            confine_summary = "**下面是对上面几篇文档进行的总结**" + "\n" + confine_summary
            confine_summary = confine_summary.replace("\n\n", "\n")
            history.append([query, all_content + confine_summary])
        else:
            # history = [[user_msg(None),system_msg],[user_hint(None),paper_id]]
            paper_id_list = history[1][1].split(",")
            content = ""
            for id in paper_id_list:
                prediction = retrieval(
                    query=query,
                    file_id=id,
                    es_host=args.es_host,
                    es_port=args.es_port,
                    es_username=args.es_username,
                    es_password=args.es_password,
                    es_index=args.es_index_full_text,
                    es_chunk_size=args.es_chunk_size,
                    es_thread_count=args.es_thread_count,
                    es_queue_size=args.es_queue_size,
                    retriever_batch_size=args.retriever_batch_size,
                    retriever_api_key=args.retriever_api_key,
                    retriever_secret_key=args.retriever_secret_key,
                    retriever_embed_title=args.retriever_embed_title,
                    retriever_topk=30,
                    rank_topk=2,
                )
                content += "\n".join([item.content for item in prediction["documents"]])
            content = PROMPT_RETRIVER_MUL.format(documents=content, query=query)
            content = content[: args.max_token]
            context.append({"role": "user", "content": content})
            eb.api_type = args.api_type
            access_token = _apply_token(args.api_key, args.secret_key)
            eb.access_token = access_token
            model = "ernie-bot-3.5" if args.ernie_model is None or args.ernie_model.strip() == "" else args.ernie_model
            response = eb.ChatCompletion.create(model=model, messages=context, stream=False)
            bot_response = response.result
            history.append([query, bot_response])
    return history


def retrieval_title(title):
    """
    Retrieve the paper_id  of  the title
    """
    prediction = retrieval(
        title,
        es_host=args.es_host,
        es_port=args.es_port,
        es_username=args.es_username,
        es_password=args.es_password,
        es_index=args.es_index_abstract,
        es_chunk_size=args.es_chunk_size,
        es_thread_count=args.es_thread_count,
        es_queue_size=args.es_queue_size,
        retriever_batch_size=args.retriever_batch_size,
        retriever_api_key=args.retriever_api_key,
        retriever_secret_key=args.retriever_secret_key,
        retriever_embed_title=args.retriever_embed_title,
        retriever_topk=30,
        rank_topk=1,
    )
    if prediction["documents"][0].rank_score > args.retriever_threshold:
        return prediction["documents"][0].meta["full_path"], prediction["documents"][0].meta["id"]
    return None


def infer(history=[]):
    """Model inference."""
    query = history.pop()[0]
    query = query.strip().replace("<br>", "\n")
    context = tackle_history(history)
    single_paper_id = history[1][1]
    if query:
        if single_paper_id:
            prediction = retrieval(
                query=query,
                file_id=single_paper_id,
                es_host=args.es_host,
                es_port=args.es_port,
                es_username=args.es_username,
                es_password=args.es_password,
                es_index=args.es_index_full_text,
                es_chunk_size=args.es_chunk_size,
                es_thread_count=args.es_thread_count,
                es_queue_size=args.es_queue_size,
                retriever_batch_size=args.retriever_batch_size,
                retriever_api_key=args.retriever_api_key,
                retriever_secret_key=args.retriever_secret_key,
                retriever_embed_title=args.retriever_embed_title,
                retriever_topk=30,
                rank_topk=2,
            )
            content = "\n".join([item.content for item in prediction["documents"]])
            content = PROMPT_SYSTEM + PROMPT_RETRIVER.format(documents=content, query=query)
            content = content[: args.max_token]
            context.append({"role": "user", "content": content})
            response = eb.ChatCompletion.create(model=model, messages=context, stream=False)
            bot_response = response.result
            try:
                bot_response = bot_response[bot_response.find("{") :]
                bot_response = bot_response[: bot_response.find("}") + 1]
                bot_response = json.loads(bot_response)
                if type(bot_response["关键句抽取任务的结果"]) == list:
                    bot_response["关键句抽取任务的结果"] = "\n".join(bot_response["关键句抽取任务的结果"])
                bot_response = (
                    "以下是我的分析内容：\n"
                    + str(bot_response["关键句抽取任务的结果"])
                    + "\n\n"
                    + "以下是我的总结："
                    + str(bot_response["问答任务的结果"])
                )
            except:
                bot_response = (
                    str(bot_response).replace("'关键句抽取任务的结果':", "以下是我的分析内容").replace("'问答任务的结果':", "\n以下是我的总结\n")
                )
            bot_response = re.sub(r"\[|\]|{|}", "", bot_response)
            bot_response = bot_response.replace("\\n", "\n")
            history.append([query, bot_response])
        else:
            context.append({"role": "user", "content": query})
            response = eb.ChatFile.create(messages=context, stream=False)
            bot_response = response.result
            history.append([query, bot_response])
    return history


def upload_file(file_name, file_url, file_upload, history=[]):
    """
    Upload the file to bos or retrieve the json_file of the paper
    """
    if file_name:
        try:
            json_file_path, file_id = retrieval_title(file_name)
            json_file_path = json_file_path.replace("/", "_").replace(".pdf", "")
            json_file_path = all_json_id[json_file_path]
            with open(json_file_path, mode="r") as json_file:
                json_content = json.load(json_file)
            content = json_content["content"]
            title = json_content["标题"]
            key_words = json_content["关键词"]
            response = eb.ChatCompletion.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT_PROBLEM.format(title=title, key_words=key_words)}],
                stream=False,
            )
            response = response.result
            history.append([None, file_id])
            history.append(["你可以参考以下问题，对论文进行提问", response])
        except:
            content = "这篇论文目前尚未加入到论文库中,请你自行上传论文的pdf或者url链接."
            file_id = None
            history.append([None, file_id])
        return (
            gr.Gallery.update(visible=False),
            gr.File.update(visible=False),
            history,
            gr.Chatbot.update(
                [[None, content]],
                visible=True,
                scale=30,
                height=600,
            ),
        )
    elif file_url:
        root_path = "./"
        paper = next(arxiv.Search(id_list=[file_url.split("/")[-1]]).results())
        real_filename = "{}.pdf".format(file_url.split("/")[-1])
        paper.download_pdf(dirpath=root_path, filename=real_filename)
        file_name = os.path.join(root_path, real_filename)
        imgs = pdf2image(pdfPath=file_name, imgPath=root_path)
    elif file_upload:
        file_name = file_upload.name
        real_filename = os.path.split(file_name)[-1]
        root_path = os.path.dirname(file_name)
        imgs = pdf2image(pdfPath=file_name, imgPath=root_path)
    # 上传到bos后到文件是否需要删除
    filename_in_bos = real_filename
    url = eb.utils.upload_file_to_bos(
        file_name, filename_in_bos, access_key_id=args.bos_ak, secret_access_key=args.bos_sk
    )
    history.append([None, None])
    content = "<file>{}</file><url>{}</url>".format(real_filename, url)
    content = content.strip().replace("<br>", "\n")
    context = tackle_history(history)
    context.append({"role": "user", "content": content})
    response = eb.ChatFile.create(messages=context, stream=False)
    bot_response = response.result
    history.append([content, bot_response])
    return (
        gr.Gallery.update(imgs, visible=True),
        gr.File.update(file_name, label="原文下载链接", visible=True),
        history,
        gr.Chatbot.update(visible=False),
    )


def add_messaget_chatbot(messages, history):
    history.append([messages, None])
    return None, history


def translation_txt(history=[], lang=""):
    if not lang:
        lang = "中文"
    message = history.pop()[0]
    if message:
        translation_content = translate_part(
            text=message,
            api_key=args.api_key,
            secret_key=args.secret_key,
            task="翻译",
            max_length=args.translation_max_token,
            lang=lang,
            chunk_size=args.translation_chunk_size,
            cycle_num=args.translation_cycle_num,
        )
        history.append([message, translation_content])
    return history


with gr.Blocks(title="维普小助手", theme=gr.themes.Base()) as demo:
    gr.HTML("""<h1 align="center">ChatPaper维普小助手</h1>""")
    with gr.Row(variant="panel"):
        # with gr.Column(scale=1):
        #     #cheetah = os.path.join(os.path.dirname(__file__), "weipu.jpeg")
        #     #gr.Image(cheetah, elem_id="banner-image", show_label=False, show_download_button=False)
        with gr.Column(scale=9):
            gr.HTML(
                """
                <p>【文章检索摘要】
                <p> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;1.适合从大量文章中，粗力度获取需要的信息。返回包含 : 文章题目+作者+关键词+术语+摘要.</p>
                <p> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;2.适合基于某个技术领域，生成对应的技术综述.</p>
                <p>【单篇精读翻译】：
                <p> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;1.适合针对具体一篇文章，详细了解细节内容.</p>
                <p> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;2.适合针对具体一篇文章，详细翻译摘要术语.</p>
            """
            )
    with gr.Tab("文章检索摘要"):
        retrieval_chatbot = gr.Chatbot(
            height=600, value=[[None, "你好, 我是维普Chatpaper小助手, 我这里收录了100w篇论文,可以提供您专业的学术咨询.请问有什么可以帮您的吗?"]]
        )  # height聊天框高度, value 默认语句
        retrieval_textbox = gr.Textbox(placeholder="最近自监督学习论文有哪些?")
        with gr.Row():
            retrieval_submit_btn = gr.Button("🚀 提交", variant="primary", scale=2, min_width=0)
            retrieval_clear_btn = gr.Button("清除", variant="primary", scale=2, min_width=0)
    retrieval_submit_btn.click(
        add_messaget_chatbot,
        inputs=[retrieval_textbox, retrieval_chatbot],
        outputs=[retrieval_textbox, retrieval_chatbot],
    ).then(retrieval_papers, retrieval_chatbot, retrieval_chatbot)
    retrieval_clear_btn.click(
        lambda _: ([[None, "你好, 我是维普Chatpaper文章精读翻译小助手,可以提供您专业的学术咨询.请问有什么可以帮您的吗?"]]),
        inputs=[retrieval_clear_btn],
        outputs=[retrieval_chatbot],
    )
    with gr.Tab("单篇精读"):  # 封装chatFile的能力
        with gr.Accordion("文章精读：输入区（输入方式三选一，三种输入方式优先级依次降低）", open=True, elem_id="input-panel") as area_input_primary:
            with gr.Row():
                with gr.Group():
                    file_name = gr.Textbox(
                        label="(输入方式1) 论文/期刊标题（仅支持单篇文章精读）",
                        value="",
                        placeholder="Human-level control through deep reinforcement learning",
                        interactive=True,
                        scale=1,
                    )
                    file_url = gr.Textbox(
                        label="(输入方式2) 论文 axiv链接（仅支持单篇文章精读）",
                        value="",
                        placeholder="https://arxiv.org/abs/2303.08774",
                        interactive=True,
                        scale=1,
                    )
                file_upload = gr.File(
                    label="(输入方式3) 上传论文/期刊PDF(仅支持单篇PDF精读)", file_count="single", height=180, min_width=50
                )
            with gr.Row():
                clear = gr.Button(value="清空输入区")
                submit = gr.Button(value="全文精读")
        with gr.Accordion("文章精读：输出区", open=True, elem_id="input-panel") as area_input_primary:
            with gr.Tab("单文解读"):  # 包含下载功能
                with gr.Row():
                    with gr.Column():
                        gr.Dropdown(choices=[""], max_choices=1, label="论文原文-PDF插件-支持下载；此处为PDF占位符")
                        ori_paper = gr.Gallery(label="论文原文", show_label=False, elem_id="gallery").style(
                            columns=[1], rows=[1], object_fit="contain", height="700px"
                        )
                        ori_json = gr.Chatbot(label="论文原文", visible=False)
                        ori_pdf = gr.File(label="原文下载链接")
                    with gr.Accordion("   "):
                        gr.Dropdown(choices=[""], max_choices=1, label="文章摘要等总结-PDF插件-支持下载；此处为PDF占位符")
                        chatbot = gr.Chatbot(
                            value=[[None, "你好, 我是维普Chatpaper文章精读翻译小助手,可以提供您专业的学术咨询.请问有什么可以帮您的吗?"]],
                            scale=30,
                            height=600,
                        )
                        message = gr.Textbox(placeholder="请问具体描述这篇文章的方法?", scale=7)
                        with gr.Row():
                            submit_btn = gr.Button("🚀 提交", variant="primary", scale=2, min_width=0)
                            clear_btn = gr.Button("清除", variant="primary", scale=2, min_width=0)
                submit.click(
                    upload_file,
                    inputs=[file_name, file_url, file_upload, chatbot],
                    outputs=[ori_paper, ori_pdf, chatbot, ori_json],
                )
                clear.click(
                    lambda _: ("", "", None, [[None, "你好, 我是维普Chatpaper文章精读翻译小助手,可以提供您专业的学术咨询.请问有什么可以帮您的吗?"]]),
                    inputs=[],
                    outputs=[file_name, file_url, file_upload, chatbot],
                )
                submit_btn.click(add_messaget_chatbot, inputs=[message, chatbot], outputs=[message, chatbot]).then(
                    infer, chatbot, chatbot
                )
                clear_btn.click(
                    lambda _: ([[None, "你好, 我是维普Chatpaper文章精读翻译小助手,可以提供您专业的学术咨询.请问有什么可以帮您的吗?"]]),
                    inputs=clear_btn,
                    outputs=[chatbot],
                    api_name="clear",
                    show_progress=False,
                )
    with gr.Tab("翻译"):
        with gr.Column():
            chatbot_translation = gr.Chatbot(value=[[None, "你好, 我是翻译小助手"]], scale=35, height=500)
            message_translation = gr.Textbox(placeholder="请输出需要翻译的内容", lines=5, max_lines=20)
            with gr.Row():
                lang = gr.Radio(choices=["中文", "英文"], max_choices=1, scale=1, value="中文", label="输入语言")
                submit_translation = gr.Button("🚀 提交", variant="primary", scale=1)
                clear_translation = gr.Button("清除", variant="primary", scale=1)
        submit_translation.click(
            add_messaget_chatbot,
            inputs=[message_translation, chatbot_translation],
            outputs=[message_translation, chatbot_translation],
        ).then(translation_txt, inputs=[chatbot_translation, lang], outputs=[chatbot_translation])
        clear_translation.click(
            lambda _: ([[None, "你好, 你好, 我是翻译小助手"]]), inputs=[clear_translation], outputs=[chatbot_translation]
        )
demo.queue(concurrency_count=40, max_size=40)
demo.launch(server_name="0.0.0.0", server_port=8084)
