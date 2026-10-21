import unittest
from automation.backup_duplicates import content_key, unique_listings


class BackupDuplicatesTests(unittest.TestCase):
    def setUp(self):
        self.listing = {'id':'one', 'facebook_present':False, 'source_deleted':True,
                        'details':{'year':'2020', 'make':'Honda', 'model':'City', 'price':'1000', 'mileage':'50000'}}

    def test_all_five_fields_matching_show_once_regardless_of_other_content(self):
        other = dict(self.listing, id='two', backup_source_account='other',
                     details=dict(self.listing['details'], description='Different description', title='Different title'),
                     downloaded_images=[{'path':'different-photo.jpg'}], downloaded_videos=[{'path':'video.mp4'}])
        self.assertEqual(content_key(self.listing), content_key(other))
        self.assertEqual(unique_listings([self.listing, other]), [self.listing])

    def test_any_one_of_the_five_fields_differing_keeps_both(self):
        for field, value in {'year':'2021', 'make':'Ford', 'model':'Civic', 'price':'1001', 'mileage':'51000'}.items():
            with self.subTest(field=field):
                other = dict(self.listing, id='two', details=dict(self.listing['details'], **{field:value}))
                self.assertEqual(len(unique_listings([self.listing, other])), 2)

    def test_live_listings_stay_separate(self):
        live = dict(self.listing, facebook_present=True, source_deleted=False)
        self.assertEqual(len(unique_listings([live, dict(live, id='two'), self.listing])), 3)

    def test_missing_fields_keep_both(self):
        for field in self.listing['details']:
            details = dict(self.listing['details'])
            details.pop(field)
            one = dict(self.listing, details=details)
            self.assertEqual(len(unique_listings([one, dict(one, id='two')])), 2)

    def test_equivalent_case_whitespace_and_numeric_formats_match(self):
        other = dict(self.listing, details={'year':'2020', 'make':' HONDA ', 'model':'City', 'price':'1,000.00', 'mileage':'50,000'})
        self.assertEqual(content_key(self.listing), content_key(other))

    def test_saved_edits_and_top_level_fields_are_compared(self):
        other = dict(self.listing, repost_preferences={'fields':{'price':'2000'}})
        self.assertNotEqual(content_key(self.listing), content_key(other))
        top_level = dict(self.listing['details'], id='top', facebook_present=False)
        self.assertEqual(content_key(self.listing), content_key(top_level))
