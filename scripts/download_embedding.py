"""从 ModelScope 下载 BGE 中文 embedding 到项目 data/models。"""
from pathlib import Path

from modelscope import snapshot_download


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    target = root / "data" / "models" / "bge-small-zh-v1.5"
    path = snapshot_download("AI-ModelScope/bge-small-zh-v1.5", local_dir=str(target), revision="master")
    print(f"Downloaded embedding model to {path}")


if __name__ == "__main__":
    main()
