"""Original 80px transparent three-dot pulse GIF; 30 frames, 1.8s, no runtime dependency.

Uploaded once per app/profile and asset digest. A cosmetic failure never blocks
message delivery. This contains no remote key or credential.
"""
import asyncio
import base64
import hashlib
import json
import logging
import zlib

_RIPPLE = (
    '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'
    'FIy82Nn/xJlT7AFkQiGK/ey/ig3LmA=='
)


def ripple_bytes():
    return zlib.decompress(base64.b64decode(_RIPPLE))


async def prepare_ripple(store, wire, binding):
    data = ripple_bytes()
    namespace = json.dumps(tuple(binding), ensure_ascii=True)
    key = "asset:ripple:" + hashlib.sha256(namespace.encode() + data).hexdigest()
    old = store.operation(key)
    if old and old.get("image_key"):
        return old["image_key"]
    try:
        image_key = await asyncio.wait_for(wire.upload_image(data), timeout=6)
        if not isinstance(image_key, str) or not image_key.startswith("img_"):
            raise ValueError("invalid image receipt")
        store.save_operation(key, {"state": "done", "image_key": image_key})
        return image_key
    except Exception:
        logging.getLogger(__name__).warning("Activity image unavailable; using static indicator")
        return ""
