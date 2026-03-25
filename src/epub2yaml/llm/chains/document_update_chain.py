from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from epub2yaml.domain.models import ChapterBatch

DEFAULT_SYSTEM_PROMPT = """你是一个负责维护小说 YAML 知识库的分析助手。
你的任务是读取当前批次章节正文，并基于上一版 actors/worldinfo 文档输出增量 Delta YAML。
输出必须只包含 YAML，不要添加解释、代码围栏或额外注释。"""

DEFAULT_FORMAT_RULES = """输出规则：
1. 顶层必须是 delta 映射。
2. 允许包含 delta.actors 与 delta.worldinfo 两个键。
3. 仅输出当前批次发生变化的角色或设定。
4. 对于发生变化的角色或设定，必须输出该条目的完整内容，不能只输出局部字段。
5. 字典字段按深层覆盖语义更新，列表字段按整字段替换语义输出。
6. actors 中禁止出现“略”“同上”“待补”“当前”“可能”“不明”“未提及”“未知”等无意义或不确定值；没有信息就省略字段。
7. 时间表述必须使用可定位、可落盘的上帝视角描述，不得使用“现在”“刚刚”“前几天”“昨天”等相对时间。
8. worldinfo.content 可以是字符串，也可以是自定义映射结构；如某设定发生变化，输出完整设定条目。
9. scenario 已经在其他阶段处理过，本次不要输出 scenario，只从 actors / worldinfo 开始输出。"""

DEFAULT_SCHEMA_DEFINITIONS = """结构定义（仅在对应角色/设定发生变化时输出完整条目）：

actors:
  <Character Name (English only)>:
    name:
      ja: <日文名>
      zh-CN: <中文名>
      romaji: <罗马音>
    trigger_keywords:
      - <关键词>
    basic_settings:
      gender: <性别>
      age: <年龄>
      birthday: <出生日期>
      identity:
        - <身份>
      character_brief_description:
        - <角色简述>
    appearance:
      overview:
        - <外观概述>
      physical_details:
        height: <身高>
        weight: <体重>
        build: <体型>
        measurements: <三围>
        physical_quirks:
          - <身体特征或习惯>
        eyes:
          color: <瞳色>
          shape: <眼型>
          special_features:
            - <眼部特征>
        hair:
          color: <发色>
          length: <长度>
          texture: <发质>
          style:
            - <发型描述>
        facial_features:
          structure:
            - <脸部结构>
          complexion: <肤色/气色>
      body_details:
        skin: <皮肤>
        muscle_tone: <肌肉线条>
        posture: <姿态>
        scars_markings_and_origin: <伤痕/纹身/标记及来源>
      attires:
        notes: <穿衣说明>
        outfits:
          - outfit_type: <服装类型>
            tops: <上装>
            bottoms: <下装>
            shoes: <鞋子>
            socks: <袜子>
            accessories: <配饰>
    personality_core:
      mbti_type: <MBTI>
      personal_traits:
        - trait_name: <人格特质>
          scope: <适用范围>
          manifestations:
            - <行为表现>
          dialogue_examples:
            - cue: <触发情境>
              response: <原文台词>
      internal_conflicts:
        - conflict_name: <内在冲突>
          scope: <适用范围>
          manifestations:
            - <冲突表现>
          dialogue_examples:
            - cue: <触发情境>
              response: <原文台词>
      motivations:
        short_term_goals: <短期目标>
        long_term_goals: <长期目标>
        ultimately_desired_goal: <终极目标>
      likes:
        - <喜好>
      dislikes:
        - <厌恶>
      fan_tropes:
        - <粉丝二设/常见同人梗>
    skills_and_vulnerabilities:
      talents_and_skills:
        - category: <技能分类>
          skill_name: <技能名>
          proficiency_level: <熟练度>
          description: <技能说明>
          manifestations: <使用表现>
      special_abilities:
        - name: <能力名>
          description: <能力说明>
          manifestations: <能力表现>
      tools_and_equipment:
        - item_name: <装备名>
          description: <装备说明>
          manifestations: <使用表现>
      vulnerabilities_and_flaws:
        - type: <弱点类型>
          flaw: <弱点>
          description: <影响说明>
    social_and_lifestyle:
      relationship_approach:
        general:
          - <一般相处方式>
        to_targets_of_affection:
          - <对爱慕对象的表现>
        to_rivals:
          - <对竞争者的表现>
      communication_style:
        verbal_tics:
          - <口癖>
        sentence_structure: <句式风格>
        honorifics_usage: <敬称使用>
      public_persona:
        - <公开形象>
      private_persona:
        - <私下形象>
    daily_routine:
      early_morning: <清晨>
      morning: <上午>
      afternoon: <下午>
      evening: <傍晚>
      night: <夜间>
      late_night: <深夜>
    signature_items:
      - <标志性物品>
    canon_timeline:
      - event: <事件>
        timeframe: <可定位时间>
        description: <客观描述>
    relationships:
      dynamic_with_<CharacterName>:
        shared_history_facts:
          - <共同经历>
        observable_behaviors_from_character:
          - <该角色的表现>
        observable_behaviors_from_other:
          - <对方的表现>
    dialogue_and_quotes:
      verbatim_quotes:
        - <原文引语>
      other_dialogue_examples:
        - cue: <情境>
          response: <原文回应>
    trivia_facts:
      - <冷知识>
    defloration: <仅女性角色需要>
    sex_history:
      - partner: <性伙伴>
        behavior: <行为>
        result: <结果>
    pregnancy:
      - weeks: <孕周>
        father: <父亲>
        race: <种族>
        bloodline: <血统>
        thought: <角色看法>
    offspring:
      - name: <子嗣名字>
        dob: <出生日期>
        father: <父亲>
        sex: <性别>
        race: <种族>
        bloodline: <血统>
        thought: <角色看法>

worldinfo:
  <设定名/标题>:
    keys: <激活关键字，半角逗号分隔>
    content: <设定内容，可为多行字符串或自定义映射>

补充约束：
- 次要男性角色、一次性角色、纯背景角色通常不要纳入 actors，除非其对主线、情感关系或后续互动有明显持续影响。
- 原文由日语翻译而来，名字可能不一致，需要基于上下文做稳定归一化修复。
- 如果字段缺少明确证据，可结合前文已确认信息补全；若仍不可靠，则省略字段，不要写占位值。"""

