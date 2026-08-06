# myJAMrobot

Bot musical do Telegram com uma superfície pública deliberadamente pequena: nove comandos, conexão por nome de usuário da **LAST FM** e nenhum OAuth de usuário.

## Comandos

| Comando | Função |
|---|---|
| `/start` | início |
| `/help` | ajuda |
| `/login` | salva somente o nome de usuário da LAST FM |
| `/playing` | música atual ou última reprodução |
| `/canvas` | Canvas da música, com fallback para a capa |
| `/story` | vídeo Canvas com áudio quando disponível; fallback estático 1080×1920 |
| `/radio` | busca e envia música sem conta vinculada |
| `/lyrics` | card imediato, busca em segundo plano e edição da mesma mensagem com trecho curto |
| `/onoff` | liga/desliga o acesso público; exclusivo do proprietário |

O menu comum mostra oito comandos; o escopo privado do proprietário mostra também `/onoff`. Não existem aliases.

## Login

`/login <entrada>` aceita exatamente:

- `username`
- `@username`
- `last.fm/username`

A entrada é normalizada e somente `username` é persistido em `lastfm_profiles`. Não há senha, OAuth, callback ou token de usuário. A leitura musical usa o método público [`user.getRecentTracks`](https://www.last.fm/api/show/user.getRecentTracks), que não exige autenticação da conta do usuário; a chave de API pertence apenas à aplicação.
O nome deve seguir a regra atual da [LAST FM](https://www.last.fm/join): 2–15 caracteres, começar por letra e conter apenas letras, números, `_` ou `-`.

## Capas e mídia

- A maior imagem declarada pelas fontes é escolhida; Deezer XL (quando corresponde à faixa), Spotify e a imagem da LAST FM são comparados.
- Não há aumento generativo ou fabricação de detalhe. O cache reutiliza `file_id` do Telegram sem trocar a imagem-fonte.
- Downloads locais aceitam apenas HTTPS e hosts previstos, validam cada redirecionamento, tipo MIME e tamanho máximo.
- O resolver público de Canvas recebe somente o ID público da faixa Spotify; nunca recebe usuário da LAST FM, token de usuário ou dados do Telegram. Defina `MYJAM_SPOTIFY_CANVAS_ENABLED=false` para desativá-lo.
- `/story` produz 1080×1920. Tenta Canvas com prévia de áudio, depois Canvas sem áudio, depois card estático e, por fim, a capa original.

## Letras

`/lyrics` envia o card imediatamente com estado de busca. Uma fila limitada mantém referência forte à tarefa; quando a busca termina, o bot edita a mesma mensagem.

O extrator prioriza seção marcada como refrão, depois estrofe repetida e depois linha repetida. Quando não existe evidência suficiente de refrão, o fallback é identificado apenas como “Trecho curto”, sem alegação falsa. O limite absoluto é **10 palavras**. A letra completa existe apenas na memória durante a seleção e nunca é gravada; o banco guarda somente o trecho final ou um cache negativo curto. Veja [LEGAL_NOTES.md](LEGAL_NOTES.md).

## Rich Messages 2026

O projeto usa `aiogram==3.30.0` e `InputRichMessage` para cabeçalhos, tabelas, mídia, citações e rodapés. Se a API rejeitar um Rich Message, há fallback HTML/foto sem interromper o comando.

## Segurança

- webhook autenticado por segredo derivado do token do bot e comparação constante;
- corpo do webhook limitado a 1 MiB e conteúdo restrito a JSON;
- sem retenção de updates brutos;
- rate limit por comando, usuário e chat;
- limite global para renderizações pesadas e limite separado para buscas de letras;
- `/onoff` autorizado por allowlist de IDs;
- logs redigem tokens e não registram letras;
- OAuth, tokens de usuário, Mini App, inline mode e rotas musicais HTTP foram removidos.

Tabelas históricas já existentes não são apagadas. O runtime cria e usa apenas os perfis da LAST FM e caches de capa, Canvas, Canvas processado, trecho e estado operacional.
Uma URL de banco que não seja SQLite causa falha explícita de inicialização; o bot nunca troca silenciosamente para um banco vazio.

## Configuração

Copie `MYJAMROBOT_ENV.env` e preencha ao menos:

```text
MYJAM_TELEGRAM_BOT_TOKEN=
MYJAM_BASE_URL=https://SEU-DOMINIO.example
MYJAM_LASTFM_API_KEY=
MYJAM_OWNER_IDS=123456789
MYJAM_DATABASE_URL=sqlite:////app/data/myjamrobot.sqlite3
```

As credenciais Spotify são opcionais e somente de aplicação; enriquecem metadados, capa e prévia, mas nunca autenticam o usuário.
Se usar canais de cache, mantenha-os privados, controlados pelo operador e compatíveis com os direitos de armazenamento/reenvio do conteúdo.

## Execução e validação

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium  # necessário fora da imagem Docker
python -m app.bootstrap
python -m compileall -q app tests
python scripts/validate_release.py
pytest -q
```

No Railway, monte um volume persistente em `/app/data`.
