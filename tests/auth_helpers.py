import json

from app.auth import IdentityStore, initialize_demo_identities


def demo_identities(root):
    path = root / "auth.json"
    keys = json.loads(initialize_demo_identities(path).read_text(encoding="utf-8"))
    return IdentityStore(path), keys


def auth_headers(keys, user="alice"):
    return {"Authorization": "Bearer " + keys[user]}
