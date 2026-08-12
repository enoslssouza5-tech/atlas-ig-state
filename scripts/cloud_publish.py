"""Publicador de nuvem — Atlas IG.

Roda numa rotina de nuvem (ou local). Lê a fila de posts no repositório de
estado (atlas-ig-state) via GitHub Contents API, publica no Instagram o item
cuja hora agendada já chegou, e move o item de queue/ para published/.

As imagens JÁ estão como URLs públicas no item da fila (subidas pelo enqueue.py),
então aqui não há upload: só cria os containers e publica.

Config por variáveis de ambiente (passadas pela rotina de nuvem):
  IG_TOKEN     token do Instagram (IGAA...)
  IG_USER      ig_user_id
  IG_VER       versão da Graph API (default v21.0)
  GH_TOKEN     token do GitHub (acesso ao atlas-ig-state)
  STATE_REPO   owner/repo do cofre de estado (ex.: enoslssouza5-tech/atlas-ig-state)
  SLOT         (opcional) faixa alvo "11:00" ou "19:00"; se ausente, publica o mais antigo vencido

Uso local de teste:
  IG_TOKEN=... IG_USER=... GH_TOKEN=... STATE_REPO=... python cloud_publish.py --dry-run
Sem dependências externas (só stdlib).
"""
import os, sys, json, time, base64, argparse, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BR = timezone(timedelta(hours=-3))  # America/Sao_Paulo (sem horário de verão)

def env(k, default=None, required=False):
    v = os.environ.get(k, default)
    if required and not v:
        sys.exit(f"ERRO: falta a variável de ambiente {k}")
    return v

# ---------- GitHub Contents API ----------
def gh_req(method, path, token, body=None):
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "User-Agent": "atlas-ig", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")

def gh_list(repo, folder, token):
    st, res = gh_req("GET", f"/repos/{repo}/contents/{folder}", token)
    if st == 404:
        return []
    if st != 200:
        raise RuntimeError(f"list {folder} -> {st}: {res}")
    return [f for f in res if f["type"] == "file"]

def gh_get_json(repo, path, token):
    st, res = gh_req("GET", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", token)
    if st != 200:
        raise RuntimeError(f"get {path} -> {st}: {res}")
    content = base64.b64decode(res["content"]).decode()
    return json.loads(content), res["sha"]

def gh_put(repo, path, token, obj, message):
    content = base64.b64encode(json.dumps(obj, ensure_ascii=False, indent=2).encode()).decode()
    body = {"message": message, "content": content, "branch": "main"}
    st, res = gh_req("PUT", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", token, body)
    if st not in (200, 201):
        raise RuntimeError(f"put {path} -> {st}: {res}")
    return res

def gh_delete(repo, path, token, sha, message):
    body = {"message": message, "sha": sha, "branch": "main"}
    st, res = gh_req("DELETE", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", token, body)
    if st != 200:
        raise RuntimeError(f"delete {path} -> {st}: {res}")
    return res

# ---------- Instagram Graph API ----------
def graph_host(token):
    return "https://graph.instagram.com" if str(token).startswith("IGAA") else "https://graph.facebook.com"

def ig_post(token, ver, path, params):
    url = f"{graph_host(token)}/{ver}/{path}"
    params = dict(params, access_token=token)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())

def ig_get(token, ver, path, params):
    url = f"{graph_host(token)}/{ver}/{path}"
    params = dict(params, access_token=token)
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}") as r:
        return json.loads(r.read().decode())

def wait_ready(token, ver, cid, tries=20, delay=3):
    for _ in range(tries):
        st = ig_get(token, ver, cid, {"fields": "status_code"})
        code = st.get("status_code")
        if code == "FINISHED":
            return True
        if code == "ERROR":
            raise RuntimeError(f"container {cid} ERROR: {st}")
        time.sleep(delay)
    return False

def publish_carousel(token, ver, ig_user, image_urls, caption):
    if len(image_urls) == 1:
        cont = ig_post(token, ver, f"{ig_user}/media", {"image_url": image_urls[0], "caption": caption})
        cid = cont["id"]
    else:
        children = []
        for u in image_urls:
            item = ig_post(token, ver, f"{ig_user}/media", {"image_url": u, "is_carousel_item": "true"})
            children.append(item["id"])
        cont = ig_post(token, ver, f"{ig_user}/media", {
            "media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})
        cid = cont["id"]
    if not wait_ready(token, ver, cid):
        raise RuntimeError("container não ficou pronto a tempo")
    return ig_post(token, ver, f"{ig_user}/media_publish", {"creation_id": cid})

# ---------- Seleção do item da fila ----------
def parse_when(item):
    """Retorna datetime (BR) agendado do item, a partir de date + slot."""
    d = item.get("date")  # "2026-08-13"
    slot = item.get("slot", "11:00")  # "11:00"
    try:
        hh, mm = slot.split(":")
        y, mo, da = map(int, d.split("-"))
        return datetime(y, mo, da, int(hh), int(mm), tzinfo=BR)
    except Exception:
        return None

def pick_due(repo, token, slot_filter):
    """Escolhe o item pendente mais antigo cujo horário já venceu (catch-up)."""
    now = datetime.now(BR)
    files = gh_list(repo, "queue", token)
    candidates = []
    for f in files:
        if not f["name"].endswith(".json"):
            continue
        item, sha = gh_get_json(repo, f"queue/{f['name']}", token)
        when = parse_when(item)
        if when is None:
            continue
        if slot_filter and item.get("slot") != slot_filter:
            continue
        if when <= now + timedelta(minutes=5):  # já venceu (5 min de folga)
            candidates.append((when, f["name"], item, sha))
    candidates.sort(key=lambda x: x[0])
    return candidates[0] if candidates else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slot", default=os.environ.get("SLOT"))
    a = ap.parse_args()

    ig_token = env("IG_TOKEN", required=True)
    ig_user = env("IG_USER", required=True)
    ig_ver = env("IG_VER", "v21.0")
    gh_token = env("GH_TOKEN", required=True)
    state_repo = env("STATE_REPO", required=True)

    picked = pick_due(state_repo, gh_token, a.slot)
    if not picked:
        print(f"Nada vencido na fila (slot={a.slot or 'qualquer'}). Nada a publicar.")
        return
    when, name, item, sha = picked
    imgs = item.get("images", [])
    caption = item.get("caption", "")
    print(f"Item: {name} | agendado {when.isoformat()} | {len(imgs)} imagem(ns) | slot {item.get('slot')}")

    if a.dry_run:
        print("[dry-run] URLs:")
        for u in imgs:
            print("  -", u)
        print("[dry-run] legenda:", caption[:80], "...")
        return

    if not imgs:
        raise RuntimeError(f"item {name} sem imagens")

    res = publish_carousel(ig_token, ig_ver, ig_user, imgs, caption)
    media_id = res.get("id")
    print("Publicado! media id:", media_id)

    # move queue -> published
    done = dict(item, status="published", media_id=media_id,
                published_at=datetime.now(BR).isoformat())
    gh_put(state_repo, f"published/{name}", gh_token, done, f"publish {name} -> {media_id}")
    gh_delete(state_repo, f"queue/{name}", gh_token, sha, f"remove {name} da fila (publicado)")
    print("Fila atualizada: movido para published/.")

if __name__ == "__main__":
    main()
