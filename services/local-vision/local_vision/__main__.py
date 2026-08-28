"""命令行入口:``miloco-local-vision`` 或 ``python -m local_vision``。"""

from __future__ import annotations

import argparse
import ipaddress
import os


def unsafe_bind_reason(host: str, token: str) -> str:
    """绑到非环回地址却没配 token 时,返回拒绝启动的理由;安全则返回空串。

    README 一直写着"不配 token 则**必须**只绑环回地址",但那只是句话:代码照样
    会起来。而 README 同时又引导部署者"把边车跑在另一台带显卡的机器上" —— 两句
    合起来,最自然的做法恰好是 `--host 0.0.0.0` 且不配 token,于是局域网里任何人
    都能无鉴权调用 /v1/perceive:白嫖 GPU,还能拿到家里画面的推理结果。
    把这条承诺变成启动时的硬性检查。
    """
    if token:
        return ""
    if host == "localhost":
        return ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        # 解析不出的主机名:拿不准就当成非环回,宁可拒绝启动。
        loopback = False
    if loopback:
        return ""
    return (
        f"监听地址 {host} 不是环回地址,而 LOCAL_VISION_TOKEN 未配置 —— "
        "局域网内任何人都能无鉴权调用推理接口。请设置 LOCAL_VISION_TOKEN,"
        "或改为监听 127.0.0.1。"
    )


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

    refuse = unsafe_bind_reason(args.host, os.environ.get("LOCAL_VISION_TOKEN", ""))
    if refuse:
        raise SystemExit(f"拒绝启动:{refuse}")

    import uvicorn

    uvicorn.run("local_vision.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
