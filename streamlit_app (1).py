"""
Streamlit App — Solapur Division Interactive Railway Map with Live NTES Trains.

Deploy for FREE on Streamlit Community Cloud (https://share.streamlit.io) to get
a single shareable public URL with live NTES trains—no localhost or API key needed.

Run locally:
  pip install streamlit ntes-client
  streamlit run streamlit_app.py
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import streamlit.components.v1 as components

try:
    from ntes import NTESClient
except ImportError:
    NTESClient = None

IST = timezone(timedelta(hours=5, minutes=30))
JUNCTIONS = ["WADI", "KLBG", "SUR", "KWV", "DD", "MRJ", "LUR"]

st.set_page_config(
    page_title="Solapur Division Live Railway Map",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide default Streamlit padding/chrome so the map fills the screen
st.markdown(
    """
    <style>
      .block-container { padding: 0.4rem 0.6rem 0 0.6rem; max-width: 100%; }
      header[data-testid="stHeader"] { height: 2.2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _parse_delay_minutes(val: Any) -> int:
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s or "on time" in s.lower() or "right time" in s.lower():
        return 0
    m_hm = re.search(r"(\d+)\s*:\s*(\d+)", s)
    if m_hm:
        return int(m_hm.group(1)) * 60 + int(m_hm.group(2))
    m_num = re.search(r"(-?\d+)", s)
    return int(m_num.group(1)) if m_num else 0


def _parse_ntes_eta_etd_ms(val: Optional[str], ref_year: int) -> Optional[int]:
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if s.lower() in ("source", "destination", "", "**ua**"):
        return None
    m = re.match(r"^(\d{1,2}:\d{2})\s+(\d{1,2}-[A-Za-z]{3})$", s)
    if not m:
        return None
    hhmm, ddmon = m.group(1), m.group(2)
    try:
        dt = datetime.strptime(f"{ddmon}-{ref_year} {hhmm}", "%d-%b-%Y %H:%M").replace(tzinfo=IST)
        now_ist = datetime.now(IST)
        if dt - now_ist > timedelta(days=180):
            dt = dt.replace(year=ref_year - 1)
        elif now_ist - dt > timedelta(days=180):
            dt = dt.replace(year=ref_year + 1)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _pick_active_start_date(client: Any, train_no: str) -> str:
    now_ist = datetime.now(IST)
    fallback = now_ist.strftime("%d-%b-%Y")
    try:
        info = client.train_info(train_no) or {}
        instances = info.get("vInstanceList") or []
        for inst in instances:
            if inst.get("trainStatus") == 1 and inst.get("startDate"):
                return str(inst["startDate"]).strip()
        upcoming = [inst for inst in instances if inst.get("trainStatus") == 0 and inst.get("startDate")]
        if upcoming:
            return str(upcoming[-1]["startDate"]).strip()
        if instances and instances[0].get("startDate"):
            return str(instances[0]["startDate"]).strip()
    except Exception:
        pass
    return fallback


def _normalize_from_live_status(train_no: str, fallback_name: str, live: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    stns = live.get("STNS") or []
    if not stns:
        return None

    now_ist = datetime.now(IST)
    now_ms = int(now_ist.timestamp() * 1000)
    ref_year = now_ist.year

    stops: List[Dict[str, Any]] = []
    prev_ts = -1
    for st_row in stns:
        code = str(st_row.get("SC") or "").strip().upper()
        if not code:
            continue
        arr = _parse_ntes_eta_etd_ms(st_row.get("ETA") or st_row.get("STA"), ref_year)
        dep = _parse_ntes_eta_etd_ms(st_row.get("ETD") or st_row.get("STD"), ref_year)
        if arr is None and dep is None:
            continue
        if arr is None:
            arr = dep
        if dep is None:
            dep = arr
        if arr < prev_ts:
            arr = prev_ts
        if dep < arr:
            dep = arr
        prev_ts = dep
        stops.append({
            "code": code,
            "name": str(st_row.get("SN") or code).strip(),
            "arr": arr,
            "dep": dep,
            "halt": True,
        })

    if not stops:
        return None

    trunst = live.get("TRUNST", 0)
    cpos = str(live.get("CPOS") or live.get("LUPDFULL") or "").strip()
    lstn = str(live.get("LSTN") or "").strip().upper()
    npstn = str(live.get("NPSTN") or live.get("NSTN") or "").strip().upper()
    levnt = str(live.get("LEVNT") or "").strip().upper()
    delay_min = _parse_delay_minutes(live.get("LDEL"))

    loc_obj = None
    if trunst == 1 and lstn:
        is_arrived = (levnt == "AR") or ("arrived" in cpos.lower())
        loc_obj = {
            "stationCode": lstn,
            "nextStationCode": npstn or None,
            "status": "at-station" if is_arrived else "departed",
            "distanceFromLastStationKm": 0 if is_arrived else 2.0,
            "speedKmh": 0 if is_arrived else 60,
        }
        cur_idx = next((i for i, s in enumerate(stops) if s["code"] == lstn), -1)
        if cur_idx != -1 and not is_arrived and cur_idx + 1 < len(stops):
            curr = stops[cur_idx]
            nxt = stops[cur_idx + 1]
            if now_ms <= curr["dep"]:
                shift_ms = (now_ms - curr["dep"]) - 120000
                for s in stops:
                    s["arr"] += shift_ms
                    s["dep"] += shift_ms
            elif now_ms >= nxt["arr"]:
                bump_ms = (now_ms - nxt["arr"]) + 120000
                for s in stops[cur_idx + 1:]:
                    s["arr"] += bump_ms
                    s["dep"] += bump_ms

    return {
        "id": str(train_no),
        "number": str(train_no),
        "name": str(live.get("TNM") or fallback_name or train_no).strip(),
        "status": cpos or ("running" if trunst == 1 else "scheduled"),
        "delayMin": delay_min,
        "updatedAt": int(time.time() * 1000),
        "stops": stops,
        "loc": loc_obj,
        "source": "live",
    }


def _fetch_board(stn: str) -> tuple:
    client = NTESClient(timeout=10, retries=2)
    out = []
    err = None
    try:
        board = client.station_live(stn, hours=4) or {}
        entries = board.get("TrainsAtStation") or board.get("trains") or []
        for item in entries:
            tno = str(item.get("TrainNumber") or item.get("trainNumber") or "").strip()
            if tno:
                out.append((tno, str(item.get("TrainName") or item.get("trainName") or tno).strip()))
    except Exception as exc:
        err = f"{stn}: {exc}"
    return out, err


def _fetch_one_train(tno: str, name: str) -> Optional[Dict[str, Any]]:
    client = NTESClient(timeout=10, retries=2)
    try:
        start_date = _pick_active_start_date(client, tno)
        live = client.live_status(tno, start_date) or {}
        return _normalize_from_live_status(tno, name, live)
    except Exception:
        return None


@st.cache_data(ttl=180, show_spinner="Fetching live NTES trains across Solapur Division...")
def fetch_live_network_trains() -> Dict[str, Any]:
    if NTESClient is None:
        return {"trains": [], "board_count": 0, "errors": ["ntes-client not installed"]}
    seen: Dict[str, str] = {}
    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for rows, err in pool.map(_fetch_board, JUNCTIONS):
            if err:
                errors.append(err)
            for tno, name in rows:
                if tno not in seen:
                    seen[tno] = name

    if not seen:
        return {"trains": [], "board_count": 0, "errors": errors}

    trains: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_fetch_one_train, tno, name) for tno, name in seen.items()]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                trains.append(res)
    return {"trains": trains, "board_count": len(seen), "errors": errors}


