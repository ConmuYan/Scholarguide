import unittest
from unittest.mock import patch

from common import Budget, Config, Paper
from research_policy import matched_publication
from source import AcademicSource


class CandidateVerificationTests(unittest.TestCase):
    def setUp(self):
        self.source = AcademicSource(Config(), Budget(30))
        self.candidate = Paper('lune:1', 'A World Model', 'abstract', 'https://example.org',
                               year=2025, authors=['Alice Smith'], venue='ICML')
        self.official = Paper('openreview:1', 'A World Model', 'abstract', 'https://openreview.net/forum?id=1',
            year=2025, authors=['Alice Smith'], venue='ICML 2025 poster', publication={
                'records': [{'type':'conference','status':'accepted','venue_id':'ICML.cc/2025/Conference',
                             'venue_name':'ICML 2025 poster','date':'2025','verified':True,
                             'identity_verified':True,'evidence':[{'source':'openreview',
                             'url':'https://openreview.net/forum?id=1','field':'decision','value':'Accept'}]}]})
        self.request = {'venue':'ICML','year_from':2025,'year_to':2025,
                        'publication_type':'conference','accepted_only':True}

    def test_exact_official_match_supplies_acceptance(self):
        with patch.object(self.source, '_openreview', return_value=[self.official]):
            result = self.source.verify_openreview_paper(self.candidate)
        self.assertTrue(matched_publication(result, self.request))
        self.assertFalse(matched_publication(self.candidate, self.request))

    def test_different_author_or_year_cannot_transfer_acceptance(self):
        for change in ({'authors':['Other Person']}, {'year':2024}):
            from dataclasses import replace
            with patch.object(self.source, '_openreview', return_value=[replace(self.official, **change)]):
                result = self.source.verify_openreview_paper(self.candidate)
            self.assertFalse(matched_publication(result, self.request))


if __name__ == '__main__':
    unittest.main()
