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
VERSAO_ESTADO = 4            # mude quando os critérios de busca mudarem

PS_GRAPHQL = "https://web.np.playstation.com/api/graphql/v1/op"
PS_CATEGORIA_PS5 = "4cbf39e2-5749-4970-ba81-93a489e4570c"   # "Todos os jogos de PS5" (inclui PS4 compatíveis)
PS_HASH_GRADE = "9845afc0dbaab4965f6563fffc703f588c8e76792000e8610843b8d3ee9c4c09"
XBOX_BROWSE = "https://emerald.xboxservices.com/xboxcomfd/browse?locale=pt-BR"
NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
# Nível de cada plano do PS Plus. Na PS Store, o selo "Extra" indica jogo completo no catálogo
# (PS_PLUS_GAME_CATALOG), mas o selo "Deluxe" indica só um TESTE gratuito (PS_PLUS_TRIAL).
PLANOS_PS = {"essential": 1, "extra": 2, "deluxe": 3, "premium": 3}


def env_int(nome, padrao):
    valor = os.environ.get(nome, "").strip()
    return int(valor) if valor else padrao


def env_bool(nome, padrao):
    valor = os.environ.get(nome, "").strip().lower()
    if not valor:
        return padrao
    return valor in ("1", "true", "sim", "yes")


DESCONTO_MINIMO = env_int("DESCONTO_MINIMO", 75)
HIST_DESCONTO_MINIMO = env_int("HIST_DESCONTO_MINIMO", 30)  # menor histórico só conta a partir daqui
NOTA_MINIMA = env_int("NOTA_MINIMA", 70)
MIN_AVALIACOES = env_int("MIN_AVALIACOES", 300)
MAX_DETALHADOS = env_int("MAX_DETALHADOS", 10)
INCLUIR_DLC = env_bool("INCLUIR_DLC", False)
MAX_PAGINAS = env_int("MAX_PAGINAS", 5)
MODO_EXEMPLO = env_bool("MODO_EXEMPLO", False)

# Consoles: descontos costumam ser menores que no PC, então o mínimo padrão é 50%.
DESCONTO_MINIMO_CONSOLE = env_int("DESCONTO_MINIMO_CONSOLE", 50)
INCLUIR_PLAYSTATION = env_bool("INCLUIR_PLAYSTATION", True)
INCLUIR_XBOX = env_bool("INCLUIR_XBOX", True)
# PlayStation: só os N jogos mais vendidos do mês (filtro de popularidade, já que a lista não traz nota).
PS_MAX_JOGOS = env_int("PS_MAX_JOGOS", 2000)
PS_ASSINATURA = os.environ.get("PS_ASSINATURA", "").strip() or "Deluxe"  # Essential, Extra ou Deluxe
XBOX_NOTA_MINIMA = float(os.environ.get("XBOX_NOTA_MINIMA", "").strip() or 3.5)   # de 0 a 5
XBOX_MIN_AVALIACOES = env_int("XBOX_MIN_AVALIACOES", 20)
XBOX_MAX_PAGINAS = env_int("XBOX_MAX_PAGINAS", 70)  # 25 jogos por página


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def requisicao(url, metodo="GET", corpo=None, cabecalhos=None, tentativas=3, texto=False):
    """Faz uma requisição HTTP e devolve o JSON (ou o texto). Tenta de novo em erros temporários."""
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
                bruto = resp.read().decode("utf-8", errors="replace")
                return bruto if texto else json.loads(bruto)
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
    menor_hist = (hist.get("amount") is not None and valor <= hist["amount"]
                  and desconto >= HIST_DESCONTO_MINIMO)
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
        # "timestamp" é quando a loja passou a cobrar este preço, ou seja, o início da promoção.
        "inicio": deal.get("timestamp"),
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
        fim_gratis = inicio_gratis = None
        for grupo in promos:
            for oferta in grupo.get("promotionalOffers") or []:
                desc = (oferta.get("discountSetting") or {}).get("discountPercentage")
                inicio, fim = oferta.get("startDate"), oferta.get("endDate")
                if desc == 0 and inicio and fim and _iso(inicio) <= agora < _iso(fim):
                    inicio_gratis, fim_gratis = inicio, fim
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
            "inicio": inicio_gratis,
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
# PlayStation Store (API interna do site; não é oficial e pode mudar)
# ---------------------------------------------------------------------------

