"""
Bot de alerta de promoções de jogos (Steam + Epic) -> Telegram.

O que ele faz a cada execução:
  1. Busca ofertas na API do IsThereAnyDeal (região BR, preços em reais):
       - jogos grátis (100% de desconto)
       - desconto >= DESCONTO_MINIMO (padrão 75%) em jogos bem avaliados
       - jogos bem avaliados no menor preço histórico
  2. Busca os jogos grátis da semana na Epic (endpoint público da loja).
  3. Descarta o que já foi avisado antes (arquivo estado.json).
  4. Para as melhores ofertas novas, manda uma mensagem por jogo com foto,
     preço, quanto baixou, avaliação e um resumo do jogo. O restante vai
     numa lista compacta.
  5. Salva o estado atualizado.

Usa só a biblioteca padrão do Python (nada para instalar).

Configuração por variáveis de ambiente (no GitHub ficam em "Secrets"):
  ITAD_API_KEY        chave da API do IsThereAnyDeal          (obrigatória)
  TELEGRAM_BOT_TOKEN  token do bot, dado pelo @BotFather        (obrigatória)
  TELEGRAM_CHAT_ID    id da sua conversa com o bot             (obrigatória)

Opcionais (têm valor padrão):
  DESCONTO_MINIMO     padrão 75
  NOTA_MINIMA         padrão 70  (% de avaliações positivas na Steam)
  MIN_AVALIACOES      padrão 300 (nº mínimo de avaliações na Steam)
  MAX_DETALHADOS      padrão 10  (quantos jogos ganham mensagem com foto por execução)
  INCLUIR_DLC         padrão "false"
  MAX_PAGINAS         padrão 5   (até 200 ofertas por página, por consulta)
  MODO_EXEMPLO        "true" = manda 3 ofertas atuais como exemplo, mesmo já avisadas

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
STEAM_DETALHES_URL = "https://store.steampowered.com/api/appdetails"
# Nomes das lojas como aparecem na API do ITAD. O ID numérico é descoberto
# em tempo de execução (assim não dependemos de um número fixo que pode mudar).
LOJAS_DESEJADAS = {"steam": "Steam", "epic": "Epic Game Store"}

ARQUIVO_ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado.json")
DIAS_PARA_ESQUECER = 45      # depois disso, uma oferta igual pode ser avisada de novo
LIMITE_MSG_TELEGRAM = 3800   # o limite real é 4096; deixamos folga
LIMITE_LEGENDA_FOTO = 1024   # limite do Telegram para legenda de foto
TAMANHO_RESUMO = 280
USER_AGENT = "alerta-promocoes-bot/1.1"
FUSO_BR = timezone(timedelta(hours=-3))


def env_int(nome, padrao):
    valor = os.environ.get(nome, "").strip()
    return int(valor) if valor else padrao


def env_bool(nome, padrao):
    valor = os.environ.get(nome, "").strip().lower()
    if not valor:
        return padrao
    return valor in ("1", "true", "sim", "yes")


DESCONTO_MINIMO = env_int("DESCONTO_MINIMO", 75)
NOTA_MINIMA = env_int("NOTA_MINIMA", 70)
MIN_AVALIACOES = env_int("MIN_AVALIACOES", 300)
MAX_DETALHADOS = env_int("MAX_DETALHADOS", 10)
INCLUIR_DLC = env_bool("INCLUIR_DLC", False)
MAX_PAGINAS = env_int("MAX_PAGINAS", 5)
MODO_EXEMPLO = env_bool("MODO_EXEMPLO", False)


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


def faixa(minimo, maximo=None):
    """A API exige os DOIS campos (min e max) em todo filtro de faixa; None = sem limite."""
    return {"min": minimo, "max": maximo}


def filtro_qualidade():
    return {
        "steamPerc": faixa(NOTA_MINIMA, 100),
        "steamCount": faixa(MIN_AVALIACOES, None),
    }


def buscar_ofertas_itad(chave, ids_lojas, filtro, descricao):
    """Pagina o endpoint POST /deals/v2 com um filtro. Devolve a lista bruta de itens."""
    if not ids_lojas:
        return []
    itens, offset = [], 0
    for _ in range(MAX_PAGINAS):
        corpo = {
            "country": PAIS,
            "offset": offset,
            "limit": 200,
            "sort": "-cut",
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
    cortes = [((i.get("deal") or {}).get("cut") or 0) for i in itens]
    faixa_txt = f" (descontos de {min(cortes)}% a {max(cortes)}%)" if cortes else ""
    print(f"[info] '{descricao}': {len(itens)} ofertas recebidas{faixa_txt}.")
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
    desconto = deal.get("cut") or 0
    gratis = valor == 0
    # Menor histórico: confirmamos pelo número, não só pela "flag" da API.
    menor_hist = hist.get("amount") is not None and valor <= hist["amount"] and desconto > 0
    # Trava de segurança: mesmo que a API ignore o filtro, só passa o que interessa.
    if not (gratis or menor_hist or desconto >= DESCONTO_MINIMO):
        return None

    assets = item.get("assets") or {}
    return {
        "titulo": item.get("title") or "?",
        "loja": (deal.get("shop") or {}).get("name") or "?",
        "preco": valor,
        "preco_int": preco.get("amountInt"),
        "regular": regular.get("amount"),
        "moeda": preco.get("currency") or "BRL",
        "desconto": desconto,
        "gratis": gratis,
        "menor_hist": menor_hist,
        "expira": deal.get("expiry"),
        "url": deal.get("url") or f"https://isthereanydeal.com/game/{item.get('slug', '')}/info/",
        "id_jogo": item.get("id") or normalizar_titulo(item.get("title")),
        "id_itad": item.get("id"),
        "imagem": assets.get("banner600") or assets.get("banner400") or assets.get("boxart"),
        "resumo": None,
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
        total = (el.get("price") or {}).get("totalPrice") or {}
        if not fim_gratis or total.get("discountPrice") not in (0, None):
            continue

        regular = None
        if isinstance(total.get("originalPrice"), int) and total.get("originalPrice") > 0:
            regular = total["originalPrice"] / 100  # a Epic manda em centavos

        slug = _slug_epic(el)
        resultado.append({
            "titulo": el.get("title") or "?",
            "loja": "Epic Game Store",
            "preco": 0,
            "preco_int": 0,
            "regular": regular,
            "moeda": "BRL",
            "desconto": 100,
            "gratis": True,
            "menor_hist": False,
            "expira": fim_gratis,
            "url": f"https://store.epicgames.com/pt-BR/p/{slug}" if slug
                   else "https://store.epicgames.com/pt-BR/free-games",
            "id_jogo": normalizar_titulo(el.get("title")),
            "id_itad": None,
            "imagem": _imagem_epic(el),
            "resumo": (el.get("description") or "").strip() or None,
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


def _imagem_epic(el):
    imagens = {i.get("type"): i.get("url") for i in el.get("keyImages") or [] if i.get("url")}
    for tipo in ("OfferImageWide", "DieselStoreFrontWide", "featuredMedia", "Thumbnail", "OfferImageTall"):
        if imagens.get(tipo):
            return imagens[tipo]
    return next(iter(imagens.values()), None)


# ---------------------------------------------------------------------------
# Detalhes do jogo (resumo, foto, avaliação) para as mensagens com foto
# ---------------------------------------------------------------------------

def limpar_texto(texto, limite=TAMANHO_RESUMO):
    texto = html.unescape(re.sub(r"<[^>]+>", " ", texto or ""))
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) > limite:
        texto = texto[:limite].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return texto


def enriquecer(o, chave_itad):
    """Completa a oferta com resumo, foto e avaliação. Qualquer falha aqui é ignorada."""
    if o.get("id_itad"):
        try:
            info = requisicao(f"{ITAD_BASE}/games/info/v2?" + urllib.parse.urlencode({"id": o["id_itad"]}),
                              cabecalhos={"ITAD-API-Key": chave_itad})
            o["tags"] = (info.get("tags") or [])[:3]
            for r in info.get("reviews") or []:
                if (r.get("source") or "").lower() == "steam" and r.get("score") is not None:
                    o["nota"], o["n_avaliacoes"] = r["score"], r.get("count")
            appid = info.get("appid")
            if not o.get("imagem"):
                assets = info.get("assets") or {}
                o["imagem"] = assets.get("banner600") or assets.get("boxart")
            if appid:
                url = f"{STEAM_DETALHES_URL}?" + urllib.parse.urlencode(
                    {"appids": appid, "l": "brazilian", "cc": "br"})
                dados = (requisicao(url).get(str(appid)) or {})
                if dados.get("success"):
                    d = dados.get("data") or {}
                    o["resumo"] = d.get("short_description") or o.get("resumo")
                    o["imagem"] = d.get("header_image") or o.get("imagem")
        except (RuntimeError, ValueError, TypeError, AttributeError) as e:
            print(f"[aviso] sem detalhes para '{o['titulo']}': {e}")
    if o.get("resumo"):
        o["resumo"] = limpar_texto(o["resumo"])
    return o


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


def enviar_detalhado(token, chat_id, o):
    """Uma mensagem por jogo: foto + legenda + botão para a loja. Sem foto, vai só o texto."""
    legenda = montar_legenda(o)
    botao = {"inline_keyboard": [[{"text": f"Ver na {o['loja']}", "url": o["url"]}]]}
    if o.get("imagem"):
        try:
            telegram("sendPhoto", token, {
                "chat_id": chat_id, "photo": o["imagem"], "caption": legenda,
                "parse_mode": "HTML", "reply_markup": botao,
            })
            return
        except RuntimeError as e:
            print(f"[aviso] foto falhou para '{o['titulo']}', enviando só texto: {e}")
    telegram("sendMessage", token, {
        "chat_id": chat_id, "text": legenda, "parse_mode": "HTML",
        "disable_web_page_preview": True, "reply_markup": botao,
    })


def formatar_preco(valor, moeda):
    if valor is None:
        return "?"
    if moeda == "BRL":
        return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{valor:.2f} {moeda}"


def formatar_prazo(o):
    if not o.get("expira"):
        return ""
    try:
        return f"até {_iso(o['expira']).astimezone(FUSO_BR):%d/%m às %H:%M}"
    except ValueError:
        return ""


def montar_legenda(o):
    if o["gratis"]:
        selo = "🎁 <b>GRÁTIS PARA RESGATAR</b>"
    elif o["menor_hist"]:
        selo = "🏆 <b>MENOR PREÇO DA HISTÓRIA</b>"
    else:
        selo = f"🔥 <b>{o['desconto']}% DE DESCONTO</b>"

    linhas = [selo, f"<b>{html.escape(o['titulo'])}</b>", ""]
    if o["gratis"]:
        preco = "💰 <b>R$ 0,00</b>"
        if o.get("regular"):
            preco += f" (custa {formatar_preco(o['regular'], o['moeda'])})"
    else:
        preco = (f"💰 <b>{formatar_preco(o['preco'], o['moeda'])}</b>"
                 f" (era <s>{formatar_preco(o['regular'], o['moeda'])}</s>)")
    linhas.append(preco)
    if not o["gratis"] and o.get("regular"):
        economia = o["regular"] - o["preco"]
        linhas.append(f"📉 Baixou <b>{formatar_preco(economia, o['moeda'])}</b> (-{o['desconto']}%)")

    loja = f"🏪 {html.escape(o['loja'])}"
    prazo = formatar_prazo(o)
    linhas.append(f"{loja} · ⏰ {prazo}" if prazo else loja)

    extras = []
    if o.get("nota") is not None:
        n = o.get("n_avaliacoes")
        qtd = f" de {n:,}".replace(",", ".") + " avaliações" if n else ""
        extras.append(f"⭐ {o['nota']}% positivas{qtd}")
    if o.get("tags"):
        extras.append("🏷 " + html.escape(", ".join(o["tags"])))
    linhas += extras

    texto = "\n".join(linhas)
    if o.get("resumo"):
        espaco = LIMITE_LEGENDA_FOTO - len(texto) - 20
        resumo = limpar_texto(o["resumo"], max(0, min(TAMANHO_RESUMO, espaco)))
        if resumo:
            texto += f"\n\n<i>{html.escape(resumo)}</i>"
    return texto


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
    prazo = formatar_prazo(o)
    if prazo:
        linha += f" · {prazo}"
    return linha


def montar_lista_compacta(ofertas):
    """Lista em texto para as ofertas que não couberam nas mensagens com foto."""
    cabecalho = f"📋 <b>E mais {len(ofertas)} oferta(s):</b>"
    mensagens, atual = [], cabecalho
    for o in ofertas:
        linha = formatar_linha(o)
        if len(atual) + len(linha) + 1 > LIMITE_MSG_TELEGRAM:
            mensagens.append(atual)
            atual = "📋 <b>(continuação)</b>"
        atual += "\n" + linha
    mensagens.append(atual)
    return mensagens


def prioridade(o):
    """Ordem de importância: grátis, menor histórico, maior desconto, mais bem avaliado."""
    return (not o["gratis"], not o["menor_hist"], -o["desconto"], o["titulo"].lower())


def enviar_ofertas(token, chat_id, ofertas, chave_itad):
    ofertas = sorted(ofertas, key=prioridade)
    detalhadas, resto = ofertas[:MAX_DETALHADOS], ofertas[MAX_DETALHADOS:]
    for i, o in enumerate(detalhadas):
        enviar_detalhado(token, chat_id, enriquecer(o, chave_itad))
        if i < len(detalhadas) - 1 or resto:
            time.sleep(1.2)
    if resto:
        enviar_mensagens(token, chat_id, montar_lista_compacta(resto))


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
        brutos = buscar_ofertas_itad(chave_itad, ids, {"cut": faixa(100, 100)}, "grátis")
        brutos += buscar_ofertas_itad(
            chave_itad, ids, {"cut": faixa(DESCONTO_MINIMO, 99), **filtro_qualidade()},
            f"desconto >= {DESCONTO_MINIMO}%")
        try:
            brutos += buscar_ofertas_itad(
                chave_itad, ids, {"flag": "H", "cut": faixa(1, 99), **filtro_qualidade()},
                "menor preço histórico")
        except RuntimeError as e:
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

    # Remove duplicatas da própria rodada (ex.: jogo que veio em dois filtros).
    unicas = {}
    for o in ofertas:
        unicas.setdefault(chave_oferta(o), o)

    agora = datetime.now(timezone.utc).isoformat()
    novas = {k: o for k, o in unicas.items() if k not in estado["enviados"]}
    print(f"[info] {len(unicas)} ofertas válidas, {len(novas)} novas.")

    if MODO_EXEMPLO:
        # Só para ver como as mensagens ficam: manda 3 ofertas atuais, sem mexer no estado.
        exemplos = sorted(unicas.values(), key=prioridade)[:3]
        enviar_mensagens(token, chat_id, ["👀 <b>Exemplo de como os alertas vão chegar:</b>"])
        for o in exemplos:
            time.sleep(1.2)
            enviar_detalhado(token, chat_id, enriquecer(o, chave_itad))
        return

    if not estado["inicializado"]:
        # Primeira execução: registra o que já está em promoção sem mandar tudo de uma vez.
        gratis = [o for o in novas.values() if o["gratis"]]
        enviar_mensagens(token, chat_id, [
            "🤖 <b>Bot de promoções ativado!</b>\n"
            f"Já havia {len(novas)} ofertas ativas; registrei todas para não lotar seu chat. "
            "A partir de agora você só recebe as novas."])
        if gratis:
            time.sleep(1.2)
            enviar_ofertas(token, chat_id, gratis, chave_itad)
        estado["inicializado"] = True
    elif novas:
        enviar_ofertas(token, chat_id, list(novas.values()), chave_itad)

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
