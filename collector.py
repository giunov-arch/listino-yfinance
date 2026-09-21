#!/usr/bin/env python3
"""
Collector for "Il Listino — Portafoglio".

Reads the instrument universe from universo.json — which now also carries
a static "f" baseline per ticker, the same fallback role Il Listino's own
built-in demo figures play — pulls STORIA_ANNI years of daily OHLCV bars,
12 fundamental fields, and (where Yahoo has it) analyst consensus via
yfinance, and writes dati/snapshot.json in the shape the app's "Snapshot
GitHub" loader expects:

{
  "generato_il": "YYYY-MM-DD",
  "titoli": [
    {
      "ticker": "ISP.MI", "nome": "Intesa Sanpaolo", "settore": "Banche",
      "barre": [{"d": "2026-08-01", "o": 4.00, "h": 4.05, "l": 3.98,
                 "c": 4.02, "v": 5200000}, ...],
      "f": {"mcap": 82.0, "pe": 8.6, "pb": 1.25, "evEbitda": null,
            "roe": 15.2, "margine": 55.0, "cet1": 13.9,
            "divYield": 8.1, "payout": 70.0,
            "crescitaRic": 4.5, "crescitaEps": 9.0, "fcfYield": 11.0},
      "con": {"tp": 4.6, "lo": 4.0, "hi": 5.1, "n": 18, "b": 11, "h": 6, "s": 1}
    }
  ]
}

Fallback: for every one of the 12 fields in "f", a value Yahoo doesn't
return for a given ticker falls back to that ticker's static entry in
universo.json instead of coming through as null — same principle Il
Listino itself uses when merging a live fetch over what it already has
(see fondi() below). CET1 in particular is never available through
yfinance at all — it isn't a field Yahoo publishes — so it always comes
from universo.json; every other field is live-first, static-fallback.

Run a dry run on one ticker before trusting the full pipeline — Yahoo has
no official API; yfinance wraps undocumented endpoints whose field names
and rate-limit behavior can change without notice. From this directory:

    python3 -c "
import json
from collector import scarica_titolo, UNIVERSO_PATH
universo = json.loads(UNIVERSO_PATH.read_text())
print(scarica_titolo(next(v for v in universo if v['ticker'] == 'ISP.MI')))
"

If that prints a plausible bar list, a fundamentals dict with most fields
filled in, and (ideally) a consensus dict, the rest of the universe should
work too.
"""

import json
import sys
import time
from datetime import date
from pathlib import Path

import yfinance as yf

STORIA_ANNI = 2        # matches Il Listino's own STORIA_ANNI convention
PAUSA_SECONDI = 1.5    # politeness delay between tickers — see chat notes
                        # on why GitHub Actions' shared IPs make yfinance
                        # more 429-prone than a home connection
MINIMO_RIUSCITI = 0.5  # abort without writing if fewer than half succeed,
                        # rather than publish a half-empty snapshot
CAMPI_F = ["mcap", "pe", "pb", "evEbitda", "roe", "margine", "debtEquity",
           "divYield", "payout", "crescitaRic", "crescitaEps", "fcfYield", "cet1"]

QUI = Path(__file__).parent
UNIVERSO_PATH = QUI / "universo.json"
OUTPUT_PATH = QUI / "dati" / "snapshot.json"


def normalizza_percentuale(valore):
    """yfinance returns some fields as a fraction (0.152) and others
    already as a percentage (15.2), inconsistently across fields and
    versions — there is no reliable way to know which in advance.
    Heuristic: a value under 1 in absolute terms is treated as a
    fraction and scaled to percent."""
    if valore is None:
        return None
    return valore * 100 if abs(valore) < 1 else valore


def arrotonda(valore, cifre=2):
    return round(valore, cifre) if valore is not None else None


def leggi_barre(storico):
    barre = []
    for indice, riga in storico.iterrows():
        o, h, l, c = riga.get("Open"), riga.get("High"), riga.get("Low"), riga.get("Close")
        if any(x is None or x != x for x in (o, h, l, c)):  # x != x intercetta i NaN di pandas, "is None" da solo non basta
            continue
        v = riga.get("Volume")
        barre.append({
            "d": indice.strftime("%Y-%m-%d"),
            "o": round(float(o), 4), "h": round(float(h), 4),
            "l": round(float(l), 4), "c": round(float(c), 4),
            "v": int(v) if v == v and v is not None else 0,  # v==v esclude NaN
        })
    return barre


