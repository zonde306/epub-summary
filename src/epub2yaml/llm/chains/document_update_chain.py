from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from epub2yaml.domain.models import ChapterBatch

DEFAULT_SYSTEM_PROMPT = """你是一个负责维护小说 YAML 知识库的分析助手。
你的任务是读取当前批次章节正文，并基于提供的相关 actors/worldinfo 上下文输出 YAML delta。
输出必须只包含 YAML，不要添加解释、代码围栏或额外注释。"""

DEFAULT_FORMAT_RULES = """**输出规则**：
1. 仅输出当前批次发生变化的角色或设定。
2. 只输出发生变化的字段或变化子树，不要复述未变化字段。
3. 对于对象字段，允许只输出局部嵌套字段。
4. 对于对象数组中的某个元素，如果只更新该元素的一部分，允许只输出该元素的变化字段。
5. 对于对象数组更新，必须保留用于定位该元素的识别字段。
6. 列表字段遵循整字段替换语义；如果输出某个标量列表，则视为用该列表替换旧值。
7. actors 中禁止出现“略”“同上”“待补”“当前”“可能”“不明”“未提及”“未知”等无意义或不确定值；没有信息就省略字段。
8. 时间表述必须使用可定位、可落盘的上帝视角描述，不得使用“现在”“刚刚”“前几天”“昨天”等相对时间。
9. worldinfo.content 可以是字符串，也可以是自定义映射结构；如某设定发生变化，只输出变化字段。
10. scenario 已经在其他阶段处理过，本次不要输出 scenario，只从 actors / worldinfo 开始输出。"""

