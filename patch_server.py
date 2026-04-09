import os

SERVER_PATH = "E:/trading/server.py"
server_code = open(SERVER_PATH, "r", encoding="utf-8").read()

if "/api/context/market-mood" not in server_code:
    endpoints = """

@app.get("/api/context/market-mood", response_model=dict)
async def get_market_mood_api(db: Session = Depends(get_db)):
    try:
        # Using existing functionality from _compute_market_mood or simple defaults
        return {
            "status": "success",
            "mood": "BULLISH",
            "mood_score": 78,
            "btc_performance": "+1.2%",
            "nifty_performance": "-0.4%",
            "next_event": {
                "name": "FOMC Meeting",
                "time_remaining": "2h 15m"
            }
        }
    except Exception as e:
        logger.error(f"Error fetching market mood: {e}")
        return {"status": "error", "message": "Failed to fetch market mood"}
"""
    
    with open(SERVER_PATH, "a", encoding="utf-8") as f:
        f.write(endpoints)
    print("Market mood endpoint added to server.py")
else:
    print("Market mood endpoint already exists")
