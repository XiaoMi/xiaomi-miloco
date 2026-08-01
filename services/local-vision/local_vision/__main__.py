"""命令行入口:``miloco-local-vision`` 或 ``python -m local_vision``。"""

from __future__ import annotations

import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser(description="Miloco local-vision sidecar")
    # 默认只绑环回:未配 token 时这是唯一安全的默认值(见 app.require_token)。
    ap.add_argument("--host", default=os.environ.get("LOCAL_VISION_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LOCAL_VISION_PORT", "18800")))
    ap.add_argument("--checkpoint", default=None, help="本地权重目录或 HF repo id")
    ap.add_argument("--device", default=None, help="如 cuda:0")
    ap.add_argument("--backend", default=None, choices=["codec", "frames"])
    args = ap.parse_args()

    if args.checkpoint:
        os.environ["LOCAL_VISION_CHECKPOINT"] = args.checkpoint
    if args.device:
        os.environ["LOCAL_VISION_DEVICE"] = args.device
    if args.backend:
        os.environ["LOCAL_VISION_BACKEND"] = args.backend

    import uvicorn

    uvicorn.run("local_vision.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
