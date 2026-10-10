import copy
import json
import threading
import unittest
from unittest.mock import patch, MagicMock
import numpy as np
from bili_sync import reference_stream, align_bilibili
from core import mapped_time


class LinkSyncTests(unittest.TestCase):
    def stream(self, data):
        response=MagicMock()
        response.__enter__.return_value.read.return_value=json.dumps(data).encode()
        with patch('bili_sync.urllib.request.urlopen',return_value=response):
            return reference_stream({'cid':7,'ep_id':99},threading.Event())

    def test_public_preview_is_usable(self):
        url,preview=self.stream({'code':0,'result':{'is_preview':1,'error_code':-10403,
            'is_drm':False,'durl':[{'url':'https://media.example.test/preview.mp4'}]}})
        self.assertTrue(preview)
        self.assertTrue(url.endswith('preview.mp4'))

    def test_unavailable_and_drm_are_not_used(self):
        for data in ({'code':-10403}, {'code':0,'result':{'is_drm':True}}):
            with self.assertRaises(ValueError):self.stream(data)

    def test_imported_xml_requires_a_matching_link(self):
        with self.assertRaises(ValueError):
            reference_stream({'cid':7},threading.Event())
        with patch('bili_sync.Client') as client:
            client.return_value.metadata.return_value={'items':[{'cid':9,'ep_id':10}]}
            with self.assertRaises(ValueError):
                reference_stream({'cid':7,'source_input':'ep10'},threading.Event())

    def test_short_reference_preserves_rate_and_segment_mapping(self):
        options={'rate':1.01,'offset':2.,'segments':[{'start':40.,'offset':3.}]}
        original=copy.deepcopy(options)
        matches=[{'reference':t,'local':mapped_time(t,options)+1.55,'score':.9}
                 for t in (10,30,60,90,130,160,200)]
        fitted={'rate':.9995,'offset':1.8,'matches':matches,'inliers':7,'error':.1,'span':190,'tested':9}
        with patch('bili_sync.reference_stream',return_value=('https://media.example.test/preview.mp4',True)), \
             patch('bili_sync.features',return_value=np.zeros((7200,16))), \
             patch('bili_sync.match_features',return_value=fitted):
            result=align_bilibili({'cid':7},'movie.mp4','ffmpeg',threading.Event(),lambda s:None,options)
        self.assertAlmostEqual(result['offset'],3.55)
        self.assertEqual(result['rate'],1.01)
        self.assertEqual(options,original)


if __name__=='__main__':unittest.main()
