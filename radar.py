"""Italy flight price radar. No credentials needed for demo mode."""
import argparse
import datetime as dt
import itertools
import json
import os
import pathlib
import random
import hashlib
import time
from decimal import Decimal
from urllib.parse import urlencode
import requests

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / 'docs' / 'data.json'
STATE = ROOT / 'state.json'
BASE = 'https://api.amadeus.com' if os.getenv('AMADEUS_ENV') == 'production' else 'https://test.api.amadeus.com'


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def days(iso, delta):
    d = dt.date.fromisoformat(iso)
    return [(d + dt.timedelta(days=x)).isoformat() for x in range(-delta, delta + 1)]


def priority(q, cfg):
    """Prefer exact travel dates, PVG and FCO/MXP; rotate among equal scores."""
    d = abs((dt.date.fromisoformat(q['departure']) - dt.date.fromisoformat(cfg['departure_date'])).days)
    r = abs((dt.date.fromisoformat(q['return']) - dt.date.fromisoformat(cfg['return_date'])).days)
    return (d + r, q['origin'] != 'PVG', q['arrive'] == 'LIN', q['leave'] == 'LIN', q['arrive'] != q['leave'])


def monthly_usage(state, now):
    month = now[:7]
    if state.get('billing_month') != month:
        state['billing_month'] = month
        state['api_calls_reserved'] = 0
    return state


def pricing():
    """Fail closed: user must configure exact quota and unit price from account."""
    try:
        free = int(os.environ['AMADEUS_MONTHLY_FREE_CALLS'])
        usd = Decimal(os.environ['AMADEUS_PRICE_USD_PER_CALL'])
        fx = Decimal(os.environ['USD_CNY_BUDGET_RATE'])
        if min(free, usd, fx) < 0 or fx == 0:
            raise ValueError('negative pricing')
        return free, usd * fx
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise RuntimeError('缺少或无效的 API 计费参数；必须设置 AMADEUS_MONTHLY_FREE_CALLS、AMADEUS_PRICE_USD_PER_CALL、USD_CNY_BUDGET_RATE，已安全停止付费查询') from exc


def projected_cost(calls, free, unit_cny):
    return Decimal(max(0, calls - free)) * unit_cny


