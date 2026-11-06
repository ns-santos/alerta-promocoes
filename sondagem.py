"""Teste de conexão com as lojas PlayStation e Xbox, rodado no GitHub Actions.
Não usa nenhuma chave e não manda nada para o Telegram: só imprime o que as lojas respondem."""
import json, re, urllib.parse, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36",
      "Accept-Language": "pt-BR,pt;q=0.9"}


def get(url, extra=None, texto=False):
    req = urllib.request.Request(url, headers={**UA, **(extra or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        corpo = r.read().decode("utf-8", "replace")
        return r.status, (corpo if texto else json.loads(corpo))


def teste(nome, fn):
    try:
        print(f"[OK] {nome}: {fn()}")
    except Exception as e:  # noqa: BLE001 - queremos ver qualquer erro
        print(f"[FALHOU] {nome}: {type(e).__name__}: {str(e)[:300]}")


def ps_grid():
    vars_ = {"id": "4cbf39e2-5749-4970-ba81-93a489e4570c", "pageArgs": {"size": 1000, "offset": 0},
             "sortBy": {"name": "sales30", "isAscending": False}, "filterBy": [], "facetOptions": []}
    ext = {"persistedQuery": {"version": 1, "sha256Hash": "9845afc0dbaab4965f6563fffc703f588c8e76792000e8610843b8d3ee9c4c09"}}
    url = ("https://web.np.playstation.com/api/graphql/v1/op?operationName=categoryGridRetrieve"
           f"&variables={urllib.parse.quote(json.dumps(vars_))}&extensions={urllib.parse.quote(json.dumps(ext))}")
    st, d = get(url, {"x-psn-store-locale-override": "pt-BR", "content-type": "application/json"})
    g = d["data"]["categoryGridRetrieve"]
    ps = g["products"]
    desc = [p for p in ps if (p.get("price") or {}).get("discountText")]
    global PS_EXEMPLO
    PS_EXEMPLO = next((p["id"] for p in desc), ps[0]["id"])
    return f"HTTP {st}, total={g['pageInfo']['totalCount']}, recebidos={len(ps)}, com desconto={len(desc)}, ex={[p['name'] + ' ' + p['price']['discountText'] for p in desc[:3]]}"


def ps_produto():
    st, h = get(f"https://store.playstation.com/pt-br/product/{PS_EXEMPLO}", texto=True)
    fim = re.search(r'"endTime":"?(\d+)', h)
    nota = re.search(r'"averageRating":([\d.]+),"totalRatingsCount":(\d+)', h)
    longa = re.search(r'"type":"LONG"[^}]*?"value":"(.{0,80})', h)
    return f"HTTP {st}, {len(h)} bytes, endTime={fim and fim.group(1)}, nota={nota and nota.groups()}, desc={longa and longa.group(1)}"


def xbox_lista():
    st, d = get("https://reco-public.rec.mp.microsoft.com/channels/Reco/V8.0/Lists/Computed/Deal"
                "?Market=BR&Language=pt-BR&ItemTypes=Game&deviceFamily=Windows.Xbox&count=200&skipitems=0")
    global XBOX_IDS
    XBOX_IDS = [i["Id"] for i in d.get("Items", [])]
    return f"HTTP {st}, itens={len(XBOX_IDS)}, paging={d.get('PagingInfo')}"


def xbox_detalhes():
    st, d = get("https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds=" + ",".join(XBOX_IDS[:3])
                + "&market=BR&languages=pt-BR")
    saida = []
    for p in d["Products"]:
        lp = p["LocalizedProperties"][0]
        precos = []
        for sku in p.get("DisplaySkuAvailabilities", []):
            for a in sku.get("Availabilities", []):
                pr = (a.get("OrderManagementData") or {}).get("Price") or {}
                if "Purchase" in (a.get("Actions") or []):
                    precos.append({"lista": pr.get("ListPrice"), "msrp": pr.get("MSRP"), "moeda": pr.get("CurrencyCode"),
                                   "fim": (a.get("Conditions") or {}).get("EndDate"),
                                   "inicio": (a.get("Conditions") or {}).get("StartDate"),
                                   "afirmacao": a.get("AffirmationId"), "remed": a.get("RemediationRequired")})
        uso = ((p.get("MarketProperties") or [{}])[0].get("UsageData") or [])
        saida.append({"titulo": lp.get("ProductTitle"), "curta": (lp.get("ShortDescription") or "")[:60],
                      "imagens": sorted({i.get("ImagePurpose") for i in lp.get("Images", [])}),
                      "uso": uso[-1:] if uso else None, "precos": precos[:4]})
    return f"HTTP {st}, " + json.dumps(saida, ensure_ascii=False)[:2500]


PS_EXEMPLO, XBOX_IDS = None, []
teste("PlayStation lista PS5", ps_grid)
if PS_EXEMPLO:
    teste("PlayStation página do produto", ps_produto)
teste("Xbox lista de ofertas", xbox_lista)
if XBOX_IDS:
    teste("Xbox detalhes", xbox_detalhes)
