"""三级记忆漏斗（多用户版）：
第一级：基因记忆/人设 → 走 config.yaml 的 prompt 字段（不在此处）
第二级：阶段摘要 → summary.json，按 role_id 隔离
第三级：长效碎片 + 关键词召回 → vector_memories.json，按 role_id 隔离
"""
import json
import os

from ..base import MemoryProviderBase, logger
from config.config_loader import get_project_dir

TAG = __name__

EXPECTED_SUMMARY_KEYS = ("user_profile", "relationship_stage", "recent_events")

# 西南风专属关键词触发网
KEYWORDS_MAP = {
    "武汉": ["武汉", "黄鹤楼", "江滩", "热干面", "灯光秀", "旅行"],
    "上海": ["上海", "外滩", "国赛", "行李", "拿奖", "国二", "拥抱"],
    "校园生活": ["包子", "豆浆", "外卖", "操场", "小树林", "图书馆", "胃"],
    "实验室与硬件": ["实验室", "板子", "电阻", "烙铁", "代码", "RV1126", "FPGA", "Bug", "比赛", "熬夜"],
    "饮食与习惯": ["烤匠", "香菜", "豆芽", "三分糖", "果茶", "海底捞", "夜宵", "蛋糕"],
    "游戏双排": ["王者", "安琪拉", "蔡文姬", "打野", "连麦", "亲密关系"],
    "细节与礼物": ["围巾", "开发板", "口红", "牵手", "三周年", "冷战"],
    "重庆与复合": ["重庆", "实习", "机器人", "分红", "好好生活", "复合", "阿姨", "妈妈"],
}

# 西南风初始画像（新设备第一次创建时写入）
DEFAULT_SUMMARY = {
    "user_profile": "西南风，理工男，常泡实验室搞嵌入式（RV1126/FPGA），忙起来不吃早饭、作息不规律，胃不好。",
    "relationship_stage": "与前任相恋三年后因家庭反对分手，现在是保持界限感的老友关系",
    "recent_events": "无特别事件",
}

