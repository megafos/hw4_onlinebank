import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from notification_delivery_model import Delivery, Conflict

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'state.sqlite'
        self.d = Delivery(self.path)
        self.n = self.accept()
    def tearDown(self): self.temp.cleanup()
    def accept(self, body=None):
        return self.d.accept('orders', 'e1', 'u1', 'payment', body or {'amount_ref': 'p1'})
    def first(self): return self.d.advance(self.n, 0, 0)
    def test_repeat_accept_returns_same_id(self):
        self.assertEqual(self.n, self.accept())
    def test_changed_payload_conflicts(self):
        with self.assertRaises(Conflict): self.accept({'amount_ref': 'other'})
    def test_concurrent_accept(self):
        with ThreadPoolExecutor(max_workers=12) as ex:
            ids = list(ex.map(lambda _: self.accept(), range(100)))
        self.assertEqual(set(ids), {self.n})
    def test_repeated_timer_does_not_duplicate_attempt(self):
        self.first()
        self.assertIsNone(self.d.advance(self.n, 0, 0))
        self.assertEqual(len(self.d.attempts(self.n)), 1)
    def test_concurrent_failover_single_active(self):
        a=self.first(); self.d.claim(a,0); self.d.transport_result(a,'UNKNOWN')
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda _: self.d.advance(self.n,1,10), range(30)))
        rows=self.d.attempts(self.n)
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(r['active'] for r in rows),1)
    def test_unknown_is_not_resubmitted_blindly(self):
        a=self.first(); self.assertTrue(self.d.claim(a,0))
        self.d.transport_result(a,'UNKNOWN')
        self.assertFalse(self.d.claim(a,1))
        self.assertEqual(self.d.attempts(self.n)[0]['request_key'],a)
    def test_late_ack_cancels_reserved_sms(self):
        p=self.first(); self.d.claim(p,0); self.d.transport_result(p,'ACCEPTED')
        s=self.d.advance(self.n,1,10)
        self.d.receipt('r1',p,'confirmed',11)
        self.assertFalse(self.d.claim(s,11))
        self.assertEqual(self.d.state(self.n)['state'],'CONFIRMED')
    def test_late_ack_cannot_undo_claimed_external_effect(self):
        p=self.first(); self.d.claim(p,0)
        s=self.d.advance(self.n,1,10); self.assertTrue(self.d.claim(s,10))
        self.d.receipt('r1',p,'confirmed',11)
        sms=self.d.attempts(self.n)[1]
        self.assertEqual(sms['state'],'SENDING')  # deliberately not 'CANCELLED'
        self.assertFalse(sms['active'])
        self.assertIsNone(self.d.advance(self.n,self.d.state(self.n)['version'],12))
    def test_receipt_repeat_and_conflict(self):
        a=self.first(); self.d.receipt('r1',a,'confirmed',1)
        self.assertFalse(self.d.receipt('r1',a,'confirmed',2))
        with self.assertRaises(Conflict): self.d.receipt('r1',a,'failed',3)
    def test_confirmed_is_not_reverted_by_late_failure(self):
        a=self.first(); self.d.claim(a,0)
        self.d.receipt('r1',a,'confirmed',1)
        self.d.transport_result(a,'UNKNOWN')
        self.d.receipt('r2',a,'failed',2)
        self.assertEqual(self.d.state(self.n)['state'],'CONFIRMED')
        self.assertEqual(self.d.attempts(self.n)[0]['state'],'CONFIRMED')
    def test_expiry_then_late_confirmation(self):
        a=self.first(); self.d.claim(a,0)
        self.d.advance(self.n,1,86400)
        self.assertEqual(self.d.state(self.n)['state'],'EXPIRED_UNCONFIRMED')
        self.d.receipt('r1',a,'confirmed',86401)
        self.assertEqual(self.d.state(self.n)['state'],'CONFIRMED')
        self.assertTrue(self.d.state(self.n)['late'])
    def test_restart_preserves_state(self):
        a=self.first(); self.d.claim(a,0); self.d.transport_result(a,'UNKNOWN')
        self.d=Delivery(self.path)
        self.assertEqual(self.d.attempts(self.n)[0]['state'],'UNKNOWN')
        self.assertEqual(self.accept(),self.n)
    def test_untrusted_and_unknown_receipts_rejected(self):
        a=self.first()
        with self.assertRaises(ValueError): self.d.receipt('r1',a,'confirmed',1,False)
        with self.assertRaises(ValueError): self.d.receipt('r2','absent','confirmed',1)
        self.assertEqual(self.d.state(self.n)['state'],'IN_PROGRESS')
    def test_no_claim_after_expiry(self):
        a=self.first(); self.assertFalse(self.d.claim(a,86400))
    def test_route_exhaustion_waits_instead_of_false_success(self):
        for version,t in [(0,0),(1,10),(2,40),(3,60)]: self.d.advance(self.n,version,t)
        self.assertEqual(self.d.state(self.n)['state'],'WAITING_RETRY')



if __name__ == '__main__':
    import io
    import sys
    from pathlib import Path
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    report = stream.getvalue()
    target = Path(__file__).resolve().with_name('notification_delivery_validation_output.txt')
    target.write_text(report, encoding='utf-8')
    print(report, end='')
    sys.exit(0 if result.wasSuccessful() else 1)