def preco_brl(texto):
    """'R$1.234,56' -> 1234.56; 'Gratuito'/'Grátis' -> 0.0; desconhecido -> None."""
    if not texto:
        return None
    if re.search(r"gr[aá]t", texto, re.I):
        return 0.0
    numeros = re.sub(r"[^\d,]", "", texto).replace(",", ".")
    try:
        return float(numeros)
    except ValueError:
        return None


def incluso_no_plano(upsell):
    """O jogo completo faz parte do catálogo do PS Plus que a pessoa assina?
    'Incluído' -> sim; 'Extra' -> sim para Extra e Deluxe; 'Deluxe' -> NÃO (é só teste gratuito)."""
    texto = (upsell or "").strip().lower()
    if texto in ("incluído", "incluido", "included"):
        return True
    if texto == "extra":
        return PLANOS_PS.get(PS_ASSINATURA.lower(), 3) >= PLANOS_PS["extra"]
    return False


def tem_teste_gratis(upsell):
    """Selo 'Deluxe'/'Premium' na lista = teste gratuito do jogo para assinantes desse plano."""
    texto = (upsell or "").strip().lower()
    return texto in ("deluxe", "premium") and PLANOS_PS.get(PS_ASSINATURA.lower(), 3) >= PLANOS_PS["deluxe"]


def converter_item_ps(p):
    preco = p.get("price") or {}
    if not preco:
        return None
    base, atual = preco_brl(preco.get("basePrice")), preco_brl(preco.get("discountedPrice"))
    if base is None or atual is None:
        return None
    m = re.search(r"(\d+)\s*%", preco.get("discountText") or "")
    desconto = int(m.group(1)) if m else 0
    upsell = preco.get("upsellText") or ""
    incluso = incluso_no_plano(upsell)
    extra_plus = re.search(r"mais\s+(\d+)\s*%", upsell, re.I)
    gratis = (preco.get("isFree") or atual == 0) and base > 0
    if not (incluso or gratis or desconto >= DESCONTO_MINIMO_CONSOLE):
        return None

    midias = {md.get("role"): md.get("url") for md in p.get("media") or [] if md.get("type") == "IMAGE"}
    imagem = next((midias[r] for r in ("FOUR_BY_THREE_BANNER", "MASTER", "GAMEHUB_COVER_ART", "BACKGROUND")
                   if midias.get(r)), None)
    return {
        "titulo": p.get("name") or "?",
        "loja": "PlayStation Store",
        "plataformas": [x for x in (p.get("platforms") or []) if x],
        "preco": 0.0 if incluso else atual,
        "preco_int": 0 if incluso else int(round(atual * 100)),
        "regular": base,
        "moeda": "BRL",
        "desconto": 100 if incluso else desconto,
        "gratis": bool(gratis) and not incluso,
        "incluso": incluso,
        "menor_hist": False,
        "so_assinante": bool(preco.get("isTiedToSubscription")),
        "teste_gratis": tem_teste_gratis(upsell),
        "extra_plus": int(extra_plus.group(1)) if extra_plus else None,
        "expira": None,
        "inicio": None,
        "url": f"https://store.playstation.com/pt-br/product/{p.get('id')}",
        "id_jogo": "ps:" + (p.get("id") or normalizar_titulo(p.get("name"))),
        "id_ps": p.get("id"),
        "imagem": imagem,
        "resumo": None,
    }