col1, col2 = st.columns([5, 1])
with col2:
    if st.button("Refresh Live Trains", use_container_width=True):
        fetch_live_network_trains.clear()

if NTESClient is None:
    st.error("`ntes-client` is not installed in this Python environment. Run: `pip install ntes-client`")

res_data = fetch_live_network_trains()
trains = res_data["trains"]
with col1:
    err_note = f" · Errors: {'; '.join(res_data['errors'][:2])}" if res_data["errors"] else ""
    st.caption(
        f"Live NTES Feed · {len(trains)}/{res_data['board_count']} trains loaded across {', '.join(JUNCTIONS)} · "
        f"Updated {datetime.now(IST).strftime('%H:%M:%S IST')}{err_note}"
    )

html_path = Path(__file__).parent / "railway_network_map.html"
if not html_path.exists():
    st.error("Place `railway_network_map.html` in the same folder as `streamlit_app.py`.")
else:
    html_text = html_path.read_text(encoding="utf-8")
    # Inject live trains into <head> before the map script initializes
    inject_script = f"""
    <script>
      window.__PRELOADED_LIVE_TRAINS__ = {json.dumps(trains)};
    </script>
    </head>
    """
    html_text = html_text.replace("</head>", inject_script, 1)
    components.html(html_text, height=820, scrolling=False)
