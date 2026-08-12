# atlas-ig-state

Cofre de estado do sistema Atlas Instagram. Nao contem segredos: o token do Instagram vive apenas no prompt da rotina de nuvem, nunca neste repositorio.

## Estrutura
- queue/ — fila de posts pendentes (um .json por post, criado pelo enqueue.py local)
- published/ — posts ja publicados (movidos pela rotina de publicacao)
- state/melhores-horarios.json — ranking vigente de janelas dia+horario
- state/insights-cache.jsonl — coleta bruta de insights (append-only)
- state/historico-horarios.jsonl — snapshot semanal do ranking (append-only)
- scripts/ — cloud_publish.py e cloud_insights.py (rodados pelas rotinas de nuvem)

## Formato de um item da fila (queue/AAAA-MM-DD-HHMM-slug.json)
    {
      "date": "2026-08-13",
      "slot": "11:00",
      "images": ["https://raw.githubusercontent.com/.../1.png", "..."],
      "caption": "legenda completa do post",
      "status": "pending"
    }

## Fluxo
1. Local: enqueue.py sobe as imagens (repo publico atlas-ig-assets) e cria o item em queue/.
2. Nuvem (11h e 19h): cloud_publish.py publica o item vencido e move para published/.
3. Nuvem (diaria e semanal): cloud_insights.py coleta metricas e atualiza state/melhores-horarios.json.

Regra da marca: nenhuma copy usa traco "-" ou "—".
