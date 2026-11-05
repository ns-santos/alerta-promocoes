# Bot de alerta de promoções (Steam + Epic → Telegram)

Verifica promoções a cada 3 horas e manda no seu Telegram:

- 🎁 **Jogos grátis** (Steam e os grátis da semana da Epic)
- 🏆 **Menor preço histórico**
- 🔥 **Desconto de 75% ou mais**

Cada oferta é avisada **uma vez só**. Se o preço do mesmo jogo cair mais, você recebe um novo aviso.

Preços em reais (região BR), vindos da API do IsThereAnyDeal.

---

## Passo 1 — Criar o bot no Telegram

1. No Telegram, abra uma conversa com **@BotFather** (confira o selo azul de verificado).
2. Mande `/newbot`, escolha um nome e um usuário terminado em `bot` (ex.: `promos_da_nick_bot`).
3. Ele responde com um **token** (algo como `123456789:AAH...`). Guarde: é a senha do bot.
4. Abra a conversa com o seu novo bot e mande qualquer mensagem (ex.: `oi`).
   Isso é necessário: um bot não pode iniciar conversa com ninguém.

## Passo 2 — Pegar a chave do IsThereAnyDeal

1. Crie uma conta em https://isthereanydeal.com e **confirme o e-mail** (contas com e-mail verificado têm limite maior de uso da API).
2. Acesse https://isthereanydeal.com/apps/my/ e registre um app (nome qualquer, ex.: "alerta pessoal").
3. Copie a **API Key** gerada.

## Passo 3 — Criar o repositório no GitHub

1. Crie uma conta em https://github.com (se não tiver).
2. Clique em **New repository**, dê um nome (ex.: `alerta-promocoes`) e marque **Public**.
   - Por que público: em repositório público o GitHub Actions é gratuito sem limite de minutos.
   - O que fica visível: só o código e a lista de ofertas já avisadas. **As chaves não ficam visíveis** (vão nos Secrets).
3. Envie os arquivos desta pasta mantendo a estrutura:
   ```
   bot.py
   README.md
   .github/workflows/alerta.yml
   ```
   Pelo site: **Add file → Upload files** e arraste a pasta inteira.
   Atenção: a pasta `.github` começa com ponto e pode ficar oculta no Windows. Se ela não subir,
   crie o arquivo pelo site em **Add file → Create new file** com o nome `.github/workflows/alerta.yml`
   e cole o conteúdo.

## Passo 4 — Cadastrar as chaves (Secrets)

No repositório: **Settings → Secrets and variables → Actions → New repository secret**. Crie:

| Nome | Valor |
|---|---|
| `ITAD_API_KEY` | chave do Passo 2 |
| `TELEGRAM_BOT_TOKEN` | token do Passo 1 |
| `TELEGRAM_CHAT_ID` | veja abaixo |

**Como descobrir o `TELEGRAM_CHAT_ID`:**
depois de ter mandado o `oi` para o bot, abra no navegador (trocando `SEU_TOKEN`):

```
https://api.telegram.org/botSEU_TOKEN/getUpdates
```

Procure `"chat":{"id":` — o número logo depois é o seu chat ID.
Se aparecer `"result":[]`, mande outra mensagem para o bot e recarregue.
Não compartilhe esse link com ninguém: ele contém o token.

## Passo 5 — Primeira execução

1. Vá na aba **Actions** do repositório. Se aparecer um aviso pedindo para habilitar workflows, habilite.
2. Clique em **Alerta de promoções → Run workflow**.
3. Em cerca de 1 minuto você recebe no Telegram: **"Bot de promoções ativado!"** e os jogos grátis atuais.

Na primeira vez o bot **registra as ofertas que já existem sem mandar todas** (seriam centenas).
A partir daí, só chega o que for novo.

Depois disso ele roda sozinho a cada 3 horas.

---

## Ajustes opcionais

Em **Settings → Secrets and variables → Actions → aba Variables**:

| Variável | Padrão | O que faz |
|---|---|---|
| `DESCONTO_MINIMO` | `75` | Desconto mínimo para avisar |
| `INCLUIR_DLC` | `false` | `true` também avisa DLCs (aumenta muito o volume) |

Para mudar a frequência, edite a linha `cron` em `.github/workflows/alerta.yml`.
O horário é em UTC (Brasília = UTC−3).

## Como desligar

- **Pausar:** aba Actions → Alerta de promoções → menu `...` → **Disable workflow**.
- **Apagar tudo:** Settings → Delete this repository. No Telegram, mande `/deletebot` para o @BotFather.

## Problemas conhecidos

- **Durante grandes promoções** (ex.: liquidações da Steam) o volume pode passar de dezenas de mensagens
  numa verificação. Se incomodar, aumente `DESCONTO_MINIMO` para 85 ou 90.
- **Repositório inativo:** o GitHub desativa workflows agendados em repositórios públicos após 60 dias sem atividade.
  O bot grava o arquivo `estado.json` a cada execução com ofertas novas, o que deve contar como atividade.
  Se mesmo assim desativar, o GitHub manda um e-mail; basta reativar na aba Actions.
- **Grátis da Epic** usa um endereço público da própria loja que não é uma API oficial documentada.
  Se a Epic mudar o formato, essa parte para de funcionar e o bot avisa no Telegram (no máximo 1 aviso por dia).
- **Atraso:** execuções agendadas no GitHub podem atrasar alguns minutos em horários de pico.