def leggi_fondamentali_live(info):
    """Solo i campi che Yahoo ha davvero restituito — un valore assente
    resta fuori dal dict piuttosto che entrarci come None, così fondi()
    qui sotto non lo confonde con "Yahoo dice esplicitamente niente"."""
    mcap_raw = info.get("marketCap")
    mcap = (mcap_raw / 1e9) if mcap_raw is not None else None
    pe = info.get("trailingPE") or info.get("forwardPE")
    roe = normalizza_percentuale(info.get("returnOnEquity"))
    # stesso campo del percorso live in JS (financialData.profitMargins),
    # non operatingMargins: "margine" deve voler dire la stessa cosa sia
    # che arrivi da qui sia da un "Aggiorna fondamentali" nell'app.
    margine = normalizza_percentuale(info.get("profitMargins"))
    debt_to_equity = info.get("debtToEquity")
    debito_equity = (debt_to_equity / 100) if debt_to_equity is not None else None
    div_yield = normalizza_percentuale(info.get("dividendYield"))
    payout = normalizza_percentuale(info.get("payoutRatio"))
    crescita_ricavi = normalizza_percentuale(info.get("revenueGrowth"))
    crescita_utile = normalizza_percentuale(info.get("earningsGrowth"))
    fcf = info.get("freeCashflow")
    fcf_yield = (fcf / mcap_raw * 100) if (fcf is not None and mcap_raw) else None

    grezzi = {
        "mcap": arrotonda(mcap, 2), "pe": arrotonda(pe, 2),
        "pb": arrotonda(info.get("priceToBook"), 2),
        "evEbitda": arrotonda(info.get("enterpriseToEbitda"), 2),
        "roe": arrotonda(roe, 2), "margine": arrotonda(margine, 2),
        "debtEquity": arrotonda(debito_equity, 3),
        "divYield": arrotonda(div_yield, 2), "payout": arrotonda(payout, 2),
        "crescitaRic": arrotonda(crescita_ricavi, 2),
        "crescitaEps": arrotonda(crescita_utile, 2),
        "fcfYield": arrotonda(fcf_yield, 2),
        # cet1 non è un campo yfinance: mai presente qui, resta sempre
        # e solo quello statico di universo.json.
    }
    return {k: v for k, v in grezzi.items() if v is not None}


def leggi_consenso(tk, info):
    tp = info.get("targetMeanPrice")
    if tp is None:
        return None
    lo, hi = info.get("targetLowPrice"), info.get("targetHighPrice")
    b = h = s = 0
    try:
        trend = tk.recommendations
    except Exception:
        trend = None
    if trend is not None and not trend.empty:
        riga = trend.iloc[0]  # periodo più recente (di norma "0m")
        b = int(riga.get("strongBuy", 0) or 0) + int(riga.get("buy", 0) or 0)
        h = int(riga.get("hold", 0) or 0)
        s = int(riga.get("sell", 0) or 0) + int(riga.get("strongSell", 0) or 0)
    n = info.get("numberOfAnalystOpinions") or (b + h + s) or None
    if n is None:
        return None
    return {
        "tp": round(tp, 3),
        "lo": round(lo, 3) if lo is not None else round(tp, 3),
        "hi": round(hi, 3) if hi is not None else round(tp, 3),
        "n": int(n), "b": b, "h": h, "s": s,
    }


def fondi(base, live):
    """Live (i campi che Yahoo ha davvero restituito) vince campo per
    campo; quello che manca resta il valore statico di universo.json —
    stesso principio della fusione che l'app fa lato client quando un
    "Aggiorna fondamentali" non copre tutto."""
    risultato = dict(base or {})
    risultato.update(live or {})
    return risultato


def scarica_titolo(voce):
    ticker = voce["ticker"]
    tk = yf.Ticker(ticker)
    storico = tk.history(period=f"{STORIA_ANNI}y", interval="1d")
    if storico.empty:
        raise RuntimeError("nessun dato storico restituito")
    barre = leggi_barre(storico)
    if len(barre) < 60:
        raise RuntimeError(f"solo {len(barre)} barre restituite, troppo poche")

    info = tk.info or {}
    fondamentali = fondi(voce.get("f"), leggi_fondamentali_live(info))
    consenso = leggi_consenso(tk, info)
    return barre, fondamentali, consenso


def main():
    universo = json.loads(UNIVERSO_PATH.read_text(encoding="utf-8"))
    titoli = []
    falliti = []

    for voce in universo:
        ticker = voce["ticker"]
        try:
            barre, fondamentali, consenso = scarica_titolo(voce)
            titoli.append({
                "ticker": ticker,
                "nome": voce["nome"],
                "settore": voce["settore"],
                "barre": barre,
                "f": fondamentali,
                "con": consenso,
            })
            campi_ok = sum(1 for c in CAMPI_F if fondamentali.get(c) is not None)
            print(f"  ok   {ticker}: {len(barre)} barre, {campi_ok}/13 campi fondamentali" + (", consenso" if consenso else ""))
        except Exception as e:
            falliti.append(ticker)
            print(f"  FAIL {ticker}: {e}", file=sys.stderr)
        time.sleep(PAUSA_SECONDI)

    if len(titoli) < len(universo) * MINIMO_RIUSCITI:
        print(
            f"Solo {len(titoli)}/{len(universo)} titoli riusciti — sotto la soglia "
            f"di sicurezza: non scrivo lo snapshot per non pubblicare dati a metà.",
            file=sys.stderr,
        )
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"generato_il": date.today().isoformat(), "titoli": titoli}
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Scritto {OUTPUT_PATH} — {len(titoli)}/{len(universo)} titoli riusciti.")
    if falliti:
        print(f"Falliti (saltati): {', '.join(falliti)}", file=sys.stderr)


if __name__ == "__main__":
    main()
