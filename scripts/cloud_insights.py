"""Analista de Performance (Nível) — loop de métricas/horários do Atlas IG (modelo git).

Opera sobre um CHECKOUT LOCAL do atlas-ig-state (clonado pela rotina de nuvem).

Dois modos:
  --mode collect   coleta insights (reach, likes, comments, saved, shares) de
                   todo post das últimas 48h e dá append em state/insights-cache.jsonl.
  --mode analyze   agrupa os últimos 90 dias por dia-da-semana + faixa de horário,
                   calcula a taxa de engajamento média por grupo, aplica a trava de
                   amostra mínima (>=3 posts por janela) e reescreve
                   state/melhores-horarios.json; dá append em state/historico-horarios.jsonl.
Ao fim, persiste com git commit + push.

taxa_engajamento = (curtidas + comentários + salvamentos*2 + compart.*2) / alcance
Estados por rodada: sucesso | bloqueado (nunca falha em silêncio).

Config por env: IG_TOKEN, IG_USER, IG_VER(=v21.0)
Args: --repo-dir (default .), --no-push
Sem dependências externas (só stdlib + git no PATH).
"""
import os, sys, json, argparse, subprocess, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BR = timezone(timedelta(hours=-3))
DIAS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]  # Monday=0
MIN_SAMPLE = 3
JANELA_DIAS = 90

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
    if not git(repo_dir, "config", "user.email", check=False):
        git(repo_dir, "config", "user.email", "bot@atlas-ig", check=False)
    if not git(repo_dir, "config", "user.name", check=False):
        git(repo_dir, "config", "user.name", "atlas-ig-bot", check=False)
    git(repo_dir, "add", "-A")
    if not git(repo_dir, "status", "--porcelain"):
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

def ig_get(token, ver, path, params):
    url = f"{graph_host(token)}/{ver}/{path}"
    params = dict(params, access_token=token)
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}") as r:
        return json.loads(r.read().decode())

def fetch_media(token, ver, ig_user, since_dt):
    out = []
    resp = ig_get(token, ver, f"{ig_user}/media",
                  {"fields": "id,timestamp,media_type,permalink", "limit": 50})
    for m in resp.get("data", []):
        ts = m.get("timestamp")
        try:
            when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(BR)
        except Exception:
            continue
        if when >= since_dt:
            m["_when"] = when
            out.append(m)
    return out

def fetch_insights(token, ver, media_id):
    for metset in ["reach,likes,comments,saved,shares", "reach,likes,comments,saved", "reach"]:
        try:
            ins = ig_get(token, ver, f"{media_id}/insights", {"metric": metset})
            return {x["name"]: x["values"][0]["value"] for x in ins.get("data", [])}
        except urllib.error.HTTPError:
            continue
    return {}

# ---------- arquivos locais ----------
def read_text(repo_dir, rel):
    p = os.path.join(repo_dir, rel)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

def append_line(repo_dir, rel, line):
    p = os.path.join(repo_dir, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")

def write_text(repo_dir, rel, text):
    p = os.path.join(repo_dir, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)

# ---------- Modos ----------
def mode_collect(token, ver, ig_user, repo_dir):
    since = datetime.now(BR) - timedelta(hours=48)
    media = fetch_media(token, ver, ig_user, since)
    if not media:
        print("Nenhum post nas últimas 48h. Nada a coletar.")
        return "sucesso"
    n = 0
    for m in media:
        ins = fetch_insights(token, ver, m["id"])
        rec = {"media_id": m["id"], "type": m.get("media_type"),
               "posted_at": m["_when"].isoformat(),
               "weekday": m["_when"].weekday(), "hour": m["_when"].hour,
               "reach": ins.get("reach", 0), "likes": ins.get("likes", 0),
               "comments": ins.get("comments", 0), "saved": ins.get("saved", 0),
               "shares": ins.get("shares", 0),
               "collected_at": datetime.now(BR).isoformat()}
        append_line(repo_dir, "state/insights-cache.jsonl", json.dumps(rec, ensure_ascii=False))
        n += 1
        print(f"  coletado {m['id']} reach={rec['reach']} likes={rec['likes']} saved={rec['saved']}")
    print(f"Coleta ok: {n} post(s).")
    return "sucesso"

def taxa(rec):
    reach = rec.get("reach", 0) or 0
    if reach <= 0:
        return None
    return (rec.get("likes", 0) + rec.get("comments", 0)
            + rec.get("saved", 0) * 2 + rec.get("shares", 0) * 2) / reach

def mode_analyze(repo_dir):
    text = read_text(repo_dir, "state/insights-cache.jsonl")
    if not text.strip():
        print("Cache vazio. Estado: bloqueado (sem dados ainda).")
        return "bloqueado"
    corte = datetime.now(BR) - timedelta(days=JANELA_DIAS)
    latest = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            posted = datetime.fromisoformat(r["posted_at"])
        except Exception:
            continue
        if posted < corte:
            continue
        cur = latest.get(r["media_id"])
        if not cur or r.get("collected_at", "") > cur.get("collected_at", ""):
            latest[r["media_id"]] = r
    grupos = {}
    for r in latest.values():
        t = taxa(r)
        if t is None:
            continue
        grupos.setdefault((r["weekday"], r["hour"]), []).append(t)
    ranking = []
    for (wd, hr), taxas in grupos.items():
        if len(taxas) < MIN_SAMPLE:
            continue
        ranking.append({"dia": DIAS[wd], "weekday": wd, "hora": hr,
                        "n_posts": len(taxas), "taxa_media": round(sum(taxas) / len(taxas), 4)})
    ranking.sort(key=lambda x: x["taxa_media"], reverse=True)

    agora = datetime.now(BR).isoformat()
    out = {"atualizado_em": agora, "janela_dias": JANELA_DIAS, "amostra_minima": MIN_SAMPLE,
           "total_posts_considerados": len(latest), "janelas_validas": len(ranking),
           "ranking": ranking,
           "observacao": ("sem janela com amostra suficiente ainda; mantenha horário padrão"
                          if not ranking else "ranking válido")}
    write_text(repo_dir, "state/melhores-horarios.json", json.dumps(out, ensure_ascii=False, indent=2))
    append_line(repo_dir, "state/historico-horarios.jsonl",
                json.dumps({"semana": agora[:10], "janelas_validas": len(ranking),
                            "top": ranking[0] if ranking else None}, ensure_ascii=False))
    if not ranking:
        print(f"Reanálise ok, mas 0 janela válida (precisa >= {MIN_SAMPLE} posts por janela). "
              f"Total considerado: {len(latest)}.")
    else:
        print(f"Reanálise ok. {len(ranking)} janela(s) válida(s). Top: "
              f"{ranking[0]['dia']} {ranking[0]['hora']}h (taxa {ranking[0]['taxa_media']}).")
    return "sucesso"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["collect", "analyze"], required=True)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()
    ig_token = env("IG_TOKEN", required=True)
    ig_user = env("IG_USER", required=True)
    ig_ver = env("IG_VER", "v21.0")
    repo_dir = os.path.abspath(a.repo_dir)

    if a.mode == "collect":
        estado = mode_collect(ig_token, ig_ver, ig_user, repo_dir)
        msg = f"insights collect {datetime.now(BR).date()}"
    else:
        estado = mode_analyze(repo_dir)
        msg = f"reanalise {datetime.now(BR).date()}"
    if estado != "bloqueado":
        git_commit_push(repo_dir, msg, push=not a.no_push)
    print("Estado:", estado)

if __name__ == "__main__":
    main()