def buscar_playstation():
    ofertas, offset = [], 0
    while offset < PS_MAX_JOGOS:
        variaveis = {"id": PS_CATEGORIA_PS5, "pageArgs": {"size": min(1000, PS_MAX_JOGOS - offset), "offset": offset},
                     "sortBy": {"name": "sales30", "isAscending": False}, "filterBy": [], "facetOptions": []}
        extensoes = {"persistedQuery": {"version": 1, "sha256Hash": PS_HASH_GRADE}}
        url = (f"{PS_GRAPHQL}?operationName=categoryGridRetrieve"
               f"&variables={urllib.parse.quote(json.dumps(variaveis))}"
               f"&extensions={urllib.parse.quote(json.dumps(extensoes))}")
        # O content-type é obrigatório: sem ele a API recusa a consulta como possível CSRF.
        dados = requisicao(url, cabecalhos={"x-psn-store-locale-override": "pt-BR", "User-Agent": NAVEGADOR,
                                            "content-type": "application/json"})
        if dados.get("errors"):
            raise RuntimeError(f"PS Store recusou a consulta: {str(dados['errors'])[:200]}")
        grade = (dados.get("data") or {}).get("categoryGridRetrieve") or {}
        produtos = grade.get("products") or []
        ofertas += [o for o in map(converter_item_ps, produtos) if o]
        info = grade.get("pageInfo") or {}
        offset += len(produtos)
        if info.get("isLast") or not produtos:
            break
    inclusos = sum(1 for o in ofertas if o["incluso"])
    print(f"[info] PlayStation: {len(ofertas) - inclusos} ofertas e {inclusos} jogos inclusos no PS Plus {PS_ASSINATURA}.")
    return ofertas


def enriquecer_ps(o):
    """A página do produto traz o fim da promoção, a descrição em português e a nota."""
    h = requisicao(o["url"], cabecalhos={"User-Agent": NAVEGADOR, "Accept": "text/html"}, texto=True)
    fim = re.search(r'"endTime":"?(\d{12,})', h)
    if fim and not o["incluso"]:
        o["expira"] = datetime.fromtimestamp(int(fim.group(1)) / 1000, timezone.utc).isoformat()
    nota = re.search(r'"averageRating":([\d.]+),"totalRatingsCount":(\d+)', h)
    if nota:
        o["estrelas"] = (float(nota.group(1)), int(nota.group(2)))
    longa = re.search(r'"type":"LONG"(?:,"subType":"[^"]*")?,"value":"((?:[^"\\]|\\.)*)"', h)
    curta = re.search(r'"type":"SHORT"(?:,"subType":"[^"]*")?,"value":"((?:[^"\\]|\\.)*)"', h)
    bruto = (longa or curta)
    if bruto:
        try:
            texto = json.loads(f'"{bruto.group(1)}"')
        except ValueError:
            texto = bruto.group(1)
        # A primeira linha costuma ser um slogan em maiúsculas; pulamos se houver mais texto depois.
        partes = [t for t in re.split(r"<br\s*/?>", texto) if t.strip()]
        if len(partes) > 1 and partes[0].isupper():
            partes = partes[1:]
        o["resumo"] = " ".join(partes)


# ---------------------------------------------------------------------------
# Xbox Store (API interna do xbox.com; não é oficial e pode mudar)
# ---------------------------------------------------------------------------

