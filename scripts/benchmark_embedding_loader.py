"""Fresh-process local encoder comparison; no API calls, fixed known BGE weights."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.retrieval_experiment import memory


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend",choices=["sentence","direct"],required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    samples=["VPN 证书过期 notAfter", "为这个句子生成表示以用于检索相关文章：登录凭据超出了有效日期", "ERP pool acquire timeout"]
    path=str(ROOT/"data/models/bge-small-zh-v1.5")
    start=time.perf_counter()
    if args.backend=="sentence":
        from sentence_transformers import SentenceTransformer
        model=SentenceTransformer(path,device="cpu")
        encode=lambda texts:model.encode(texts,normalize_embeddings=True).tolist()
    else:
        import torch
        from transformers import AutoModel, AutoTokenizer
        tokenizer=AutoTokenizer.from_pretrained(path,local_files_only=True)
        model=AutoModel.from_pretrained(path,local_files_only=True).eval()
        def encode(texts):
            tokens=tokenizer([t.lower() for t in texts],padding=True,truncation=True,max_length=512,return_tensors="pt")
            with torch.inference_mode():
                return torch.nn.functional.normalize(model(**tokens).last_hidden_state[:,0],p=2,dim=1).tolist()
    vectors=encode(samples)
    cold=(time.perf_counter()-start)*1000
    times=[]
    for _ in range(10):
        start=time.perf_counter();encode(samples[:1]);times.append((time.perf_counter()-start)*1000)
    with args.output.open("x",encoding="utf-8") as stream:
        json.dump({"backend":args.backend,"cold_ms":cold,"warm_ms":times,"memory":memory(),"vectors":vectors},stream)
    print(args.backend,round(cold,2),memory(),flush=True)


if __name__=="__main__":main()
