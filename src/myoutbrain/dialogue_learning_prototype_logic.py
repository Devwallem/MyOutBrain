"""PROTOTYPE: pure state transitions for dialogue-derived reusable experience."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import re


class ArtifactType(StrEnum):
    KNOWLEDGE = "knowledge"
    LESSON = "lesson"
    SKILL = "skill"


@dataclass(frozen=True)
class DialogueTurn:
    turn_id: str
    speaker: str
    text: str


@dataclass(frozen=True)
class ArtifactDraft:
    fingerprint: str
    artifact_type: ArtifactType
    title: str
    trigger: str
    practice: str
    evidence_turn_ids: tuple[str, ...]


@dataclass(frozen=True)
class MemoryArtifact:
    memory_id: str
    artifact_type: ArtifactType
    title: str
    trigger: str
    practice: str
    evidence_turn_ids: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class PrototypeState:
    turns: tuple[DialogueTurn, ...] = ()
    drafts: tuple[ArtifactDraft, ...] = ()
    selected_draft: int = 0
    memories: tuple[MemoryArtifact, ...] = ()
    rejected_fingerprints: frozenset[str] = frozenset()
    last_query: str = ""
    recalled_memory_ids: tuple[str, ...] = ()
    last_event: str = "Ready. Capture a sample dialogue."


@dataclass(frozen=True)
class ExtractionRule:
    terms: tuple[str, ...]
    artifact_type: ArtifactType
    title: str
    trigger: str
    practice: str


SCENARIOS: dict[str, tuple[tuple[str, str], ...]] = {
    "1": (
        ("user", "请把当前分支上传到 GitHub main。"),
        ("assistant", "GitHub 认证和网络连接失败，推送没有发生。"),
        ("user", "似乎是 GitHub 网络出问题了，先停止上传。"),
    ),
    "2": (
        ("user", "现在可以构建 Obsidian 文件框架了吗？"),
        (
            "assistant",
            "创建索引笔记前先检查 rebuild；索引不能伪造 id frontmatter，"
            "否则会被当成正式知识记录。",
        ),
    ),
    "3": (
        ("user", "再次尝试上传到 GitHub。"),
        (
            "assistant",
            "应先验证 GitHub 网络和 gh auth status，再创建发布提交，避免重复失败。",
        ),
    ),
    "4": (
        ("user", "今天天气不错。"),
        ("assistant", "是的，适合出去走走。"),
    ),
}


RULES = (
    ExtractionRule(
        terms=("github", "认证", "网络", "推送"),
        artifact_type=ArtifactType.LESSON,
        title="发布前验证 GitHub 网络与认证",
        trigger="准备向 GitHub 发布本地分支时",
        practice=(
            "先验证 github.com 连通性和 gh auth status；确认远端历史后再创建发布"
            "提交并推送。连接失败时停止，不要反复生成提交或覆盖远端。"
        ),
    ),
    ExtractionRule(
        terms=("obsidian", "索引", "frontmatter", "rebuild"),
        artifact_type=ArtifactType.SKILL,
        title="创建安全的 Obsidian 索引笔记",
        trigger="为 MyOutBrain Vault 增加入口、索引或导航笔记时",
        practice=(
            "使用普通 Markdown 和 wikilink，不写系统 id 元数据；创建后运行 rebuild，"
            "确认索引被忽略且正式知识状态保持完整。"
        ),
    ),
)


def capture_scenario(state: PrototypeState, scenario_key: str) -> PrototypeState:
    scenario = SCENARIOS[scenario_key]
    first_number = len(state.turns) + 1
    captured = tuple(
        DialogueTurn(
            turn_id=f"turn_{first_number + offset:03d}",
            speaker=speaker,
            text=text,
        )
        for offset, (speaker, text) in enumerate(scenario)
    )
    return replace(
        state,
        turns=state.turns + captured,
        last_event=f"Captured scenario {scenario_key} as one immutable dialogue source.",
    )


def distill(state: PrototypeState) -> PrototypeState:
    existing = {
        draft.fingerprint for draft in state.drafts
    } | {
        memory.fingerprint for memory in state.memories
    } | set(state.rejected_fingerprints)
    new_drafts: list[ArtifactDraft] = []
    for rule in RULES:
        evidence = tuple(
            turn.turn_id
            for turn in state.turns
            if any(term in turn.text.casefold() for term in rule.terms)
        )
        if not evidence:
            continue
        fingerprint = _fingerprint(rule)
        if fingerprint in existing:
            continue
        new_drafts.append(
            ArtifactDraft(
                fingerprint=fingerprint,
                artifact_type=rule.artifact_type,
                title=rule.title,
                trigger=rule.trigger,
                practice=rule.practice,
                evidence_turn_ids=evidence,
            )
        )
    event = (
        f"Distilled {len(new_drafts)} new candidate(s); duplicates and rejections stayed suppressed."
        if new_drafts
        else "No new reusable candidate; raw dialogue was not copied."
    )
    return replace(
        state,
        drafts=state.drafts + tuple(new_drafts),
        selected_draft=min(state.selected_draft, len(state.drafts + tuple(new_drafts)) - 1)
        if state.drafts or new_drafts
        else 0,
        last_event=event,
    )


def select_next_draft(state: PrototypeState) -> PrototypeState:
    if not state.drafts:
        return replace(state, last_event="There is no candidate to select.")
    return replace(
        state,
        selected_draft=(state.selected_draft + 1) % len(state.drafts),
        last_event="Selected the next candidate.",
    )


def accept_selected(state: PrototypeState) -> PrototypeState:
    if not state.drafts:
        return replace(state, last_event="Nothing was promoted; review queue is empty.")
    selected = state.drafts[state.selected_draft]
    memory = MemoryArtifact(
        memory_id=f"memory_{len(state.memories) + 1:03d}",
        artifact_type=selected.artifact_type,
        title=selected.title,
        trigger=selected.trigger,
        practice=selected.practice,
        evidence_turn_ids=selected.evidence_turn_ids,
        fingerprint=selected.fingerprint,
    )
    remaining = state.drafts[: state.selected_draft] + state.drafts[state.selected_draft + 1 :]
    return replace(
        state,
        drafts=remaining,
        selected_draft=min(state.selected_draft, max(0, len(remaining) - 1)),
        memories=state.memories + (memory,),
        last_event=f"Human accepted {memory.memory_id}; it is now recallable.",
    )


def reject_selected(state: PrototypeState) -> PrototypeState:
    if not state.drafts:
        return replace(state, last_event="Nothing was rejected; review queue is empty.")
    selected = state.drafts[state.selected_draft]
    remaining = state.drafts[: state.selected_draft] + state.drafts[state.selected_draft + 1 :]
    return replace(
        state,
        drafts=remaining,
        selected_draft=min(state.selected_draft, max(0, len(remaining) - 1)),
        rejected_fingerprints=state.rejected_fingerprints | {selected.fingerprint},
        last_event="Human rejected the candidate; only its compact fingerprint remains.",
    )


def recall(state: PrototypeState, query: str) -> PrototypeState:
    query_terms = _terms(query)
    scored = [
        (
            len(query_terms & _terms(f"{memory.title} {memory.trigger} {memory.practice}")),
            memory.memory_id,
        )
        for memory in state.memories
    ]
    best_score = max((score for score, _ in scored), default=0)
    recalled = tuple(
        memory_id
        for score, memory_id in scored
        if score == best_score and score > 0
    )
    return replace(
        state,
        last_query=query,
        recalled_memory_ids=recalled,
        last_event=(
            f"Recalled {len(recalled)} accepted memory artifact(s) before answering."
            if recalled
            else "No accepted prior experience matched; answer without invented memory."
        ),
    )


def selected_markdown(state: PrototypeState) -> str:
    if not state.drafts:
        return "(no candidate selected)"
    draft = state.drafts[state.selected_draft]
    evidence = "\n".join(f"  - {turn_id}" for turn_id in draft.evidence_turn_ids)
    return (
        "---\n"
        f"artifact_type: {draft.artifact_type.value}\n"
        "status: candidate\n"
        f"fingerprint: {draft.fingerprint}\n"
        "evidence_turns:\n"
        f"{evidence}\n"
        "---\n"
        f"# {draft.title}\n\n"
        "## When To Use\n\n"
        f"{draft.trigger}\n\n"
        "## Practice\n\n"
        f"{draft.practice}"
    )


def storage_summary(state: PrototypeState) -> tuple[int, int, int]:
    raw_characters = sum(len(turn.text) for turn in state.turns)
    compact_characters = sum(
        len(memory.title) + len(memory.trigger) + len(memory.practice)
        for memory in state.memories
    )
    return raw_characters, compact_characters, 0


def _fingerprint(rule: ExtractionRule) -> str:
    canonical = f"{rule.artifact_type.value}\n{rule.title}\n{rule.trigger}\n{rule.practice}"
    return sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _terms(text: str) -> frozenset[str]:
    normalized = text.casefold()
    words = set(re.findall(r"[a-z0-9]+", normalized))
    han_runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", normalized)
    bigrams = {
        run[index : index + 2]
        for run in han_runs
        for index in range(max(1, len(run) - 1))
    }
    return frozenset(words | bigrams)
