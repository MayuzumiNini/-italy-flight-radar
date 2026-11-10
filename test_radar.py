import copy
import datetime as dt
import unittest
from unittest.mock import patch
import radar

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.clock=dt.datetime(2026,12,1,tzinfo=radar.UTC)
        self.a={'success':True,'account':{'unlimited':False},'plan':None,
                'credits':{'free':999,'total':999,'plan':0,'payg':0,'free_resets_at':'2026-12-09T02:51:58.972Z'},
                'spend':{'this_month':1}}
        self.cfg=radar.read('config.json')
    def test_december_first_does_not_reset_free_cycle(self):
        self.assertEqual(radar.account_guard(self.a,self.clock)['free_resets_at'],'2026-12-09T02:51:58.972Z')
    def test_guard_rejects_paid_balance_empty_and_old_reset(self):
        for field,val in [('free',20),('payg',1),('plan',1),('free_resets_at','2026-11-09T00:00:00Z')]:
            a=copy.deepcopy(self.a);a['credits'][field]=val
            with self.assertRaises(radar.Stop):radar.account_guard(a,self.clock)
    def test_missing_account_fields_fail_closed(self):
        with self.assertRaises(radar.Stop):radar.account_guard({},self.clock)
    def test_all_cities_and_directions_rotated(self):
        qs=radar.queries(self.cfg)
        self.assertEqual(len(qs),6)
        self.assertEqual({q['origin'] for q in qs},{'SHA'})
        self.assertEqual({q['kind'] for q in qs},{'return','outbound','inbound'})
    def test_actual_date_window_parameters(self):
        p=radar.params({'origin':'SHA','city':'ROM','kind':'return'},self.cfg)
        self.assertEqual(p['date'],'2027-02-01');self.assertEqual(p['return_date_end'],'2027-02-11')
        self.assertEqual(p['fresh'],1);self.assertEqual(p['max_stops'],1)
    def test_reject_bad_currency_stops_dates_and_nonfinite_prices(self):
        q={'origin':'SHA','city':'ROM','kind':'outbound'}
        d={'from':{'code':'PVG'},'to':{'code':'FCO'},'departure':'2027-02-02T01:15:00',
           'arrival':'2027-02-03T12:00:00','stops':1,'duration_seconds':36000,'legs':[{}]}
        o={'price':2000,'currency':'CNY','outbound':d,'hidden_city':False,'throwaway_ticket':False}
        self.assertIsNotNone(radar.normalize(o,q,self.cfg,'2026-12-01T00:00:00Z',{}))
        for field,val in [('currency','USD'),('price',float('nan'))]:
            bad=copy.deepcopy(o);bad[field]=val
            self.assertIsNone(radar.normalize(bad,q,self.cfg,'',{}))
        for field,val in [('stops',2),('departure','2027-03-02T12:00:00')]:
            bad=copy.deepcopy(o);bad['outbound'][field]=val
            self.assertIsNone(radar.normalize(bad,q,self.cfg,'',{}))
    def test_open_jaw_requires_recent_different_cities_same_origin(self):
        def o(kind,city,stamp):
            return {'id':kind,'kind':kind,'query':{'origin':'SHA','city':city},'price_cny':2400,
                    'checked_at':stamp,'booking_urls':[],
                    'legs':[{'departure':'2027-02-02' if kind=='outbound' else '2027-02-10','arrival':'2027-02-03' if kind=='outbound' else '2027-02-11'}]}
        a=o('outbound','ROM','2026-12-01T00:00:00Z');b=o('inbound','MIL','2026-12-01T00:00:00Z')
        self.assertEqual(radar.open_jaw([a,b],self.clock)[0]['price_cny'],4800)
        b['checked_at']='2026-11-28T00:00:00Z';self.assertEqual(radar.open_jaw([a,b],self.clock),[])
    def test_interrupted_state_blocks_network(self):
        state={'version':3,'pending':True}
        with patch('radar.read',side_effect=[self.cfg,state,{'offers':[],'history':[]}]),patch('radar.get') as get,patch('sys.argv',['radar.py']):
            with self.assertRaises(radar.Stop):radar.main()
            get.assert_not_called()
    def test_failed_persistence_prevents_flight_call(self):
        state={'version':3,'cursor':0,'pending':False}
        with patch('radar.read',side_effect=[self.cfg,state,{'offers':[],'history':[]}]),patch('radar.now',return_value=self.clock),patch('radar.save_report'),patch('radar.get',return_value=self.a) as get,patch('radar.persist',side_effect=radar.Stop('State persistence failed')),patch('sys.argv',['radar.py']):
            with self.assertRaises(radar.Stop):radar.main()
            self.assertEqual(get.call_count,1)
            self.assertTrue(state['pending'])

class NewFilterTests(unittest.TestCase):
    def check_offer(self,departure,duration=72000,airport='PVG'):
        cfg=radar.read('config.json')
        q={'origin':'SHA','city':'ROM','kind':'outbound'}
        o={'price':2000,'currency':'CNY','hidden_city':False,'throwaway_ticket':False,
           'outbound':{'from':{'code':airport},'to':{'code':'FCO'},'departure':departure,
                       'arrival':'2027-02-02T12:00:00','stops':1,'duration_seconds':duration,'legs':[{},{}]}}
        return radar.normalize(o,q,cfg,'2026-12-01T00:00:00Z',{})
    def test_outbound_time_boundaries(self):
        for t in ['2027-02-01T18:00:00','2027-02-01T23:59:00','2027-02-02T00:00:00','2027-02-02T01:59:00','2027-02-02T02:00:00']:
            self.assertIsNotNone(self.check_offer(t),t)
        for t in ['2027-02-01T17:59:59','2027-02-02T02:00:01','2027-02-03T01:00:00']:
            self.assertIsNone(self.check_offer(t),t)
    def test_duration_and_shanghai_required(self):
        for duration in [72001,None,float('nan'),0]:
            self.assertIsNone(self.check_offer('2027-02-02T01:00:00',duration))
        self.assertIsNone(self.check_offer('2027-02-02T01:00:00',airport='PEK'))
    def test_alerts_reject_old_criteria_and_failed_runs(self):
        import notify
        clock=dt.datetime(2026,12,1,tzinfo=radar.UTC);cfg=radar.read('config.json')
        offer={'kind':'return','price_cny':4500,'criteria_version':4,'checked_at':clock.isoformat()}
        data={'status':'ok','updated_at':clock.isoformat(),'low_prices':[offer]}
        self.assertEqual(len(notify.eligible(data,cfg,clock)),1)
        offer['criteria_version']=3;self.assertEqual(notify.eligible(data,cfg,clock),[])
        offer['criteria_version']=4;data['status']='API HTTP 503';self.assertEqual(notify.eligible(data,cfg,clock),[])
    def test_issue_recovery_does_not_duplicate(self):
        import notify
        with patch('notify.github') as api:
            self.assertEqual(notify.deliver('t','b','marker',[{'body':'marker','html_url':'https://github.com/example'}]),'https://github.com/example')
            api.assert_not_called()

if __name__=='__main__':unittest.main()
