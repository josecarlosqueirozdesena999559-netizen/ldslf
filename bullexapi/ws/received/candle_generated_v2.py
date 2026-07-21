import bullexapi.constants as OP_code

def candle_generated_v2(api, message, dict_queue_add):
    if message["name"] == "candles-generated":
        Active_name = list(OP_code.ACTIVES.keys())[list(
                OP_code.ACTIVES.values()).index(message["msg"]["active_id"])]
        active = str(Active_name)
        candles = message["msg"].get("candles", {})
        if not isinstance(candles, dict):
            return
        for k, v in candles.items():
            if not isinstance(v, dict):
                continue
            v["active_id"] = message["msg"]["active_id"]
            v["at"] = message["msg"]["at"]
            v["ask"] = message["msg"]["ask"]
            v["bid"] = message["msg"]["bid"]
            try:
                size = int(k)
                from_ = int(v["from"])
                at = int(message["msg"]["at"])
            except (TypeError, ValueError, KeyError):
                continue
            is_current = from_ + size > at
            if is_current:
                v["close"] = message["msg"]["value"]
            v["size"] = size
            maxdict = api.real_time_candles_maxdict_table[Active_name][size]
            msg = v
            dict_queue_add(api.real_time_candles, maxdict, active, size, from_, msg)

        api.candle_generated_all_size_check[active] = True