def converter_item_xbox(p):
    compras = ((p.get("specificPrices") or {}).get("purchaseable")) or []
    if not compras:
        return None
    c = compras[0]
    atual, regular = c.get("listPrice"), c.get("msrp")
    if atual is None or not regular:
        return None
    desconto = int(round(c.get("discountPercentage") or (1 - atual / regular) * 100))
    gratis = atual == 0 and regular > 0
    if not (gratis or desconto >= DESCONTO_MINIMO_CONSOLE):
        return None
    nota, qtd = p.get("averageRating"), p.get("ratingCount") or 0
    if not gratis and (nota is None or nota < XBOX_NOTA_MINIMA or qtd < XBOX_MIN_AVALIACOES):
        return None

    expira = None
    if c.get("endDateUtc"):
        try:
            expira = datetime.strptime(c["endDateUtc"], "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    imagens = p.get("images") or {}
    imagem = next(((imagens.get(k) or {}).get("url") for k in ("superHeroArt", "titledHeroArt", "poster", "boxArt")
                   if (imagens.get(k) or {}).get("url")), None)
    nomes = {"XboxSeriesX": "Xbox Series X|S", "XboxOne": "Xbox One", "PC": "PC"}
    return {
        "titulo": p.get("title") or "?",
        "loja": "Xbox Store",
        "plataformas": [nomes.get(x, x) for x in p.get("availableOn") or []],
        "preco": atual,
        "preco_int": int(round(atual * 100)),
        "regular": regular,
        "moeda": c.get("currency") or "BRL",
        "desconto": desconto,
        "gratis": gratis,
        "incluso": False,
        "menor_hist": False,
        "no_game_pass": bool(p.get("includedWithPassesProductIds")),
        "expira": expira,
        "inicio": None,
        "url": f"https://www.xbox.com/pt-BR/games/store/jogo/{p.get('productId')}",
        "id_jogo": "xbox:" + (p.get("productId") or normalizar_titulo(p.get("title"))),
        "imagem": imagem,
        "resumo": p.get("shortDescription") or p.get("description"),
        "estrelas": (nota, qtd) if nota is not None else None,
    }


def buscar_xbox():
    ofertas, ct = [], None
    for pagina in range(XBOX_MAX_PAGINAS):
        corpo = {"Filters": "e30=", "ReturnFilters": False, "ChannelKeyToBeUsedInResponse": "OFERTAS",
                 "EncodedCT": ct, "ChannelId": "DynamicChannel.GameDeals"}
        dados = requisicao(XBOX_BROWSE, metodo="POST", corpo=corpo, cabecalhos={
            "x-ms-api-version": "1.1", "MS-CV": f"alertaPromocoes.{pagina + 1}", "User-Agent": NAVEGADOR})
        ofertas += [o for o in map(converter_item_xbox, dados.get("productSummaries") or []) if o]
        ct = ((dados.get("channels") or {}).get("OFERTAS") or {}).get("encodedCT")
        if not ct or not dados.get("productSummaries"):
            break
        time.sleep(0.3)
    print(f"[info] Xbox: {len(ofertas)} ofertas bem avaliadas ({pagina + 1} página(s) lidas).")
    return ofertas


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
    if o.get("id_ps"):
        try:
            enriquecer_ps(o)
        except (RuntimeError, ValueError, TypeError, AttributeError) as e:
            print(f"[aviso] sem detalhes da PS Store para '{o['titulo']}': {e}")
    elif o.get("id_itad"):
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
    if o.get("incluso"):
        return f"incluso|{o['id_jogo']}"
    if o["gratis"]:
        return f"gratis|{normalizar_titulo(o['loja'])}|{normalizar_titulo(o['titulo'])}"
    return f"{o['id_jogo']}|{normalizar_titulo(o['loja'])}|{o['preco_int']}"


def carregar_estado():
    if not os.path.exists(ARQUIVO_ESTADO):
        return {"inicializado": False, "enviados": {}, "versao": VERSAO_ESTADO}
    with open(ARQUIVO_ESTADO, encoding="utf-8") as f:
        estado = json.load(f)
    estado.setdefault("inicializado", True)
    estado.setdefault("enviados", {})
    if estado.get("versao") != VERSAO_ESTADO:
        # Os critérios de busca mudaram: registra de novo as ofertas atuais em silêncio,
        # para a atualização não disparar centenas de avisos de uma vez.
        estado["inicializado"] = False
        estado["versao"] = VERSAO_ESTADO
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


MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
SEPARADOR = "━━━━━━━━━━━━━━━━"


def data_curta(texto_iso):
    """'2026-12-17T15:00:00Z' -> '17 dez' (no horário de Brasília)."""
    try:
        d = _iso(texto_iso).astimezone(FUSO_BR)
    except (ValueError, TypeError, AttributeError):
        return None
    return f"{d.day:02d} {MESES[d.month - 1]}"


def tempo_restante(texto_iso, agora=None):
    """Contagem regressiva legível: 'termina hoje', 'termina em 3 dias'..."""
    try:
        fim = _iso(texto_iso)
    except (ValueError, TypeError, AttributeError):
        return None
    agora = agora or datetime.now(timezone.utc)
    horas = (fim - agora).total_seconds() / 3600
    if horas <= 0:
        return None
    if horas < 24:
        return f"⚠️ termina em {max(1, int(horas))}h"
    dias = max(1, round(horas / 24))
    return "⏳ termina amanhã" if dias == 1 else f"⏳ termina em {dias} dias"


def montar_legenda(o):
    if o.get("incluso"):
        selo = f"✨ <b>NOVO NO SEU PS PLUS {PS_ASSINATURA.upper()}</b>"
    elif o["gratis"]:
        selo = "🎁 <b>GRÁTIS PARA RESGATAR</b>"
    elif o["menor_hist"]:
        selo = "🏆 <b>MENOR PREÇO DA HISTÓRIA</b>"
    else:
        selo = f"🔥 <b>{o['desconto']}% DE DESCONTO</b>"

    # Bloco 1: identificação
    linhas = [selo, SEPARADOR, f"🎮 <b>{html.escape(o['titulo'])}</b>"]
    if o.get("plataformas"):
        linhas.append("🕹 " + html.escape(" · ".join(o["plataformas"])))
    if o.get("tags"):
        linhas.append("🏷 <i>" + html.escape(" · ".join(o["tags"])) + "</i>")

    # Bloco 2: preço
    linhas.append("")
    if o.get("incluso"):
        linhas.append(f"💰 <b>Já incluso na sua assinatura</b>   <s>{formatar_preco(o['regular'], o['moeda'])}</s>")
        linhas.append("📥 É só baixar, sem pagar nada a mais")
    elif o["gratis"]:
        preco = "💰 <b>R$ 0,00</b>"
        if o.get("regular"):
            preco += f"   <s>{formatar_preco(o['regular'], o['moeda'])}</s>"
        linhas.append(preco)
    else:
        linhas.append(f"💰 <b>{formatar_preco(o['preco'], o['moeda'])}</b>"
                      f"   <s>{formatar_preco(o['regular'], o['moeda'])}</s>")
        if o.get("regular"):
            economia = o["regular"] - o["preco"]
            linhas.append(f"📉 <b>−{o['desconto']}%</b> · economia de <b>{formatar_preco(economia, o['moeda'])}</b>")
        if o.get("so_assinante"):
            linhas.append(f"💎 Preço exclusivo para assinantes PS Plus (você tem o {html.escape(PS_ASSINATURA)})")
        if o.get("extra_plus"):
            linhas.append(f"➕ Com seu PS Plus: <b>mais {o['extra_plus']}% de desconto</b> no carrinho")
        if o.get("teste_gratis"):
            linhas.append(f"🧪 Você pode testar de graça antes com o seu PS Plus {html.escape(PS_ASSINATURA)}")
        if o.get("no_game_pass"):
            linhas.append("ℹ️ Também está no Game Pass")

    # Bloco 3: período da promoção
    linhas.append("")
    inicio, fim = data_curta(o.get("inicio")), data_curta(o.get("expira"))
    if o.get("incluso"):
        pass  # catálogo de assinatura não tem "promoção"
    elif inicio and fim:
        linhas.append(f"📅 <b>Promoção:</b> {inicio} → {fim}")
    elif fim:
        linhas.append(f"📅 <b>Promoção até:</b> {fim}")
    elif inicio:
        linhas.append(f"📅 <b>Promoção desde:</b> {inicio} (sem data de fim divulgada)")
    else:
        linhas.append("📅 <b>Promoção:</b> sem data de fim divulgada pela loja")
    restante = tempo_restante(o.get("expira"))
    if restante:
        linhas.append(restante)

    # Bloco 4: loja e avaliação
    detalhes = [f"🏪 {html.escape(o['loja'])}"]
    if o.get("nota") is not None:
        n = o.get("n_avaliacoes")
        qtd = f" ({n:,})".replace(",", ".") if n else ""
        detalhes.append(f"⭐ {o['nota']}% positivas{qtd}")
    elif o.get("estrelas"):
        media, n = o["estrelas"]
        qtd = f" ({n:,})".replace(",", ".") if n else ""
        detalhes.append(f"⭐ {media:.1f}/5{qtd}".replace(".", ",", 1))
    linhas.append(" · ".join(detalhes))

    texto = "\n".join(linhas)

    # Bloco 5: resumo, numa citação que se expande ao tocar
    if o.get("resumo"):
        moldura = len("\n\n<blockquote expandable></blockquote>") + len(SEPARADOR) + 1
        espaco = LIMITE_LEGENDA_FOTO - len(texto) - moldura - 10
        resumo = limpar_texto(o["resumo"], max(0, min(TAMANHO_RESUMO, espaco)))
        if resumo:
            texto += f"\n{SEPARADOR}\n<blockquote expandable>{html.escape(resumo)}</blockquote>"
    return texto


def formatar_linha(o):
    titulo = html.escape(o["titulo"])
    loja = html.escape(o["loja"])
    link = html.escape(o["url"], quote=True)
    if o.get("incluso"):
        linha = f'• <a href="{link}">{titulo}</a> — <b>NO SEU PS PLUS</b>'
    elif o["gratis"]:
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
    """Ordem de importância: grátis, novo no PS Plus, menor histórico, maior desconto."""
    return (not o["gratis"], not o.get("incluso"), not o["menor_hist"], -o["desconto"], o["titulo"].lower())


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
                chave_itad, ids, {"flag": "H", "cut": faixa(HIST_DESCONTO_MINIMO, 99), **filtro_qualidade()},
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

    # Consoles: cada loja isolada, para a falha de uma não derrubar as outras.
    if INCLUIR_PLAYSTATION:
        try:
            ofertas += buscar_playstation()
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as e:
            falhas.append(f"PlayStation Store: {e}")
    if INCLUIR_XBOX:
        try:
            ofertas += buscar_xbox()
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as e:
            falhas.append(f"Xbox Store: {e}")

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
        # Só para ver como as mensagens ficam: a melhor oferta atual de cada loja, sem mexer no estado.
        exemplos, lojas_vistas = [], set()
        for o in sorted(unicas.values(), key=prioridade):
            if o["loja"] not in lojas_vistas:
                lojas_vistas.add(o["loja"])
                exemplos.append(o)
        enviar_mensagens(token, chat_id, ["👀 <b>Exemplo de como os alertas vão chegar:</b>"])
        for o in exemplos:
            time.sleep(1.2)
            enviar_detalhado(token, chat_id, enriquecer(o, chave_itad))
        return

    if not estado["inicializado"]:
        # Primeira execução: registra o que já está em promoção sem mandar tudo de uma vez.
        gratis = [o for o in novas.values() if o["gratis"]]
        titulo = "atualizado" if estado["enviados"] else "ativado"
        enviar_mensagens(token, chat_id, [
            f"🤖 <b>Bot de promoções {titulo}!</b>\n"
            f"Há {len(unicas)} ofertas ativas que se encaixam nos seus critérios; registrei todas "
            "para não lotar seu chat. A partir de agora você só recebe as novas."])
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
