"""Analista de Performance (Nível) — loop de métricas/horários do Atlas IG.

Dois modos:
  --mode collect   coleta insights (reach, likes, comments, saved, shares) de
                   todo post das últimas 48h e dá append em state/insights-cache.jsonl.
  --mode analyze   agrupa os últimos 90 dias por dia-da-semana + faixa de horário,
                   calcula a taxa de engajamento média por grupo, aplica a trava de
                   amostra mínima (>=3 posts por janela) e reescreve
                   state/melhores-horarios.json; dá append em state/historico-horarios.jsonl.

taxa_engajamento = (curtidas + comentários + salvamentos*2 + compart.*2) / alcance

Estados por rodada: sucesso | sem-progresso-instavel | bloqueado (nunca falha em silêncio).

Config por env (passadas pela rotina de nuvem):
  IG_TOKEN, IG_USER, IG_VER(=v21.0), GH_TOKEN, STATE_REPO
Sem dependências externas (só stdlib).
"""
import os, sys, json, base64, argparse, urllib.request, urllib.parse, urllib.error
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
        return e.code, (json.loads(e.read().decode() or "{}"))

def gh_read(repo, path, token):
    """Retorna (texto, sha) ou (None, None) se não existir."""
    st, res = gh_req("GET", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", token)
    if st == 404:
        return None, None
    if st != 200:
        raise RuntimeError(f"read {path} -> {st}: {res}")
    return base64.b64decode(res["content"]).decode(), res["sha"]

def gh_write(repo, path, token, text, message, sha=None):
    body = {"message": message, "content": base64.b64encode(text.encode()).decode(), "branch": "main"}
    if sha:
        body["sha"] = sha
    st, res = gh_req("PUT", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", token, body)
    if st not in (200, 201):
        raise RuntimeError(f"write {path} -> {st}: {res}")
    return res

def gh_append_line(repo, path, token, line, message):
    text, sha = gh_read(repo, path, token)
    new = (text or "") + line.rstrip("\n") + "\n"
    gh_write(repo, path, token, new, message, sha)

# ---------- Instagram Graph API ----------
def graph_host(token):
    return "https://graph.instagram.com" if str(token).startswith("IGAA") else "https://graph.facebook.com"

def ig_get(token, ver, path, params):
    url = f"{graph_host(token)}/{ver}/{path}"
    params = dict(params, access_token=token)
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}") as r:
        return json.loads(r.read().decode())

def fetch_media(token, ver, ig_user, since_dt):
    """Lista media publicada desde since_dt (usa timestamp do post)."""
    out = []
    resp = ig_get(token, ver, f"{ig_user}/media",
                  {"fields": "id,timestamp,media_type,permalink", "limit": 50})
    for m in resp.get("data", []):
        ts = m.get("timestamp")  # 2026-08-12T16:08:22+0000
        try:
            when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(BR)
        except Exception:
            continue
        if when >= since_dt:
            m["_when"] = when
            out.append(m)
    return out

def fetch_insights(token, ver, media_id):
    """Tenta o conjunto completo e degrada se alguma métrica não existir p/ o tipo."""
    for metset in ["reach,likes,comments,saved,shares", "reach,likes,comments,saved", "reach"]:
        try:
            ins = ig_get(token, ver, f"{media_id}/insights", {"metric": metset})
            d = {x["name"]: x["values"][0]["value"] for x in ins.get("data", [])}
            return d
        except urllib.error.HTTPError:
            continue
    return {}

# ---------- Modos ----------
def mode_collect(token, ver, ig_user, repo, gh_token):
    since = datetime.now(BR) - timedelta(hours=48)
    media = fetch_media(token, ver, ig_user, since)
    if not media:
        print("Nenhum post nas últimas 48h. Nada a coletar.")
        return "sucesso"
    n = 0
    for m in media:
        ins = fetch_insights(token, ver, m["id"])
        rec = {
            "media_id": m["id"], "type": m.get("media_type"),
            "posted_at": m["_when"].isoformat(),
            "weekday": m["_when"].weekday(), "hour": m["_when"].hour,
            "reach": ins.get("reach", 0), "likes": ins.get("likes", 0),
            "comments": ins.get("comments", 0), "saved": ins.get("saved", 0),
            "shares": ins.get("shares", 0),
            "collected_at": datetime.now(BR).isoformat(),
        }
        gh_append_line(repo, "state/insights-cache.jsonl", gh_token,
                       json.dumps(rec, ensure_ascii=False),
                       f"insights {m['id']} @ {rec['collected_at'][:10]}")
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

def mode_analyze(repo, gh_token):
    text, _ = gh_read(repo, "state/insights-cache.jsonl", gh_token)
    if not text:
        print("Cache vazio. Estado: bloqueado (sem dados ainda).")
        return "bloqueado"
    # última medição por media_id, dentro da janela de 90 dias
    corte = datetime.now(BR) - timedelta(days=JANELA_DIAS)
    latest = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        try:
            posted = datetime.fromisoformat(r["posted_at"])
        except Exception:
            continue
        if posted < corte:
            continue
        cur = latest.get(r["media_id"])
        if not cur or r.get("collected_at", "") > cur.get("collected_at", ""):
            latest[r["media_id"]] = r
    # agrupa por (weekday, hour)
    grupos = {}
    for r in latest.values():
        t = taxa(r)
        if t is None:
            continue
        key = (r["weekday"], r["hour"])
        grupos.setdefault(key, []).append(t)
    ranking = []
    for (wd, hr), taxas in grupos.items():
        if len(taxas) < MIN_SAMPLE:  # trava de amostra mínima
            continue
        ranking.append({
            "dia": DIAS[wd], "weekday": wd, "hora": hr,
            "n_posts": len(taxas),
            "taxa_media": round(sum(taxas) / len(taxas), 4),
        })
    ranking.sort(key=lambda x: x["taxa_media"], reverse=True)

    agora = datetime.now(BR).isoformat()
    out = {
        "atualizado_em": agora,
        "janela_dias": JANELA_DIAS,
        "amostra_minima": MIN_SAMPLE,
        "total_posts_considerados": len(latest),
        "janelas_validas": len(ranking),
        "ranking": ranking,
        "observacao": ("sem janela com amostra suficiente ainda; mantenha horário padrão"
                       if not ranking else "ranking válido"),
    }
    _, sha = gh_read(repo, "state/melhores-horarios.json", gh_token)
    gh_write(repo, "state/melhores-horarios.json", gh_token,
             json.dumps(out, ensure_ascii=False, indent=2),
             f"reanalise {agora[:10]}", sha)
    gh_append_line(repo, "state/historico-horarios.jsonl", gh_token,
                   json.dumps({"semana": agora[:10], "janelas_validas": len(ranking),
                               "top": ranking[0] if ranking else None}, ensure_ascii=False),
                   f"snapshot semanal {agora[:10]}")
    if not ranking:
        print(f"Reanálise ok, mas 0 janela válida (precisa >= {MIN_SAMPLE} posts por janela). "
              f"Total considerado: {len(latest)}.")
        return "sucesso"
    print(f"Reanálise ok. {len(ranking)} janela(s) válida(s). Top: "
          f"{ranking[0]['dia']} {ranking[0]['hora']}h (taxa {ranking[0]['taxa_media']}).")
    return "sucesso"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["collect", "analyze"], required=True)
    a = ap.parse_args()
    ig_token = env("IG_TOKEN", required=True)
    ig_user = env("IG_USER", required=True)
    ig_ver = env("IG_VER", "v21.0")
    gh_token = env("GH_TOKEN", required=True)
    state_repo = env("STATE_REPO", required=True)
    if a.mode == "collect":
        estado = mode_collect(ig_token, ig_ver, ig_user, state_repo, gh_token)
    else:
        estado = mode_analyze(state_repo, gh_token)
    print("Estado:", estado)

if __name__ == "__main__":
    main()
