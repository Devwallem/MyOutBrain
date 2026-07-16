"""PROTOTYPE TUI for testing dialogue-to-memory state transitions."""

from __future__ import annotations

import argparse

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


def render(state: PrototypeState, *, clear: bool = True) -> None:
    if clear:
        print("\x1b[2J\x1b[H", end="")
    raw_chars, compact_chars, copied_transcripts = storage_summary(state)
    print(f"{BOLD}Dialogue Learning Prototype — throwaway, in-memory only{RESET}")
    print(f"{DIM}{state.last_event}{RESET}\n")
    print(f"{BOLD}Dialogue source{RESET}")
    print(f"turns: {len(state.turns)}  raw characters stored once: {raw_chars}")
    for turn in state.turns[-5:]:
        preview = turn.text if len(turn.text) <= 68 else turn.text[:65] + "..."
        print(f"  {turn.turn_id} {turn.speaker}: {preview}")
    print(f"\n{BOLD}Review queue{RESET}")
    if state.drafts:
        for index, draft in enumerate(state.drafts):
            marker = ">" if index == state.selected_draft else " "
            print(f"{marker} [{draft.artifact_type.value}] {draft.title}")
    else:
        print("  (empty)")
    print(f"\n{BOLD}Accepted reusable memory{RESET}")
    if state.memories:
        for memory in state.memories:
            print(f"  {memory.memory_id} [{memory.artifact_type.value}] {memory.title}")
    else:
        print("  (empty — recall cannot use unreviewed candidates)")
    print(f"rejected fingerprints: {len(state.rejected_fingerprints)}")
    print(
        "storage: "
        f"raw={raw_chars} chars, compact accepted={compact_chars} chars, "
        f"copied transcripts={copied_transcripts}"
    )
    print(f"\n{BOLD}Pre-answer recall{RESET}")
    print(f"query: {state.last_query or '(none)'}")
    print(
        "recalled: "
        + (", ".join(state.recalled_memory_ids) if state.recalled_memory_ids else "none")
    )
    print(f"\n{BOLD}Selected candidate Markdown{RESET}")
    print(selected_markdown(state))
    print(
        f"\n{BOLD}[1]{RESET} GitHub failure  {BOLD}[2]{RESET} Vault index  "
        f"{BOLD}[3]{RESET} repeat lesson  {BOLD}[4]{RESET} small talk"
    )
    print(
        f"{BOLD}[x]{RESET} distill  {BOLD}[n]{RESET} next  {BOLD}[a]{RESET} accept  "
        f"{BOLD}[r]{RESET} reject  {BOLD}[/]{RESET} ask new question  {BOLD}[q]{RESET} quit"
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
            state = recall(state, input("Question: ").strip())
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    arguments = parser.parse_args()
    if arguments.demo:
        run_demo()
    else:
        run_interactive()


if __name__ == "__main__":
    main()