# 记忆分析师提示词（源自 ai_demo 的 summarize_memory）
SUMMARY_PROMPT = """你是一个记忆分析师。请根据最新的对话内容，用【中文】增量更新用户的记忆摘要。

【角色背景，务必记住】：
- 用户叫"西南风"，是个理工男。
- 西南风有一位前女友，对话里一律用"她"指代，不要写出她的名字。带早饭、陪实验室、织围巾等温柔举动都是【她】做的，绝不是
AI 助手做的。
- AI 助手叫"小雨"，是用户的 AI 陪伴者/朋友，不是情感专家，也不是当事人，千万别把她写成"助理"或"前女友"。

必须返回一个 JSON 对象（不要 Markdown 标记），包含以下三个字段，每个字段的值都必须是【纯文本字符串】，禁止嵌套 JSON
对象：
{
  "user_profile": "用户画像：一两句话概括（身份、性格、喜好等）",
  "relationship_stage": "西南风与前女友当前的关系阶段（一句话）",
  "recent_events": "最近一次聊到的关键事件（一句话，不超过 40 字）"
}

硬性要求：
1. 全程使用中文输出。
2. 三个字段都必须是一句紧凑的话，禁止写成长段落。
3. recent_events 只保留【最新】一件事，用新事件替换旧事件，不要用分号不断堆砌，控制在 40 字以内。
4. 在上一条历史摘要的基础上增量更新，保留仍有价值的信息，不要丢字段、不要越写越长。
"""


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config)
        self.data_dir = get_project_dir() + "data/"
        os.makedirs(self.data_dir, exist_ok=True)
        self.summary_file = os.path.join(self.data_dir, "summary.json")
        self.vector_store_file = os.path.join(self.data_dir, "vector_memories.json")

    def init_memory(self, role_id, llm, summary_memory=None, **kwargs):
        super().init_memory(role_id, llm, **kwargs)  # 保存 self.role_id 和 self.llm
        self._init_storage()

    # ---------- 存储（按 role_id 隔离） ----------
    def _load_json(self, path, default):
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return default

    def _save_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _init_storage(self):
        if not self.role_id:
            return
        summary_all = self._load_json(self.summary_file, {})
        if self.role_id not in summary_all:
            summary_all[self.role_id] = dict(DEFAULT_SUMMARY)
            self._save_json(self.summary_file, summary_all)
        vec_all = self._load_json(self.vector_store_file, {})
        if self.role_id not in vec_all:
            vec_all[self.role_id] = []
            self._save_json(self.vector_store_file, vec_all)

    # ---------- 第二级：阶段摘要 ----------
    def _get_summary(self):
        summary_all = self._load_json(self.summary_file, {})
        return summary_all.get(self.role_id, dict(DEFAULT_SUMMARY))

    def get_summary_memory(self):
        data = self._get_summary()
        return (
            f"- 西南风的画像：{data.get('user_profile', '')}\n"
            f"- 他的情感现状（与前任的关系）：{data.get('relationship_stage', '')}\n"
            f"- 最近一次交集：{data.get('recent_events', '')}"
        )

    @staticmethod
    def _merge_summary(old, new):
        merged = dict(old)
        for key in EXPECTED_SUMMARY_KEYS:
            val = new.get(key)
            if isinstance(val, str):
                val = val.strip()
            elif isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            elif val is not None:
                val = str(val)
            else:
                val = ""
            if val:
                merged[key] = val
        if len(merged.get("recent_events", "")) > 60:
            merged["recent_events"] = merged["recent_events"][:60]
        return merged

    @staticmethod
    def _parse_json_object(raw_text):
        t = (raw_text or "").strip()
        if "```json" in t:
            t = t.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in t:
            t = t.split("```", 1)[1].split("```", 1)[0].strip()
        data = json.loads(t)
        if not isinstance(data, dict):
            raise ValueError("摘要内容必须是 JSON 对象")
        return data

    # ---------- 第三级：长效碎片 + 关键词召回 ----------
    def _get_fragments(self):
        vec_all = self._load_json(self.vector_store_file, {})
        return vec_all.get(self.role_id, [])

    def recall_relevant_memories(self, query):
        memories = self._get_fragments()
        matched = []
        for mem in memories:
            for kw_list in KEYWORDS_MAP.values():
                if any(kw in query for kw in kw_list) and any(kw in mem for kw in kw_list):
                    matched.append(f"• {mem}")
                    break
        if matched:
            return "\n".join(matched)
        return "（未触发特定的具象回忆片段）"

    # ---------- 框架接口 ----------
    async def save_memory(self, msgs, session_id=None):
        if self.llm is None or len(msgs) < 2:
            return None

        lines = []
        for msg in msgs:
            content = msg.content or ""
            if content.strip().startswith("{") and content.strip().endswith("}"):
                try:
                    data = json.loads(content)
                    if "content" in data:
                        content = data["content"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            if msg.role == "user":
                lines.append(f"User: {content}")
            elif msg.role == "assistant":
                lines.append(f"Assistant: {content}")

        conversation_text = "\n".join(lines)
        history_text = json.dumps(self._get_summary(), ensure_ascii=False)
        user_prompt = f"历史摘要：\n{history_text}\n\n最新对话：\n{conversation_text}"

        try:
            result = self.llm.response_no_stream(
                SUMMARY_PROMPT, user_prompt, max_tokens=800, temperature=0.3
            )
            new_summary = self._parse_json_object(result)
            merged = self._merge_summary(self._get_summary(), new_summary)
            summary_all = self._load_json(self.summary_file, {})
            summary_all[self.role_id] = merged
            self._save_json(self.summary_file, summary_all)
            logger.bind(tag=TAG).info("阶段摘要已增量更新")
            # 可选增强：从最新对话抽取一条高价值的长效碎片并追加到 vector store
            try:
                FRAGMENT_PROMPT = (
                    "请从下面的最新对话中抽取一条最具记忆价值的具体碎片，" 
                    "用中文输出，控制在40字以内，不要Markdown或说明，只返回纯文本碎片。\n\n" 
                )
                frag_result = self.llm.response_no_stream(
                    FRAGMENT_PROMPT, conversation_text, max_tokens=120, temperature=0.2
                )
                frag = (frag_result or "").strip()
                if frag:
                    vec_all = self._load_json(self.vector_store_file, {})
                    lst = vec_all.get(self.role_id, [])
                    # 去重并限制长度
                    if frag not in lst:
                        lst.insert(0, frag)
                        # 保持列表长度不超过200
                        vec_all[self.role_id] = lst[:200]
                        self._save_json(self.vector_store_file, vec_all)
                        logger.bind(tag=TAG).info("已追加长效碎片到 vector store")
            except Exception:
                # 不影响主流程，失败则忽略
                pass
            return merged
        except Exception as e:
            logger.bind(tag=TAG).error(f"更新摘要失败，保留旧摘要: {e}")
            return self._get_summary()

    async def query_memory(self, query: str) -> str:
        return (
            "=== 西南风当下的状态与你们的现状 ===\n"
            f"{self.get_summary_memory()}\n\n"
            "=== 听到西南风的话，你想起的过去三年碎片 ===\n"
            f"{self.recall_relevant_memories(query)}"
        )
