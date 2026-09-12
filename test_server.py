import os
os.environ['LEADERBOARD_PUSH_TOKEN'] = 'test-only-key-not-for-deployment-1234567890'
import copy
import importlib.util
import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path
import server

class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + os.environ['LEADERBOARD_PUSH_TOKEN']}
        server._snapshot = {'updated_at':None,'received_at':None,'rankings':{k:[] for k in server.KEYS}}
        server._received = 0
        self.payload = {'rankings': {k:[{'name':'Hunter <script>', 'score':'90071992547409931234567'}] for k in server.KEYS}}
    def test_auth_and_public_read(self):
        self.assertEqual(self.client.post('/api/leaderboard',json=self.payload).status_code,401)
        self.assertEqual(self.client.post('/api/leaderboard',json=self.payload,headers={'Authorization':'Bearer wrong'}).status_code,401)
        r=self.client.get('/api/leaderboard')
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['stale'])
        self.assertNotIn(server._secret,r.text)
    def test_update_exact_scores_and_stale(self):
        r=self.client.post('/api/leaderboard',json=self.payload,headers=self.headers)
        self.assertEqual(r.status_code,200)
        result=self.client.get('/api/leaderboard').json
        self.assertFalse(result['stale'])
        self.assertEqual(result['rankings']['money'][0]['score'],'90071992547409931234567')
        server._received=time.time()-200
        self.assertTrue(self.client.get('/api/leaderboard').json['stale'])
    def test_reject_private_fields_and_bad_scores(self):
        bad=copy.deepcopy(self.payload);bad['rankings']['money'][0]['user_id']='private'
        self.assertEqual(self.client.post('/api/leaderboard',json=bad,headers=self.headers).status_code,400)
        bad=copy.deepcopy(self.payload);bad['rankings']['money'][0]['score']='NaN'
        self.assertEqual(self.client.post('/api/leaderboard',json=bad,headers=self.headers).status_code,400)
        self.assertIsNone(self.client.get('/api/leaderboard').json['updated_at'])
    def test_malformed_and_oversize(self):
        self.assertEqual(self.client.post('/api/leaderboard',data='{bad',content_type='application/json',headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/leaderboard',data='x'*300000,content_type='application/json',headers=self.headers).status_code,413)
        self.assertEqual(self.client.post('/api/leaderboard',data='text',headers=self.headers).status_code,415)
    def test_source_isolation_and_media(self):
        for path in ['/server.py','/.env','/../server.py','/website/../server.py','/%2e%2e/server.py','/test_server.py']:
            self.assertEqual(self.client.get(path).status_code,404,path)
        for path in ['/','/leaderboard.html','/weapons.html','/assets/weapons/bare-hands.png']:
            with self.client.get(path) as r:self.assertEqual(r.status_code,200,path)
        with self.client.get('/assets/demo.mp4',headers={'Range':'bytes=0-99'}) as r:
            self.assertEqual(r.status_code,206)
            self.assertEqual(len(r.data),100)
    def test_over_limit(self):
        bad=copy.deepcopy(self.payload);bad['rankings']['level']*=101
        self.assertEqual(self.client.post('/api/leaderboard',json=bad,headers=self.headers).status_code,400)
    def test_badge_accepted_and_validated(self):
        good=copy.deepcopy(self.payload)
        good['rankings']['level'][0]['badge']={'icon':'leveler','tier':'platinum'}
        self.assertEqual(self.client.post('/api/leaderboard',json=good,headers=self.headers).status_code,200)
        result=self.client.get('/api/leaderboard').json
        self.assertEqual(result['rankings']['level'][0]['badge'],{'icon':'leveler','tier':'platinum'})
        # ammo_variety has no Platinum artwork — must be rejected at that tier
        bad=copy.deepcopy(self.payload)
        bad['rankings']['level'][0]['badge']={'icon':'ammo_variety','tier':'platinum'}
        self.assertEqual(self.client.post('/api/leaderboard',json=bad,headers=self.headers).status_code,400)
        # unknown icon slug
        bad=copy.deepcopy(self.payload)
        bad['rankings']['level'][0]['badge']={'icon':'made_up','tier':'gold'}
        self.assertEqual(self.client.post('/api/leaderboard',json=bad,headers=self.headers).status_code,400)
        # extra field inside badge
        bad=copy.deepcopy(self.payload)
        bad['rankings']['level'][0]['badge']={'icon':'leveler','tier':'gold','extra':1}
        self.assertEqual(self.client.post('/api/leaderboard',json=bad,headers=self.headers).status_code,400)

if __name__=='__main__':unittest.main()
