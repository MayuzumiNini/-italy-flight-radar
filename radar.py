"""API.SH Kiwi monitor. Fail closed; never log credentials or raw responses."""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parent
UTC = dt.timezone.utc

class Stop(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def now():
    return dt.datetime.now(UTC)

def parse_time(s):
    d = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('timezone required')
    return d

def read(name):
    return json.loads((ROOT / name).read_text())

def write(name, value):
    p = ROOT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(p.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(p)

def persist():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise Stop('Live queries require durable GitHub Actions state')
    for cmd in [['git','add','state.json','docs'],
                ['git','commit','-m','Save flight monitor checkpoint [skip ci]'],
                ['git','push','origin','HEAD:main']]:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True)
        if r.returncode:
            raise Stop('State persistence failed; no further API calls')

def get(path, params=None):
    key = os.environ.get('APISH_API_KEY')
    if not key:
        raise Stop('APISH_API_KEY missing')
    url = 'https://api.api.sh' + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'x-api-key':key})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=130) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Body, request headers and exception URL are intentionally not logged.
        raise Stop('API HTTP ' + str(e.code)) from None
    except Exception:
        raise Stop('API connection/JSON failure; no automatic retry') from None

def account_guard(a, clock):
    try:
        c = a['credits']
        if a['success'] is not True or a['account']['unlimited'] is not False:
            raise ValueError()
        for k in ('free','plan','payg','total'):
            if type(c[k]) not in (int,float) or not math.isfinite(c[k]) or c[k] < 0:
                raise ValueError()
        # No paid bucket or plan is permitted by this monitor.
        if c['plan'] != 0 or c['payg'] != 0 or a['plan'] is not None or c['total'] != c['free']:
            raise Stop('Paid account resources detected; free-only monitor stopped')
        reset = parse_time(c['free_resets_at'])
        if not clock < reset <= clock + dt.timedelta(days=32):
            raise ValueError()
        if c['free'] <= 20:
            raise Stop('Free balance reserve reached (20 credits)')
        if a['spend']['this_month'] >= 950:
            raise Stop('UTC monthly account reserve reached (950 credits)')
        return {'free':c['free'], 'free_resets_at':c['free_resets_at'],
                'utc_month_spend':a['spend']['this_month']}
    except (KeyError,TypeError,ValueError):
        raise Stop('Account schema/reset time not verified; flight calls stopped') from None

def queries(cfg):
    result=[]
    # Round trips and both sets of one-way legs rotate evenly across all cities.
    for origin in cfg['origins']:
        for city in cfg['destinations']:
            for kind in ('return','outbound','inbound'):
                result.append({'origin':origin,'city':city,'kind':kind})
    return result

def params(q,cfg):
    p={'from':q['origin'], 'to':q['city'], 'date':cfg['departure_start'],
       'date_end':cfg['departure_end'], 'currency':'CNY','adults':1,'cabin':'economy',
       'max_stops':1,'sort':'price','size':50,'page':1,'raw':0,'fresh':1}
    if q['kind']=='return':
        p.update(return_date=cfg['return_start'],return_date_end=cfg['return_end'])
    elif q['kind']=='inbound':
        p.update({'from':q['city'],'to':q['origin'],'date':cfg['return_start'],'date_end':cfg['return_end']})
    return p