def offer_fingerprint(o):
    legs = o.get('legs', [])
    return hashlib.sha256(json.dumps([o.get('origin'), o.get('arrive'), o.get('leave'), o.get('departure'), o.get('return'), legs], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def all_queries(cfg):
    out = []
    for origin, inbound, outbound, dep, ret in itertools.product(
        cfg['origins'], cfg['italy_airports'], cfg['italy_airports'],
        days(cfg['departure_date'], cfg['flex_days']),
        days(cfg['return_date'], cfg['flex_days'])
    ):
        if dep < ret:
            out.append({'origin': origin, 'arrive': inbound, 'leave': outbound,
                        'departure': dep, 'return': ret})
    return sorted(out, key=lambda q: priority(q, cfg))


def token(session):
    key, secret = os.getenv('AMADEUS_CLIENT_ID'), os.getenv('AMADEUS_CLIENT_SECRET')
    if not key or not secret:
        raise RuntimeError('请设置 AMADEUS_CLIENT_ID 和 AMADEUS_CLIENT_SECRET')
    r = session.post(BASE + '/v1/security/oauth2/token', data={
        'grant_type': 'client_credentials', 'client_id': key, 'client_secret': secret
    }, timeout=25)
    r.raise_for_status()
    return r.json()['access_token']


def search(session, access_token, query, max_offers):
    # POST supports open-jaw, including different Italian arrival/departure airports.
    payload = {
        'currencyCode': 'CNY',
        'originDestinations': [
            {'id': '1', 'originLocationCode': query['origin'],
             'destinationLocationCode': query['arrive'],
             'departureDateTimeRange': {'date': query['departure']}},
            {'id': '2', 'originLocationCode': query['leave'],
             'destinationLocationCode': query['origin'],
             'departureDateTimeRange': {'date': query['return']}}
        ],
        'travelers': [{'id': '1', 'travelerType': 'ADULT'}],
        'sources': ['GDS'],
        'searchCriteria': {'maxFlightOffers': max_offers}
    }
    r = session.post(BASE + '/v2/shopping/flight-offers',
        headers={'Authorization': 'Bearer ' + access_token,
                 'Content-Type': 'application/json',
                 'X-HTTP-Method-Override': 'GET'},
        json=payload, timeout=55)
    if r.status_code == 429:
        raise RuntimeError('API 429：达到速率或配额限制')
    r.raise_for_status()
    return r.json().get('data', [])


def checked_bag(offer):
    vals = []
    for traveler in offer.get('travelerPricings', []):
        for fare in traveler.get('fareDetailsBySegment', []):
            bags = fare.get('includedCheckedBags', {})
            vals.append(bool(bags.get('quantity', 0) or bags.get('weight', 0)))
    return bool(vals) and all(vals)


def normalize(offer, query, cfg, timestamp):
    itineraries = offer.get('itineraries', [])
    if len(itineraries) != 2:
        return None
    stops = [max(0, len(it.get('segments', [])) - 1) for it in itineraries]
    if any(n > cfg['max_stops_per_direction'] for n in stops):
        return None
    if any(not it.get('segments') for it in itineraries):
        return None
    bag = checked_bag(offer)
    if cfg.get('require_checked_bag') and not bag:
        return None
    try:
        amount = float(Decimal(offer['price']['grandTotal']))
    except (KeyError, ValueError, TypeError):
        return None
    currency = offer['price'].get('currency', 'CNY')
    if currency != 'CNY':
        return None
    legs = []
    for it in itineraries:
        segments = it['segments']
        legs.append({
            'from': segments[0]['departure']['iataCode'],
            'to': segments[-1]['arrival']['iataCode'],
            'depart_at': segments[0]['departure']['at'],
            'arrive_at': segments[-1]['arrival']['at'],
            'stops': len(segments) - 1,
            'airlines': list(dict.fromkeys(s.get('carrierCode', '?') for s in segments)),
            'via': [s['arrival']['iataCode'] for s in segments[:-1]]
        })
    return {'key': '|'.join(map(str, [query['origin'], query['arrive'], query['leave'],
                                     query['departure'], query['return'],
                                     ','.join(legs[0]['airlines']), ','.join(legs[1]['airlines'])])),
            'price_cny': amount, 'currency': currency, 'legs': legs,
            'departure': query['departure'], 'return': query['return'],
            'arrive': query['arrive'], 'leave': query['leave'],
            'origin': query['origin'], 'checked_bag_included': bag,
            'checked_bag_status': 'included' if bag else 'not_confirmed',
            'source': 'Amadeus Flight Offers Search', 'checked_at': timestamp,
            'below_target': amount <= cfg['target_cny']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    cfg = load_json(ROOT / 'config.json', {})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    old = load_json(DATA, {'history': [], 'offers': [], 'alerts': []})
    state = monthly_usage(load_json(STATE, {'cursor': 0}), now)
    queries = all_queries(cfg)
    cursor = state['cursor'] % len(queries)
    count = min(cfg['queries_per_run'], len(queries))
    budget_warning = False
    budget_stopped = False
    cost_cny = Decimal('0')
    if not args.demo:
        free, unit = pricing()
        budget = Decimal(str(cfg['monthly_budget_cny']))
        used = int(state['api_calls_reserved'])
        remaining = 0
        for i in range(count):
            if projected_cost(used + i + 1, free, unit) > budget:
                break
            remaining += 1
        count = remaining
        cost_cny = projected_cost(used, free, unit)
        budget_stopped = count == 0
        budget_warning = budget > 0 and projected_cost(used + count, free, unit) >= budget * Decimal('0.8')
    batch = [queries[(cursor + i) % len(queries)] for i in range(count)]
    new = []
    errors = []
    if args.demo:
        rng = random.Random(42)
        for q in batch:
            price = rng.randint(3800, 7400)
            new.append({'key': '|'.join(q.values()), 'price_cny': price, 'currency': 'CNY',
                        'origin': q['origin'], 'arrive': q['arrive'], 'leave': q['leave'],
                        'departure': q['departure'], 'return': q['return'],
                        'checked_bag_included': False, 'checked_bag_status': 'not_confirmed',
                        'legs': [], 'checked_at': now, 'below_target': price <= cfg['target_cny'],
                        'source': 'DEMO (synthetic)'})
    elif batch:
        with requests.Session() as session:
            auth = token(session)
            for q in batch:
                # Reserve BEFORE the request, including failures, to avoid undercounting.
                state['api_calls_reserved'] += 1
                STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
                try:
                    offers = search(session, auth, q, cfg['max_offers'])
                    for o in offers:
                        normalized = normalize(o, q, cfg, now)
                        if normalized:
                            new.append(normalized)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    errors.append(f"{q['origin']}-{q['arrive']}/{q['leave']} {q['departure']}: {exc}")
                time.sleep(0.4)
    old_offers = {o['key']: o for o in old.get('offers', [])}
    prior_alerts = old.get('alerts', [])
    fresh_alerts = []
    # Stable itinerary fingerprint avoids duplicate alerts from changed provider offer IDs.
    alert_state = state.setdefault('alert_state', {})
    for offer in new:
        fingerprint = offer_fingerprint(offer)
        previous_price = alert_state.get(fingerprint)
        price = offer['price_cny']
        if offer['below_target'] and (previous_price is None or price < previous_price):
            fresh_alerts.append(offer)
        alert_state[fingerprint] = price
        old_offers[offer['key']] = offer
    # Bound dedup state so it does not grow forever.
    if len(alert_state) > 10000:
        state['alert_state'] = dict(list(alert_state.items())[-5000:])
    history = old.get('history', [])
    history.extend({'checked_at': now, 'key': o['key'], 'price_cny': o['price_cny']}
                   for o in new)
    # Keep bounded state in GitHub; remove stale offers after 14 days.
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)
    def recent(o):
        try:
            return dt.datetime.fromisoformat(o['checked_at']) >= cutoff
        except (ValueError, KeyError):
            return False
    result = {
        'mode': 'demo' if args.demo else os.getenv('AMADEUS_ENV', 'test'),
        'updated_at': now, 'target_cny': cfg['target_cny'],
        'queries_this_run': len(batch), 'total_query_combinations': len(queries),
        'offers': sorted((o for o in old_offers.values() if recent(o)),
                         key=lambda o: o['price_cny'])[:300],
        'history': history[-3000:],
        'alerts': (fresh_alerts + prior_alerts)[:100],
        'billing': {'month': state['billing_month'], 'calls_reserved': state['api_calls_reserved'],
                    'free_calls_configured': None if args.demo else free,
                    'estimated_spend_cny': None if args.demo else str(projected_cost(state['api_calls_reserved'], free, unit)),
                    'monthly_budget_cny': cfg['monthly_budget_cny'],
                    'warning_80_percent': budget_warning, 'budget_stopped': budget_stopped},
        'errors': errors[:20]
    }
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    # Retry failed queries next run rather than silently skipping them.
    # Always advance past attempted combinations; prevents repeating failures indefinitely.
    state['cursor'] = (cursor + count) % len(queries)
    STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
    print(f"mode={result['mode']} queries={count} offers={len(new)} alerts={len(fresh_alerts)} errors={len(errors)}")
    if errors:
        print('\n'.join(errors))
    # Non-zero failure when nothing was successfully searched.
    if errors and not new:
        print('所有查询失败；费用预留记录已保存。')

if __name__ == '__main__':
    main()
