import WS


def test_global_streams_only_during_scan_window():
    assert WS._PERIODIC_SCAN and WS._RADAR_STREAM == "bookTicker"
    c = WS.SymbolWebSocketPriceCache(["AAAUSDT"])
    radar = {"aaausdt@bookTicker", "aaausdt@ticker", "aaausdt@markPrice@1s"}
    assert c._desired_streams() == radar
    c._scan_active = True
    assert c._desired_streams() == radar | {WS._ALL_MARKET_STREAM, WS._ALL_TICKER_STREAM}
