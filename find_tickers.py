import yfinance as yf

# yFinance sometimes needs different suffixes for NSE stocks
# Testing alternate forms
alternates = {
    'ZOMATO': ['ZOMATO.NS', 'ZOMATO.BO'],
    'TATAMOTORS': ['TATAMOTORS.NS', 'TATAMTRDVR.NS', 'TM'],
    'DRLALPATHLABS': ['DRLALPATHLABS.NS', 'DRREDDY.NS'],
    'FLAMINGO': ['FLAMINGO.NS', 'FMGOING.NS'],
    'AEGISCHEM': ['AEGISCHEM.NS', 'AEGISC.NS'],
    'JCHAC': ['JCHAC.NS', 'JOHNSONCONT.NS'],
    'CENTURYTEX': ['CENTURYTEX.NS', 'CENTURYTEXTIND.NS'],
}

for name, variants in alternates.items():
    found = False
    for ticker in variants:
        try:
            p = yf.Ticker(ticker).fast_info.last_price
            if p and p > 0:
                print(f"FOUND {name} -> {ticker}: {p:.2f}")
                found = True
                break
        except Exception:
            pass
    if not found:
        print(f"NONE  {name}: no valid yFinance ticker found")
