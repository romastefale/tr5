# Relatório de correção de segurança

Data: 2026-08-05  
Scan de origem: `da277d12-5244-455c-bad9-c83506ecd326`  
Resultado formal: **blocked**

O código vulnerável foi removido e a fronteira substituta passou na reprodução focada. O resultado permanece formalmente `blocked`, e não `fixed`, porque este ambiente recusou o acesso ao índice Python e, portanto, não permitiu instalar `aiogram` e `pytest` para executar toda a suíte.

## Caminho vulnerável original

Um usuário autenticado da antiga Mini App alcançava rotas de comandos musicais, que chamavam o runner compartilhado e criavam tarefas `asyncio` sem limite. O requisito de segurança é que toda admissão de trabalho acionável por usuário tenha limite por usuário/chat e capacidade global finita.

## Estratégia aplicada

- Remoção completa da Mini App, das rotas musicais HTTP e do runner antigo, conforme o escopo funcional confirmado.
- `BackgroundTaskPool` com capacidade máxima, chave de deduplicação, referências fortes e encerramento explícito para a busca de letras.
- Semáforo global para Canvas e Story, além de rate limit por comando, usuário e chat.
- Corpo do webhook limitado durante o streaming, antes da desserialização.
- Caches interativos de `/radio` limitados e expiráveis.

Arquivos centrais: `app/security/work_limits.py`, `app/security/rate_limit.py`, `app/bot/lyrics.py`, `app/bot/canvas.py`, `app/bot/story.py`, `app/bot/radio.py` e `app/main.py`.

## Verificação ordenada

| Porta | Comando/evidência | Resultado |
|---|---|---|
| Aplicabilidade e build | `python -m compileall -q app tests scripts` | PASS |
| Fechamento da falha | `MYJAM_DATA_DIR=/tmp/myjamrobot-validation-data python scripts/validate_release.py` | PASS |
| Regressão maliciosa | pool de capacidade 2 admite duas tarefas e recusa a terceira e uma duplicata | PASS |
| Bypass equivalente | busca por `asyncio.create_task` deixa somente a tarefa única de startup e o pool limitado | PASS |
| Comportamento legítimo | após concluir duas tarefas, a capacidade é recuperada e uma nova tarefa é admitida | PASS |
| Contrato funcional | exatamente nove filtros `Command` e nove entradas de menu, sem duplicatas | PASS |
| Suíte do repositório | instalação com `uv pip install -r requirements-dev.txt` | BLOCKED: índice `pypi.org` respondeu 403 |
| Integração externa | Telegram, LAST FM, Spotify, Deezer e provedores de letra reais | UNKNOWN: sem credenciais/teste de produção |

## Não reprodução e risco restante

O caminho original não reproduz porque `app/web_music` e o runner de comandos da Mini App não existem mais. Na única tarefa assíncrona acionada por usuário que permanece (`/lyrics`), a admissão passa por `BackgroundTaskPool.submit`; a prova executável confirma recusa na saturação e recuperação posterior.

O comportamento suportado permaneceu: `/lyrics` ainda envia o card imediatamente e edita a mesma mensagem; Canvas e Story continuam disponíveis sob limite global. A incerteza restante é de integração, não de alcance estático da fronteira corrigida: a suíte completa e chamadas reais devem rodar em CI/implantação com dependências e credenciais válidas antes da promoção à produção.
