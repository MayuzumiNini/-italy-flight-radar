"""Create deduplicated GitHub Issue alerts. Never claim inbox delivery."""
import datetime as dt
import hashlib
import json
import os
import urllib.request
from pathlib import Path
import radar

REPO='MayuzumiNini/-italy-flight-radar'
TEST_MARKER='<!-- flight-radar-notification-test-v1 -->'

def github(path, payload=None):
    token=os.environ.get('GITHUB_TOKEN')
    if not token:raise radar.Stop('GitHub notification token missing')
    req=urllib.request.Request('https://api.github.com/repos/'+REPO+path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                 'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
    try:
        with urllib.request.build_opener(radar.NoRedirect).open(req,timeout=40) as r:
            return json.load(r)
    except Exception:
        raise radar.Stop('GitHub notification failed; inbox delivery not confirmed') from None

def marker(offer):
    identity=[offer['kind'],offer['price_cny'],[(l['from'],l['to'],l['departure'],l['arrival']) for l in offer['legs']]]
    return '<!-- flight-radar-'+hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()+' -->'

def eligible(data,cfg,clock):
    if not data.get('status','').startswith('ok'):return []
    if clock-radar.parse_time(data['updated_at'])>dt.timedelta(hours=2):return []
    return sorted([o for o in data.get('low_prices',[]) if o.get('criteria_version')==cfg['criteria_version'] and o['price_cny']<=cfg['target_cny'] and
                   clock-radar.parse_time(o['checked_at'])<=dt.timedelta(hours=2)],key=lambda o:o['price_cny'])

def deliver(title,body,tag,issues):
    existing=next((i for i in issues if tag in (i.get('body') or '')),None)
    if existing:return existing['html_url']
    created=github('/issues',{'title':title,'body':body+'\n\n'+tag,'assignees':['MayuzumiNini']})
    if not created.get('html_url'):raise radar.Stop('Issue creation not confirmed')
    issues.insert(0,created)
    return created['html_url']

def main():
    path=radar.ROOT/'docs/notifications.json'
    receipts=json.loads(path.read_text()) if path.exists() else {'sent':{},'events':[]}
    issues=[]
    # Recover receipts after an interrupted POST/persist without duplicate notifications.
    for page in range(1,11):
        batch=github('/issues?state=all&per_page=100&page='+str(page))
        issues.extend(batch)
        if len(batch)<100:break
    else:raise radar.Stop('Notification issue history exceeds bound; manual review required')
    clock=radar.now();day=clock.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    if not receipts.get('test_issue'):
        url=deliver('【测试提醒】上海—意大利机票价格监测',
            '@MayuzumiNini 这是一条通知渠道测试，不是低价机票。\n\n'
            '已配置：只从上海出发并回上海；2月1日18:00后或2月2日00:00–02:00出发；回程优先当地2月10/11日起飞；每方向≤20小时、最多一次中转。\n\n'
            '发现往返或两张单程组合价格≤¥5000时创建Issue。同一报价不重复发送，同日仅在最低价再降至少¥100时再提醒。\n\n'
            '若未收到邮件/手机通知，请检查 GitHub Settings → Notifications，并在仓库 Watch → Custom 勾选 Issues。\n\n'
            '这里只能确认Issue创建成功，不能确认邮件或手机通知已送达。',TEST_MARKER,issues)
        receipts['test_issue']=url
    cfg=radar.read('config.json');data=radar.read('docs/data.json')
    offers=eligible(data,cfg,clock)
    if offers:
        best=offers[0];tag=marker(best)
        day_price=receipts.get('days',{}).get(day)
        if tag not in receipts['sent'] and (day_price is None or best['price_cny']<=day_price-100):
            legs='\n'.join('- '+l['from']+' → '+l['to']+'，'+l['departure']+' → '+l['arrival']+
                           '，中转'+str(l['stops'])+'次，总时长'+str(round(l['duration_seconds']/3600,1))+'小时' for l in best['legs'])
            body='@MayuzumiNini 发现符合当前筛选的报价 **¥'+str(best['price_cny'])+'**。\n\n'+legs
            body+='\n\n查询时间：'+best['checked_at']+'\n\n'
            if best['kind']=='open_jaw_separate_tickets':body+='**开口程：两张单程票估算，不含意大利境内地面交通，需要分别购票核价。**\n\n'
            body+='行李、转机及最终价格以购票页面为准。\n\n[监测报告](https://github.com/'+REPO+'/blob/main/docs/REPORT.md)'
            # Booking URLs are only included when the provider actually supplied them.
            for i,url in enumerate(best.get('booking_urls',[])[:2]):
                if url.startswith('https://'):body+='\n\n[供应商订票链接 '+str(i+1)+']('+url+')'
            url=deliver('【机票低价】上海往返意大利 ¥'+str(best['price_cny']),body,tag,issues)
            receipts['sent'][tag]=url;receipts.setdefault('days',{})[day]=best['price_cny']
            receipts['events'].append({'created_at':clock.isoformat(),'price_cny':best['price_cny'],'issue':url,'inbox_delivery_confirmed':False})
    receipts['last_check']=clock.isoformat()
    receipts['status']='github_issue_channel_verified'
    radar.write('docs/notifications.json',receipts)
    radar.persist()
    print('GitHub Issue channel verified; inbox delivery not observable. Test: '+receipts['test_issue'])

if __name__=='__main__':
    try:main()
    except radar.Stop as e:raise SystemExit(str(e))
