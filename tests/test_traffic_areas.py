import os
import unittest
from unittest.mock import patch
import traffic_responder as r

class AreaTests(unittest.TestCase):
    def test_aliases(self):
        for query in ('Rio Rico', 'riorico', 'rio-rico'):
            self.assertEqual(r.get_area(query)[0], 'Rio Rico')
        for query in ('santa cruz', 'Santa Cruz County', 'scz'):
            self.assertEqual(r.get_area(query)[0], 'Santa Cruz County')
        self.assertIsNone(r.get_area('I-19'))

    def test_geography(self):
        event = {'Latitude':31.48, 'Longitude':-110.98}
        self.assertTrue(r.in_area(event, r.get_area('rio rico')[1]))
        self.assertFalse(r.in_area(event, r.get_area('nogales')[1]))
        self.assertTrue(r.in_area(event, r.get_area('santa cruz')[1]))
        self.assertFalse(r.in_area({}, r.get_area('nogales')[1]))

    def test_message_and_response(self):
        event = {'Latitude':31.48, 'Longitude':-110.98,
                 'RoadwayName':'I-19', 'EventType':'roadwork', 'LastUpdated':1}
        with patch.dict(os.environ, {'MESSAGE':'!traffic rio rico'}, clear=True):
            self.assertEqual(r.get_highway_param(), 'rio rico')
            with patch.object(r, 'fetch_traffic_events', return_value=([event],None)), \
                 patch.object(r, 'respond_multi') as send:
                r.main()
                self.assertEqual(len(send.call_args.args[0]),1)
        with patch.dict(os.environ, {'MESSAGE':'!traffic nogales'}, clear=True), \
             patch.object(r, 'fetch_traffic_events', return_value=([event],None)), \
             patch.object(r, 'respond') as send:
            r.main()
            self.assertIn('No active AZ511 events reported for Nogales',send.call_args.args[0])

if __name__ == '__main__':
    unittest.main()