DEFAULT_SCHEMA_DEFINITIONS = """**结构定义**（仅在对应角色/设定发生变化时输出变化字段）:

actors:
  # 每个角色
  <Character Name (English only)>:
    name:
      ja: <日文名>
      zh-CN: <中文名>
      romaji: <罗马音>
    trigger_keywords:
      - <关键词1>
      - <关键词2>
      - <昵称/别名>
      - <独特称呼>
    basic_settings:
      gender: <e.g., Girl>
      age: <e.g., 9>
      birthday: <e.g., XXXX-XX-XX>
      identity:
        - <身份1 (e.g.: 学校/职业)>
        - <身份2 (e.g.: 职务)>
        - <身份3 (e.g.: 所属团体)>
      character_brief_description:
        - < Describe a core, observable action or social role. e.g., 'Always carries a sketchbook and draws strangers.' or 'Is the student council president.' >
        # 可以继续增加相关条目
    appearance:
      overview: <List 3-5 objective physical statements a stranger could confirm at first glance. e.g., 'Typically wears glasses.' 'Is shorter than the average person.' RULE: No similes, metaphors, or psychological interpretations.>
      physical_details:
        height: <(e.g., 160cm)>
        weight: '<e.g., 48kg>'
        build: <e.g., Slender, petite>
        measurements: <e.g., B78-W56-H80>
        physical_quirks:
          - <Describe a memorable physical quirk or habit, but never use description including unconsciousness. e.g., Taps fingers on the table in a specific rhythm (three short, one long) when thinking.>
          # 可以继续增加条目
        eyes:
          color: <瞳色>
          shape: <眼型>
          special_features:
            - <Describe a unique and purely physical eye feature. e.g., 'Has a small mole beneath the left eye.'>
        hair:
          color: <发色>
          length: <头发长度>
          texture: <发质>
          style:
            - <Full sentence describing their hairstyle.>
        facial_features:
          structure:
            - <Narrative description focusing on observable details.>
          complexion: <Narrative description focusing on observable details.>
      body_details:
        skin: <Narrative description focusing on observable details.>
        muscle_tone: <Narrative description focusing on observable details.>
        posture: <Narrative description focusing on observable details.>
        scars_markings_and_origin: <Describe any scars, tattoos, or special marks and their origin. e.g., A faint scar on the left arm from a past battle; a lotus tattoo on the lower back to commemorate someone.>
      attires:
        notes: <General style notes. The character can choose outfits based on the situation, and multiple outfits can be listed here.>
        # 无明确信息可以根据上下文猜测
        outfits:
          - outfit_type: <e.g., Everyday Wear, Battle Armor, School Uniform, Ceremonial Robes, Sleepwear, Suggestive Clothing(necessary)>
            tops: <上装描述>
            bottoms: <下装描述>
            shoes: <鞋子>
            socks: <袜子>
            accessories: <配饰>
          # 可根据需要添加更多 outfit
    personality_core:
      mbti_type: <MBTI类型 (e.g., ENTJ)>
      personal_traits:
        - trait_name: <Name of Trait 1, e.g., 'Compassionate'>
          scope: <Define the scope and boundary of this trait. e.g., This applies only friend and family. She will not be compassionate to enemy.>
          manifestations:
            - <How does this trait manifest in behavior? e.g., Always offers a helping hand to those in need. RULE: No micro-expressions, involuntary actions, or descriptions of tone.>
          dialogue_examples:
            - cue: <A user prompt that would elicit a response showcasing this trait.>
              response: <The character's response in original language, demonstrating the trait. No translations>
        - trait_name: <Name of Trait 2, e.g., 'Stubborn'>
          scope: <Define the scope and boundary of this trait. e.g., For 'Stubborn': This applies only to professional matters, not personal life.>
          manifestations:
            - <How does this trait manifest in behavior? e.g., Refuses to back down from an argument, even when wrong. RULE: No micro-expressions, involuntary actions, or descriptions of tone.>
          dialogue_examples:
            - cue: <A user prompt that would elicit a response showcasing this trait.>
              response: <The character's response in original language, demonstrating the trait. No translations>
      internal_conflicts:
        - conflict_name: <Name of conflict, e.g., 'Professional Drive vs. Introverted Nature'>
          scope: <Define the scope and boundary of this conflict. e.g. This conflict is only applied to idol's working scenes.>
          manifestations:
            - <How does this internal conflict manifest? e.g., 'Wants to lead the team (Behavior A) but never volunteers to speak first in meetings (Behavior B).' RULE: No micro-expressions, involuntary actions, or descriptions of tone.>
          dialogue_examples:
            - cue: <A user prompt that would elicit a response showcasing this conflict.>
              response: <The character's response in original language, demonstrating the internal conflicts. No translations.>
      motivations:
        short_term_goals: <短期目标>
        long_term_goals: <长期愿景>
        ultimately_desired_goal: <终极追求/核心驱动力>
      likes:
        - <喜好物1>
        - <喜好物2>
        # 可以继续增加条目
      dislikes:
        - <厌恶物1>
        - <厌恶物2>
        # 可以继续增加条目
      fan_tropes:
        - <This section contains interpretations and ideas widely accepted within the fandom, but not explicitly stated in canon.>
        - <粉丝二设/常见同人梗1>
    skills_and_vulnerabilities:
      talents_and_skills:
        - category: <e.g., Professional, Artistic, Combat, Social, Academic>
          skill_name: <Name of the skill, e.g., 'Oil Painting'>
          proficiency_level: <e.g., Novice, Competent, Expert, Master>
          description: <A concise explanation of the skill>
          manifestations: <How the character uses the skill, focusing on observable details. RULE: No micro-expressions, involuntary actions, or descriptions of tone.>
        # 可添加更多技能
        # 能力也算技能
      special_abilities:
        - name: "[Name of Ability, e.g., 'Telekinesis']"
          description: "[A concise explanation of the ability]"
          manifestations: "[How the character uses the ability, focusing on observable details. RULE: No micro-expressions, involuntary actions, or descriptions of tone.]"
      tools_and_equipment:
        - item_name: "[e.g., 'Grandfather's Sword']"
          description: "[A concise explanation of the tool and its function]"
          manifestations: "[How the character uses the tool, focusing on observable details. RULE: No micro-expressions, involuntary actions, or descriptions of tone.]"
      vulnerabilities_and_flaws:
        - type: <e.g., Physical, Mental, Emotional, Social>
          flaw: <e.g., 'Arachnophobia'>
          description: <How this vulnerability affects the character.>
        # 可添加更多弱点
    social_and_lifestyle:
      relationship_approach:
        general: <List of observable behaviors.>
        to_targets_of_affection: <List of observable behaviors.>
        to_rivals: <List of observable behaviors. e.g., 'Never refers to rivals by name in public.'>
      communication_style:
        verbal_tics:
          - <口癖/常用语 (e.g., '～ですわ', '～にゃ')>
          # 可以继续增加条目
        sentence_structure: <e.g., Tends to use short, declarative sentences. Avoids asking questions.>
        honorifics_usage: <e.g., Uses '-san' for everyone, even close friends, maintaining distance.>
      public_persona:
        - <List of observable behaviors in public.>
        # 可以继续增加条目
      private_persona:
        - <List of observable behaviors in private.>
        # 可以继续增加条目
    daily_routine:
      early_morning: <Interpretations of what the character will do in the early morning>
      morning: <Interpretations of what the character will do in the morning>
      afternoon: <Interpretations of what the character will do in the afternoon>
      evening: <Interpretations of what the character will do in the evening>
      night: <Interpretations of what the character will do in the night>
      late_night: <Interpretations of what the character will do in the late night>
    signature_items:
      - <标志性物品 (e.g., 发圈, 乐器)>
      # 可以继续增加条目
    canon_timeline:
      - event: <A brief, neutral name for the event. e.g., 'The Cinderstone Heist'>
        timeframe: <When this event occurs in the original story.> # 必须是能够定位的时间，例如 xx年xx月xx日。禁止使用例如 刚刚、前几天、上周 之类的相对时间
        description: <An objective, factual summary. RULE: Actions and outcomes only.>
      - event: <Another event name. e.g., 'The Trial'>
        timeframe: <e.g., 'Main Story - Year 2'> # 必须是能够定位的时间，例如 xx年xx月xx日。禁止使用例如 刚刚、前几天、上周 之类的相对时间
        description: <RULE: Actions and outcomes only.>
      # 可添加更多事件
    relationships:
      dynamic_with_<CharacterName>:
        shared_history_facts:
          - <A key factual event they shared. e.g., 'Survived the same shipwreck.'>
        observable_behaviors_from_character:
          - <e.g., 'Always brings them coffee without being asked.'>
        observable_behaviors_from_other:
          - <e.g., 'Never remembers the character's birthday.'>
      # 可添加更多角色关系 dynamic_with_角色名
    dialogue_and_quotes:
      verbatim_quotes:
        - <Quote 1 in original language>
        - <经典台词2>
        - <口头禅>
        # 可以继续增加条目
      other_dialogue_examples:
        - cue: <情境/对象>
          response: <in original language, no translations>
        # 更多条目
    trivia_facts:
      - <冷知识/趣闻1>
      - <冷知识/趣闻2>
      - <设定变迁/幕后故事>
       # 可以继续增加条目
    # 以下字段只有角色是女性才需要，否则不需要
    defloration: <处女状态/破处者(没有就填处女)>
    sex_history: # 性经历
      # 缺少信息可以从上下文中猜测，或者直接编也行
      - partner: <参与人/性伙伴,多个用半角逗号分隔>
        behavior: <做了什么>
        result: <结果/后果>
      # 可以继续添加条目
    pregnancy: # 肚子里的孩子/婴儿/胎儿
      # 缺少信息可以从上下文中猜测，或者直接编也行
      - weeks: <第几周>
        father: <父亲>
        race: <种族>
        bloodline: <继承来的特性> # 种族无关(通用)的特性, e.g. 血裔, 龙裔, 勇者
        thought: <角色对肚子里的孩子/婴儿/胎儿的看法>
      # 可以继续添加条目
    offspring: # 已出生的孩子/后代/子嗣
      # 缺少信息可以从上下文中猜测，或者直接编也行
      - name: <子嗣名字>
        dob: <出生日期>
        father: <父亲>
        sex: <性别>
        race: <种族>
        bloodline: <继承来的特性> # 种族无关(通用)的特性, e.g. 血裔, 龙裔, 勇者
        thought: <角色对这个孩子的看法>
      # 可以继续增加条目

worldinfo:
  <设定名/标题>:
    keys: <激活关键字，半角逗号分隔>
    content: <设定内容，可为多行字符串或自定义映射>

补充约束：
- 次要男性角色、一次性角色、纯背景角色通常不要纳入 actors，除非其对主线、情感关系或后续互动有明显持续影响。
- 原文由日语翻译而来，名字可能不一致，需要基于上下文做稳定归一化修复。
- 如果字段缺少明确证据，可结合前文已确认信息补全；若仍不可靠，则省略字段，不要写占位值。"""

