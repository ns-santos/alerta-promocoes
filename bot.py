"""
Bot de alerta de promoções de jogos (Steam + Epic) -> Telegram.

O que ele faz a cada execução:
  1. Busca ofertas na API do IsThereAnyDeal (região BR, preços em reais):
       - desconto >= DESCONTO_MINIMO (padrão 75%)
       - jogos no menor preço histórico
  2. Busca os jogos grátis da semana na Epic (endpoint público da loja).
  3. Descarta o que já foi avisado antes (arquivo estado.json).
  4. Envia o restante para o seu Telegram, agrupado por categoria.
  5. Salva o estado atualizado.

Usa só a biblioteca padrão do Python (nada para instalar).

Configuração por variáveis de ambiente (no GitHub ficam em "Secrets"):
  ITAD_API_KEY        chave da API do IsThereAnyDeal          (obrigatória)
  TELEGRAM_BOT_TOKEN  token do bot, dado pelo @BotFather        (obrigatória)
  TELEGRAM_CHAT_ID    id da sua conversa com o bot             (obrigatória)

Opcionais (têm valor padrão):
  DESCONTO_MINIMO     padrão 75
  INCLUIR_DLC         padrão "false" (DLCs geram muito volume)
  MAX_PAGINAS         padrão 10 (até 200 ofertas por página, por consulta)

Uso:
  python bot.py                    execução normal
  python bot.py --descobrir-chat   mostra o seu TELEGRAM_CHAT_ID
  python bot.py --teste            manda uma mensagem de teste no Telegram
"""

import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

PAIS = "BR"
ITAD_BASE = "https://api.isthereanydeal.com"
EPIC_GRATIS_URL = (
    "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
    "?locale=pt-BR&country=BR&allowCountries=BR"
)
# Nomes das lojas como aparecem na API do ITAD. O ID numérico é descoberto
# em tempo de execução (assim não dependemos de um número fixo que pode mudar).
LOJAS_DESEJADAS = {"steam": "Steam", "epic": "Epic Game Store"}

ARQUIVO_ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado.json")
DIAS_PARA_ESQUECER = 45      # depois disso, uma oferta igual pode ser avisada de novo
LIMITE_MSG_TELEGRAM = 3800   # o limite real é 4096; deixamos folga
USER_AGENT = "alerta-promocoes-bot/1.0"


def env_int(nome, padrao):
    valor = os.environ.get(nome, "").strip()
    return int(valor) if valor else padrao


def env_bool(nome, padrao):
    valor = os.environ.get(nome, "").strip().lower()
    if not valor:
        return padrao
    return valor in ("1", "true", "sim", "yes")


