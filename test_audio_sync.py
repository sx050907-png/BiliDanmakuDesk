import threading
import unittest
import numpy as np
from audio_sync import match_features, fit_matches, SyncCancelled


class AudioSyncTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.reference = np.random.default_rng(19).normal(size=(3000,16)).astype(np.float32)

    def test_added_intro_and_speed_difference(self):
        for rate in (1.,1.04):
            size = round(len(self.reference)*rate)
            positions = np.arange(size)/rate
            stretched = np.stack([np.interp(positions, np.arange(len(self.reference)), self.reference[:,k])
                                  for k in range(16)],axis=1)
            local = np.concatenate((np.zeros((125,16)), stretched, np.zeros((60,16))))
            result = match_features(self.reference,local,self.cancel,lambda s:None)
            self.assertAlmostEqual(result['offset'],6.25,delta=.08)
            self.assertAlmostEqual(result['rate'],rate,delta=.001)
            self.assertGreaterEqual(result['inliers'],7)

    def test_unrelated_audio_is_not_accepted(self):
        local = np.random.default_rng(37).normal(size=(3300,16))
        with self.assertRaises(ValueError):
            match_features(self.reference,local,self.cancel,lambda s:None)

    def test_cut_versions_are_rejected(self):
        matches=[{'reference':i*100.,'local':i*100.+(3 if i<5 else 23),'score':.9} for i in range(9)]
        with self.assertRaises(ValueError):
            fit_matches(matches)

    def test_cancel(self):
        self.cancel.set()
        with self.assertRaises(SyncCancelled):
            match_features(self.reference,self.reference,self.cancel,lambda s:None)


if __name__=='__main__': unittest.main()
