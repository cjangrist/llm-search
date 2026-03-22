"""OpenAI Chat Completions response format builder.

Converts provider-internal Responses API format into the external
Chat Completions format with nested url_citation annotations.
"""

import time
import uuid


def extract_annotations_from_provider_output(provider_output):
    """Collect all url_citation annotations from Responses API-style provider output."""
    annotations = []
    for item in provider_output:
        if item.get("type") != "message":
            continue
        for content_block in item.get("content", []):
            if content_block.get("type") == "output_text":
                annotations.extend(content_block.get("annotations", []))
    return annotations


def convert_annotations_to_chat_format(flat_annotations):
    """Convert flat Responses API annotations to nested Chat Completions format.

    Input:  {"type": "url_citation", "start_index": N, "end_index": M, "url": "...", "title": "..."}
    Output: {"type": "url_citation", "url_citation": {"start_index": N, "end_index": M, "url": "...", "title": "..."}}
    """
    return [
        {
            "type": "url_citation",
            "url_citation": {
                "start_index": annotation.get("start_index", 0),
                "end_index": annotation.get("end_index", 0),
                "url": annotation.get("url", ""),
                "title": annotation.get("title", ""),
            },
        }
        for annotation in flat_annotations
        if annotation.get("type") == "url_citation"
    ]


def build_chat_completion_response(model_string, model_response_text, provider_output):
    """Build an OpenAI Chat Completions API response from provider output.

    Args:
        model_string: The original "provider/model" string from the request.
        model_response_text: The model's text response.
        provider_output: List of Responses API-style items from the provider.

    Returns:
        Dict matching the OpenAI Chat Completions response schema.
    """
    flat_annotations = extract_annotations_from_provider_output(provider_output)
    nested_annotations = convert_annotations_to_chat_format(flat_annotations)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_string,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": model_response_text,
                    "annotations": nested_annotations,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
