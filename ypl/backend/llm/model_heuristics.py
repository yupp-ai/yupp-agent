"""Minimal model heuristics stub for yupp-agent."""

DEFAULT_TIKTOKEN_TOKENIZER_NAME = "gpt-4o-mini"

NUM_CHARS_PER_TOKEN = 3.3
NUM_CHARS_PER_CODING_TOKEN = 2.5


def rough_estimate_num_tokens(text: str, is_coding: bool = False) -> int:
    chars_per_token = NUM_CHARS_PER_CODING_TOKEN if is_coding else NUM_CHARS_PER_TOKEN
    return int(len(text) / chars_per_token)
