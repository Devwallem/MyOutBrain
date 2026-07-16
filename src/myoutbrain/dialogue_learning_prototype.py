"""用于验证“对话到记忆”状态转换的简体中文原型终端。"""

from __future__ import annotations

import argparse
from io import TextIOWrapper
import sys

from myoutbrain.dialogue_learning_prototype_logic import (
    PrototypeState,
    accept_selected,
    capture_scenario,
    distill,
    recall,
    reject_selected,
    select_next_draft,
    selected_markdown,
    storage_summary,
)


BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"
ARTIFACT_LABELS = {
    "knowledge": "知识",
    "lesson": "教训",
    "skill": "技能",
}
SPEAKER_LABELS = {
    "user": "用户",
    "assistant": "智能体",
}


class SimplifiedChineseArgumentParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法：", 1)


def render(state: PrototypeState, *, clear: bool = True) -> None:
    if clear:
        print("\x1b[2J\x1b[H", end="")
    raw_chars, compact_chars, copied_transcripts = storage_summary(state)
    print(f"{BOLD}对话沉淀与经验召回原型——一次性实验，仅保存在内存中{RESET}")
    print(f"{DIM}{state.last_event}{RESET}\n")
    print(f"{BOLD}对话来源{RESET}")
    print(f"轮次：{len(state.turns)}  原文字符数（仅保存一次）：{raw_chars}")
    for turn in state.turns[-5:]:
        preview = turn.text if len(turn.text) <= 68 else turn.text[:65] + "..."
        print(f"  {turn.turn_id} {SPEAKER_LABELS[turn.speaker]}：{preview}")
    print(f"\n{BOLD}审阅队列{RESET}")
    if state.drafts:
        for index, draft in enumerate(state.drafts):
            marker = ">" if index == state.selected_draft else " "
            label = ARTIFACT_LABELS[draft.artifact_type.value]
            print(f"{marker} [{label}] {draft.title}")
    else:
        print("  （空）")
    print(f"\n{BOLD}已接受的可复用记忆{RESET}")
    if state.memories:
        for memory in state.memories:
            label = ARTIFACT_LABELS[memory.artifact_type.value]
            print(f"  {memory.memory_id} [{label}] {memory.title}")
    else:
        print("  （空——未经审阅的候选不能参与召回）")
    print(f"已拒绝指纹数：{len(state.rejected_fingerprints)}")
    print(
        "存储统计："
        f"原文={raw_chars} 字符，已接受压缩产物={compact_chars} 字符，"
        f"对话全文副本={copied_transcripts}"
    )
    print(f"\n{BOLD}回答前召回{RESET}")
    print(f"问题：{state.last_query or '（暂无）'}")
    print(
        "召回结果："
        + (", ".join(state.recalled_memory_ids) if state.recalled_memory_ids else "无")
    )
    print(f"\n{BOLD}当前候选 Markdown{RESET}")
    print(selected_markdown(state))
    print(
        f"\n{BOLD}[1]{RESET} GitHub 失败  {BOLD}[2]{RESET} Vault 索引  "
        f"{BOLD}[3]{RESET} 重复教训  {BOLD}[4]{RESET} 普通闲聊"
    )
    print(
        f"{BOLD}[x]{RESET} 提炼  {BOLD}[n]{RESET} 下一个  {BOLD}[a]{RESET} 接受  "
        f"{BOLD}[r]{RESET} 拒绝  {BOLD}[/]{RESET} 提出新问题  {BOLD}[q]{RESET} 退出"
    )


def run_interactive() -> None:
    state = PrototypeState()
    while True:
        render(state)
        command = input("\n> ").strip().lower()
        if command in {"1", "2", "3", "4"}:
            state = capture_scenario(state, command)
        elif command == "x":
            state = distill(state)
        elif command == "n":
            state = select_next_draft(state)
        elif command == "a":
            state = accept_selected(state)
        elif command == "r":
            state = reject_selected(state)
        elif command == "/":
            state = recall(state, input("请输入问题：").strip())
        elif command == "q":
            return


def run_demo() -> None:
    state = capture_scenario(PrototypeState(), "1")
    state = distill(state)
    state = accept_selected(state)
    state = capture_scenario(state, "3")
    state = distill(state)
    state = recall(state, "上传 GitHub 前应该检查什么？")
    render(state, clear=False)


def main() -> None:
    if isinstance(sys.stdout, TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = SimplifiedChineseArgumentParser(
        description="对话沉淀与经验召回状态模型原型",
        add_help=False,
    )
    options = parser.add_argument_group("选项")
    options.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    options.add_argument("--demo", action="store_true", help="运行确定性演示")
    arguments = parser.parse_args()
    if arguments.demo:
        run_demo()
    else:
        run_interactive()


if __name__ == "__main__":
    main()
