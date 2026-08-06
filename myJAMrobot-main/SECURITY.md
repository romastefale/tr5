# Política de segurança

## Propriedades esperadas

- Somente nove comandos são registrados.
- `/onoff` exige que o ID esteja em `MYJAM_OWNER_IDS`.
- `/login` nunca recebe senha, OAuth ou token; persiste apenas o nome da LAST FM.
- O webhook exige o segredo do Telegram e limita o corpo antes da desserialização.
- URLs de mídia vindas de APIs externas passam por allowlist HTTPS, validação de redirecionamento, MIME e tamanho.
- Trabalho caro e tarefas em segundo plano têm limites globais.
- Letras completas não são persistidas nem registradas em log.
- Migrações não excluem tabelas ou dados históricos.
- O arquivo SQLite é restringido a modo `0600` quando o sistema de arquivos permite.

## Segredos

Use variáveis de ambiente. Nunca versione tokens ou arquivos `.env`. Rotacione imediatamente qualquer segredo exposto e invalide o token no provedor.

## Relato

Envie um relato privado ao mantenedor com versão, impacto, pré-condições e reprodução mínima. Não inclua tokens reais nem dados pessoais.
