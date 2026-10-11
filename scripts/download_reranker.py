"""预置 Cross-Encoder 重排模型；运行时不会在请求线程联网下载。"""
import argparse
from pathlib import Path

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="BAAI/bge-reranker-base")
    parser.add_argument("--target", type=Path, default=None)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = args.target or root / "data" / "models" / "bge-reranker-base"
    from sentence_transformers import CrossEncoder
    model = CrossEncoder(args.model, device="cpu")
    model.save(str(target))
    print(f"Saved reranker to {target.resolve()}")

if __name__ == "__main__":
    main()
