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
        self.assertEqual(p['date'],'2027-01-30');self.assertEqual(p['return_date_end'],'2027-02-13')
        self.assertEqual(p['fresh'],1);self.assertEqual(p['max_stops'],1)
    def test_reject_bad_currency_stops_dates_and_nonfinite_prices(self):
        q={'origin':'SHA','city':'ROM','kind':'outbound'}
        d={'from':{'code':'PVG'},'to':{'code':'FCO'},'departure':'2027-02-02T12:00:00',
           'arrival':'2027-02-03T12:00:00','stops':1,'legs':[{}]}
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

if __name__=='__main__':unittest.main()