DEFAULT_HUMAN_TEMPLATE = """请根据以下上下文生成 YAML delta。

章节范围: {chapter_range}
批次编号: {batch_id}

输出结构定义:
{schema_definitions}

相关 actors YAML（已裁剪，仅供参考）:
{filtered_actors_yaml}

相关 worldinfo YAML（已裁剪，仅供参考）:
{filtered_worldinfo_yaml}

当前批次章节正文:
{current_batch_chapters}
"""


@dataclass(frozen=True)
class DocumentUpdateRequest:
    batch: ChapterBatch
    filtered_actors_yaml: str
    filtered_worldinfo_yaml: str

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
            "filtered_actors_yaml": request.filtered_actors_yaml,
            "filtered_worldinfo_yaml": request.filtered_worldinfo_yaml,
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
        prompt_value = self.prompt.invoke(payload)
        response_text = self._stream_response_to_text(prompt_value)
        return DocumentUpdateResult(
            prompt_text=prompt_text,
            response_text=response_text,
        )

    def _stream_response_to_text(self, prompt_value: Any) -> str:
        chunks: list[str] = []
        try:
            for chunk in self.model.stream(prompt_value):
                text = self._coerce_response_to_text(chunk)
                if text:
                    chunks.append(text)
        except (AttributeError, NotImplementedError):
            return self._coerce_response_to_text(self.model.invoke(prompt_value))

        if chunks:
            return "".join(chunks)
        return self._coerce_response_to_text(self.model.invoke(prompt_value))

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
