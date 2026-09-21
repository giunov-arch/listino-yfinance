#!/usr/bin/env python3
"""
Collector for "Il Listino — Portafoglio".

Reads the instrument universe from universo.json, pulls STORIA_ANNI years
of daily closing prices and a handful of fundamental fields via yfinance
for each ticker, and writes dati/snapshot.json in the exact shape the
app's "Snapshot GitHub" loader expects:

{
  "generato_il": "YYYY-MM-DD",
  "titoli": [
    {
      "ticker": "ENI.MI", "nome": "Eni", "settore": "Energia",
      "chiusure": [14.02, 14.10, ...],
      "f": {"pe": 7.8, "roe": 15.2, "crescitaRicavi": 2.1,
            "debitoEquity": 0.55, "rendimentoDividendo": 6.4}
    }
  ]
}

Run a dry run on one ticker before trusting the full pipeline — Yahoo has
no official API; yfinance wraps undocumented endpoints whose field names
and rate-limit behavior can change without notice. From this directory:

    python3 -c "from collector import scarica_titolo; print(scarica_titolo('ISP.MI'))"

If that prints a plausible price list and fundamentals dict, the rest of
the universe should work too.
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


def leggi_fondamentali(info):
    pe = info.get("trailingPE") or info.get("forwardPE")
    roe = normalizza_percentuale(info.get("returnOnEquity"))
    crescita = normalizza_percentuale(info.get("revenueGrowth"))
    debt_to_equity = info.get("debtToEquity")
    # yfinance expresses debtToEquity as a percentage (e.g. 55.3 = 0.553)
    debito_equity = (debt_to_equity / 100) if debt_to_equity is not None else None
    dividendo = normalizza_percentuale(info.get("dividendYield"))
    return {
        "pe": round(pe, 2) if pe is not None else None,
        "roe": round(roe, 2) if roe is not None else None,
        "crescitaRicavi": round(crescita, 2) if crescita is not None else None,
        "debitoEquity": round(debito_equity, 3) if debito_equity is not None else None,
        "rendimentoDividendo": round(dividendo, 2) if dividendo is not None else None,
    }


def scarica_titolo(ticker):
    tk = yf.Ticker(ticker)
    storico = tk.history(period=f"{STORIA_ANNI}y", interval="1d")
    if storico.empty:
        raise RuntimeError("nessun dato storico restituito")
    chiusure = [round(float(v), 4) for v in storico["Close"].tolist()]
    if len(chiusure) < 60:
        raise RuntimeError(f"solo {len(chiusure)} sedute restituite, troppo poche")
    fondamentali = leggi_fondamentali(tk.info or {})
    return chiusure, fondamentali


def main():
    universo = json.loads(UNIVERSO_PATH.read_text(encoding="utf-8"))
    titoli = []
    falliti = []

    for voce in universo:
        ticker = voce["ticker"]
        try:
            chiusure, fondamentali = scarica_titolo(ticker)
            titoli.append({
                "ticker": ticker,
                "nome": voce["nome"],
                "settore": voce["settore"],
                "chiusure": chiusure,
                "f": fondamentali,
            })
            print(f"  ok   {ticker}: {len(chiusure)} sedute")
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
