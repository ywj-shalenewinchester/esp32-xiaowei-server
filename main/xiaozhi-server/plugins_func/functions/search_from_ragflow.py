import json
import httpx
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 定义基础的函数描述模板
SEARCH_FROM_RAGFLOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "search_from_ragflow",
        "description": "从知识库中查询信息",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "查询的问题"}},
            "required": ["question"],
        },
    },
}


@register_function(
    "search_from_ragflow", SEARCH_FROM_RAGFLOW_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def search_from_ragflow(conn: "ConnectionHandler", question=None):
    # 确保字符串参数正确处理编码
    if question and isinstance(question, str):
        # 确保问题参数是UTF-8编码的字符串
        pass
    else:
        question = str(question) if question is not None else ""

    ragflow_config = conn.config.get("plugins", {}).get("search_from_ragflow", {})
    base_url = ragflow_config.get("base_url", "")
    api_key = ragflow_config.get("api_key", "")
    dataset_ids = ragflow_config.get("dataset_ids", [])

    url = base_url + "/api/v1/retrieval"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # 确保payload中的字符串都是UTF-8编码
    # RAGFlow 的多数据集统一检索要求所有库的 embedding 模型一致(否则返回 102：
    # "Datasets use different embedding models")。智控台常把一个 agent 绑到多个
    # 不同 embedding 的知识库上，因此这里按数据集逐个检索、合并结果，不同
    # embedding 模型的知识库也能一起命中。
    if not dataset_ids:
        return ActionResponse(
            Action.RESPONSE,
            None,
            "知识库未配置数据集（dataset_ids 为空），请先在管理端绑定知识库",
        )

    contents = []
    api_error = None
    try:
        # 使用ensure_ascii=False确保JSON序列化时正确处理中文
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0), verify=False) as client:
            for ds_id in dataset_ids:
                payload = {"question": question, "dataset_ids": [ds_id]}
                response = await client.post(url, json=payload, headers=headers)

                # 显式设置响应的编码为utf-8
                response.encoding = "utf-8"

                response.raise_for_status()

                # 先获取文本内容，然后手动处理JSON解码
                result = json.loads(response.text)

                if result.get("code") != 0:
                    error_detail = result.get("error", {}).get("detail", "未知错误")
                    error_message = result.get("error", {}).get("message", "")
                    error_code = result.get("code", "")

                    # 安全地记录错误信息，继续检索其余数据集
                    logger.bind(tag=TAG).error(
                        f"RAGFlow 数据集 {ds_id} 检索失败，响应码：{error_code}，错误详情：{error_detail}，完整响应：{result}"
                    )
                    if api_error is None:
                        # 构建详细的错误响应
                        api_error = f"RAG接口返回异常（错误码：{error_code}）"
                        if error_message:
                            api_error += f"：{error_message}"
                        if error_detail:
                            api_error += f"\n详情：{error_detail}"
                    continue

                chunks = result.get("data", {}).get("chunks", [])
                for chunk in chunks:
                    content = chunk.get("content", "")
                    if content:
                        # 安全地处理内容字符串
                        if isinstance(content, str):
                            contents.append(content)
                        elif isinstance(content, bytes):
                            contents.append(content.decode("utf-8", errors="replace"))
                        else:
                            contents.append(str(content))

        if contents:
            # 组织知识库内容为引用模式
            context_text = f"# 关于问题【{question}】查到知识库如下\n"
            context_text += "```\n\n\n".join(contents[:5])
            context_text += "\n```"
        elif api_error:
            return ActionResponse(Action.RESPONSE, None, api_error)
        else:
            context_text = "根据知识库查询结果，没有相关信息。"
        return ActionResponse(Action.REQLLM, context_text, None)

    except httpx.TimeoutException as e:
        error_response = "RAG接口请求超时"
        error_response += "\n可能原因：RAGflow服务响应缓慢或网络延迟"
        error_response += "\n解决方案：请稍后重试或检查RAGflow服务性能"
        return ActionResponse(Action.RESPONSE, None, error_response)

    except httpx.HTTPStatusError as e:
        if hasattr(e.response, "status_code"):
            status_code = e.response.status_code
            error_response = f"RAG接口HTTP错误（状态码：{status_code}）"
            try:
                error_detail = e.response.json().get("error", {}).get("message", "")
                if error_detail:
                    error_response += f"\n错误详情：{error_detail}"
            except:
                pass
        else:
            error_response = f"RAG接口HTTP异常：{str(e)}"
        return ActionResponse(Action.RESPONSE, None, error_response)

    except httpx.HTTPError as e:
        error_response = "无法连接到RAG接口"
        error_response += "\n可能原因：RAGflow服务地址错误或服务未运行"
        error_response += "\n解决方案：请检查RAGflow服务地址配置和服务状态"
        return ActionResponse(Action.RESPONSE, None, error_response)

    except Exception as e:
        # 其他异常
        error_type = type(e).__name__
        logger.bind(tag=TAG).error(
            f"RAGflow处理异常，异常类型：{error_type}，详情：{str(e)}"
        )

        # 提供详细的错误信息
        error_response = f"RAG接口处理异常（{error_type}）：{str(e)}"
        return ActionResponse(Action.RESPONSE, None, error_response)
