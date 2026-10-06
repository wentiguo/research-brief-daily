#!/usr/bin/env python3
"""Small zero-dependency DeepSeek chat client for research brief scripts."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"


def enabled() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY"))


def chat_text(system: str, user: str, *, max_tokens: int = 900, temperature: float = 0.4) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return ""
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if model in {"deepseek-v4-pro", "deepseek-v4-flash"}:
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"warning: DeepSeek request failed: {exc}")
        return ""
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        print("warning: DeepSeek response did not contain message content")
        return ""


def paper_digest(paper: dict, *, style: str = "brief") -> str:
    title = paper.get("title", "")
    venue = paper.get("venue", "")
    abstract = paper.get("abstract", "")
    prompt = f"""请基于以下论文元数据生成中文解读。

标题：{title}
期刊/来源：{venue}
摘要：{abstract}

要求：
1. 不要编造摘要之外的信息。
2. 用材料物理/凝聚态物理研究者能读懂的语言。
3. 输出 2-3 句话，突出研究问题、可能贡献、为什么值得看。
4. 如果信息不足，明确说“摘要信息不足”。
"""
    if style == "xhs":
        prompt += "\n5. 语气适合小红书科研图文：清楚、有吸引力，但不要夸大。"
    return chat_text(
        "你是凝聚态物理、材料科学和科研写作助手，擅长严谨地把论文摘要改写成中文科研简报。",
        prompt,
        max_tokens=500,
        temperature=0.35,
    )


def innovation_summary(paper: dict) -> str:
    title = paper.get("title", "")
    venue = paper.get("venue", "")
    abstract = paper.get("abstract", "")
    prompt = f"""请只根据以下论文题名、期刊和摘要，总结它的研究创新点，供科研邮件简报推送使用。

题名：{title}
期刊/来源：{venue}
摘要：{abstract}

请用中文输出，格式固定为四段（每段 1-3 句，共不超过 260 字）：
问题：本文要解决的是哪一件事，此前缺口在哪里（点明具体材料/体系/尺度/对称性/序参量，不要写「相关问题」这类空词）
方法：用了什么理论或实验手段（如朗道自由能、k·p 展开、DFT+U、中子衍射……摘要里写了就照写；摘要没写的维度直接不写该句，不要补写「摘要未披露」之类的占位语）
结果：摘要给出的具体结论——出现了什么新相/新判据/新标度关系，或把哪两个原先分离的概念联系了起来
意义：对同领域后续工作的实际用处，以及一个可检验的后续方向

要求：
1. 只能依据摘要和题名，不要编造全文结果、图号或实验细节。
2. 四段都要写出可核验的具体名词（材料、体系、序参量、对称性、能标、温区等）。
   「本文提出一种建模方法」「需阅读全文确认」这类空话一律不得出现；只要摘要确实
   没有给出某一维度的信息，就直接省略该维度、不写该段，绝不补写「摘要未披露」之类的
   占位语，也绝不用推测补足。
3. 如果摘要确实缺位、且题名也不足以概括，则直接返回空输出，不要写「摘要信息不足」之类的话术占位。
4. 写成能直接转发给同行的中文，不要客套话、不要自我评价、不要复述题名。
"""
    return chat_text(
        "你是严谨的凝聚态物理和材料科学文献解读助手，擅长根据论文摘要提炼研究背景、创新点和展望。",
        prompt,
        max_tokens=1100,
        temperature=0.3,
    )


def xhs_post_for_paper(paper: dict) -> str:
    scores = paper.get("scores", {})
    prompt = f"""请把下面论文写成可发布于小红书的中文科研图文文案。

标题：{paper.get('title', '')}
期刊/来源：{paper.get('venue', '')}
综合评分：{scores.get('overall', '')}/100
相关度：{scores.get('relevance', '')}
期刊水平：{scores.get('journal_level', '')}
写作质量：{scores.get('writing_quality', '')}
摘要：{paper.get('abstract', '')}

输出格式：
标题备选：
1.
2.
3.

正文：
用 4-6 个短段落，适合小红书科研读者。不要编造实验结果。

标签：
给出 8-12 个话题标签。
"""
    return chat_text(
        "你是严谨的科研新媒体编辑，面向材料科学/凝聚态物理研究生和科研工作者写小红书图文。",
        prompt,
        max_tokens=1100,
        temperature=0.55,
    )


def research_design_ideas(paper: dict) -> str:
    prompt = f"""Based only on the metadata below, propose exactly three precise follow-up research topics or research designs.
Title: {paper.get('title', '')}
Venue/source: {paper.get('venue', '')}
Published: {paper.get('published', '')}
Abstract: {paper.get('abstract', '')}
Matched keywords: {', '.join(paper.get('reasons', []))}

Output in Chinese. Format as three numbered lines.
Each line should be concrete enough to guide a materials/condensed-matter project, for example specifying target material family, physical observable, method, comparison baseline, or validation route.
Do not invent results that are not in the title or abstract. If metadata is insufficient, write a conservative design and mark it as needing full-text confirmation."""
    return chat_text(
        "You are a rigorous condensed-matter physics and materials-science research planning assistant.",
        prompt,
        max_tokens=700,
        temperature=0.35,
    )
