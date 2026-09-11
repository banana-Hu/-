"""一键构建 Windows 分发包（PyInstaller onedir + 内置模型 + 使用说明 → zip）。

用法（在 tools/douyin_transcript 目录下）：
    python -X utf8 build_windows.py
前置：pip install pyinstaller；本机 HF 缓存中已有 faster-whisper-small 模型
（或任一可用的本地模型，改下方 MODEL_REPO/MODEL_NAME）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
MODEL_NAME = "faster-whisper-small"
# 本机 HF 缓存中的模型快照位置（Windows 无符号链接时快照内是实体文件）
HF_SNAPSHOT = Path.home() / ".cache" / "huggingface" / "hub" / f"models--Systran--{MODEL_NAME}" / "snapshots"

EXCLUDED_MODULES = [
    "torch", "torchvision", "torchaudio", "transformers", "scipy", "matplotlib",
    "sklearn", "pandas", "PIL", "imageio_ffmpeg", "grpc", "cryptography",
    "tensorboard", "IPython", "accelerate", "safetensors", "tkinter", "psutil",
    "yadisk", "websockets", "mutagen", "yt_dlp", "brotli", "secretstorage",
]

COLLECTED_PACKAGES = [
    "faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
    "curl_cffi", "huggingface_hub",
]


def find_model_dir() -> Path:
    candidates = sorted(HF_SNAPSHOT.glob("*"))
    if not candidates:
        sys.exit(f"未找到模型快照：{HF_SNAPSHOT}（先用 python 工具跑一次即可自动下载）")
    return candidates[-1]


def run_build() -> Path:
    dist_dir = TOOL_DIR / "build_dist"
    work_dir = TOOL_DIR / "build_work"
    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)

    cmd = [sys.executable, "-X", "utf8", "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", "douyin_transcript", "--onedir", "--console",
           "--distpath", str(dist_dir), "--workpath", str(work_dir),
           "--specpath", str(work_dir), "douyin_transcript.py"]
    for pkg in COLLECTED_PACKAGES:
        cmd += ["--collect-all", pkg]
    for mod in EXCLUDED_MODULES:
        cmd += ["--exclude-module", mod]
    env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"}
    env.pop("HTTP_PROXY", None), env.pop("HTTPS_PROXY", None)
    env.pop("http_proxy", None), env.pop("https_proxy", None)
    subprocess.run(cmd, check=True, env=env)

    app_dir = dist_dir / "douyin_transcript"
    model_target = app_dir / "models" / MODEL_NAME
    shutil.copytree(find_model_dir(), model_target, dirs_exist_ok=True)
    shutil.copy2(TOOL_DIR / "使用说明.txt", app_dir / "使用说明.txt")
    # 不把构建验证产生的缓存/输出带进分发包
    shutil.rmtree(app_dir / "cache", ignore_errors=True)
    shutil.rmtree(app_dir / "output", ignore_errors=True)
    return app_dir


def make_zip(app_dir: Path) -> Path:
    out_root = TOOL_DIR.parents[1] / "dist" / "douyin_transcript"
    out_root.mkdir(parents=True, exist_ok=True)
    zip_path = out_root / "抖音文字稿工具-win64.zip"
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Compress-Archive -Path '{app_dir}' "
                    f"-DestinationPath '{zip_path}' -Force"], check=True)
    return zip_path


if __name__ == "__main__":
    app = run_build()
    print(f"[完成] 分发目录：{app}")
    zip_file = make_zip(app)
    print(f"[完成] 分发包：{zip_file}（{zip_file.stat().st_size / 1048576:.0f} MB）")