DESCONTO_MINIMO = env_int("DESCONTO_MINIMO", 75)
INCLUIR_DLC = env_bool("INCLUIR_DLC", False)
MAX_PAGINAS = env_int("MAX_PAGINAS", 10)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def requisicao(url, metodo="GET", corpo=None, cabecalhos=None, tentativas=3):
    """Faz uma requisição HTTP e devolve o JSON. Tenta de novo em erros temporários."""
    cab = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if cabecalhos:
        cab.update(cabecalhos)
    dados = None
    if corpo is not None:
        dados = json.dumps(corpo).encode("utf-8")
        cab["Content-Type"] = "application/json"

    for tentativa in range(1, tentativas + 1):
        req = urllib.request.Request(url, data=dados, headers=cab, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 429 = muitas requisições; 5xx = erro do servidor. Vale esperar e tentar de novo.
            if e.code == 429 or 500 <= e.code < 600:
                espera = int(e.headers.get("Retry-After") or 5 * tentativa)
                if tentativa < tentativas:
                    time.sleep(min(espera, 60))
                    continue
            detalhe = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {e.code} em {url.split('?')[0]}: {detalhe}") from e
        except urllib.error.URLError as e:
            if tentativa < tentativas:
                time.sleep(5 * tentativa)
                continue
            raise RuntimeError(f"Falha de rede em {url.split('?')[0]}: {e.reason}") from e
    raise RuntimeError("Número de tentativas esgotado")


# ---------------------------------------------------------------------------
# IsThereAnyDeal (Steam + Epic)
# ---------------------------------------------------------------------------

def descobrir_ids_lojas(chave):
    """Consulta a lista de lojas ativas no Brasil e pega os IDs da Steam e da Epic."""
    url = f"{ITAD_BASE}/service/shops/v1?" + urllib.parse.urlencode({"country": PAIS})
    lojas = requisicao(url, cabecalhos={"ITAD-API-Key": chave})
    ids = {}
    for loja in lojas:
        titulo = (loja.get("title") or "").strip().lower()
        for apelido, nome in LOJAS_DESEJADAS.items():
            if titulo == nome.lower():
                ids[apelido] = loja["id"]
    faltando = set(LOJAS_DESEJADAS) - set(ids)
    if faltando:
        nomes = ", ".join(sorted(l.get("title", "?") for l in lojas))
        print(f"[aviso] Loja(s) não encontrada(s) no ITAD: {faltando}. Lojas disponíveis: {nomes}")
    return ids


def buscar_ofertas_itad(chave, ids_lojas, filtro, descricao):
    """Pagina o endpoint POST /deals/v2 com um filtro. Devolve a lista bruta de itens."""
    if not ids_lojas:
        return []
    itens, offset = [], 0
    for pagina in range(MAX_PAGINAS):
        corpo = {
            "country": PAIS,
            "offset": offset,
            "limit": 200,
            "mature": False,
            "shops": list(ids_lojas.values()),
            "filter": filtro,
        }
        resp = requisicao(f"{ITAD_BASE}/deals/v2", metodo="POST", corpo=corpo,
                          cabecalhos={"ITAD-API-Key": chave})
        itens.extend(resp.get("list") or [])
        if not resp.get("hasMore"):
            break
        offset = resp.get("nextOffset", offset + 200)
    else:
        print(f"[aviso] '{descricao}': parou no limite de {MAX_PAGINAS} páginas; pode haver mais ofertas.")
    print(f"[info] '{descricao}': {len(itens)} ofertas recebidas.")
    return itens


def normalizar_titulo(titulo):
    """'Hollow Knight®: Edição' -> 'hollowknightedicao'. Serve para casar o mesmo jogo em fontes diferentes."""
    t = unicodedata.normalize("NFKD", titulo or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


def converter_item_itad(item):
    """Transforma um item do ITAD no formato interno do bot (ou None se não servir)."""
    deal = item.get("deal") or {}
    preco = deal.get("price") or {}
    regular = deal.get("regular") or {}
    hist = deal.get("historyLow") or {}
    if preco.get("amount") is None:
        return None
    if not INCLUIR_DLC and item.get("type") == "dlc":
        return None

    valor = preco["amount"]
    gratis = valor == 0
    # Menor histórico: confirmamos pelo número, não só pela "flag" da API.
    menor_hist = hist.get("amount") is not None and valor <= hist["amount"]
    return {
        "titulo": item.get("title") or "?",
        "loja": (deal.get("shop") or {}).get("name") or "?",
        "preco": valor,
        "preco_int": preco.get("amountInt"),
        "regular": regular.get("amount"),
        "moeda": preco.get("currency") or "BRL",
        "desconto": deal.get("cut") or 0,
        "gratis": gratis,
        "menor_hist": menor_hist,
        "expira": deal.get("expiry"),
        "url": deal.get("url") or f"https://isthereanydeal.com/game/{item.get('slug', '')}/info/",
        "id_jogo": item.get("id") or normalizar_titulo(item.get("title")),
    }


# ---------------------------------------------------------------------------
# Epic: jogos grátis da semana
# ---------------------------------------------------------------------------

def buscar_gratis_epic():
    """Usa o endpoint público que a própria página da Epic usa. Não é API oficial documentada."""
    dados = requisicao(EPIC_GRATIS_URL)
    elementos = (((dados.get("data") or {}).get("Catalog") or {})
                 .get("searchStore") or {}).get("elements") or []
    agora = datetime.now(timezone.utc)
    resultado = []
    for el in elementos:
        promos = (el.get("promotions") or {}).get("promotionalOffers") or []
        fim_gratis = None
        for grupo in promos:
            for oferta in grupo.get("promotionalOffers") or []:
                desc = (oferta.get("discountSetting") or {}).get("discountPercentage")
                inicio, fim = oferta.get("startDate"), oferta.get("endDate")
                if desc == 0 and inicio and fim and _iso(inicio) <= agora < _iso(fim):
                    fim_gratis = fim
        preco_atual = (((el.get("price") or {}).get("totalPrice") or {}).get("discountPrice"))
        if not fim_gratis or preco_atual not in (0, None):
            continue

        slug = _slug_epic(el)
        resultado.append({
            "titulo": el.get("title") or "?",
            "loja": "Epic Game Store",
            "preco": 0,
            "preco_int": 0,
            "regular": None,
            "moeda": "BRL",
            "desconto": 100,
            "gratis": True,
            "menor_hist": False,
            "expira": fim_gratis,
            "url": f"https://store.epicgames.com/pt-BR/p/{slug}" if slug
                   else "https://store.epicgames.com/pt-BR/free-games",
            "id_jogo": normalizar_titulo(el.get("title")),
        })
    print(f"[info] Epic grátis da semana: {len(resultado)} jogo(s).")
    return resultado


def _iso(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def _slug_epic(el):
    mapeamentos = (el.get("offerMappings") or []) + ((el.get("catalogNs") or {}).get("mappings") or [])
    for m in mapeamentos:
        if m.get("pageSlug"):
            return m["pageSlug"]
    slug = el.get("productSlug")
    if slug and slug != "[]":
        return slug.split("/")[0]
    return None


# ---------------------------------------------------------------------------
# Estado (o que já foi avisado)
# ---------------------------------------------------------------------------

def chave_oferta(o):
    """
    Identifica uma oferta para não repetir aviso.
    - Grátis: por loja + título (pega o mesmo jogo vindo do ITAD e da Epic).
    - Paga: por jogo + loja + preço. Se o preço cair de novo, é outra chave -> novo aviso.
    """
    if o["gratis"]:
        return f"gratis|{normalizar_titulo(o['loja'])}|{normalizar_titulo(o['titulo'])}"
    return f"{o['id_jogo']}|{normalizar_titulo(o['loja'])}|{o['preco_int']}"


def carregar_estado():
    if not os.path.exists(ARQUIVO_ESTADO):
        return {"inicializado": False, "enviados": {}}
    with open(ARQUIVO_ESTADO, encoding="utf-8") as f:
        estado = json.load(f)
    estado.setdefault("inicializado", True)
    estado.setdefault("enviados", {})
    return estado


def salvar_estado(estado):
    limite = datetime.now(timezone.utc) - timedelta(days=DIAS_PARA_ESQUECER)
    estado["enviados"] = {
        k: v for k, v in estado["enviados"].items() if _iso(v) >= limite
    }
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def telegram(metodo, token, corpo=None):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    try:
        return requisicao(url, metodo="POST" if corpo else "GET", corpo=corpo)
    except RuntimeError as e:
        # Nunca imprimir a URL completa: ela contém o token.
        raise RuntimeError(str(e).replace(token, "<TOKEN>")) from None


def enviar_mensagens(token, chat_id, textos):
    for i, texto in enumerate(textos):
        telegram("sendMessage", token, {
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if i < len(textos) - 1:
            time.sleep(1.2)  # Telegram recomenda ~1 msg/s por conversa


def formatar_preco(valor, moeda):
    if valor is None:
        return "?"
    if moeda == "BRL":
        return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{valor:.2f} {moeda}"


def formatar_linha(o):
    titulo = html.escape(o["titulo"])
    loja = html.escape(o["loja"])
    link = html.escape(o["url"], quote=True)
    if o["gratis"]:
        linha = f'• <a href="{link}">{titulo}</a> — <b>GRÁTIS</b> ({loja})'
    else:
        linha = (f'• <a href="{link}">{titulo}</a> — <b>{formatar_preco(o["preco"], o["moeda"])}</b>'
                 f' <s>{formatar_preco(o["regular"], o["moeda"])}</s> (-{o["desconto"]}%, {loja})')
        if o["menor_hist"]:
            linha += " 🏆"
    if o.get("expira"):
        try:
            fim = _iso(o["expira"]).astimezone(timezone(timedelta(hours=-3)))
            linha += f" · até {fim:%d/%m %H:%M}"
        except ValueError:
            pass
    return linha


def montar_mensagens(ofertas):
    """Agrupa por categoria e quebra em mensagens abaixo do limite do Telegram."""
    grupos = [
        ("🎁 <b>Grátis para resgatar</b>", [o for o in ofertas if o["gratis"]]),
        ("🏆 <b>Menor preço histórico</b>", [o for o in ofertas if not o["gratis"] and o["menor_hist"]]),
        (f"🔥 <b>Desconto de {DESCONTO_MINIMO}% ou mais</b>",
         [o for o in ofertas if not o["gratis"] and not o["menor_hist"]]),
    ]
    mensagens = []
    for cabecalho, itens in grupos:
        if not itens:
            continue
        itens.sort(key=lambda o: (-o["desconto"], o["titulo"].lower()))
        atual = cabecalho
        for o in itens:
            linha = formatar_linha(o)
            if len(atual) + len(linha) + 1 > LIMITE_MSG_TELEGRAM:
                mensagens.append(atual)
                atual = cabecalho + " (cont.)"
            atual += "\n" + linha
        mensagens.append(atual)
    return mensagens


# ---------------------------------------------------------------------------
# Execução principal
# ---------------------------------------------------------------------------

def obrigatoria(nome):
    valor = os.environ.get(nome, "").strip()
    if not valor:
        sys.exit(f"[erro] Variável {nome} não configurada.")
    return valor


def coletar_ofertas(chave_itad):
    ofertas, falhas = [], []

    try:
        ids = descobrir_ids_lojas(chave_itad)
        brutos = buscar_ofertas_itad(chave_itad, ids, {"cut": {"min": DESCONTO_MINIMO}},
                                     f"desconto >= {DESCONTO_MINIMO}%")
        try:
            brutos += buscar_ofertas_itad(chave_itad, ids, {"flag": "H"}, "menor preço histórico")
        except RuntimeError as e:
            # O formato desse filtro não é bem documentado. Se a API recusar, seguimos sem ele.
            falhas.append(f"filtro de menor histórico: {e}")
        ofertas += [o for o in map(converter_item_itad, brutos) if o]
    except RuntimeError as e:
        falhas.append(f"IsThereAnyDeal: {e}")

    try:
        ofertas += buscar_gratis_epic()
    except (RuntimeError, ValueError, KeyError, TypeError) as e:
        falhas.append(f"Epic grátis: {e}")

    return ofertas, falhas


def main():
    if "--descobrir-chat" in sys.argv:
        token = obrigatoria("TELEGRAM_BOT_TOKEN")
        resp = telegram("getUpdates", token)
        chats = {(u.get("message") or {}).get("chat", {}).get("id")
                 for u in resp.get("result", [])} - {None}
        if not chats:
            print("Nenhuma mensagem encontrada. Mande um 'oi' para o seu bot no Telegram e rode de novo.")
        for c in chats:
            print(f"TELEGRAM_CHAT_ID = {c}")
        return

    token = obrigatoria("TELEGRAM_BOT_TOKEN")
    chat_id = obrigatoria("TELEGRAM_CHAT_ID")

    if "--teste" in sys.argv:
        enviar_mensagens(token, chat_id, ["✅ Teste do bot de promoções: está funcionando!"])
        print("Mensagem de teste enviada.")
        return

    chave_itad = obrigatoria("ITAD_API_KEY")
    estado = carregar_estado()
    ofertas, falhas = coletar_ofertas(chave_itad)

    # Remove duplicatas da própria rodada (ex.: jogo que veio nos dois filtros).
    unicas = {}
    for o in ofertas:
        unicas.setdefault(chave_oferta(o), o)

    agora = datetime.now(timezone.utc).isoformat()
    novas = {k: o for k, o in unicas.items() if k not in estado["enviados"]}
    print(f"[info] {len(unicas)} ofertas válidas, {len(novas)} novas.")

    if not estado["inicializado"]:
        # Primeira execução: registra o que já está em promoção sem mandar tudo de uma vez.
        gratis = [o for o in novas.values() if o["gratis"]]
        textos = ["🤖 <b>Bot de promoções ativado!</b>\n"
                  f"Já havia {len(novas)} ofertas ativas; registrei todas para não lotar seu chat. "
                  "A partir de agora você só recebe as novas."]
        textos += montar_mensagens(gratis)  # grátis vale avisar logo
        enviar_mensagens(token, chat_id, textos)
        estado["inicializado"] = True
    elif novas:
        enviar_mensagens(token, chat_id, montar_mensagens(list(novas.values())))

    # Só marca como enviado depois que o envio deu certo.
    for k in novas:
        estado["enviados"][k] = agora
    salvar_estado(estado)

    if falhas:
        for f in falhas:
            print(f"[erro] {f}")
        # Avisa no Telegram no máximo 1 vez por dia, para um erro persistente não virar spam.
        ultimo = estado.get("ultimo_aviso_falha")
        if not ultimo or _iso(ultimo) < datetime.now(timezone.utc) - timedelta(hours=24):
            aviso = "⚠️ <b>Problemas na verificação de promoções:</b>\n" + "\n".join(
                "• " + html.escape(f[:300]) for f in falhas)
            try:
                enviar_mensagens(token, chat_id, [aviso])
                estado["ultimo_aviso_falha"] = agora
                salvar_estado(estado)
            except RuntimeError:
                pass
        # Se nenhuma fonte funcionou, encerra com erro para o GitHub marcar a execução como falha.
        if not unicas:
            sys.exit(1)


if __name__ == "__main__":
    main()
