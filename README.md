# atlas-ig-state

Cofre de estado do sistema Atlas Instagram. NAO contem segredos (o token vive so no prompt da rotina de nuvem).

## Estrutura
-  fila de posts pendentes (um .json por post, criado pelo enqueue.py local)
-  posts ja publicados (movidos pela rotina de publicacao)
-  ranking vigente de janelas dia+horario
-  coleta bruta de insights (append)
-  snapshot semanal do ranking (append)
-  cloud_publish.py e cloud_insights.py (rodados pelas rotinas de nuvem)

## Fluxo
1. Local:  sobe imagens (repo publico atlas-ig-assets) e cria item em .
2. Nuvem (11h/19h):  publica o item vencido e move para .
3. Nuvem (diaria/semanal):  coleta metricas e atualiza o ranking.