def normalize(o,q,cfg,stamp,meta):
    try:
        price=o['price']
        if o['currency'].upper()!='CNY' or type(price) not in (int,float) or not math.isfinite(price) or price<=0:
            return None
        if o.get('hidden_city') is not False or o.get('throwaway_ticket') is not False:
            return None
        directions=[o['outbound']]
        if q['kind']=='return': directions.append(o['inbound'])
        legs=[]
        for n,d in enumerate(directions):
            if type(d['stops']) is not int or not 0<=d['stops']<=1 or not d['legs']:
                return None
            day=d['departure'][:10]
            inbound=q['kind']=='inbound' or n==1
            lo,hi=(cfg['return_start'],cfg['return_end']) if inbound else (cfg['departure_start'],cfg['departure_end'])
            if not lo<=day<=hi: return None
            legs.append({'from':d['from']['code'],'to':d['to']['code'],
                         'departure':d['departure'],'arrival':d['arrival'],'stops':d['stops'],
                         'details':d['legs']})
        urls=[b['url'] for b in o.get('booking_options',[]) if isinstance(b.get('url'),str) and b['url'].startswith('https://')]
        fp=hashlib.sha256(json.dumps([q,legs],sort_keys=True).encode()).hexdigest()[:24]
        return {'id':fp,'query':q,'price_cny':price,'legs':legs,'checked_at':stamp,
                'fetched_at':meta.get('fetched_at'),'cached':meta.get('cached'),
                'booking_urls':urls,'self_transfer':o.get('self_transfer'),
                'included_checked_bags':o.get('included_checked_bags'),
                'kind':q['kind'],'source':'API.SH / Kiwi.com'}
    except (KeyError,TypeError,ValueError,AttributeError):
        return None

def open_jaw(offers, clock):
    # Pair only recent same-origin legs; no ground-transfer cost is invented.
    fresh=[o for o in offers if clock-parse_time(o['checked_at'])<=dt.timedelta(hours=24)]
    out=[]
    for a in fresh:
        if a['kind']!='outbound':continue
        for b in fresh:
            if b['kind']!='inbound' or a['query']['origin']!=b['query']['origin'] or a['query']['city']==b['query']['city']:continue
            if a['legs'][0]['arrival']>=b['legs'][0]['departure']:continue
            out.append({'id':a['id']+'-'+b['id'],'kind':'open_jaw_separate_tickets',
                        'price_cny':round(a['price_cny']+b['price_cny'],2),
                        'legs':a['legs']+b['legs'],'booking_urls':a['booking_urls']+b['booking_urls'],
                        'checked_at':min(a['checked_at'],b['checked_at']),
                        'warning':'两张单程票估算；不同查询时间；不含罗马/米兰地面交通；请分别核价',
                        'self_transfer':True})
    return sorted(out,key=lambda o:o['price_cny'])[:30]

