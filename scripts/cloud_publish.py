"""Publicador de nuvem — Atlas IG (modelo git).

Opera sobre um CHECKOUT LOCAL do repositório de estado (atlas-ig-state), que a
rotina de nuvem clona no diretório de trabalho. Lê a fila em queue/, publica no
Instagram o item cuja hora agendada já chegou, move o item para published/ e
persiste o estado com git commit + push.

As imagens JÁ estão como URLs públicas no item da fila (subidas pelo enqueue.py),
então aqui não há upload: só cria os containers e publica.

Config por env (passadas pela rotina de nuvem):
  IG_TOKEN     token do Instagram (IGAA...)
  IG_USER      ig_user_id
  IG_VER       versão da Graph API (default v21.0)

Args:
  --repo-dir   raiz do checkout do atlas-ig-state (default: diretório atual)
  --slot       (opcional) faixa alvo "11:00"/"19:00"; se ausente, publica o mais antigo vencido
  --dry-run    não publica nem faz commit; só mostra o que faria
  --no-push    faz commit local mas não dá push (para teste)

Sem dependências externas (só stdlib + git no PATH).
"""
import os, sys, json, time, glob, argparse, subprocess, urllib.request, urllib.parse
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

# ---------- git ----------
def git(repo_dir, *args, check=True):
    r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {r.returncode}: {r.stderr.strip()}")
    return r.stdout.strip()

def git_commit_push(repo_dir, message, push=True):
    # garante identidade (ambiente de nuvem pode não ter)
    if not git(repo_dir, "config", "user.email", check=False):
        git(repo_dir, "config", "user.email", "bot@atlas-ig", check=False)
    if not git(repo_dir, "config", "user.name", check=False):
        git(repo_dir, "config", "user.name", "atlas-ig-bot", check=False)
    git(repo_dir, "add", "-A")
    status = git(repo_dir, "status", "--porcelain")
    if not status:
        print("Nada para commitar.")
        return
    git(repo_dir, "commit", "-m", message)
    if push:
        git(repo_dir, "push", "origin", "HEAD")
        print("git push ok.")
    else:
        print("commit local ok (sem push).")

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
    d = item.get("date")
    slot = item.get("slot", "11:00")
    try:
        hh, mm = slot.split(":")
        y, mo, da = map(int, d.split("-"))
        return datetime(y, mo, da, int(hh), int(mm), tzinfo=BR)
    except Exception:
        return None

def pick_due(repo_dir, slot_filter):
    now = datetime.now(BR)
    candidates = []
    for path in sorted(glob.glob(os.path.join(repo_dir, "queue", "*.json"))):
        try:
            item = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        when = parse_when(item)
        if when is None:
            continue
        if slot_filter and item.get("slot") != slot_filter:
            continue
        if when <= now + timedelta(minutes=5):
            candidates.append((when, path, item))
    candidates.sort(key=lambda x: x[0])
    return candidates[0] if candidates else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--slot", default=os.environ.get("SLOT"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()

    ig_token = env("IG_TOKEN", required=True)
    ig_user = env("IG_USER", required=True)
    ig_ver = env("IG_VER", "v21.0")
    repo_dir = os.path.abspath(a.repo_dir)

    picked = pick_due(repo_dir, a.slot)
    if not picked:
        print(f"Nada vencido na fila (slot={a.slot or 'qualquer'}). Nada a publicar.")
        return
    when, path, item = picked
    name = os.path.basename(path)
    imgs = item.get("images", [])
    caption = item.get("caption", "")
    print(f"Item: {name} | agendado {when.isoformat()} | {len(imgs)} imagem(ns) | slot {item.get('slot')}")

    if a.dry_run:
        print("[dry-run] URLs:")
        for u in imgs:
            print("  -", u)
        print("[dry-run] legenda:", (caption[:80] + "...") if caption else "(vazia)")
        return
    if not imgs:
        raise RuntimeError(f"item {name} sem imagens")

    res = publish_carousel(ig_token, ig_ver, ig_user, imgs, caption)
    media_id = res.get("id")
    print("Publicado! media id:", media_id)

    # move queue -> published no checkout local
    done = dict(item, status="published", media_id=media_id,
                published_at=datetime.now(BR).isoformat())
    os.makedirs(os.path.join(repo_dir, "published"), exist_ok=True)
    with open(os.path.join(repo_dir, "published", name), "w", encoding="utf-8") as f:
        json.dump(done, f, ensure_ascii=False, indent=2)
    os.remove(path)
    git_commit_push(repo_dir, f"publish {name} -> {media_id}", push=not a.no_push)
    print("Fila atualizada: movido para published/.")

if __name__ == "__main__":
    main()