DEFAULT_HUMAN_TEMPLATE = """请根据以下上下文生成 YAML Delta。

章节范围: {chapter_range}
批次编号: {batch_id}

输出结构定义:
{schema_definitions}

上一版 actors YAML:
{previous_actors_yaml}

上一版 worldinfo YAML:
{previous_worldinfo_yaml}

当前批次章节正文:
{current_batch_chapters}
"""


@dataclass(frozen=True)
class DocumentUpdateRequest:
    batch: ChapterBatch
    previous_actors_yaml: str
    previous_worldinfo_yaml: str

    @property
    def chapter_range(self) -> str:
        return f"{self.batch.start_chapter_index}-{self.batch.end_chapter_index}"


@dataclass(frozen=True)
class DocumentUpdateResult:
    prompt_text: str
    response_text: str


class DocumentUpdateChain:
    def __init__(
        self,
        model: Runnable[Any, Any],
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        format_rules: str = DEFAULT_FORMAT_RULES,
        schema_definitions: str = DEFAULT_SCHEMA_DEFINITIONS,
    ) -> None:
        self.model = model
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "{system_prompt}\n\n{format_rules}"),
                ("human", DEFAULT_HUMAN_TEMPLATE),
            ]
        ).partial(system_prompt=system_prompt, format_rules=format_rules, schema_definitions=schema_definitions)
        self._chain = self.prompt | self.model

    def build_payload(self, request: DocumentUpdateRequest) -> dict[str, str]:
        return {
            "batch_id": request.batch.batch_id,
            "chapter_range": request.chapter_range,
            "previous_actors_yaml": request.previous_actors_yaml,
            "previous_worldinfo_yaml": request.previous_worldinfo_yaml,
            "current_batch_chapters": request.batch.combined_text,
        }

    def render_prompt(self, request: DocumentUpdateRequest) -> str:
        prompt_value = self.prompt.invoke(self.build_payload(request))
        return "\n\n".join(
            f"[{message.type}]\n{self._message_content_to_text(message)}" for message in prompt_value.messages
        )

    def invoke(self, request: DocumentUpdateRequest) -> DocumentUpdateResult:
        payload = self.build_payload(request)
        prompt_text = self.render_prompt(request)
        response = self._chain.invoke(payload)
        return DocumentUpdateResult(
            prompt_text=prompt_text,
            response_text=self._coerce_response_to_text(response),
        )

    def _coerce_response_to_text(self, response: Any) -> str:
        if isinstance(response, BaseMessage):
            return self._message_content_to_text(response)
        if isinstance(response, str):
            return response
        return str(response)

    @staticmethod
    def _message_content_to_text(message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text is not None:
                        parts.append(str(text))
                        continue
                parts.append(str(item))
            return "\n".join(parts)
        return str(content)