def save_report(state,data,cfg):
    clock=now()
    data['offers']=[o for o in data['offers'] if o['query']['origin'] in cfg['origins'] and clock-parse_time(o['checked_at'])<=dt.timedelta(hours=24)]
    combinations=open_jaw(data['offers'],clock)
    candidates=[o for o in data['offers'] if o['kind']=='return']+combinations
    data['open_jaw']=combinations
    data['low_prices']=[o for o in candidates if o['price_cny']<=cfg['target_cny']]
    data['notification']={'channel':'report_only','delivered':False,'note':'尚未配置主动通知；不会向 ChatGPT 发消息'}
    data['updated_at']=clock.isoformat()
    data['status']=state.get('status','unknown')
    data['quota']=state.get('quota',{})
    write('docs/data.json',data)
    write('docs/low-prices.json',data['low_prices'])
    write('docs/search-history.json',data['history'])
    lines=['# Italy Flight Radar','',f"更新：{data['updated_at']}",f"状态：{data['status']}",'',
           '仅显示 API.SH / Kiwi 实际返回的报价；24小时后不再当作当前报价。',
           '低价记录不等于已发送通知。开口程为分开购票估算，不含意大利境内交通。','',
           '|类型|路线|人民币|查询时间|','|---|---|---:|---|']
    for o in sorted(candidates,key=lambda x:x['price_cny'])[:30]:
        route=' / '.join(x['from']+'→'+x['to']+' '+x['departure'][:10] for x in o['legs'])
        lines.append(f"|{o['kind']}|{route}|{o['price_cny']:.2f}|{o['checked_at']}|")
    if not candidates:lines.append('\n尚无符合条件的往返报价；这不表示市场没有航班。')
    lines.extend(['','详细航段、行李、订票链接及报价历史见 [data.json](data.json)。'])
    (ROOT/'docs'/'REPORT.md').write_text('\n'.join(lines)+'\n')
    write('state.json',state)
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary: Path(summary).write_text('\n'.join(lines)+'\n')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--check',action='store_true');args=ap.parse_args()
    cfg=read('config.json');state=read('state.json');clock=now()
    if state.get('version')!=3:raise Stop('State missing or incompatible; refusing reset')
    data=read('docs/data.json')
    if args.check:
        print(json.dumps(account_guard(get('/v1/account'),clock)));return
    if clock.date()>dt.date.fromisoformat(cfg['departure_end']):
        print('Travel departure window has ended; no API calls');return
    if state.get('pending'):raise Stop('Previous run interrupted; inspect reserved batch before clearing pending')
    if state.get('failure_streak',0)>=3:raise Stop('Circuit open after 3 failed runs; manual review required')
    day=clock.date().isoformat()
    if state.get('day')!=day:state.update(day=day,day_reserved=0)
    if state.get('day_reserved',0)>=28:
        print('Daily limit reached');return
    # Prevent re-runs and repeated push events from spending again in the same 2h window.
    if state.get('last_run') and clock-parse_time(state['last_run'])<dt.timedelta(hours=2):
        state['status']='ok (recent run; no repeat API calls)'
        save_report(state,data,cfg);persist()
        print('Recent run; report refreshed without API calls');return
    try:
        quota=account_guard(get('/v1/account'),clock)
        cycle=quota['free_resets_at']
        if state.get('free_cycle')!=cycle:
            if state.get('free_cycle') and parse_time(state['free_cycle'])>clock:
                raise Stop('Unexpected free reset change; review required')
            state.update(free_cycle=cycle,cycle_reserved=0)
        month=clock.strftime('%Y-%m')
        if state.get('utc_month')!=month:state.update(utc_month=month,month_reserved=0)
        count=min(7,len(queries(cfg)),28-state.get('day_reserved',0),900-state.get('cycle_reserved',0),
                  900-state.get('month_reserved',0),int(quota['free'])-20,950-int(quota['utc_month_spend']))
        if count<=0:raise Stop('Local quota reserve reached')
        allq=queries(cfg);cursor=state['cursor']
        batch=[allq[(cursor+i)%len(allq)] for i in range(count)]
        state.update(pending=True,last_run=clock.isoformat(),quota=quota,status='reserved',
                     cursor=(cursor+count)%len(allq))
        for field in ('cycle_reserved','month_reserved','day_reserved'):state[field]=state.get(field,0)+count
        save_report(state,data,cfg);persist()  # durable before any credit-bearing call
        for q in batch:
            state['quota']=account_guard(get('/v1/account'),now())
            response=get('/kiwi/v1/flights',params(q,cfg))
            if response.get('credits_used') not in (0,1) or not isinstance(response.get('itineraries'),list):
                raise Stop('Flight schema/cost changed; review required')
            stamp=now().isoformat()
            new=[v for o in response['itineraries'] if (v:=normalize(o,q,cfg,stamp,response))]
            data['offers']=[o for o in data['offers'] if o['query']!=q]+new
            data['history'].append({'checked_at':stamp,'query':q,'offer_count':len(new),
                                    'minimum_cny':min([o['price_cny'] for o in new],default=None),
                                    'fetched_at':response.get('fetched_at'),'cached':response.get('cached')})
            data['history']=data['history'][-5000:]
        state.update(status='ok',failure_streak=0,pending=False)
    except Stop as e:
        state.update(status=str(e),failure_streak=state.get('failure_streak',0)+1)
        # Only known, handled failures clear the marker. A persistence failure never does.
        if 'persistence' in str(e):raise
        state['pending']=False
        save_report(state,data,cfg);persist()
        raise
    save_report(state,data,cfg);persist()
    print('Flight monitoring complete; see docs/REPORT.md')

if __name__=='__main__':
    try:main()
    except Stop as e:
        print(str(e));sys.exit(1)
    except Exception:
        print('Unexpected failure; no retry. Check persisted pending state.');sys.exit(1)
