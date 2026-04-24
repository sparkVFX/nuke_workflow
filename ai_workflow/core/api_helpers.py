"""
Gemini API helper functions — shared by NanoBanana and Gemini Chat.
"""

import os
import sys
import json
import base64

from ai_workflow.core.mime_types import guess_mime_type


def image_to_base64(image_path):
    """Convert an image file to base64 string."""
    if not image_path or not os.path.exists(image_path):
        return None
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_mime_type(image_path):
    """Get MIME type based on file extension."""
    return guess_mime_type(image_path, default="image/png")


def call_gemini_api(api_key, model, contents, generation_config):
    """Call Gemini API to generate content (REST endpoint).

    Returns (success: bool, result_or_error: dict|str)
    """
    try:
        if sys.version_info[0] >= 3:
            import urllib.request as urllib_request
            import urllib.error as urllib_error
        else:
            import urllib2 as urllib_request
            urllib_error = urllib_request

        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(
            model, api_key
        )

        request_body = {
            "contents": contents,
            "generationConfig": generation_config
        }

        json_data = json.dumps(request_body).encode("utf-8")

        req = urllib_request.Request(url, data=json_data)
        req.add_header("Content-Type", "application/json")

        response = urllib_request.urlopen(req, timeout=120)
        response_data = response.read().decode("utf-8")
        result = json.loads(response_data)

        return True, result

    except Exception as e:
        error_msg = str(e)
        if hasattr(e, 'read'):
            try:
                error_body = e.read().decode("utf-8")
                error_json = json.loads(error_body)
                if "error" in error_json:
                    error_msg = error_json["error"].get("message", error_msg)
            except Exception:
                pass
        return False, error_msg


def extract_image_from_response(response, output_dir, gen_name="nanobanana"):
    """Extract generated image from Gemini API response.

    Args:
        response: Gemini API response dict
        output_dir: Directory to save the image
        gen_name: Generator node name, used as filename prefix

    Returns (image_path: str|None, error: str|None)
    """
    try:
        candidates = response.get("candidates", [])
        if not candidates:
            return None, "No candidates in response"

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])

        for part in parts:
            if "inlineData" in part:
                inline_data = part["inlineData"]
                mime_type = inline_data.get("mimeType", "image/png")
                data = inline_data.get("data", "")

                if data:
                    ext_map = {
                        "image/png": ".png",
                        "image/jpeg": ".jpg",
                        "image/webp": ".webp",
                        "image/gif": ".gif"
                    }
                    ext = ext_map.get(mime_type, ".png")

                    # Find next available frame number
                    frame_num = 1
                    while True:
                        filename = "{}_frame{}{}".format(gen_name, frame_num, ext)
                        output_path = os.path.join(output_dir, filename)
                        if not os.path.exists(output_path):
                            break
                        frame_num += 1

                    image_data = base64.b64decode(data)
                    with open(output_path, "wb") as f:
                        f.write(image_data)

                    return output_path, None

            if "text" in part:
                text = part.get("text", "")
                if text:
                    print("[AI Workflow] API text response: {}".format(text[:500]))

        return None, "No image data in response"

    except Exception as e:
        return None, "Error extracting image: {}".format(str(e))
