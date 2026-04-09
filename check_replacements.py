import yfinance as yf

replacements = [
    'SWIGGY.NS', 'M&M.NS', 'METROPOLIS.NS',
    'DEEPAKNTR.NS', 'RAYMOND.NS', 'NYKAA.NS', 'PAYTM.NS',
]

for t in replacements:
    try:
        p = yf.Ticker(t).fast_info.last_price
        if p and p > 0:
            print(f"OK  {t}: {p:.2f}")
        else:
            print(f"NO_PRICE {t}")
    except Exception as e:
        print(f"BAD {t}")
