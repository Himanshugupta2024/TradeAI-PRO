import yfinance as yf

tickers = [
    'ZOMATO.NS', 'TATAMOTORS.NS', 'HUL.NS', 'DRLALPATHLABS.NS',
    'FLAMINGO.NS', 'AEGISCHEM.NS', 'JCHAC.NS', 'CENTURYTEX.NS', 'HINDUNILVR.NS'
]

for t in tickers:
    try:
        p = yf.Ticker(t).fast_info.last_price
        if p and p > 0:
            print(f"OK  {t}: {p:.2f}")
        else:
            print(f"NO_PRICE {t}")
    except Exception as e:
        print(f"BAD {t}: {e}")
